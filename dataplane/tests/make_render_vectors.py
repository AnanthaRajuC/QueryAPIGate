"""Write render_vectors.jsonl: for typed values on real servers, the exact JSON body Python answers with - through its
driver, masking.apply(), formats.ResultSetDTO and Flask's encoder - beside the column types and raw bytes the data plane
receives for the same statement. The data plane's renderer (dataplane/src/render.rs) must produce the same body, byte
for byte, or decline (and forward); where Python fails, it must decline.

    cargo build --release --manifest-path dataplane/Cargo.toml
    QUERYAPIGATE_DATAPLANE_TEST_POSTGRES=127.0.0.1:55435:postgres:pw:postgres \\
    QUERYAPIGATE_DATAPLANE_TEST_MYSQL=127.0.0.1:55436:root:pw:shop \\
    .venv/bin/python dataplane/tests/make_render_vectors.py
"""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))
os.environ['QUERYAPIGATE_MASK_KEY'] = 'render-vectors-key'

from flask import Flask, jsonify  # noqa: E402
from test_forwarding import binary  # noqa: E402
from test_queries import details  # noqa: E402

from queryapigate import dataplane, formats, masking, runners  # noqa: E402
from queryapigate.app import JSONProvider  # noqa: E402

POSTGRES = [
    "5::int2", "-5::int4", "9223372036854775807::int8", "42::oid", "0.1::float4", "0.1::float8", "1e300::float8",
    "'-0'::float8", "'NaN'::float8", "'Infinity'::float8", "'-Infinity'::float8", "1e16::float8", "123.456::float8",
    "3.4028235e38::float4", "5.0::numeric", "0.0000001::numeric", "12345678901234567890.123::numeric",
    "'NaN'::numeric", "1e20::numeric", "-3.14::numeric", "true", "false", "'hello'::text", "'é😀汉'::text",
    "E'q\"\\\\\\n\\t\\x01\\x7f'::text", "''::text", "'a'::char(5)", "'x'::varchar", "'pg_name'::name", "'c'::\"char\"",
    "'\\x48656c6c6f'::bytea", "'\\xff00fe80'::bytea", "''::bytea", "'2024-02-29'::date", "'0001-01-01'::date",
    "'9999-12-31'::date", "'infinity'::date", "'12:34:56'::time", "'12:34:56.5'::time", "'23:59:59.999999'::time",
    "'12:34:56+05:30'::timetz", "'12:34:56.25-08'::timetz", "'2024-01-02 03:04:05'::timestamp",
    "'2024-01-02 03:04:05.678'::timestamp", "'2024-01-02 03:04:05+00'::timestamptz",
    "'2024-07-02 03:04:05.5+00'::timestamptz", "'1 day 02:03:04'::interval", "'-1 hour'::interval",
    "'{\"a\": 1, \"b\": [1.5, \"x\", null, true], \"c\": \"é\"}'::json", "'5'::jsonb", "'\"s\"'::jsonb",
    "'null'::jsonb", "'[1, 2.0, 1e2, -0, 0.1]'::jsonb", "'{\"k\": 12345678901234567890, \"z\": 1, \"a\": 2}'::json",
    "'{\"z\": 1, \"a\": 2}'::jsonb", "'a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11'::uuid", "'10.0.0.1/24'::inet",
    "'12.34'::money", "ARRAY[1,2,3]", "ARRAY['a','b']", "NULL::int", "NULL::text", "'1.5'::real",
]
MYSQL = [
    "CAST(5 AS SIGNED)", "-5", "CAST(18446744073709551615 AS UNSIGNED)", "TRUE", "CAST(0.1 AS FLOAT)",
    "CAST(0.1 AS DOUBLE)", "1e300", "-0.0e0", "CAST(5.0 AS DECIMAL(10,1))", "CAST(0.0000001 AS DECIMAL(10,7))",
    "12345678901234567890.123", "'hello'", "'é😀汉'", "'q\"\\\\\\n\\t'", "''", "CAST('a' AS CHAR(5))",
    "CONVERT('ß' USING latin1)", "BINARY 'abc'", "X'FF00FE80'", "DATE '2024-02-29'", "TIMESTAMP '2024-01-02 03:04:05'",
    "CAST('2024-01-02 03:04:05.678' AS DATETIME(3))", "TIME '12:34:56'", "TIME '-01:00:00'", "TIME '76:00:00'",
    "CAST('12:34:56.5' AS TIME(1))", "CAST('{\"a\": 1, \"b\": [1.5]}' AS JSON)", "JSON_OBJECT('k', 'é')",
    "NULL", "CAST(NULL AS SIGNED)", "1.5", "CAST(2024 AS YEAR)",
]
MASKED = {  # value -> each action applied to it
    'postgres': ["'jane@example.com'::text", "'@example.com'::text", "'x'::text", "''::text", "'abcd'::text",
                 "'é😀@x'::text", "5::int4", "true", "'\\x41'::bytea", "'2024-01-01'::date", "NULL::text",
                 "'a@b@c'::text"],
    'mysql': ["'jane@example.com'", "'x'", "''", "5", "BINARY 'ab'", "NULL"],
}
SHAPES = {  # several columns: names repeated, as ResultSetDTO._unique() names them
    'postgres': ["1 AS a, 2 AS a, 3 AS a_2", "1 AS \"x y\", 'é' AS \"é\"", "1 AS a, 2 AS b"],
    'mysql': ["1 AS a, 2 AS a, 3 AS a_2", "1 AS `x y`"],
}


def python_body(dialect, settings, sql, actions):
    """Python's response body for `sql`, or None when Python fails on it."""
    try:
        columns, rows = runners.RUNNERS[dialect](settings, sql, {}, 100, 0, True, 30.0)
        if actions:
            columns, rows = masking.apply(list(columns), list(rows), actions)
        result = formats.ResultSetDTO(rows, columns)
        with APP.app_context():
            response = jsonify({'message': 'No results returned'}) if not result else result.to_json()
        return response.get_data(as_text=True)
    except Exception:
        return None


def rust_rows(dialect, settings, sqls):
    body = {'dialect': dialect, 'details': json.dumps(settings), 'requests': [
        {'statement': dataplane.statement(sql, dialect), 'values': {}, 'limit': 100, 'offset': 0, 'timeout': 30.0,
         'search_path': None} for sql in sqls]}
    probe = subprocess.run([str(binary()), '--probe-query'], input=json.dumps(body), capture_output=True, text=True,
                           timeout=120, check=True)
    return json.loads(probe.stdout)


APP = Flask('render-vectors')
APP.json = JSONProvider(APP)


def main():
    vectors = []
    for dialect, env, expressions in (('postgres', 'QUERYAPIGATE_DATAPLANE_TEST_POSTGRES', POSTGRES),
                                      ('mysql', 'QUERYAPIGATE_DATAPLANE_TEST_MYSQL', MYSQL)):
        settings = details(env)
        cases = [(f'SELECT {e} AS v', None) for e in expressions]
        cases += [(f'SELECT {e} AS v', [action]) for e in MASKED[dialect] for action in masking.ACTIONS]
        cases += [(f'SELECT {s}', None) for s in SHAPES[dialect]]
        cases += [(f'SELECT {MASKED[dialect][0]} AS e, 7 AS n', ['drop', None])]
        results = rust_rows(dialect, settings, [sql for sql, _ in cases])
        for (sql, actions), rust in zip(cases, results, strict=True):
            vectors.append({'dialect': dialect, 'sql': sql, 'actions': actions, 'columns': rust.get('columns'),
                            'hex': rust.get('hex'), 'rust_error': rust.get('error'),
                            'expected': python_body(dialect, settings, sql, actions)})
    path = Path(__file__).parent / 'render_vectors.jsonl'
    path.write_text(''.join(json.dumps(v, ensure_ascii=False, separators=(',', ':')) + '\n' for v in vectors))
    failed = sum(v['expected'] is None for v in vectors)
    print(f'{len(vectors)} cases ({failed} that Python fails on) -> {path}')


if __name__ == '__main__':
    main()
