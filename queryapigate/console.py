"""Serves the QueryAPIGate Console - the React/TypeScript frontend in frontend/ (ADR 0001, BACKLOG #73) - at /console.

The Console is a static single-page app: `npm run build` in frontend/ writes it to queryapigate/console_dist/, which
release builds (the wheel and the Docker image) include, so users never need Node.js. A source checkout that hasn't
built it gets a short "not built" page instead of an error - backend work never requires Node either.

Like /ui, the page itself is public and holds no data: everything it shows comes from the JSON API, called with the
API key the user enters (kept in sessionStorage under the same name /ui and /docs use).
"""
from pathlib import Path

from flask import Response, send_from_directory

DIST = Path(__file__).parent / 'console_dist'

# No inline scripts and no third-party scripts: everything is bundled. 'unsafe-inline' for styles only (CodeMirror
# injects its own, and a few classic components size themselves with style attributes); the one third-party origin
# is img.shields.io, for the sidebar's GitHub-stars badge - an image, as on /ui.
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
       # any https image: Help > Docs renders the project's markdown, badges and screenshots included
       "img-src 'self' data: https:; "
       # Help > Docs reads this project's own markdown from GitHub, at the running version's tag
       "font-src 'self'; connect-src 'self' https://raw.githubusercontent.com; object-src 'none'; base-uri 'self'; "
       "frame-ancestors 'none'; form-action 'self'")

NOT_BUILT_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>QueryAPIGate Console - not built</title>
<style>
  :root { color-scheme: light dark; }
  body { font: 15px/1.6 system-ui, sans-serif; max-width: 40rem; margin: 4rem auto; padding: 0 1rem;
         background: light-dark(#f5f5f3, #131416); color: light-dark(#1b1c1f, #e7e7e4); }
  code { background: light-dark(#e8e8e4, #2a2b30); padding: .1rem .3rem; border-radius: 4px; }
</style></head><body>
<h1>The Console isn't built in this installation</h1>
<p>This is a source checkout without the Console's compiled assets. Release builds (<code>pip install
queryapigate</code> and the Docker image) include them.</p>
<p>To build it: <code>cd frontend &amp;&amp; npm ci &amp;&amp; npm run build</code>, then reload this page.</p>
<p>The classic admin UI is still available at <a href="/ui">/ui</a>.</p>
</body></html>"""


def built():
    return (DIST / 'index.html').is_file()


def _secure(response):
    response.headers['Content-Security-Policy'] = CSP
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'same-origin'
    return response


def serve(path=''):
    """`path` is whatever follows /console/. A real file (a hashed asset under assets/, or e.g. favicon.svg) is
    served as is; anything else is a client-side route, answered with index.html so a reload or a deep link
    works. A missing file that looks like an asset (it has an extension) is a 404, not index.html, so a broken
    asset reference fails visibly instead of loading HTML as a script."""
    if not built():
        return _secure(Response(NOT_BUILT_HTML, mimetype='text/html'))
    if path and (DIST / path).is_file():
        response = send_from_directory(DIST, path, max_age=0)
        if path.startswith('assets/'):  # content-hashed file names: safe to cache forever
            response.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
        return _secure(response)
    if path and '.' in path.rsplit('/', 1)[-1]:
        return _secure(Response('Not found', status=404, mimetype='text/plain'))
    response = send_from_directory(DIST, 'index.html', max_age=0)
    response.headers['Cache-Control'] = 'no-cache'  # always revalidate, so a new release's assets are picked up
    return _secure(response)
