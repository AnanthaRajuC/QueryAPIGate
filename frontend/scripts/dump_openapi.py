#!/usr/bin/env python3
"""Regenerate frontend/openapi.json from the backend's OpenAPI description - run it after changing
queryapigate/openapi.py. Needs only Python and the backend installed (pip install -e .), not Node.js.

    python frontend/scripts/dump_openapi.py

The Console's TypeScript types are generated from this file at build time, and a backend test fails while it is
out of date (tests/test_collections.py, RouteDocumentationTests)."""
from pathlib import Path

from queryapigate.openapi import dump

target = Path(__file__).resolve().parent.parent / 'openapi.json'
target.write_text(dump())
print(f'Wrote {target}')
