"""Write params_vectors.jsonl (one case per line): what Python's params.resolve() makes of many definitions and
supplied values - the reference the data plane's own parameter rules (dataplane/src/params.rs) are tested against.
Where the data plane binds a value it must be Python's exactly; where Python refuses, the data plane must forward.

    .venv/bin/python dataplane/tests/make_vectors.py

Values are tagged so a float 5.0 and an int 5 stay apart: ["int", "5"], ["float", "5.0"], ["str", s], ["bool", b],
["null"]. A supplied value is the text the request carried - a query-string value, or a JSON body's value as JSON.
"""
import itertools
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from queryapigate import params  # noqa: E402
from queryapigate.errors import ApiError  # noqa: E402

SPECS = [
    'int', 'float', 'str', 'bool', 'INT', 'integer', 'number', 'string', 'boolean', 'nonsense', '',
    {}, {'type': None}, {'type': 5}, {'type': 'int', 'unknown_rule': 1},
    {'type': 'int', 'required': False}, {'type': 'int', 'required': 'no'}, {'type': 'int', 'default': 7},
    {'type': 'int', 'default': '7'}, {'type': 'int', 'default': 7.5}, {'type': 'int', 'default': None},
    {'type': 'int', 'default': 7, 'required': True}, {'type': 'int', 'default': 7, 'required': False},
    {'type': 'int', 'min': 1, 'max': 10}, {'type': 'int', 'min': 1.5}, {'type': 'int', 'min': 10, 'max': 1},
    {'type': 'int', 'min': True}, {'type': 'int', 'min': '1'},
    {'type': 'float', 'min': -1, 'max': 1.25}, {'type': 'float', 'enum': [1, 2.5, '3']},
    {'type': 'int', 'enum': [1, 2, 3]}, {'type': 'int', 'enum': ['1', '2']}, {'type': 'int', 'enum': [1.0]},
    {'type': 'int', 'enum': []}, {'type': 'str', 'enum': ['G', 'PG', 'R']}, {'type': 'str', 'enum': [1]},
    {'enum': [1, 'a', True]}, {'enum': [None]},
    {'type': 'str', 'min_length': 2, 'max_length': 4}, {'type': 'str', 'min_length': 2.0},
    {'type': 'str', 'min_length': -1}, {'type': 'str', 'min_length': 5, 'max_length': 2},
    {'type': 'int', 'min_length': 2}, {'min': 0, 'min_length': 1},
    {'type': 'str', 'pattern': '[A-Z]+'}, {'type': 'str', 'description': 'd'}, {'type': 'str', 'description': 5},
    {'type': 'str', 'example': 'x'}, {'type': 'int', 'example': 'x'}, {'type': 'int', 'example': 3, 'max': 2},
    {'type': 'str', 'embed': False}, {'type': 'str', 'embed': True}, {'type': 'str', 'from_claim': 'sub'},
    {'type': 'bool', 'default': 'yes'}, {'type': 'bool', 'default': 1}, {'type': 'str', 'default': 'PG',
                                                                         'enum': ['G', 'PG']},
    {'type': 'str', 'default': 'X', 'enum': ['G', 'PG']}, {'type': 'float', 'default': 1},
    5, None, ['int'],
]

URL_VALUES = ['5', '-5', '+5', '007', ' 5', '5 ', '1_000', '٣', '5.0', '5.5', '1e3', '.5', '5.', '-0', 'inf',
              'nan', '1e999', '9223372036854775807', '9223372036854775808', '-9223372036854775809', 'true',
              'TRUE', ' yes ', 'No', '0', '1', 'maybe', '', 'PG', 'G', 'abc', 'abcde', 'ab', 'é', 'Ünï', "O'Brien",
              'a\\b', 'x' * 3, '2.5', '3', '1.0', 'TRUE ', '١']
BODY_VALUES = ['5', '-5', '5.0', '5.5', '1e3', '1E3', '-0.0', '0', '1', 'true', 'false', 'null', '"5"', '"PG"',
               '"abc"', '""', '[1]', '{"a": 1}', '9223372036854775807', '9223372036854775808',
               '123456789012345678901234567890', '1e400', '2.5', '3', '1.0', '"é"', '0.1', '1e-7',
               '100000000000000000.0']


def tag(value):
    if value is None:
        return ['null']
    if isinstance(value, bool):
        return ['bool', value]
    if isinstance(value, int):
        return ['int', str(value)]
    if isinstance(value, float):
        return ['float', repr(value)]
    if isinstance(value, str):
        return ['str', value]
    return ['other', repr(value)]


def outcome(declared, supplied, used):
    values = {name: value for name, value in supplied}
    try:
        resolved = params.resolve(declared, values, used=used)
    except ApiError as error:
        return {'error': error.code}
    return {'values': {name: tag(value) for name, value in resolved.items() if name in used}}


def valid(declared):
    """Whether the definitions pass the validation a query is saved through - the only ones the API can store."""
    try:
        params.parse_definitions(declared)
    except ApiError:
        return False
    return True


def cases():
    for spec in SPECS:
        declared = {'p': spec}
        yield {'declared': declared, 'supplied': [], 'used': ['p']}
        yield {'declared': declared, 'supplied': [], 'used': []}  # declared, but the SQL doesn't use it
        for raw in URL_VALUES:
            yield {'declared': declared, 'supplied': [{'name': 'p', 'from': 'url', 'raw': raw}], 'used': ['p']}
        for raw in BODY_VALUES:
            yield {'declared': declared, 'supplied': [{'name': 'p', 'from': 'body', 'raw': raw}], 'used': ['p']}
    # undeclared parameters pass through untouched; several at once, and the body overriding the URL
    for raw in BODY_VALUES:
        yield {'declared': {}, 'supplied': [{'name': 'q', 'from': 'body', 'raw': raw}], 'used': ['q']}
    for a, b in itertools.product(('5', 'x'), ('6', '"y"')):
        yield {'declared': {'p': 'int', 'q': 'str'},
               'supplied': [{'name': 'p', 'from': 'url', 'raw': a}, {'name': 'q', 'from': 'url', 'raw': 'u'},
                            {'name': 'p', 'from': 'body', 'raw': b}], 'used': ['p', 'q']}
    yield {'declared': 'not an object', 'supplied': [{'name': 'p', 'from': 'url', 'raw': 'x'}], 'used': ['p']}
    yield {'declared': None, 'supplied': [], 'used': []}


def main():
    vectors = []
    for case in cases():
        supplied = [(s['name'], s['raw'] if s['from'] == 'url' else json.loads(s['raw'])) for s in case['supplied']]
        vectors.append({**case, 'valid': valid(case['declared']),
                        'expected': outcome(case['declared'], supplied, set(case['used']))})
    path = Path(__file__).parent / 'params_vectors.jsonl'
    path.write_text(''.join(json.dumps(v, ensure_ascii=False, separators=(',', ':')) + '\n' for v in vectors))
    errors = sum('error' in v['expected'] for v in vectors)
    print(f'{len(vectors)} cases ({errors} refused by Python) -> {path}')


if __name__ == '__main__':
    main()
