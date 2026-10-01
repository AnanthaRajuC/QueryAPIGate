"""Parameter definitions of a saved query: checking the definitions, and enforcing them on request values.

A saved query declares its parameters in ``query_parameters``. The short form is just a type
(``{"id": "int"}``); the full form adds rules::

    {
        "rating":     {"type": "str", "enum": ["G", "PG", "R"], "default": "PG", "description": "MPAA rating"},
        "max_length": {"type": "int", "min": 1, "max": 600},
        "title":      {"type": "str", "required": false, "min_length": 2, "pattern": "[A-Za-z ]+"}
    }

Rules: ``type`` (int, float, str, bool), ``required`` (default: true unless a ``default`` is given), ``default``,
``enum``, ``min``/``max`` (numbers), ``min_length``/``max_length``/``pattern`` (text) and ``description``.
An optional parameter that is neither supplied nor defaulted is bound as NULL.

Definitions are validated strictly when a query is saved. At run time they are read leniently, so older saved
files with unusual definitions keep working.
"""
import math
import re

from .errors import ApiError

_TYPES = {'int': 'integer', 'integer': 'integer', 'float': 'number', 'number': 'number',
          'str': 'string', 'string': 'string', 'bool': 'boolean', 'boolean': 'boolean'}
_RULE_KEYS = {'type', 'required', 'default', 'enum', 'min', 'max', 'min_length', 'max_length', 'pattern',
              'description', 'from_claim'}
_MAX_CLAIM_NAME_LENGTH = 200
_NAME_RE = re.compile(r'^[A-Za-z_]\w*$')
_MAX_PATTERN_LENGTH = 500
_INT64_MIN, _INT64_MAX = -2 ** 63, 2 ** 63 - 1


class _Invalid(Exception):
    """A value or a definition that breaks a rule; the message completes the sentence '<name> ...'."""


def _to_bool(text):
    lowered = text.strip().lower()
    if lowered in ('true', '1', 'yes'):
        return True
    if lowered in ('false', '0', 'no'):
        return False
    raise ValueError(text)


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _plain(spec):
    return {'type': None, 'required': True, 'has_default': False, 'default': None, 'enum': None, 'min': None,
            'max': None, 'min_length': None, 'max_length': None, 'pattern': None, 'description': None,
            'from_claim': None, **spec}


def _coerce(type_name, value):
    """Convert a value (usually a query-string text) to the declared type, or raise _Invalid."""
    if type_name == 'integer':
        if isinstance(value, str):
            try:
                value = int(value)
            except ValueError:
                raise _Invalid('must be an integer') from None
        if isinstance(value, int) and not isinstance(value, bool):
            # Outside a signed 64-bit integer, no supported driver can bind the value - it would fail inside
            # the database call as a 500 instead of a 400 naming the parameter.
            if not _INT64_MIN <= value <= _INT64_MAX:
                raise _Invalid('must be a 64-bit integer')
            return value
        raise _Invalid('must be an integer')
    if type_name == 'number':
        if isinstance(value, str):
            try:
                value = float(value)
            except ValueError:
                raise _Invalid('must be a number') from None
        if _is_number(value):
            return value
        raise _Invalid('must be a number')
    if type_name == 'boolean':
        if isinstance(value, str):
            try:
                return _to_bool(value)
            except ValueError:
                raise _Invalid('must be true or false') from None
        if isinstance(value, bool):
            return value
        raise _Invalid('must be true or false')
    if type_name == 'string':
        if isinstance(value, str):
            return value
        raise _Invalid('must be text')
    if isinstance(value, (str, bool)) or _is_number(value):  # untyped: any scalar
        return value
    raise _Invalid('must be text, a number or true/false')


def _check(spec, value):
    """Return the value converted to the declared type after applying every rule, or raise _Invalid."""
    value = _coerce(spec['type'], value)
    if spec['enum'] is not None and value not in spec['enum']:
        raise _Invalid('must be one of: ' + ', '.join(str(v) for v in spec['enum']))
    if _is_number(value):
        if spec['min'] is not None and value < spec['min']:
            raise _Invalid(f"must be at least {spec['min']:g}")
        if spec['max'] is not None and value > spec['max']:
            raise _Invalid(f"must be at most {spec['max']:g}")
    if isinstance(value, str):
        if spec['min_length'] is not None and len(value) < spec['min_length']:
            raise _Invalid(f"must be at least {spec['min_length']} characters long")
        if spec['max_length'] is not None and len(value) > spec['max_length']:
            raise _Invalid(f"must be at most {spec['max_length']} characters long")
        if spec['pattern'] is not None and not re.fullmatch(spec['pattern'], value):
            raise _Invalid(f"must match the pattern {spec['pattern']}")
    return value


# --------------------------------------------------------------------------------------
# Definitions
# --------------------------------------------------------------------------------------

def _parse_strict(spec):
    if isinstance(spec, str):
        spec = {'type': spec}
    if not isinstance(spec, dict):
        raise _Invalid("must be a type name such as 'int' or an object of rules")
    unknown = sorted(set(spec) - _RULE_KEYS)
    if unknown:
        raise _Invalid(f"has unknown rule(s): {', '.join(unknown)} (allowed: {', '.join(sorted(_RULE_KEYS))})")

    type_name = None
    if spec.get('type') is not None:
        type_name = _TYPES.get(str(spec['type']).lower())
        if type_name is None:
            raise _Invalid(f"has unknown type '{spec['type']}' (use int, float, str or bool)")
    parsed = _plain({'type': type_name})

    if 'required' in spec:
        if not isinstance(spec['required'], bool):
            raise _Invalid("'required' must be true or false")
        parsed['required'] = spec['required']
    elif 'default' in spec:
        parsed['required'] = False
    if 'description' in spec:
        if not isinstance(spec['description'], str):
            raise _Invalid("'description' must be text")
        parsed['description'] = spec['description']
    if 'from_claim' in spec:
        # The value comes from the caller's verified sign-in token (jwtauth.py), never from the request - so it is
        # always required, and a default would be a way for a request without that claim to get a value anyway.
        claim = spec['from_claim']
        if not isinstance(claim, str) or not claim.strip() or len(claim) > _MAX_CLAIM_NAME_LENGTH:
            raise _Invalid(f"'from_claim' must be a token claim name, e.g. \"sub\" (at most "
                           f"{_MAX_CLAIM_NAME_LENGTH} characters)")
        if 'default' in spec or spec.get('required') is False:
            raise _Invalid("a 'from_claim' parameter is always required and cannot have a default")
        parsed['from_claim'] = claim.strip()

    numeric = type_name in (None, 'integer', 'number')
    textual = type_name in (None, 'string')
    for key in ('min', 'max'):
        if key in spec:
            if not numeric:
                raise _Invalid(f"'{key}' only applies to numbers (use min_length/max_length for text)")
            if not _is_number(spec[key]):
                raise _Invalid(f"'{key}' must be a number")
            parsed[key] = spec[key]
    if parsed['min'] is not None and parsed['max'] is not None and parsed['min'] > parsed['max']:
        raise _Invalid("'min' must not be greater than 'max'")
    for key in ('min_length', 'max_length'):
        if key in spec:
            if not textual:
                raise _Invalid(f"'{key}' only applies to text")
            if not isinstance(spec[key], int) or isinstance(spec[key], bool) or spec[key] < 0:
                raise _Invalid(f"'{key}' must be a non-negative integer")
            parsed[key] = spec[key]
    if (parsed['min_length'] is not None and parsed['max_length'] is not None
            and parsed['min_length'] > parsed['max_length']):
        raise _Invalid("'min_length' must not be greater than 'max_length'")
    if 'pattern' in spec:
        if not textual:
            raise _Invalid("'pattern' only applies to text")
        if not isinstance(spec['pattern'], str) or len(spec['pattern']) > _MAX_PATTERN_LENGTH:
            raise _Invalid(f"'pattern' must be text of at most {_MAX_PATTERN_LENGTH} characters")
        try:
            re.compile(spec['pattern'])
        except re.error as error:
            raise _Invalid(f"'pattern' is not a valid regular expression ({error})") from None
        parsed['pattern'] = spec['pattern']

    if 'enum' in spec:
        if not isinstance(spec['enum'], list) or not spec['enum']:
            raise _Invalid("'enum' must be a non-empty list")
        try:
            parsed['enum'] = [_coerce(type_name, v) for v in spec['enum']]
        except _Invalid as error:
            raise _Invalid(f"has an 'enum' value that {error}") from None
    if 'default' in spec:
        if spec.get('required') is True:
            raise _Invalid("cannot be both 'required' and have a 'default'")
        try:
            parsed['default'] = _check(parsed, spec['default'])
        except _Invalid as error:
            raise _Invalid(f"has a 'default' that {error}") from None
        parsed['has_default'] = True
    return parsed


def parse_definitions(query_parameters):
    """Validate the ``query_parameters`` of a query about to be saved; raises ApiError(400) listing every problem."""
    if not isinstance(query_parameters, dict):
        raise ApiError('query_parameters must be a JSON object')
    errors = {}
    for name, spec in query_parameters.items():
        if not _NAME_RE.match(name):
            errors[name] = 'is not a valid parameter name (letters, digits and underscores; not starting with a digit)'
            continue
        try:
            _parse_strict(spec)
        except _Invalid as error:
            errors[name] = str(error)
    if errors:
        raise ApiError('Invalid query_parameters: ' + '; '.join(f'{n} {m}' for n, m in errors.items()), 400,
                       errors=errors)


def read_definition(spec):
    """A definition as the normalised rule dict; anything unusable degrades to an untyped, required parameter -
    keeping a usable `from_claim` though: degrading that one would quietly let the request supply a value meant
    to come only from the caller's token, so it fails closed instead."""
    try:
        return _parse_strict(spec)
    except _Invalid:
        if isinstance(spec, str):
            return _plain({'type': _TYPES.get(spec.lower())})
        if isinstance(spec, dict) and isinstance(spec.get('from_claim'), str) and spec['from_claim'].strip():
            return _plain({'from_claim': spec['from_claim'].strip()})
        return _plain({})


def read_definitions(query_parameters):
    if not isinstance(query_parameters, dict):
        return {}
    return {name: read_definition(spec) for name, spec in query_parameters.items()}


# --------------------------------------------------------------------------------------
# Values
# --------------------------------------------------------------------------------------

def resolve(declared, supplied, used=None):
    """Apply a saved query's parameter definitions to the values a request supplied.

    Returns every value to bind: supplied ones converted and checked, defaults filled in, optional ones without
    a value set to None. Values for undeclared parameters pass through untouched. Declared parameters that the
    SQL does not use (``used`` is the set of names it does use) are not enforced. Raises ApiError(400) listing
    every violation.
    """
    values = dict(supplied)
    errors = {}
    for name, spec in read_definitions(declared).items():
        if used is not None and name not in used:
            continue
        if name not in values:
            if spec['has_default']:
                values[name] = spec['default']
            elif spec['required']:
                errors[name] = 'is required'
            else:
                values[name] = None
            continue
        if values[name] is None:
            if spec['required']:
                errors[name] = 'must not be null'
            continue
        try:
            values[name] = _check(spec, values[name])
        except _Invalid as error:
            errors[name] = str(error)
    if errors:
        raise ApiError('Invalid parameters: ' + '; '.join(f'{n} {m}' for n, m in errors.items()), 400,
                       errors=errors)
    return values


def json_schema(spec):
    """The OpenAPI/JSON-schema description of a parameter's type and rules."""
    schema = {'type': spec['type'] or 'string'}
    for rule, key in (('enum', 'enum'), ('min', 'minimum'), ('max', 'maximum'), ('min_length', 'minLength'),
                      ('max_length', 'maxLength'), ('pattern', 'pattern')):
        if spec[rule] is not None:
            schema[key] = spec[rule]
    if spec['has_default']:
        schema['default'] = spec['default']
    return schema
