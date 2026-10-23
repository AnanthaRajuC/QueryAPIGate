"""extract_tables gives a frozenset of names or refuses with an ApiError (see fuzz/README.md) - a query whose tables
can't be worked out must be refused, never let through."""
import sys

import atheris

with atheris.instrument_imports():
    from queryapigate import tableguard
    from queryapigate.errors import ApiError

DIALECTS = ('postgres', 'mysql', 'sqlite', 'duckdb', 'clickhouse')


def TestOneInput(data):
    fdp = atheris.FuzzedDataProvider(data)
    dialect = DIALECTS[fdp.ConsumeIntInRange(0, len(DIALECTS) - 1)]
    sql = fdp.ConsumeUnicodeNoSurrogates(2048)
    try:
        tables = tableguard.extract_tables(sql, dialect)
    except ApiError:
        return
    assert isinstance(tables, frozenset) and all(isinstance(t, str) and t == t.lower() for t in tables), tables


if __name__ == '__main__':
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()
