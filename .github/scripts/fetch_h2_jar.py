#!/usr/bin/env python3
"""Fetch the H2 driver the wheel bundles (queryapigate/lib/), checked against its pinned SHA-256.

It isn't committed (OpenSSF Scorecard: Binary-Artifacts) - so run this before building the wheel or the sdist, or to
use `h2` connections from a source checkout:

    python .github/scripts/fetch_h2_jar.py

The Dockerfile runs it too, with `--dest`. Nothing is written unless a download's hash is the one below; a jar already
there with that hash is left alone. Because the hash decides, any mirror of Maven Central is as good as Maven Central
itself: they are tried in turn, each a few times - repo1.maven.org has twice answered GitHub's runners with something
else (2026-10-15), and a build shouldn't depend on one CDN's mood.
"""
import argparse
import hashlib
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

VERSION = '2.2.224'
SHA256 = 'b9d8f19358ada82a4f6eb5b174c6cfe320a375b5a9cb5a4fe456d623e6e55497'
PATH = f'com/h2database/h2/{VERSION}/h2-{VERSION}.jar'
MIRRORS = ('https://repo1.maven.org/maven2/', 'https://repo.maven.apache.org/maven2/',
           'https://maven-central.storage-download.googleapis.com/maven2/')
URL = MIRRORS[0] + PATH
NAME = f'h2-{VERSION}.jar'
# This repository's queryapigate/lib/ - the default when run from a checkout; copied elsewhere (the Dockerfile), the
# script is told where with --dest
_HERE = Path(__file__).resolve()
TARGET = (_HERE.parents[2] if len(_HERE.parents) > 2 else _HERE.parent) / 'queryapigate' / 'lib' / NAME
ATTEMPTS = 3


def _download(url):
    """(bytes, what the server said) - the second only for the message when the bytes are wrong."""
    request = urllib.request.Request(url, headers={'User-Agent': 'queryapigate-build (fetch_h2_jar.py)'})
    with urllib.request.urlopen(request, timeout=60) as response:
        data = response.read()
        return data, f'HTTP {response.status}, {response.headers.get("Content-Type")}, {len(data):,} bytes'


def fetch(target=TARGET):
    """Write the driver to ``target``; returns 0, or 1 when no mirror gave the right bytes."""
    if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() == SHA256:
        print(f'{target.name} is already here')
        return 0
    for mirror in MIRRORS:
        url = mirror + PATH
        for attempt in range(1, ATTEMPTS + 1):
            try:
                data, said = _download(url)
            except (urllib.error.URLError, OSError) as error:
                print(f'{url} (attempt {attempt}): {getattr(error, "reason", error)}', file=sys.stderr)
            else:
                digest = hashlib.sha256(data).hexdigest()
                if digest == SHA256:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    partial = target.with_suffix('.part')
                    partial.write_bytes(data)
                    partial.chmod(0o644)  # readable by an image's own user, whoever writes it
                    partial.replace(target)
                    print(f'Fetched {target.name} from {mirror} ({len(data):,} bytes, SHA-256 checked)')
                    return 0
                print(f'{url} (attempt {attempt}): SHA-256 {digest}, not {SHA256} - {said}; starts '
                      f'{data[:80]!r} - not written', file=sys.stderr)
            time.sleep(2 * attempt)
    print(f'No mirror gave {NAME} with SHA-256 {SHA256}', file=sys.stderr)
    return 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n', 1)[0])
    parser.add_argument('--dest', type=Path, help=f'the folder to write {NAME} to (default: queryapigate/lib/)')
    args = parser.parse_args(argv)
    return fetch(args.dest / NAME if args.dest else TARGET)


if __name__ == '__main__':
    sys.exit(main())
