# Fuzz targets

[Atheris](https://github.com/google/atheris) targets for the code that guards access - each asserts a property, not
only "doesn't crash". ClusterFuzzLite builds and runs them on every pull request and push to `main`
(`.clusterfuzzlite/`, `.github/workflows/cflite.yml`); `tests/test_sql_guard_fuzz.py` property-tests the same
guards with Hypothesis in the ordinary suite.

| Target | Property |
|---|---|
| `fuzz_sql_guard.py` | `sqltools.validate_sql` answers any text with the statement or an `ApiError` - nothing else escapes |
| `fuzz_tableguard.py` | `tableguard.extract_tables` gives a set of names or refuses with an `ApiError` |
| `fuzz_mongo.py` | once a filter, projection, sort and parameters are all accepted, no server-side JavaScript operator is anywhere in what would be sent, and no parameter became a document |
| `fuzz_params.py` | `params.resolve` binds or refuses with an `ApiError`; a declared parameter is never bound to a document |

Run one locally (`pip install atheris`, on a Python it has wheels for):

    python fuzz/fuzz_mongo.py -max_total_time=60
