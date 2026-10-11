"""An identity provider's published metadata (RFC 8414 / OpenID Connect discovery), shared by everything that trusts
one: the MCP server's OAuth (ADR 0006) needs its keys, the Console's sign-in (ADR 0007) its endpoints too.

metadata(issuer) finds the first document that answers, in the order the MCP specification gives, and uses it only if
it names that issuer as its own (RFC 8414: a document naming another issuer must not be used) and every endpoint in it
is `https://` (`localhost` excepted) - a document fetched over HTTPS that sends the browser, or this server, to a plain
HTTP endpoint is refused whole. A redirect off `https://` is refused too. A document is reused for an hour; one that
can't be fetched again is kept while it can't (an outage doesn't stop sign-in that worked a minute ago); a provider
never reached is tried again after RETRY seconds, not on every request.
"""
import json
import logging
import threading
import time
import urllib.parse
import urllib.request

from . import config

log = logging.getLogger('queryapigate')

RETRY = 30            # seconds before a provider never reached is tried again
FRESH_FOR = 3600      # seconds a fetched document is used before it is fetched again
MAX_DOCUMENT = 1024 * 1024
ENDPOINTS = ('jwks_uri', 'authorization_endpoint', 'token_endpoint', 'end_session_endpoint', 'userinfo_endpoint')

_cache: dict = {}     # issuer -> (metadata or None, fetched at)
_lock = threading.Lock()


def discovery_urls(issuer):
    """Where an issuer's metadata may be, in the MCP specification's order: RFC 8414's oauth-authorization-server and
    OpenID Connect's openid-configuration, both inserted before an issuer's path, then OIDC's appended to it."""
    parts = urllib.parse.urlsplit(issuer)
    origin = f'{parts.scheme}://{parts.netloc}'
    path = parts.path.rstrip('/')
    if not path:
        return [f'{origin}/.well-known/oauth-authorization-server', f'{origin}/.well-known/openid-configuration']
    return [f'{origin}/.well-known/oauth-authorization-server{path}',
            f'{origin}/.well-known/openid-configuration{path}', f'{origin}{path}/.well-known/openid-configuration']


def fetch_json(url, data=None, headers=None):
    """A JSON document from `url` (a POST when `data` is given), refusing one that was redirected off https."""
    request = urllib.request.Request(url, data=data, headers={'Accept': 'application/json', **(headers or {})})
    with urllib.request.urlopen(request, timeout=config.CONNECT_TIMEOUT) as response:  # noqa: S310 - an operator's URL
        if not config._secure_url(response.geturl()):  # redirected off https: what it says could be anyone's
            raise ValueError(f'{url} redirected to {response.geturl()}')
        return json.loads(response.read(MAX_DOCUMENT))


def _usable(document, issuer):
    if not isinstance(document, dict) or document.get('issuer') != issuer:
        return False
    if not isinstance(document.get('jwks_uri'), str):
        return False
    return all(isinstance(document[name], str) and config._secure_url(document[name])
               for name in ENDPOINTS if document.get(name) is not None)


def _fetch(issuer):
    for url in discovery_urls(issuer):
        try:
            document = fetch_json(url)
        except (OSError, ValueError):
            continue
        if _usable(document, issuer):
            return document
    return None


def metadata(issuer):
    """The issuer's metadata (see the module docstring), or None."""
    with _lock:
        cached, fetched_at = _cache.get(issuer, (None, None))
        age = time.monotonic() - fetched_at if fetched_at is not None else None
        if cached is not None and age < FRESH_FOR:
            return cached
        if cached is None and age is not None and age < RETRY:
            return None
        found = _fetch(issuer)
        if found is None and cached is not None:
            log.warning("Could not refresh the metadata of %s - using what was fetched before", issuer)
            _cache[issuer] = (cached, time.monotonic())
            return cached
        if found is None:
            log.warning('Could not find the metadata of the identity provider %s - tried %s', issuer,
                        ', '.join(discovery_urls(issuer)))
        _cache[issuer] = (found, time.monotonic())
        return found


def jwks_uri(issuer):
    found = metadata(issuer)
    return found['jwks_uri'] if found else None


def forget():
    """Drop every cached document (tests, and a changed setting)."""
    with _lock:
        _cache.clear()
