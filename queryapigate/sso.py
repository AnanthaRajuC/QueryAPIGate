"""Console sign-in through the organisation's identity provider (ADR 0007, BACKLOG #84 Phase 2).

The server is the OpenID Connect client - a confidential one, with a client secret - and runs the authorization code
flow with PKCE itself, so the browser never holds a token:

1. GET /auth/sso/login: a pending sign-in (state, nonce, a PKCE verifier) is stored server-side for ten minutes and
   bound to this browser by a short-lived cookie; the browser goes to the provider.
2. The provider signs the person in - its MFA, its policies - and sends them to GET /auth/sso/callback.
3. The pending sign-in is taken (once: it's deleted as it's read) by its state, and must belong to this browser - a
   callback link someone else started can't sign you in as them. The code is exchanged at the token endpoint with the
   client secret and the verifier, and the ID token checked: signature against the provider's keys with the key's own
   algorithm, `iss`, `aud` the client ID (and `azp`, when given), `nonce`, `exp`, `iat`.
4. The person's groups give a role (QUERYAPIGATE_SSO_ROLES; the most capable wins; none, no sign-in); the
   administrator is found by (issuer, sub) or created; a session starts.

**The session** is a random ID in a `__Host-` cookie - HttpOnly, Secure, SameSite=Strict - stored only as a hash
(admin_sessions), with a CSRF token. A request authenticates with the cookie only when it also carries that token in
X-QAG-CSRF - reads included - so a link, form or image elsewhere can never act as the administrator. The cookie is
honoured where an admin token is (resolve_permission()); the MCP and events servers never look at it.
"""
import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
import urllib.parse

from flask import Blueprint, jsonify, redirect, request

from . import admins, config, db, jwtauth, oidc, store
from .errors import ApiError

log = logging.getLogger('queryapigate')

SESSION_COOKIE = '__Host-qag_session'
STATE_COOKIE = '__Host-qag_sso'
CSRF_HEADER = 'X-QAG-CSRF'
LOGIN_TTL = 600                 # seconds a started sign-in may take
CLOCK_LEEWAY = 30
CONSOLE = '/console/'
_SEEN_INTERVAL = 60.0           # seconds between last_seen_at writes per session
_last_seen: dict = {}

bp = Blueprint('auth', __name__, url_prefix='/auth')


class Refused(Exception):
    """A sign-in that doesn't go ahead: `code` for the audit log and the Console, `message` for the person."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()


def _provider():
    found = oidc.metadata(config.sso_issuer())
    if found is None or not found.get('authorization_endpoint') or not found.get('token_endpoint'):
        raise Refused('sso_unreachable', "The identity provider's sign-in can't be reached - try again shortly, or "
                                         'sign in with a token')
    methods = found.get('code_challenge_methods_supported')
    if isinstance(methods, list) and 'S256' not in methods:
        raise Refused('sso_unsupported', "The identity provider doesn't support PKCE with S256")
    return found


def safe_return_to(value):
    """Only ever a Console page on this server: an open redirect after sign-in is a phishing tool."""
    if isinstance(value, str) and value.startswith(CONSOLE) and '//' not in value and '\\' not in value \
            and not any(c in value for c in '\r\n'):
        return value
    return CONSOLE


# --------------------------------------------------------------------------------------
# The flow
# --------------------------------------------------------------------------------------

def begin(return_to):
    """(the provider's authorization URL, the browser-binding cookie value) for a new sign-in."""
    provider = _provider()
    state, nonce, verifier, browser = (secrets.token_urlsafe(32) for _ in range(4))
    now = time.time()
    with db.transaction() as conn:
        conn.execute('DELETE FROM sso_logins WHERE created_at < ?', (now - LOGIN_TTL,))
        conn.execute('INSERT INTO sso_logins (state_hash, browser_hash, nonce, verifier, return_to, created_at) '
                     'VALUES (?, ?, ?, ?, ?, ?)',
                     (_hash(state), _hash(browser), nonce, verifier, safe_return_to(return_to), now))
    query = urllib.parse.urlencode({
        'response_type': 'code', 'client_id': config.sso_client_id(), 'redirect_uri': config.sso_redirect_url(),
        'scope': 'openid email profile', 'state': state, 'nonce': nonce,
        'code_challenge': _b64(hashlib.sha256(verifier.encode()).digest()), 'code_challenge_method': 'S256'})
    separator = '&' if '?' in provider['authorization_endpoint'] else '?'
    return f"{provider['authorization_endpoint']}{separator}{query}", browser


def _take_pending(state, browser):
    """The pending sign-in for this state - deleted as it's read, so a callback works once - if this browser started
    it and it's no older than LOGIN_TTL."""
    if not state or not browser:
        raise Refused('sso_state', 'This sign-in has expired or was started elsewhere - sign in again')
    with db.transaction() as conn:
        row = conn.execute('SELECT browser_hash, nonce, verifier, return_to, created_at FROM sso_logins '
                           'WHERE state_hash = ?', (_hash(state),)).fetchone()
        if row is not None:
            conn.execute('DELETE FROM sso_logins WHERE state_hash = ?', (_hash(state),))
    if row is None or time.time() - row['created_at'] > LOGIN_TTL \
            or not hmac.compare_digest(row['browser_hash'], _hash(browser)):
        raise Refused('sso_state', 'This sign-in has expired or was started elsewhere - sign in again')
    return row


def _exchange(provider, code, verifier):
    """The token endpoint's answer for an authorization code: client_secret_basic, unless the provider says it only
    takes client_secret_post."""
    form = {'grant_type': 'authorization_code', 'code': code, 'redirect_uri': config.sso_redirect_url(),
            'code_verifier': verifier}
    headers = {'Content-Type': 'application/x-www-form-urlencoded'}
    methods = provider.get('token_endpoint_auth_methods_supported')
    if isinstance(methods, list) and 'client_secret_basic' not in methods and 'client_secret_post' in methods:
        form.update(client_id=config.sso_client_id(), client_secret=config.sso_client_secret())
    else:
        pair = f'{urllib.parse.quote(config.sso_client_id(), safe="")}:' \
               f'{urllib.parse.quote(config.sso_client_secret(), safe="")}'
        headers['Authorization'] = 'Basic ' + base64.b64encode(pair.encode()).decode()
    try:
        answer = oidc.fetch_json(provider['token_endpoint'], data=urllib.parse.urlencode(form).encode(),
                                 headers=headers)
    except (OSError, ValueError) as error:
        log.warning('SSO: the token endpoint refused the code: %s', error)
        raise Refused('sso_token', "The identity provider didn't accept this sign-in - sign in again") from None
    if not isinstance(answer, dict) or not isinstance(answer.get('id_token'), str):
        raise Refused('sso_token', "The identity provider didn't send an ID token")
    return answer['id_token']


def verify_id_token(provider, id_token, nonce):
    """The ID token's claims, checked as section 3 of the module docstring says, or Refused."""
    import jwt
    try:
        signing_key = jwtauth._jwks_client(provider['jwks_uri']).get_signing_key_from_jwt(id_token)
        algorithm = signing_key.algorithm_name
        if algorithm not in config.JWT_ASYMMETRIC_ALGORITHMS:
            raise jwt.InvalidTokenError(f'algorithm {algorithm} not accepted')
        claims = jwt.decode(id_token, signing_key.key, algorithms=[algorithm], audience=config.sso_client_id(),
                            issuer=config.sso_issuer(), leeway=CLOCK_LEEWAY,
                            options={'require': ['exp', 'iat', 'iss', 'aud', 'sub', 'nonce']})
    except (jwt.InvalidTokenError, jwt.PyJWKClientError) as error:
        log.warning('SSO: an ID token was refused: %s', error)
        raise Refused('sso_id_token', "The identity provider's answer couldn't be verified - sign in again") from None
    if not hmac.compare_digest(str(claims.get('nonce')), nonce):
        raise Refused('sso_id_token', "The identity provider's answer couldn't be verified - sign in again")
    audiences = claims['aud'] if isinstance(claims['aud'], list) else [claims['aud']]
    if (len(audiences) > 1 or 'azp' in claims) and claims.get('azp') != config.sso_client_id():
        raise Refused('sso_id_token', "The identity provider's answer was issued to another application")
    if not isinstance(claims.get('sub'), str) or not claims['sub']:
        raise Refused('sso_id_token', "The identity provider's answer names no one")
    return claims


def _groups(claims):
    value = jwtauth.claim(claims, config.sso_groups_claim())
    values = [value] if isinstance(value, str) else value if isinstance(value, list) else []
    return {v.strip('/') for v in values if isinstance(v, str)} | {v for v in values if isinstance(v, str)}


def _verified_email(claims):
    email = claims.get('email')
    return email.lower() if isinstance(email, str) and claims.get('email_verified') is True else None


def role_for(claims):
    """The most capable role the person's groups map to; else the default role, for a verified email at an allowed
    domain; else Refused."""
    groups = _groups(claims)
    mapped = {role for role, group in config.sso_roles() if group in groups}
    for role in config.SSO_ROLE_ORDER:
        if role in mapped:
            return role
    default, email = config.sso_default_role(), _verified_email(claims)
    if default and email and email.rsplit('@', 1)[-1] in config.sso_allowed_domains():
        return default
    raise Refused('sso_no_role', "You signed in, but you're in no group that's given a role here - ask an owner to "
                                 'add you to one')


def finish(code, state, browser, error=None):
    """(administrator, session id, return_to) for a callback, or Refused. Every outcome is audited."""
    pending = _take_pending(state, browser)
    if error:
        raise Refused('sso_denied', 'The identity provider ended the sign-in: ' + str(error)[:200])
    if not code:
        raise Refused('sso_token', 'The identity provider sent no code - sign in again')
    provider = _provider()
    claims = verify_id_token(provider, _exchange(provider, code, pending['verifier']), pending['nonce'])
    role = role_for(claims)
    wanted = claims.get('preferred_username') if isinstance(claims.get('preferred_username'), str) else None
    email = _verified_email(claims)
    try:
        admin, change = admins.sign_in_sso(claims['iss'], claims['sub'], role,
                                           wanted or (email.split('@')[0] if email else 'sso-' + claims['sub'][:8]),
                                           email)
    except ApiError as error:
        raise Refused(error.code or 'sso_refused', error.message) from None
    if not admin['active']:
        raise Refused('sso_inactive', 'Your administrator account is switched off here - ask an owner')
    if change == 'created':
        store.record_audit(admin['name'], 'create_admin', admin['name'], {'role': role, 'email': admin['email'],
                                                                          'issuer': claims['iss']}, via='sso')
    elif change in ('linked', 'linked_role'):
        store.record_audit(admin['name'], 'link_admin_sso', admin['name'], {'issuer': claims['iss'], 'role': role},
                           via='sso')
    elif change == 'role':
        store.record_audit(admin['name'], 'update_admin', admin['name'], {'role': role, 'from': 'sso groups'},
                           via='sso')
    session_id = start_session(admin['name'], claims['iss'])
    store.record_audit(admin['name'], 'sign_in', admin['name'], {'issuer': claims['iss'], 'role': role}, via='sso')
    return admin, session_id, pending['return_to']


# --------------------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------------------

def start_session(admin_name, issuer):
    session_id, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    now = time.time()
    with db.transaction() as conn:
        conn.execute('DELETE FROM admin_sessions WHERE expires_at < ?', (now,))
        conn.execute('INSERT INTO admin_sessions (id_hash, admin_name, csrf, created_at, expires_at, details_json) '
                     'VALUES (?, ?, ?, ?, ?, ?)',
                     (_hash(session_id), admin_name, csrf, store.now(), now + config.sso_session_hours() * 3600,
                      json.dumps({'via': 'sso', 'issuer': issuer})))
    return session_id


def _session(session_id):
    if not session_id or len(session_id) > 200:
        return None
    row = db.connection().execute('SELECT id_hash, admin_name, csrf, created_at, expires_at FROM admin_sessions '
                                  'WHERE id_hash = ?', (_hash(session_id),)).fetchone()
    if row is None or row['expires_at'] < time.time():
        return None
    return row


def authenticate(session_id, csrf):
    """The Permission for a session cookie and its CSRF token, or None - for no session, an expired one, a missing or
    wrong token, or an inactive administrator, all alike."""
    if not config.sso_enabled() or not session_id:
        return None
    row = _session(session_id)
    if row is None or not csrf or not hmac.compare_digest(row['csrf'], csrf):
        return None
    admin = admins.get_admin(row['admin_name'])
    if admin is None or not admin['active']:
        return None
    _note_seen(row['id_hash'], admin)
    return admins.permission_for(admin, 'sso')


def _note_seen(id_hash, admin):
    now = time.monotonic()
    if now - _last_seen.get(id_hash, -_SEEN_INTERVAL) < _SEEN_INTERVAL:
        return
    _last_seen[id_hash] = now
    admins.note_seen(admin['name'])


def end_session(session_id):
    with db.transaction() as conn:
        conn.execute('DELETE FROM admin_sessions WHERE id_hash = ?', (_hash(session_id),))


def end_sessions_of(admin_name):
    """Every session of one administrator - when they're switched off or removed."""
    with db.transaction() as conn:
        conn.execute('DELETE FROM admin_sessions WHERE admin_name = ?', (admin_name,))


# --------------------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------------------

def _cookie(response, name, value, max_age, same_site):
    response.set_cookie(name, value, max_age=max_age, path='/', secure=True, httponly=True, samesite=same_site)


def _off():
    return jsonify({'error': 'Console sign-in through SSO is not configured', 'code': 'not_found'}), 404


def _refused_page(refused):
    store.record_audit('-', 'sign_in_refused', '-', {'code': refused.code, 'reason': refused.message}, via='sso')
    response = redirect(f'{CONSOLE}?' + urllib.parse.urlencode({'sso_error': refused.code,
                                                               'sso_message': refused.message}))
    response.delete_cookie(STATE_COOKIE, path='/', secure=True, httponly=True, samesite='Lax')
    return response


@bp.route('/sso/login', methods=['GET'])
def login():
    if not config.sso_enabled():
        return _off()
    try:
        url, browser = begin(request.args.get('next'))
    except Refused as refused:
        return _refused_page(refused)
    response = redirect(url)
    # Lax, not Strict: the provider sends the person back with a cross-site navigation, which must carry it.
    _cookie(response, STATE_COOKIE, browser, LOGIN_TTL, 'Lax')
    return response


@bp.route('/sso/callback', methods=['GET'])
def callback():
    if not config.sso_enabled():
        return _off()
    try:
        admin, session_id, return_to = finish(request.args.get('code'), request.args.get('state'),
                                              request.cookies.get(STATE_COOKIE),
                                              request.args.get('error_description') or request.args.get('error'))
    except Refused as refused:
        return _refused_page(refused)
    response = redirect(return_to)
    _cookie(response, SESSION_COOKIE, session_id, config.sso_session_hours() * 3600, 'Strict')
    response.delete_cookie(STATE_COOKIE, path='/', secure=True, httponly=True, samesite='Lax')
    return response


@bp.route('/session', methods=['GET'])
def session():
    """For the Console: whether SSO is on, and the signed-in administrator with the session's CSRF token - which only
    a page on this origin can read (the cookie is SameSite=Strict, and CORS never shares credentials)."""
    body = {'sso': config.sso_enabled(), 'signed_in': False, 'sso_only': config.sso_only()}
    row = _session(request.cookies.get(SESSION_COOKIE)) if config.sso_enabled() else None
    admin = admins.get_admin(row['admin_name']) if row is not None else None
    if row is not None and admin is not None and admin['active']:
        body.update(signed_in=True, admin=admin['name'], role=admin['role'], email=admin['email'], csrf=row['csrf'],
                    expires_at=time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(row['expires_at'])))
    response = jsonify(body)
    response.headers['Cache-Control'] = 'no-store'
    return response


@bp.route('/sso/logout', methods=['POST'])
def logout():
    if not config.sso_enabled():
        return _off()
    session_id = request.cookies.get(SESSION_COOKIE)
    permission = authenticate(session_id, request.headers.get(CSRF_HEADER))
    if permission is None:
        return jsonify({'error': 'Not signed in', 'code': 'unauthorized'}), 401
    end_session(session_id)
    store.record_audit(permission.name, 'sign_out', permission.name, None, via='sso')
    provider = oidc.metadata(config.sso_issuer()) or {}
    end = provider.get('end_session_endpoint')
    response = jsonify({'signed_out': True, 'provider_sign_out': (
        f"{end}{'&' if '?' in end else '?'}{urllib.parse.urlencode({'client_id': config.sso_client_id()})}"
        if end else None)})
    response.delete_cookie(SESSION_COOKIE, path='/', secure=True, httponly=True, samesite='Strict')
    return response
