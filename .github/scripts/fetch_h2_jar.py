#!/usr/bin/env python3
"""Fetch the H2 driver the wheel bundles (queryapigate/lib/), from Maven Central, checked against its pinned SHA-256.

It isn't committed (OpenSSF Scorecard: Binary-Artifacts) - so run this before building the wheel or the sdist, or to
use `h2` connections from a source checkout:

    python .github/scripts/fetch_h2_jar.py

Nothing is written unless the download's hash is the one below; a jar already there with that hash is left alone.
The Dockerfile fetches the same file with `ADD --checksum` - tests/test_supply_chain.py keeps the two in step.
"""
import hashlib
import sys
import urllib.request
from pathlib import Path

VERSION = '2.2.224'
SHA256 = 'b9d8f19358ada82a4f6eb5b174c6cfe320a375b5a9cb5a4fe456d623e6e55497'
URL = f'https://repo1.maven.org/maven2/com/h2database/h2/{VERSION}/h2-{VERSION}.jar'
TARGET = Path(__file__).resolve().parents[2] / 'queryapigate' / 'lib' / f'h2-{VERSION}.jar'


def main():
    if TARGET.exists() and hashlib.sha256(TARGET.read_bytes()).hexdigest() == SHA256:
        print(f'{TARGET.name} is already here')
        return 0
    with urllib.request.urlopen(URL, timeout=60) as response:
        data = response.read()
    digest = hashlib.sha256(data).hexdigest()
    if digest != SHA256:
        print(f'{URL} has SHA-256 {digest}, not {SHA256} - not written', file=sys.stderr)
        return 1
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    partial = TARGET.with_suffix('.part')
    partial.write_bytes(data)
    partial.replace(TARGET)
    print(f'Fetched {TARGET.name} ({len(data):,} bytes, SHA-256 checked)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
