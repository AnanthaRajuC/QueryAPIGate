#!/bin/bash -eu
# Build each fuzz target (fuzz/fuzz_*.py) with the package and sqlglot, which table checks need.
pip3 install ".[flow]"
for fuzzer in fuzz/fuzz_*.py; do
  compile_python_fuzzer "$fuzzer"
done
