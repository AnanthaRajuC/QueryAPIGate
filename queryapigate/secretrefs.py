"""Secrets by reference (ADR 0015, BACKLOG #98): `${VAR}` from the environment, and `${provider:...}` from where a
secret actually lives - a file today (`${file:/run/secrets/warehouse}`), Vault and AWS Secrets Manager next.

A reference is what's stored, shown and put in a configuration file; the value is resolved only when a connection
is opened or a destination written to, kept in memory for a while, and never written anywhere.

Two rules hold for every provider:

- **Nothing is reachable until the deployer says so.** A provider reads whatever the server's identity can - its
  files, its Vault token - which is more than any one connection should: an administrator could point a connection
  at a host of theirs and have the server send them a secret meant for something else as its password. So each
  provider has an allow-list (QUERYAPIGATE_SECRET_FILE_DIRS for files), checked when a reference is saved and again
  when it's resolved, in case the list shrank. The server's own settings are never reachable, through any provider.
- **It fails closed.** A value is kept for the shorter of what its provider says and QUERYAPIGATE_SECRET_CACHE_SECONDS,
  and fetched again a little before that. A provider that fails then is forgiven while the value is still within its
  time; past it, the connection fails with `secret_unavailable` (503) - never a stale secret.
"""
import logging
import os
import re
import threading
import time

from . import config
from .errors import ApiError

log = logging.getLogger('queryapigate')

ENV_REF_RE = re.compile(r'\$\{(\w+)\}')
# ${VAR}, or ${provider:what} - the provider's name lowercase, what follows it anything but braces and a nested ${
REF_RE = re.compile(r'\$\{(?:([a-z][a-z0-9-]*):([^{}$]+)|(\w+))\}')
REFRESH_AT = 0.8  # of a value's time: fetched again from then on, so a slow or failing provider is noticed early


RETRY_SECONDS = 10.0  # after a provider fails, it isn't asked again for this long: no request waits on one that's down


def lease_margin(lasts):
    """How long before a lease ends its credentials stop being used: a tenth of it, between 5 and 300 seconds."""
    return min(300.0, max(5.0, lasts * 0.1))
MAX_FILE_BYTES = 64 * 1024


def unavailable(message):
    """A provider couldn't give a secret, and no value still within its time is held: the connection fails closed."""
    return ApiError(message, 503, code='secret_unavailable')


class _FetchError(Exception):
    """A provider couldn't fetch a value; the message names why, never the secret."""


def is_reference(value):
    """Whether `value` is one reference and nothing else - shown as written, never encrypted, never masked."""
    return isinstance(value, str) and bool(REF_RE.fullmatch(value))


def server_setting(name):
    # The server's own settings - QUERYAPIGATE_API_KEY, QUERYAPIGATE_SECRET_KEY, the store's URL, and the OpenTelemetry
    # exporter's (OTEL_EXPORTER_OTLP_HEADERS often carries a collector token)... - are never a connection's or
    # destination's to read: an administrator could put one in a password sent to a host of theirs.
    return name.upper().startswith(('QUERYAPIGATE_', 'OTEL_'))


# Credentials the server itself may run with - its cloud identity (boto3's AWS_*, Azure's, Google's), a Vault client's
# token, the Kubernetes service account's - are never a connection's to read either, whatever
# QUERYAPIGATE_SECRET_ENV_VARS says: they reach far more than any one connection's secret (ADR 0017).
CREDENTIAL_PREFIXES = ('AWS_', 'AZURE_', 'ARM_', 'GOOGLE_', 'GCP_', 'VAULT_', 'KUBERNETES_')


def env_refusal(name):
    """Why `${name}` may not be referenced, or None - checked when a reference is saved and again when it's used."""
    if server_setting(name):
        return "is one of this server's own settings"
    if name.upper().startswith(CREDENTIAL_PREFIXES):
        return 'is a cloud, Vault or Kubernetes credential the server itself may use'
    allowed = config.secret_env_vars()
    if allowed and not any(name == a or (a.endswith('*') and name.startswith(a[:-1])) for a in allowed):
        return "isn't one of QUERYAPIGATE_SECRET_ENV_VARS"
    return None


def env_variables(objects):
    """The environment variables the given connections' and destinations' fields reference, sorted."""
    found = set()
    for details in objects:
        for value in (details.values() if isinstance(details, dict) else ()):
            if isinstance(value, str):
                found.update(m.group(3) for m in REF_RE.finditer(value) if m.group(3))
    return sorted(found)


# --------------------------------------------------------------------------------------
# Providers
# --------------------------------------------------------------------------------------

def _inside(path, folder):
    return path == folder or path.startswith(folder.rstrip(os.sep) + os.sep)


class FileProvider:
    """`${file:/run/secrets/name}`: a file's content, read again when it changes - what the Secrets Store CSI driver,
    Docker secrets and Vault Agent write, rotating it in place. One trailing newline is dropped."""

    name = 'file'

    def check(self, what):
        """The reason `what` may not be read, or None."""
        if not os.path.isabs(what):
            return f"'{what}' must be an absolute path"
        folders = config.secret_file_dirs()
        if not folders:
            return 'QUERYAPIGATE_SECRET_FILE_DIRS is not set: no file may be read as a secret'
        real = os.path.realpath(what)
        home = os.path.realpath(config.home())
        if _inside(real, home) or _inside(home, real):
            return "the server's own home folder, where its store lives, is never readable as a secret"
        if not any(_inside(real, folder) for folder in folders):
            return f"'{what}' isn't inside a folder QUERYAPIGATE_SECRET_FILE_DIRS allows"
        return None

    def version(self, what):
        """What changes when the file does: a value read before is fetched again once this differs."""
        try:
            stat = os.stat(what)
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size, stat.st_ino)

    def fetch(self, what):
        """(value, seconds it may be kept, or None for the cache's own limit) - from the file check() let through:
        where the path really leads, checked again here, then opened as that, so a link changed in between can't
        point it elsewhere."""
        real = os.path.realpath(what)
        reason = self.check(real)
        if reason:
            raise _FetchError(reason)
        try:
            with open(real, 'rb') as f:
                data = f.read(MAX_FILE_BYTES + 1)
        except OSError as error:
            raise _FetchError(error.strerror or str(error)) from None
        if len(data) > MAX_FILE_BYTES:
            raise _FetchError(f'larger than {MAX_FILE_BYTES // 1024} KiB - not a secret')
        try:
            text = data.decode('utf-8')
        except UnicodeDecodeError:
            raise _FetchError('not UTF-8 text') from None
        return text[:-2] if text.endswith('\r\n') else text[:-1] if text.endswith('\n') else text, None


_VAULT_PATH_RE = re.compile(r'^[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)*$')


class VaultProvider:
    """`${vault:secret/data/warehouse#password}`: a field of a secret in Vault's KV engine, version 1 or 2 - the path
    as Vault's API names it, then the field."""

    name = 'vault'

    def check(self, what):
        path, _, field = what.partition('#')
        if not config.vault_addr():
            return 'QUERYAPIGATE_VAULT_ADDR is not set'
        if not field:
            return "name the field after the path: ${vault:secret/data/warehouse#password}"
        if not _VAULT_PATH_RE.match(path) or '..' in path.split('/'):
            return f"'{path}' isn't a Vault path"
        prefixes = config.vault_paths()
        if not prefixes:
            return 'QUERYAPIGATE_VAULT_PATHS is not set: no Vault path may be read'
        if not any((path + '/').startswith(prefix) for prefix in prefixes):
            return f"'{path}' isn't under a path QUERYAPIGATE_VAULT_PATHS allows"
        return None

    def fetch(self, what):
        from . import vault
        path, _, field = what.partition('#')
        try:
            return vault.client.kv(path, field)
        except vault.VaultError as error:
            raise _FetchError(str(error)) from None


class AwsSecretsProvider:
    """`${aws-sm:prod/warehouse#password}`: an AWS Secrets Manager secret - by name or ARN - and, after `#`, a field of
    its JSON; without one, the whole SecretString."""

    name = 'aws-sm'

    @staticmethod
    def _name(secret_id):
        """The secret's name, from a name or an ARN (arn:aws:secretsmanager:region:account:secret:name-AbCdEf)."""
        return secret_id.split(':secret:', 1)[1] if secret_id.startswith('arn:') and ':secret:' in secret_id \
            else secret_id

    def check(self, what):
        secret_id = what.partition('#')[0]
        prefixes = config.aws_secret_prefixes()
        if not prefixes:
            return 'QUERYAPIGATE_AWS_SECRET_PREFIXES is not set: no secret may be read'
        if not any(self._name(secret_id).startswith(prefix) for prefix in prefixes):
            return f"'{secret_id}' isn't a secret QUERYAPIGATE_AWS_SECRET_PREFIXES allows"
        return None

    def fetch(self, what):
        from . import aws
        secret_id, _, field = what.partition('#')
        try:
            return aws.secret(secret_id, field or None)
        except aws.AwsError as error:
            raise _FetchError(str(error)) from None


PROVIDERS = {provider.name: provider for provider in (FileProvider(), VaultProvider(), AwsSecretsProvider())}


# --------------------------------------------------------------------------------------
# Short-lived database credentials: a connection's `credentials`
# --------------------------------------------------------------------------------------

# Connections that have no user and password to replace, so nothing to ask a credential source for.
_NO_CREDENTIALS = ('sqlite', 'duckdb', 'h2')


def _refused(message):
    return ApiError(message, code='invalid_body')


class VaultDatabaseCredentials:
    """`"credentials": {"source": "vault-database", "role": "reporting-ro"}`: a username and password from Vault's
    database secrets engine, for the lease's length - the same pair for every connection opened meanwhile."""

    name = 'vault-database'
    shape = {'source', 'role'}

    def validate(self, spec, fields):
        if fields.get('db') in _NO_CREDENTIALS:
            raise _refused(f"A {fields['db']} connection has no user and password for credentials to give")
        given = sorted(k for k in ('user', 'password') if fields.get(k) not in (None, ''))
        if given:
            raise _refused(f"credentials gives the user and password - leave out {', '.join(given)}")

    def reason(self, spec, details):
        """A role may be bound to a host (`reporting-ro@warehouse.internal`, or with :port): its credentials then go
        to no other - otherwise an administrator could point a connection elsewhere and have the server log in there
        with the warehouse's."""
        if not config.vault_addr():
            return 'QUERYAPIGATE_VAULT_ADDR is not set'
        allowed = config.vault_database_roles()
        if not allowed:
            return 'QUERYAPIGATE_VAULT_DATABASE_ROLES is not set: no database role may be asked for'
        bound = [entry.partition('@')[2] for entry in allowed if entry.partition('@')[0] == spec['role']]
        if not bound:
            return f"'{spec['role']}' isn't a role QUERYAPIGATE_VAULT_DATABASE_ROLES allows"
        if '' in bound:
            return None  # allowed for any host
        host = str(details.get('host') or '').lower()
        port = str(details.get('port') or '')
        for entry in bound:
            want_host, _, want_port = entry.lower().partition(':')
            if host == want_host and (not want_port or port == want_port):
                return None
        return (f"'{spec['role']}' is bound to {', '.join(bound)} by QUERYAPIGATE_VAULT_DATABASE_ROLES - not "
                f"{host}{':' + port if port else ''}")

    def key(self, spec, details):
        return spec['role']

    def fetch(self, spec, details):
        from . import vault
        try:
            return vault.client.database_credentials(spec['role'])
        except vault.VaultError as error:
            raise _FetchError(str(error)) from None

    def apply(self, details, pair):
        return {**details, 'user': pair[0], 'password': pair[1]}


class AwsRdsIamCredentials:
    """`"credentials": {"source": "aws-rds-iam"}`: an IAM authentication token as the connection's password, for its
    `user` on its host - RDS and Aurora, PostgreSQL or MySQL. Good for 15 minutes; only ever sent over TLS."""

    name = 'aws-rds-iam'
    shape = {'source'}
    _TLS = ('require', 'verify-ca', 'verify-full')

    def validate(self, spec, fields):
        db = fields.get('db')
        if db not in ('postgres', 'mysql'):
            raise _refused('aws-rds-iam credentials are for postgres and mysql connections (RDS and Aurora)')
        if fields.get('password') not in (None, ''):
            raise _refused('aws-rds-iam credentials give the password - leave it out')
        for need in ('host', 'user'):
            if fields.get(need) in (None, ''):
                raise _refused(f'aws-rds-iam credentials need the connection\'s {need}')
        if db == 'postgres' and fields.get('sslmode') not in self._TLS:
            raise _refused('aws-rds-iam sends a token as the password: set sslmode to verify-full (with sslrootcert, '
                           'the RDS CA bundle) - or require')
        verified = fields.get('ssl_ca') or fields.get('ssl_verify_cert')
        if db == 'mysql' and (fields.get('ssl_disabled') or not verified):
            raise _refused('aws-rds-iam sends a token as the password: set ssl_ca to the RDS CA bundle')

    def reason(self, spec, details):
        allowed = config.aws_rds_hosts()
        if not allowed:
            return 'QUERYAPIGATE_AWS_RDS_HOSTS is not set: no IAM token may be made'
        host = str(details.get('host') or '').lower()
        if not any(host == h or (h.startswith('.') and host.endswith(h)) for h in allowed):
            return f"'{host}' isn't a host QUERYAPIGATE_AWS_RDS_HOSTS allows"
        return None

    def key(self, spec, details):
        return (str(details.get('host')).lower(), self._port(details), details.get('user'))

    @staticmethod
    def _port(details):
        return int(details.get('port') or (5432 if details.get('db') == 'postgres' else 3306))

    def fetch(self, spec, details):
        from . import aws
        try:
            return aws.rds_token(details['host'], self._port(details), details['user'])
        except aws.AwsError as error:
            raise _FetchError(str(error)) from None

    def apply(self, details, pair):
        extra = {'auth_plugin': 'mysql_clear_password'} if details.get('db') == 'mysql' else {}
        return {**details, 'password': pair[1], **extra}


CREDENTIAL_SOURCES = {source.name: source for source in (VaultDatabaseCredentials(), AwsRdsIamCredentials())}


def check_credentials(spec, fields):
    """A connection's `credentials`, when it's saved (invalid_body)."""
    source = CREDENTIAL_SOURCES.get(spec.get('source')) if isinstance(spec, dict) else None
    if source is None:
        raise _refused(f"credentials.source must be one of: {', '.join(sorted(CREDENTIAL_SOURCES))}")
    if set(spec) != source.shape or not all(isinstance(spec[k], str) and spec[k] for k in source.shape):
        shape = ', '.join(f'"{k}": ...' for k in sorted(source.shape))
        raise _refused(f'credentials for {source.name} is {{{shape}}} - and nothing else')
    source.validate(spec, fields)
    reason = source.reason(spec, fields)
    if reason:
        raise _refused(f'credentials {source.name} is refused: {reason}')


_expiries: dict[str, float] = {}  # sha256 of (user, password) -> when they stop being usable (monotonic)
_expiries_lock = threading.Lock()


def _pair_key(user, password):
    import hashlib
    return hashlib.sha256(f'{user}\0{password}'.encode()).hexdigest()


def resolve_credentials(details):
    """`details` with its `credentials` replaced by the user and password they name - at the moment of use."""
    spec = details.get('credentials')
    if not isinstance(spec, dict):
        return details
    source = CREDENTIAL_SOURCES.get(spec.get('source'))
    if source is None:
        raise ApiError(f"There is no credential source '{spec.get('source')}'", 500, code='connection_misconfigured')
    rest = {k: v for k, v in details.items() if k != 'credentials'}
    pair, until = cache.resolve_credentials(source, spec, rest)
    resolved = source.apply(rest, pair)
    with _expiries_lock:
        now = time.monotonic()
        for gone in [k for k, v in _expiries.items() if v < now]:
            del _expiries[gone]  # past their lease: no pooled connection still uses them
        _expiries[_pair_key(resolved.get('user'), resolved.get('password'))] = until
    return resolved


def credential_expiry(details):
    """When the user and password in `details` stop being usable (monotonic) - for credentials from a lease - else
    None. The pool retires a connection opened with them before then."""
    with _expiries_lock:
        return _expiries.get(_pair_key(details.get('user'), details.get('password')))


# --------------------------------------------------------------------------------------
# Checking a reference when it's saved
# --------------------------------------------------------------------------------------

def check_value(value):
    """Refuse - when a connection or destination is saved or tried - a reference to the server's own settings, to a
    provider there is none of, or to what a provider's allow-list doesn't reach (invalid_body, 400)."""
    if not isinstance(value, str):
        return
    for match in REF_RE.finditer(value):
        provider, what, variable = match.groups()
        if variable is not None:
            reason = env_refusal(variable)
            if reason:
                raise ApiError(f"${{{variable}}} {reason} and can't be referenced - use a variable of its own for the "
                               'secret', code='invalid_body')
            continue
        if provider not in PROVIDERS:
            raise ApiError(f"${{{provider}:...}}: there is no secret provider '{provider}' - one of: "
                           f"{', '.join(sorted(PROVIDERS))}, or ${{VAR}} for the environment", code='invalid_body')
        reason = PROVIDERS[provider].check(what)
        if reason:
            raise ApiError(f'${{{provider}:{what}}} is refused: {reason}', code='invalid_body')


def check_fields(fields):
    for value in (fields or {}).values():
        check_value(value)
    if 'credentials' in (fields or {}):
        check_credentials(fields['credentials'], fields)


# --------------------------------------------------------------------------------------
# Resolving it when a connection is opened
# --------------------------------------------------------------------------------------

class _Cache:
    def __init__(self):
        self._lock = threading.Lock()
        self._entries = {}  # (provider, what) -> (value, version, refresh after, usable until) - monotonic times
        self._failed = {}  # (provider, what) -> (ask again after - monotonic, the error): a failure, remembered
        self._fetching = set()  # keys a fetch is under way for: meanwhile, what's held is served
        self.status = {}  # provider -> {'last_ok', 'last_error', 'failing_since'} (wall-clock times)

    def clear(self):
        with self._lock:
            self._entries.clear()
            self._failed.clear()
            self.status.clear()

    def _remembered_failure(self, key, now):
        with self._lock:
            failed = self._failed.get(key)
        return failed[1] if failed is not None and now < failed[0] else None

    def _start_fetch(self, key, usable):
        """Whether to fetch now: not while another request is fetching the same and something usable is held."""
        with self._lock:
            if key in self._fetching and usable:
                return False
            self._fetching.add(key)
            return True

    def _end_fetch(self, key):
        with self._lock:
            self._fetching.discard(key)

    def _held_after_failure(self, key, held, error):
        """A fetch failed: remember it for RETRY_SECONDS from now - not from when it started, which may have been a
        timeout ago - and serve what's held until then, if it's still usable."""
        now = time.monotonic()
        with self._lock:
            self._failed[key] = (now + RETRY_SECONDS, error)
            if held is not None:
                self._entries[key] = (*held[:2], min(held[3], now + RETRY_SECONDS), held[3])

    def resolve(self, name, what):
        provider = PROVIDERS[name]
        reason = provider.check(what)
        if reason:  # the allow-list may have shrunk since the reference was saved
            raise ApiError(f'${{{name}:{what}}} is refused: {reason}', 500, code='connection_misconfigured')
        key = (name, what)
        now = time.monotonic()
        version = provider.version(what) if hasattr(provider, 'version') else None
        with self._lock:
            held = self._entries.get(key)
        if held is not None and now < held[2] and held[1] == version:
            return held[0]
        usable = held is not None and now < held[3] and held[1] == version
        remembered = self._remembered_failure(key, now)
        if remembered is not None and not usable:
            raise unavailable(f'${{{name}:{what}}} could not be resolved: {remembered}')
        if not self._start_fetch(key, usable):
            return held[0]
        try:
            value, ttl = provider.fetch(what)
        except _FetchError as error:
            self._note(name, error)
            self._held_after_failure(key, held if usable else None, error)
            if usable:
                log.warning('The secret provider %s failed (%s) - using the value it gave before, good for %ds more',
                            name, error, int(held[3] - time.monotonic()))
                return held[0]
            raise unavailable(f'${{{name}:{what}}} could not be resolved: {error}') from None
        finally:
            self._end_fetch(key)
        self._note(name, None)
        with self._lock:
            self._failed.pop(key, None)
        keep = self._keep(ttl)
        with self._lock:
            self._entries[key] = (value, version, now + keep * REFRESH_AT, now + keep)
        return value

    def resolve_credentials(self, source, spec, details):
        """((user, password), usable until - monotonic) for a credential source, kept for its lease: fetched again
        from REFRESH_AT of it, and never used within lease_margin() of its end - so a connection opened with it has
        time to be used and retired before the lease is revoked."""
        reason = source.reason(spec, details)
        if reason:  # the allow-list may have shrunk since the connection was saved
            raise ApiError(f'credentials {source.name} is refused: {reason}', 500, code='connection_misconfigured')
        key = (source.name, source.key(spec, details))
        now = time.monotonic()
        with self._lock:
            held = self._entries.get(key)
        if held is not None and now < held[2]:
            return held[0], held[3]
        usable = held is not None and now < held[3]
        remembered = self._remembered_failure(key, now)
        if remembered is not None and not usable:
            raise unavailable(f'credentials {source.name} could not be resolved: {remembered}')
        if not self._start_fetch(key, usable):
            return held[0], held[3]
        try:
            pair, ttl = source.fetch(spec, details)
        except _FetchError as error:
            self._note(source.name, error)
            self._held_after_failure(key, held if usable else None, error)
            if usable:
                log.warning('The credential source %s failed (%s) - using the credentials it gave before, good for '
                            '%ds more', source.name, error, int(held[3] - time.monotonic()))
                return held[0], held[3]
            raise unavailable(f'credentials {source.name} could not be resolved: {error}') from None
        finally:
            self._end_fetch(key)
        self._note(source.name, None)
        with self._lock:
            self._failed.pop(key, None)
        lasts = float(ttl) if ttl else float(config.secret_cache_seconds())
        usable = max(0.0, lasts - lease_margin(lasts))
        with self._lock:  # a short lease's margin can reach past REFRESH_AT: fetched again by the time it's unusable
            self._entries[key] = (pair, None, now + min(lasts * REFRESH_AT, usable), now + usable)
        return pair, now + usable

    @staticmethod
    def _keep(ttl):
        limit = config.secret_cache_seconds()
        return limit if ttl is None else max(0.0, min(float(ttl), limit))

    def _note(self, name, error):
        stamp = time.time()
        with self._lock:
            status = self.status.setdefault(name, {'last_ok': None, 'last_error': None, 'failing_since': None})
            if error is None:
                status.update(last_ok=stamp, failing_since=None)
            else:
                status['last_error'] = str(error)
                status['failing_since'] = status['failing_since'] or stamp


cache = _Cache()


def expand(value):
    """`value` with every reference replaced by what it names - only ever at the moment of use."""
    if not isinstance(value, str) or '${' not in value:
        return value

    def replace(match):
        provider, what, variable = match.groups()
        if variable is not None:
            reason = env_refusal(variable)
            if reason:
                raise ApiError(f"${{{variable}}} {reason} and can't be referenced by a connection or destination", 500,
                               code='connection_misconfigured')
            if variable not in os.environ:
                raise ApiError(f"Environment variable '{variable}' referenced by the connection is not set", 500,
                               code='connection_misconfigured')
            return os.environ[variable]
        if provider not in PROVIDERS:
            raise ApiError(f"There is no secret provider '{provider}'", 500, code='connection_misconfigured')
        return cache.resolve(provider, what)

    return REF_RE.sub(replace, value)
