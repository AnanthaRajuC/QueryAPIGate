"""HashiCorp Vault (ADR 0015): what `${vault:...}` references and `vault-database` credentials resolve through.

Over Vault's HTTP API - no SDK - with the server signing in one of three ways (QUERYAPIGATE_VAULT_AUTH):

- `token`: QUERYAPIGATE_VAULT_TOKEN, or QUERYAPIGATE_VAULT_TOKEN_FILE - read again at each sign-in, so a token Vault
  Agent keeps current in that file is the one used;
- `kubernetes`: the pod's service account token (QUERYAPIGATE_VAULT_K8S_TOKEN_FILE), for QUERYAPIGATE_VAULT_K8S_ROLE;
- `approle`: QUERYAPIGATE_VAULT_ROLE_ID and QUERYAPIGATE_VAULT_SECRET_ID.

A client token from a sign-in is kept until shortly before its lease ends; a request Vault refuses as forbidden signs
in again, once, then fails. Every database lease this process took is revoked when it exits cleanly, so they don't
pile up in Vault until their TTL.

Nothing here decides what may be read - secretrefs.py checks the allow-lists (QUERYAPIGATE_VAULT_PATHS,
QUERYAPIGATE_VAULT_DATABASE_ROLES) before anything is asked of Vault.
"""
import atexit
import json
import logging
import ssl
import threading
import time
import urllib.error
import urllib.request

from . import config

log = logging.getLogger('queryapigate')

TIMEOUT = 10  # seconds per request to Vault
RENEW_MARGIN = 0.2  # of a client token's lease: signed in again from then on


class VaultError(Exception):
    """What Vault, or reaching it, refused - the message names why, never a secret."""


def _read_file(path, what):
    try:
        with open(path, encoding='utf-8') as f:
            return f.read().strip()
    except OSError as error:
        raise VaultError(f'{what} {path} could not be read: {error.strerror or error}') from None


class Client:
    def __init__(self):
        self._lock = threading.Lock()
        self._token = None
        self._token_until = 0.0  # monotonic
        self._leases = set()
        self._revoke_registered = False

    # ---- HTTP ----------------------------------------------------------------------

    def _context(self):
        cafile = config.vault_cacert()
        return ssl.create_default_context(cafile=cafile) if cafile else ssl.create_default_context()

    def _call(self, method, path, body=None, token=None):
        url = config.vault_addr().rstrip('/') + '/v1/' + path.lstrip('/')
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['X-Vault-Token'] = token
        if config.vault_namespace():
            headers['X-Vault-Namespace'] = config.vault_namespace()
        request = urllib.request.Request(url, method=method, headers=headers,
                                         data=json.dumps(body).encode() if body is not None else None)
        try:
            context = self._context() if url.startswith('https://') else None
            with urllib.request.urlopen(request, timeout=TIMEOUT, context=context) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            try:
                errors = json.load(error).get('errors') or []
            except ValueError:
                errors = []
            raise VaultError(f'Vault answered {error.code} for {path}' + (f": {'; '.join(errors)}" if errors else
                                                                         '')) from None
        except (urllib.error.URLError, OSError) as error:
            raise VaultError(f'Vault at {config.vault_addr()} could not be reached: '
                             f'{getattr(error, "reason", error)}') from None
        return json.loads(raw) if raw else {}

    # ---- signing in ----------------------------------------------------------------

    def _sign_in(self):
        """(client token, seconds it lasts or None for a token that doesn't expire)."""
        method = config.vault_auth()
        if method == 'token':
            token = config.vault_token()
            if not token and config.vault_token_file():
                token = _read_file(config.vault_token_file(), 'QUERYAPIGATE_VAULT_TOKEN_FILE')
            if not token:
                raise VaultError('QUERYAPIGATE_VAULT_AUTH is token, but neither QUERYAPIGATE_VAULT_TOKEN nor '
                                 'QUERYAPIGATE_VAULT_TOKEN_FILE gives one')
            return token, None
        mount = config.vault_auth_mount()
        if method == 'kubernetes':
            body = {'role': config.vault_k8s_role(),
                    'jwt': _read_file(config.vault_k8s_token_file(), 'The service account token')}
        else:
            body = {'role_id': config.vault_role_id(), 'secret_id': config.vault_secret_id()}
        auth = self._call('POST', f'auth/{mount}/login', body).get('auth') or {}
        if not auth.get('client_token'):
            raise VaultError(f'Signing in to Vault ({method}) gave no token')
        return auth['client_token'], auth.get('lease_duration') or None

    def _client_token(self, fresh=False):
        with self._lock:
            if not fresh and self._token and time.monotonic() < self._token_until:
                return self._token
        token, lasts = self._sign_in()
        with self._lock:
            self._token = token
            self._token_until = (time.monotonic() + lasts * (1 - RENEW_MARGIN)) if lasts else float('inf')
        return token

    def request(self, method, path, body=None):
        """A request with the client token - signed in again once if Vault refuses it as forbidden."""
        try:
            return self._call(method, path, body, self._client_token())
        except VaultError as error:
            if ' 403 ' not in f'{error} ':
                raise
        return self._call(method, path, body, self._client_token(fresh=True))

    # ---- what secretrefs.py asks for -------------------------------------------------

    def kv(self, path, field):
        """A field of a KV secret, version 1 or 2: (value, seconds it may be kept, or None)."""
        answer = self.request('GET', path)
        data = answer.get('data') or {}
        if isinstance(data.get('data'), dict) and 'metadata' in data:  # KV version 2
            data = data['data']
        if field not in data:
            raise VaultError(f"{path} has no field '{field}'")
        value = data[field]
        if not isinstance(value, str):
            raise VaultError(f"{path}#{field} isn't text")
        return value, answer.get('lease_duration') or None

    def database_credentials(self, role):
        """A username and password from the database secrets engine: ((username, password), seconds they last)."""
        answer = self.request('GET', f'{config.vault_database_mount()}/creds/{role}')
        data, lease = answer.get('data') or {}, answer.get('lease_id')
        if not data.get('username') or not data.get('password'):
            raise VaultError(f'{config.vault_database_mount()}/creds/{role} gave no username and password')
        if lease:
            with self._lock:
                self._leases.add(lease)
                if not self._revoke_registered:
                    atexit.register(self.revoke_leases)
                    self._revoke_registered = True
        return (data['username'], data['password']), answer.get('lease_duration') or None

    def revoke_leases(self):
        """Revoke every database lease this process took - at exit, so they don't outlive it in Vault."""
        with self._lock:
            leases, self._leases = sorted(self._leases), set()
        for lease in leases:
            try:
                self.request('PUT', 'sys/leases/revoke', {'lease_id': lease})
            except VaultError as error:  # it expires on its own; nothing more to do on the way out
                log.debug('Could not revoke Vault lease %s: %s', lease, error)

    def reset(self):
        with self._lock:
            self._token, self._token_until, self._leases = None, 0.0, set()


client = Client()
