"""Errors, and the one shape every front door reports them in (BACKLOG #69).

An error response is `{"error": <message>, "code": <code>, "request_id": <id>}` plus any extras its raiser adds
(`detail`, `errors`, `retry_after`, ...). `code` is stable and documented in API.md's Errors section - clients
branch on it; `error` is for people and may be reworded in any release. MCP tool errors carry the same `error` and
`code` in their structuredContent.
"""

# The code an error gets when its raiser names none: one per HTTP status, so nothing reaches a client without a code.
DEFAULT_CODES = {400: 'invalid_request', 401: 'unauthorized', 403: 'forbidden', 404: 'not_found',
                 405: 'method_not_allowed', 409: 'conflict', 412: 'precondition_failed', 413: 'payload_too_large',
                 415: 'unsupported_media_type', 429: 'rate_limited', 500: 'internal_error', 502: 'upstream_failed',
                 503: 'unavailable', 504: 'query_timeout'}


def code_for(status, code=None):
    """`code` when the raiser gave one, else the default for `status`."""
    return code or DEFAULT_CODES.get(status, 'error')


class ApiError(Exception):
    """An error that maps directly onto an HTTP response (or an MCP tool error) - see the module docstring."""

    def __init__(self, message, status=400, code=None, **extra):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code_for(status, code)
        self.extra = extra
