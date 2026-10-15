"""validate_sql answers any text with the statement or an ApiError - nothing else escapes (see fuzz/README.md)."""
import sys

import atheris

with atheris.instrument_imports():
    from queryapigate import sqltools
    from queryapigate.errors import ApiError

DIALECTS = (None, 'postgres', 'mysql', 'sqlite', 'duckdb', 'clickhouse', 'h2', 'jdbc')


def TestOneInput(data):
    fdp = atheris.FuzzedDataProvider(data)
    dialect = DIALECTS[fdp.ConsumeIntInRange(0, len(DIALECTS) - 1)]
    allow_writes = fdp.ConsumeBool()
    sql = fdp.ConsumeUnicodeNoSurrogates(4096)
    try:
        result = sqltools.validate_sql(sql, dialect=dialect, allow_writes=allow_writes)
    except ApiError:
        return
    assert isinstance(result, str), result


if __name__ == '__main__':
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()
