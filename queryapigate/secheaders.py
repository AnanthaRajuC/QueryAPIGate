"""Security headers on every response (ADR 0010, BACKLOG #93) - one policy for the main server (app.py's
after_request), the MCP server (asgi()) and the live events server (events.py), so none relies on a reverse proxy to
add them.

- Every response: `X-Content-Type-Options: nosniff`.
- API responses (JSON, CSV, Parquet... anything but an HTML page): a CSP that lets a browser led to render one run
  nothing and frame it nowhere, and `Referrer-Policy: no-referrer`. HTML pages - the Console (console.py) and /docs
  (openapi.py) - set their own CSP; this never overwrites a header a route set.
- `Cache-Control: no-store` unless the route said otherwise: rows, keys and settings belong to the caller who asked.
  A saved query with cache_ttl says `private, max-age=N` (app.py); the Console's assets say their own.
- HSTS only when QUERYAPIGATE_HSTS_MAX_AGE is set, and only on a request that arrived over HTTPS (behind a proxy, as
  QUERYAPIGATE_TRUST_PROXY lets the server tell): sent over plain HTTP it would lock browsers out of a server with none.
"""
from . import config

API_CSP = "default-src 'none'; frame-ancestors 'none'"


def api_headers():
    """The headers every API response gets, as (name, value) pairs - for front doors that write their own."""
    return [('X-Content-Type-Options', 'nosniff'), ('Content-Security-Policy', API_CSP),
            ('Referrer-Policy', 'no-referrer'), ('Cache-Control', 'no-store')]


def apply(response, secure):
    """Add what `response` doesn't already have. `secure`: the request arrived over HTTPS."""
    headers = response.headers
    headers.setdefault('X-Content-Type-Options', 'nosniff')
    if response.mimetype != 'text/html':
        headers.setdefault('Content-Security-Policy', API_CSP)
        headers.setdefault('Referrer-Policy', 'no-referrer')
    headers.setdefault('Cache-Control', 'no-store')
    max_age = config.hsts_max_age()
    if max_age and secure:
        headers.setdefault('Strict-Transport-Security', f'max-age={max_age}')
    return response


def asgi(app):
    """An ASGI app with api_headers() added to each HTTP response that doesn't set them itself (the MCP server)."""
    defaults = [(name.lower().encode(), value.encode()) for name, value in api_headers()]

    async def wrapped(scope, receive, send):
        if scope['type'] != 'http':
            return await app(scope, receive, send)

        async def send_with_headers(message):
            if message['type'] == 'http.response.start':
                present = {name.lower() for name, _ in message.get('headers', [])}
                message = {**message, 'headers': [*message.get('headers', []),
                                                  *((n, v) for n, v in defaults if n not in present)]}
            await send(message)

        return await app(scope, receive, send_with_headers)

    return wrapped
