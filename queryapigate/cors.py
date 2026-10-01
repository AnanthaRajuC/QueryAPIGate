"""Cross-origin (CORS) support for browser clients, off unless QUERYAPIGATE_CORS_ORIGINS is set.

Without it a web page on another origin cannot call the API: browsers refuse to send the JSON requests and to read
the answers. CORS only tells the *browser* which sites may call; it is not authentication (use QUERYAPIGATE_API_KEY).
"""
from flask import Response

from . import config

ALLOWED_METHODS = 'GET, POST, PATCH, DELETE, OPTIONS'
ALLOWED_HEADERS = 'Content-Type, X-API-Key, Authorization, X-Request-Id'
# Browsers hide response headers a page has not been told about, and pagination depends on these.
EXPOSED_HEADERS = ('X-Page, X-Page-Size, X-Has-More, X-RateLimit-Limit, X-RateLimit-Remaining, Retry-After, '
                   'X-Request-Id')
PREFLIGHT_MAX_AGE = '600'


def allow_origin_value(origin):
    """The Access-Control-Allow-Origin value for a request from ``origin``, or None when it is not allowed."""
    allowed = config.cors_origins()
    if allowed is None or not origin:
        return None
    if allowed == '*':
        return '*'
    return origin if origin.rstrip('/').lower() in allowed else None


def is_preflight(request):
    return (request.method == 'OPTIONS' and 'Access-Control-Request-Method' in request.headers
            and config.cors_origins() is not None)


def preflight_response(origin):
    """Answer a browser's permission check before it sends the real request (no auth: it carries no key)."""
    response = Response(status=204)
    value = allow_origin_value(origin)
    if value:
        response.headers['Access-Control-Allow-Methods'] = ALLOWED_METHODS
        response.headers['Access-Control-Allow-Headers'] = ALLOWED_HEADERS
        response.headers['Access-Control-Max-Age'] = PREFLIGHT_MAX_AGE
        add_headers(response, origin)
    return response


def add_headers(response, origin):
    value = allow_origin_value(origin)
    if value:
        response.headers['Access-Control-Allow-Origin'] = value
        response.headers['Access-Control-Expose-Headers'] = EXPOSED_HEADERS
        if value != '*':
            response.headers.add('Vary', 'Origin')  # the answer depends on who asked
    return response
