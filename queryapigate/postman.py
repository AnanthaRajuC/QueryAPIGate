"""Export a collection as a Postman Collection (v2.1) that Postman can import: one ``GET /q/<name>`` request per
saved query, with its parameters filled in and documented. A snapshot of the latest version of each query - see
the docs for why it is a file and not a live link.

The file never contains a credential: authentication is a collection-level ``X-API-Key`` header taken from the
``{{apiKey}}`` variable, which is left empty for the person importing it to fill in.

Example values are derived from each parameter's own rules so that a request works as generated; a test runs
every one of them back through the same validation the server applies. The one case with no safe example is a
``pattern`` (no way to synthesise a match), which is left empty and flagged in the parameter's description.
"""
from urllib.parse import quote

from . import definitions, store
from .errors import ApiError

SCHEMA = 'https://schema.getpostman.com/json/collection/v2.1.0/collection.json'
DEFAULT_BASE_URL = 'http://127.0.0.1:5000'


def example_value(spec):
    """A value valid under ``spec`` (a params.read_definition() result), as the text a query string carries, or
    '' when none can be generated (a ``pattern``)."""
    if spec['has_default']:
        value = spec['default']
    elif spec['enum']:
        value = spec['enum'][0]
    elif spec['type'] in ('integer', 'number'):
        low, high = spec['min'], spec['max']
        value = low if low is not None else (1 if high is None or high >= 1 else high)
        if spec['type'] == 'integer':
            value = int(value) if float(value).is_integer() else int(value) + 1
    elif spec['type'] == 'boolean':
        value = True
    elif spec['pattern']:
        return ''
    else:
        value = 'x' * max(1, spec['min_length'] or 0)
    if isinstance(value, bool):
        return 'true' if value else 'false'
    return '' if value is None else str(value)


def _rules_text(spec):
    rules = []
    if spec['type']:
        rules.append(spec['type'])
    if spec['enum']:
        rules.append('one of ' + ', '.join(map(str, spec['enum'])))
    for key in ('min', 'max', 'min_length', 'max_length'):
        if spec[key] is not None:
            rules.append(f'{key} {spec[key]}')
    if spec['pattern']:
        rules.append(f"must match /{spec['pattern']}/ - fill this in")
    return ', '.join(rules)


def _parameter_entries(parameters):
    entries = []
    for name, spec in parameters.items():
        notes = [spec['description']] if spec['description'] else []
        notes.append(('Required. ' if spec['required'] else 'Optional (enable to send). ') + _rules_text(spec))
        entry = {'key': name, 'value': example_value(spec), 'description': ' '.join(notes).strip()}
        if not spec['required']:
            entry['disabled'] = True
        entries.append(entry)
    return entries


def _request(name, version, data):
    query = _parameter_entries(definitions.effective_parameters(data))
    if not data.get('connection_name'):
        query.append({'key': 'connection_name', 'value': '',
                      'description': 'Required: this query has no default connection.'})
    query += [
        {'key': 'format', 'value': 'json', 'disabled': True,
         'description': 'json (default), csv, tsv, ndjson, ...'},
        {'key': 'page', 'value': '1', 'disabled': True, 'description': 'Page number (1-based).'},
        {'key': 'page_size', 'value': '10', 'disabled': True, 'description': 'Rows per page.'},
    ]
    encoded = quote(name, safe='')
    active = '&'.join(f"{q['key']}={quote(q['value'], safe='')}" for q in query if not q.get('disabled'))
    description = [data.get('description') or f'Run the saved query {name}',
                   f"Runs version {version} of `{name}`."]
    return {'name': name, 'request': {
        'method': 'GET', 'header': [], 'description': '\n\n'.join(description),
        'url': {'raw': '{{baseUrl}}/q/' + encoded + ('?' + active if active else ''),
                'host': ['{{baseUrl}}'], 'path': ['q', encoded], 'query': query}}}


def build_collection(collection, base_url=DEFAULT_BASE_URL):
    store.validate_collection_name(collection)
    members = sorted((n, v, d) for n, v, d, c in store.live_versions()
                     if c == collection and isinstance(d.get('sql_query'), str))
    if not members:
        raise ApiError(f"Collection '{collection}' not found or empty", 404)
    return {
        'info': {'name': collection, 'schema': SCHEMA,
                 'description': f"The '{collection}' collection, exported from QueryAPIGate. A snapshot of the "
                                'latest version of each query - export again after queries change. Set the '
                                'apiKey variable to a key that may run them.'},
        'auth': {'type': 'apikey', 'apikey': [{'key': 'key', 'value': 'X-API-Key', 'type': 'string'},
                                              {'key': 'value', 'value': '{{apiKey}}', 'type': 'string'},
                                              {'key': 'in', 'value': 'header', 'type': 'string'}]},
        'variable': [{'key': 'baseUrl', 'value': base_url.rstrip('/')}, {'key': 'apiKey', 'value': ''}],
        'item': [_request(name, version, data) for name, version, data in members],
    }
