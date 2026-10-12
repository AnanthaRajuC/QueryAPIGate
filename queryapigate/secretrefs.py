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
        """(value, seconds it may be kept, or None for the cache's own limit)."""
        try:
            with open(what, 'rb') as f:
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


PROVIDERS = {provider.name: provider for provider in (FileProvider(),)}


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
            if server_setting(variable):
                raise ApiError(f"${{{variable}}} is one of this server's own settings and can't be referenced - "
                               'use a variable of its own for the secret', code='invalid_body')
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


# --------------------------------------------------------------------------------------
# Resolving it when a connection is opened
# --------------------------------------------------------------------------------------

class _Cache:
    def __init__(self):
        self._lock = threading.Lock()
        self._entries = {}  # (provider, what) -> (value, version, refresh after, usable until) - monotonic times
        self.status = {}  # provider -> {'last_ok', 'last_error', 'failing_since'} (wall-clock times)

    def clear(self):
        with self._lock:
            self._entries.clear()
            self.status.clear()

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
        try:
            value, ttl = provider.fetch(what)
        except _FetchError as error:
            self._note(name, error)
            if held is not None and now < held[3] and held[1] == version:
                log.warning('The secret provider %s failed (%s) - using the value it gave before, good for %ds more',
                            name, error, int(held[3] - now))
                return held[0]
            raise unavailable(f'${{{name}:{what}}} could not be resolved: {error}') from None
        self._note(name, None)
        keep = self._keep(ttl)
        with self._lock:
            self._entries[key] = (value, version, now + keep * REFRESH_AT, now + keep)
        return value

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
            if server_setting(variable):
                raise ApiError(f"${{{variable}}} is one of this server's own settings and can't be referenced by a "
                               'connection or destination', 500, code='connection_misconfigured')
            if variable not in os.environ:
                raise ApiError(f"Environment variable '{variable}' referenced by the connection is not set", 500,
                               code='connection_misconfigured')
            return os.environ[variable]
        if provider not in PROVIDERS:
            raise ApiError(f"There is no secret provider '{provider}'", 500, code='connection_misconfigured')
        return cache.resolve(provider, what)

    return REF_RE.sub(replace, value)
