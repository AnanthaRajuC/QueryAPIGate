"""Signed-in users: `Authorization: Bearer <JWT>` as an alternative to an API key, off unless configured.

An API key identifies an *application*; a JWT identifies a *person* - the token your app already gets when a user
signs in, from your own backend (QUERYAPIGATE_JWT_SECRET, HS256) or from an identity provider such as Auth0,
Cognito, Firebase, Keycloak or Azure AD (QUERYAPIGATE_JWT_JWKS_URL, RS256). Every app user can then call the API
as themselves without anyone issuing them a key.

**Verification** (authenticate()): the signature, against the secret or the provider's published keys (fetched
from the JWKS URL and cached; refetched for an unknown key id at most once per 30 seconds - PyJWT's own cooldown,
so tokens with made-up key ids can't make this server hammer the provider; providers publish a new key well
before signing with it, so a rotation is picked up in time); `exp` is required and checked, as are `nbf`/`iat`
when present (30 s leeway for clock skew); `iss` and `aud` when configured - and with a JWKS URL they must be,
see config.check_jwt_settings(). Only the configured algorithms are accepted, never `none`, and never an HMAC
algorithm against a public key. Any failure is a plain 401, the same as a wrong API key - the caller learns
nothing about why.

**What a user may do** comes from a role (apikeys.py): QUERYAPIGATE_JWT_ROLE for everyone, or the role named by the
token's QUERYAPIGATE_JWT_ROLE_CLAIM. A role's grants apply exactly as they do to a key created from it, including
its rate limit (counted per user) and allowed_ips. A token naming no existing role is refused.

**Who the user is**: the QUERYAPIGATE_JWT_USER_CLAIM claim (default `sub`). The caller's name is `jwt:<that value>`
- in logs, run history and GET /api/v1/history (`key=jwt:...`), and the live-event filter, so each user's event stream
carries only their own runs. API key names can't contain `:`, so the two can never collide. The verified claims
travel with the caller (Permission.claims), which is what lets a saved query take a parameter from the token
instead of the request (params.py's `from_claim`): `WHERE user_id = :user_id` with `"user_id": {"from_claim":
"sub"}` can only ever return the caller's own rows.

A JWT can't be revoked before it expires - keep token lifetimes short (minutes, with refresh), as identity
providers do by default. Disabling the role, or removing the grant from it, takes effect on the next request.
"""
import logging
import threading

from . import apikeys, config

log = logging.getLogger('queryapigate')

NAME_PREFIX = 'jwt:'
_LEEWAY = 30            # seconds of clock skew tolerated on exp/nbf/iat
_MAX_TOKEN_LENGTH = 8192
_jwks_clients: dict = {}  # JWKS URL -> PyJWKClient
_jwks_lock = threading.Lock()


def _jwks_client(url):
    """One cached PyJWKClient per URL: keys are fetched once and reused, and refetched only for a key id it hasn't
    seen (a provider rotating its keys) - and then at most once per cooldown (PyJWT >= 2.14), never per request."""
    with _jwks_lock:
        client = _jwks_clients.get(url)
        if client is None:
            from jwt import PyJWKClient
            client = _jwks_clients[url] = PyJWKClient(url, cache_keys=True, lifespan=3600,
                                                      timeout=config.CONNECT_TIMEOUT)
        return client


def bearer_token(authorization):
    """The token in an `Authorization: Bearer ...` header value, or None."""
    if not authorization:
        return None
    scheme, _, token = authorization.strip().partition(' ')
    token = token.strip()
    if scheme.lower() != 'bearer' or not token or len(token) > _MAX_TOKEN_LENGTH:
        return None
    return token


def verify(token):
    """The token's verified claims, or None if it isn't valid for this server (see the module docstring)."""
    import jwt
    try:
        if config.jwt_jwks_url():
            key = _jwks_client(config.jwt_jwks_url()).get_signing_key_from_jwt(token).key
        else:
            key = config.jwt_secret()
        audience = config.jwt_audience()
        return jwt.decode(token, key, algorithms=config.jwt_algorithms(), audience=audience,
                          issuer=config.jwt_issuer(), leeway=_LEEWAY,
                          options={'require': ['exp'], 'verify_aud': audience is not None})
    except jwt.PyJWKClientError:
        log.warning('Could not get the signing key for a token from %s', config.jwt_jwks_url(), exc_info=True)
        return None
    except jwt.InvalidTokenError:
        return None


def claim(claims, name):
    """A claim by name - or, for a dotted name that isn't itself a claim, by path (`app_metadata.tenant`).
    Namespaced claims (`https://example.com/tenant`) contain dots too, so the exact name is tried first."""
    if name in claims:
        return claims[name]
    value = claims
    for part in name.split('.'):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _role_for(claims):
    """The first existing role among those the token's role claim names, then QUERYAPIGATE_JWT_ROLE."""
    names = []
    claim_name = config.jwt_role_claim()
    if claim_name:
        value = claim(claims, claim_name)
        if isinstance(value, str):
            names.append(value)
        elif isinstance(value, list):
            names += [v for v in value if isinstance(v, str)]
    if config.jwt_role():
        names.append(config.jwt_role())
    for name in names:
        role = apikeys.get_role(name)
        if role is not None:
            return name, role
    return None, None


def authenticate(authorization, client_ip=None):
    """A Permission for a valid bearer token, or None - see the module docstring."""
    if not config.jwt_enabled():
        return None
    token = bearer_token(authorization)
    if token is None:
        return None
    claims = verify(token)
    if claims is None:
        return None
    user = claim(claims, config.jwt_user_claim())
    if not isinstance(user, (str, int)) or isinstance(user, bool) or str(user) == '':
        return None
    role_name, role = _role_for(claims)
    if role is None:
        log.warning('A valid token for %s names no existing role - refused (QUERYAPIGATE_JWT_ROLE / '
                    'QUERYAPIGATE_JWT_ROLE_CLAIM)', user)
        return None
    if not apikeys.ip_allowed(role, client_ip):
        return None
    return apikeys.permission_from_grants(f'{NAME_PREFIX}{user}', role, claims=claims)
