"""Pure JSON-document handling for Mongo find queries: the non-SQL sibling of sqltools.py.

A Mongo filter is already a real, structured value (a dict), not text to be parsed or spliced - so there is
no equivalent of sqltools.py's literal-aware regex scanning here. A placeholder is a string leaf that is
exactly ``:name``; substituting it replaces that whole leaf with the resolved value (any JSON-safe scalar -
a number, bool, null or string), never string-splicing into a larger value, so there is no way for a
resolved value to "break out" into a different operator the way SQL text injection would.
"""
import re

from .errors import ApiError

_PLACEHOLDER_RE = re.compile(r'^:([A-Za-z_]\w*)$')

# $where and $function/$accumulator run arbitrary server-side JavaScript - the Mongo equivalent of SQL
# injection into a WHERE clause, and the one thing a find-only, read-only path must never allow through.
_FORBIDDEN_OPERATORS = frozenset({'$where', '$function', '$accumulator'})


def _walk_names(node, seen):
    if isinstance(node, str):
        match = _PLACEHOLDER_RE.match(node)
        if match:
            seen.append(match.group(1))
    elif isinstance(node, dict):
        for value in node.values():
            _walk_names(value, seen)
    elif isinstance(node, list):
        for item in node:
            _walk_names(item, seen)


def placeholder_names(filter_doc):
    """Every ``:name`` placeholder ``filter_doc`` uses, once each, in order of appearance."""
    seen = []
    _walk_names(filter_doc, seen)
    return list(dict.fromkeys(seen))


def _substitute(node, values):
    if isinstance(node, str):
        match = _PLACEHOLDER_RE.match(node)
        if match and match.group(1) in values:
            return values[match.group(1)]
        return node
    if isinstance(node, dict):
        return {key: _substitute(value, values) for key, value in node.items()}
    if isinstance(node, list):
        return [_substitute(item, values) for item in node]
    return node


def fill_placeholders(filter_doc, values):
    """Return a copy of ``filter_doc`` with every ``:name`` placeholder replaced by ``values[name]``.

    Unlike sqltools.fill_placeholders, this never produces text to re-parse - the substituted value becomes
    a real value in the returned document, so it can be a number, bool or null, not just safe text.
    """
    missing = sorted(set(placeholder_names(filter_doc)) - set(values))
    if missing:
        raise ApiError(f"No value provided for placeholder(s): {', '.join(missing)}")
    return _substitute(filter_doc, values)


def _check_forbidden(node):
    if isinstance(node, dict):
        for key, value in node.items():
            if key in _FORBIDDEN_OPERATORS:
                raise ApiError(f"'{key}' is not allowed - it runs arbitrary server-side JavaScript", 403)
            _check_forbidden(value)
    elif isinstance(node, list):
        for item in node:
            _check_forbidden(item)


def validate_filter(filter_doc):
    """Check a Mongo filter document is well-shaped and free of the JS-execution operators - the find-only
    equivalent of sqltools.validate_sql()'s single-statement/read-only checks."""
    if not isinstance(filter_doc, dict):
        raise ApiError('filter must be a JSON object')
    _check_forbidden(filter_doc)
    return filter_doc
