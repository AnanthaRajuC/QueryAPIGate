"""Configuration as code (ADR 0014, BACKLOG #97): one file describing what a server holds - connections, destinations,
roles, saved queries, API keys' grants, descriptions and exports - read, checked and compared with the store here,
and the store written out as one.

The file's objects take the Management API's own fields (`POST /api/v1/connections` and so on), checked by the
functions the API uses, so a file can't declare what the API would refuse. Two differences, both because a file is
a declaration rather than a request:

- **Nothing in it is a secret.** A connection's or destination's `password` must be a `${VAR}` reference, expanded by
  the server where it runs, as today. An API key carries `secret_sha256`, the hash the store keeps, never the secret.
- **A key's `role` stays live.** Through the API a role is a template a key copies once; in a file, `role: partner`
  means "this key's grants are `partner`'s", so changing the role changes its keys at the next apply.

Here: `load()` reads a file, `desired()` checks it and normalises every object, `current()` reads the store in the
same shape, `diff()` compares the two and `export()` writes the store out. Nothing in this module writes the store.

Checking happens in two layers. Everything that depends only on the file - each field, each secret, each reference
to something that must exist (in the file or in the store) - is checked by `desired()`, and every problem is reported
at once. What also depends on the store as it will be - an export against the published query it names - is checked
again when it's applied, by the same validation the API runs.
"""
import difflib
import hashlib
import json
import re

import yaml

from . import apikeys, db, definitions, descriptions, destinations, exports, store
from .errors import ApiError
from .services import access
from .services import queries as query_service

FORMAT = 'queryapigate-config'
FORMAT_VERSION = 1
# Every kind, in the order apply writes them: each only refers to kinds before it.
KINDS = ('connections', 'destinations', 'queries', 'roles', 'api_keys', 'descriptions', 'exports')
SECRET = '<stored secret>'  # a password the store holds as a value (literal or encrypted): compared, never shown
CHANGED = '<changed>'  # how a diff shows a key's secret hash changing
MAX_PROBLEMS = 50
_FAILED = object()  # what _Problems.check() returns for a check that raised

_ENV_REF_RE = re.compile(r'^\$\{\w+\}$')
_HASH_RE = re.compile(r'^[0-9a-f]{64}$')
_VAR_UNSAFE_RE = re.compile(r'[^A-Z0-9]+')
# The fields an object of each kind may carry in a file - the API's create fields, less what a file can't hold.
_QUERY_FIELDS = frozenset({'name', 'description', 'author', 'tags', 'query_type', 'sql', 'connection_name',
                           'parameters', 'cache_ttl', 'columns', 'agent_writes', 'max_affected_rows', 'collection',
                           'mongo_collection', 'mongo_filter', 'mongo_projection', 'mongo_sort'})
# What a saved query's definition is compared on - its v1 shape (query_service.to_version()), less what isn't its
# definition (version, status, timestamps) and `author`, compared only when the file gives one.
_QUERY_COMPARED = ('description', 'tags', 'query_type', 'sql', 'mongo', 'connection_name', 'parameters', 'cache_ttl',
                   'columns', 'agent_writes', 'max_affected_rows')
_KEY_FIELDS = frozenset({'name', 'role', 'active', 'expires_at', 'secret_sha256', *access.GRANT_FIELDS})
_ROLE_FIELDS = frozenset({'name', *access.GRANT_FIELDS})


class ConfigInvalid(ApiError):
    """A file that can't be applied, with every problem found: each names the object and the field."""

    def __init__(self, problems):
        shown = '; '.join(problems[:3]) + (f' (and {len(problems) - 3} more)' if len(problems) > 3 else '')
        super().__init__(f'The configuration file has {len(problems)} problem(s): {shown}', code='invalid_config',
                         problems=problems)
        self.problems = problems


def _env_ref(value):
    return isinstance(value, str) and bool(_ENV_REF_RE.fullmatch(value))


# --------------------------------------------------------------------------------------
# Reading a file
# --------------------------------------------------------------------------------------

def load(text):
    """A file's text (YAML, or JSON - which YAML reads too) as the decoded document, its format checked."""
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise ConfigInvalid([f'Not valid YAML or JSON: {error}']) from None
    if not isinstance(doc, dict) or doc.get('format') != FORMAT:
        raise ConfigInvalid([f"Not a QueryAPIGate configuration file (expected format: {FORMAT})"])
    if doc.get('format_version') != FORMAT_VERSION:
        raise ConfigInvalid([f"Unsupported format_version {doc.get('format_version')!r} - this version reads "
                             f'{FORMAT_VERSION}'])
    unknown = sorted(set(doc) - {'format', 'format_version', *KINDS})
    if unknown:
        raise ConfigInvalid([f"Unknown section(s): {', '.join(map(str, unknown))} (known: {', '.join(KINDS)})"])
    return doc


class _Problems:
    """Collects every problem in a file, each with where it is, so one run reports all of them."""

    def __init__(self):
        self.items = []

    def add(self, where, message):
        if len(self.items) < MAX_PROBLEMS:
            self.items.append(f'{where}: {message}')

    def check(self, where, check, *args, **kwargs):
        """check(*args, **kwargs), recording an ApiError it raises; its result, or _FAILED after a problem."""
        try:
            return check(*args, **kwargs)
        except ApiError as error:
            self.add(where, error.message)
            return _FAILED


def _objects(doc, kind, problems):
    """{name: object} for one list section, each a mapping with a name, none twice."""
    found = {}
    entries = doc[kind]
    if not isinstance(entries, list):
        problems.add(kind, 'must be a list of objects, each with a name')
        return found
    for position, entry in enumerate(entries, 1):
        name = entry.get('name') if isinstance(entry, dict) else None
        if not isinstance(name, str) or not name:
            problems.add(f'{kind}[{position}]', 'must be an object with a name')
        elif name in found:
            problems.add(f'{kind}[{name}]', 'is listed twice')
        else:
            found[name] = entry
    return found


def _refuse_unknown(where, entry, allowed, problems):
    unknown = sorted(set(entry) - allowed)
    if unknown:
        problems.add(where, f"unknown field(s): {', '.join(unknown)}")
    return not unknown


def _check_password(where, value, problems):
    """A secret in a file is a ${VAR} reference, never a value."""
    if value in (None, ''):
        return
    if not _env_ref(value):
        problems.add(where, 'password must be a ${VAR} reference, set where the server runs - a configuration file '
                            'never holds a secret')
        return
    problems.check(where, store.refuse_server_settings, {'password': value})


# --------------------------------------------------------------------------------------
# Normalised objects - the same shape from a file and from the store, so they compare
# --------------------------------------------------------------------------------------

def _connection_from_file(where, entry, problems):
    from .services import connections
    name = entry['name']
    if not connections._NAME_RE.match(name):
        problems.add(where, "a connection name is 1-100 letters, digits, spaces, '.', '_' or '-'")
        return None
    fields = problems.check(where, connections._fields, entry)  # options may be given flat, or under `options`
    if fields is _FAILED:
        return None
    if 'db' not in fields:
        problems.add(where, 'db is required')
        return None
    _check_password(where, fields.get('password'), problems)
    fields.setdefault('active', True)
    problems.check(where, store.refuse_server_settings, fields)
    problems.check(where, store.refuse_store_access, fields)
    return {k: v for k, v in fields.items() if v is not None}


def _connection_from_store(details):
    out = {k: v for k, v in details.items() if k not in ('created_at', 'updated_at', 'example')}
    if out.get('password') not in (None, '') and not _env_ref(out['password']):
        out['password'] = SECRET
    return out


def _destination_from_file(where, entry, problems):
    if problems.check(where, destinations._check_name, entry['name']) is _FAILED:
        return None
    fields = problems.check(where, destinations.validate, {k: v for k, v in entry.items() if k != 'name'})
    if fields is _FAILED:
        return None
    _check_password(where, fields.get('password'), problems)
    return fields


def _destination_from_store(destination):
    out = {k: destination[k] for k in destinations.FIELDS if k in destination}
    if out.get('password') not in (None, '') and not _env_ref(out['password']):
        out['password'] = SECRET
    return out


def _grants_from_file(where, entry, problems):
    fields = {field: entry.get(field) for field in access.GRANT_FIELDS}
    grants = problems.check(where, apikeys.normalize_grants, **fields)
    return None if grants is _FAILED else access._grants(grants)


def _role_from_file(where, entry, problems):
    if not _refuse_unknown(where, entry, _ROLE_FIELDS, problems):
        return None
    if not apikeys._NAME_RE.match(entry['name']):
        problems.add(where, "a role name is 1-100 letters, digits, spaces, '.', '_' or '-'")
        return None
    return _grants_from_file(where, entry, problems)


def _key_from_file(where, entry, problems):
    """A key as {'grants', 'role', 'active', 'expires_at', 'secret_sha256'}; its grants resolved from its role later
    (_resolve_roles()), when every role is known."""
    if not _refuse_unknown(where, entry, _KEY_FIELDS, problems):
        return None
    if not apikeys._NAME_RE.match(entry['name']):
        problems.add(where, "a key name is 1-100 letters, digits, spaces, '.', '_' or '-'")
        return None
    secret_hash = entry.get('secret_sha256')
    if secret_hash is not None and (not isinstance(secret_hash, str) or not _HASH_RE.match(secret_hash)):
        problems.add(where, 'secret_sha256 must be the SHA-256 of the secret, as 64 lowercase hex digits - '
                            '`queryapigate config new-key` makes a secret and prints both')
    active = entry.get('active', True)
    if not isinstance(active, bool):
        problems.add(where, 'active must be true or false')
    expires_at = problems.check(where, apikeys._validate_expiry, entry.get('expires_at'))
    expires_at = None if expires_at is _FAILED else expires_at
    role = entry.get('role')
    if role is not None:
        given = sorted(field for field in access.GRANT_FIELDS if field in entry)
        if not isinstance(role, str):
            problems.add(where, 'role must be the name of a role')
        elif given:
            problems.add(where, f"a key with a role takes its grants from it - leave out {', '.join(given)}, or the "
                                'role')
        grants = None
    else:
        grants = _grants_from_file(where, entry, problems)
    return {'grants': grants, 'role': role, 'active': active, 'expires_at': expires_at,
            'secret_sha256': secret_hash}


def _key_from_store(entry, secret_hash):
    return {'grants': access._grants(entry), 'role': entry.get('created_from_role'),
            'active': entry.get('active', True) is not False, 'expires_at': entry.get('expires_at'),
            'secret_sha256': secret_hash}


def _query_shape(fields, collection):
    """A saved query's definition as compared and shown: its v1 fields, and its collection."""
    version = query_service.to_version({}, 0, fields)
    shape = {field: version[field] for field in _QUERY_COMPARED}
    shape['author'] = fields.get('author')
    shape['collection'] = collection
    return shape


def _query_from_file(where, entry, problems):
    if not _refuse_unknown(where, entry, _QUERY_FIELDS, problems):
        return None
    name = entry['name']
    if problems.check(where, store.saved_path_for_name, name) is _FAILED:
        return None
    body = problems.check(where, query_service._legacy_body, {**entry, 'author': entry.get('author') or 'config'},
                          name, 'config')
    if body is _FAILED:
        return None
    validated = problems.check(where, definitions.validate_definition, body)
    if validated is _FAILED:
        return None
    fields, collection = validated
    shape = _query_shape(fields, None if collection is definitions.NO_COLLECTION_GIVEN else collection)
    if 'author' not in entry:
        shape['author'] = None  # not compared: an apply records 'config' as the author of what it writes
    return shape


def _query_from_store(content):
    """The published definition, or None for a query with only drafts; and whether there's a newer draft."""
    published = store.read_published(content)
    numbers = store.version_numbers(content)
    latest = max(numbers) if numbers else None
    if published is None:
        return None, latest
    return (_query_shape(content[str(published)], store.read_collection(content)),
            latest if latest is not None and latest > published else None)


def _descriptions_from_file(where, items, problems):
    if not isinstance(items, list):
        problems.add(where, 'must be a list of {table, column, description}')
        return None
    validated = problems.check(where, descriptions.validate_items, items)
    if validated is _FAILED:
        return None
    return {(table, column): text for table, column, text in validated}


def _descriptions_from_store(connection_name):
    return {(item['table'] or '', item['column'] or ''): item['description']
            for item in descriptions.list_for(connection_name)}


def _export_from_file(where, entry, problems):
    from .exports import _NAME_RE
    if not _NAME_RE.match(entry['name']):
        problems.add(where, "an export's name is 1-100 letters, digits, '.', '_' or '-'")
        return None
    fields = problems.check(where, exports.check_fields, {k: v for k, v in entry.items() if k != 'name'})
    return None if fields is _FAILED else fields


def _export_from_store(export):
    return {k: export[k] for k in exports.FIELDS if k in export}


# --------------------------------------------------------------------------------------
# The file, checked; the store, read
# --------------------------------------------------------------------------------------

def desired(doc):
    """{kind: {name: normalised object}} for every kind the file has; ConfigInvalid listing every problem.

    `descriptions` is keyed by connection name, each a {(table, column): description} map."""
    problems = _Problems()
    out = {}
    readers = {'connections': _connection_from_file, 'destinations': _destination_from_file,
               'roles': _role_from_file, 'queries': _query_from_file, 'api_keys': _key_from_file,
               'exports': _export_from_file}
    for kind in KINDS:
        if kind not in doc:
            continue
        if kind == 'descriptions':
            section = doc[kind]
            if not isinstance(section, dict):
                problems.add(kind, 'must map each connection name to a list of {table, column, description}')
                continue
            out[kind] = {name: _descriptions_from_file(f'descriptions[{name}]', items, problems)
                         for name, items in section.items()}
            continue
        out[kind] = {name: readers[kind](f'{kind}[{name}]', entry, problems)
                     for name, entry in _objects(doc, kind, problems).items()}
    if not problems.items:
        _check_references(out, problems)
    if problems.items:
        raise ConfigInvalid(problems.items)
    _resolve_roles(out)
    return out


def _check_references(wanted, problems):
    """Everything an object names must exist: in the file, or in the store already."""
    def names(kind, stored):
        return set(wanted.get(kind, {})) | set(stored)

    connections = names('connections', store.read_connections())
    roles = names('roles', apikeys.list_roles())
    query_names = names('queries', (row[0] for row in store.latest_versions()))
    destination_names = names('destinations', (d['name'] for d in destinations.list_all()))
    collections = set(store.collection_members())
    collections |= {q['collection'] for q in wanted.get('queries', {}).values() if q and q['collection']}
    for name, query in wanted.get('queries', {}).items():
        if query['connection_name'] and query['connection_name'] not in connections:
            problems.add(f'queries[{name}]', f"connection_name '{query['connection_name']}' is no connection, in the "
                                             'file or on the server')
    for kind in ('roles', 'api_keys'):
        for name, entry in wanted.get(kind, {}).items():
            grants = entry['grants'] if kind == 'api_keys' else entry
            if kind == 'api_keys' and entry['role'] is not None and entry['role'] not in roles:
                problems.add(f'{kind}[{name}]', f"role '{entry['role']}' is no role, in the file or on the server")
            for collection in (grants or {}).get('collections', []):
                if collection not in collections:
                    problems.add(f'{kind}[{name}]', f"collection '{collection}' has no queries, in the file or on the "
                                                    'server')
    stored_keys = apikeys.list_keys()
    for name, entry in wanted.get('api_keys', {}).items():
        if name not in stored_keys and entry['secret_sha256'] is None:
            problems.add(f'api_keys[{name}]', 'a new key needs secret_sha256 - `queryapigate config new-key` makes a '
                                              'secret and prints its hash')
    for name in wanted.get('descriptions', {}):
        if name not in connections:
            problems.add(f'descriptions[{name}]', 'is no connection, in the file or on the server')
    for name, export in wanted.get('exports', {}).items():
        if export['query'] not in query_names:
            problems.add(f'exports[{name}]', f"query '{export['query']}' is no saved query, in the file or on the "
                                             'server')
        if export['destination'] not in destination_names:
            problems.add(f'exports[{name}]', f"destination '{export['destination']}' is no destination, in the file or "
                                             'on the server')


def _resolve_roles(wanted):
    """A key with a role has that role's grants: as the file declares the role, or else as the store holds it."""
    stored = None
    for entry in wanted.get('api_keys', {}).values():
        if entry['role'] is None:
            continue
        role = wanted.get('roles', {}).get(entry['role'])
        if role is None:
            stored = apikeys.list_roles() if stored is None else stored
            role = access._grants(stored[entry['role']])
        entry['grants'] = role


def current(kinds=KINDS, examples=False):
    """{kind: {name: normalised object}} as the store holds them now. The bundled examples (`queryapigate examples
    load`) are left out unless `examples`. Queries carry a third element: (shape, newer draft)."""
    out = {}
    if 'connections' in kinds:
        out['connections'] = {name: _connection_from_store(details)
                              for name, details in sorted(store.read_connections().items())
                              if examples or details.get('example') is not True}
    if 'destinations' in kinds:
        out['destinations'] = {d['name']: _destination_from_store(d) for d in destinations.list_all()}
    if 'roles' in kinds:
        out['roles'] = {name: access._grants(entry) for name, entry in sorted(apikeys.list_roles().items())
                        if examples or entry.get('example') is not True}
    if 'queries' in kinds:
        out['queries'] = {}
        for name, content in store.iter_saved():
            if examples or not store.read_example(content):
                out['queries'][name] = _query_from_store(content)
    if 'api_keys' in kinds:
        hashes = apikeys.secret_hashes()
        out['api_keys'] = {name: _key_from_store(entry, hashes[name])
                           for name, entry in sorted(apikeys.list_keys().items())
                           if examples or entry.get('example') is not True}
    if 'descriptions' in kinds:
        out['descriptions'] = {name: found for name in sorted(store.read_connections())
                               if (found := _descriptions_from_store(name))}
    if 'exports' in kinds:
        out['exports'] = {e['name']: _export_from_store(e) for e in exports.list_all()}
    return out


# --------------------------------------------------------------------------------------
# What applying the file would change
# --------------------------------------------------------------------------------------

def _changes(before, after, ignore=()):
    """{field: {'from', 'to'}} for every field that differs."""
    fields = (set(before) | set(after)) - set(ignore)
    return {field: {'from': before.get(field), 'to': after.get(field)} for field in sorted(fields)
            if before.get(field) != after.get(field)}


def _key_fields(key):
    """A key flattened for comparing: its grants, `active`, `expires_at` and whether its secret changes."""
    return {**(key['grants'] or {}), 'active': key['active'], 'expires_at': key['expires_at']}


def _managed():
    """{(kind, name)} for every object a configuration file manages (ADR 0014)."""
    return {(row[0], row[1]) for row in db.connection().execute('SELECT kind, name FROM config_managed').fetchall()}


def diff(doc, prune=False):
    """What applying `doc` would do: {'changes': [...], 'not_in_file': [...]}.

    Each change is {'kind', 'name', 'action': 'create' | 'update' | 'delete', 'fields': {field: {'from', 'to'}},
    'notes'}. A saved query's update is a new version (published) when its definition changes, and a move when only
    its collection does. With `prune`, what a file manages (it created, changed or named it) and this one doesn't
    have is deleted - and a managed connection's descriptions this one doesn't list. `not_in_file` names the rest of
    what the server holds, of the kinds the file has, that the file doesn't: applying leaves it alone."""
    return _plan(desired(doc), prune)['result']


def _plan(wanted, prune):
    have = current(tuple(wanted), examples=True)  # a file may name the bundled examples too
    ours = current(tuple(wanted))  # but one that doesn't hasn't left them out: they're not listed below
    managed = _managed() if prune else set()
    changes, deletes, not_in_file = [], [], []
    for kind in KINDS:
        if kind not in wanted:
            continue
        for name, after in wanted[kind].items():
            change = _change(kind, name, have[kind].get(name), after, prune and (kind, name) in managed)
            if change is not None:
                changes.append(change)
        for name in ours[kind]:
            if name in wanted[kind]:
                continue
            if (kind, name) in managed:
                deletes.append({'kind': kind, 'name': name, 'action': 'delete', 'fields': {},
                                'notes': _delete_notes(kind, name)})
            else:
                not_in_file.append({'kind': kind, 'name': name})
    # deletes last, and in reverse: an export before the destination it writes to
    order = {kind: i for i, kind in enumerate(KINDS)}
    deletes.sort(key=lambda c: -order[c['kind']])
    return {'result': {'changes': changes + deletes, 'not_in_file': not_in_file}, 'wanted': wanted, 'have': have}


def _delete_notes(kind, name):
    if kind == 'connections':
        users = sorted(q for q, _, data, _ in store.latest_versions() if data.get('connection_name') == name)
        if users:
            return [f"saved queries still use it: {', '.join(users)} - they stop working"]
    return []


def _change(kind, name, before, after, prune_items=False):
    notes = []
    if kind == 'queries':
        stored, draft = before if before is not None else (None, None)
        if draft is not None:
            notes.append(f'has a newer draft, version {draft} - the file\'s definition is published as a version '
                         'above it')
        if before is not None and stored is None:
            return {'kind': kind, 'name': name, 'action': 'update', 'notes': notes + ['has no published version'],
                    'fields': _changes({}, after, ignore=('author',) if after['author'] is None else ())}
        before = stored
        ignore = ('author',) if after['author'] is None else ()
    elif kind == 'api_keys':
        fields = _key_fields(after)
        if before is None:
            return {'kind': kind, 'name': name, 'action': 'create', 'notes': notes,
                    'fields': {**_changes({}, fields), 'secret': {'from': None, 'to': CHANGED}}}
        found = _changes(_key_fields(before), fields)
        if after['secret_sha256'] is not None and after['secret_sha256'] != before['secret_sha256']:
            found['secret'] = {'from': CHANGED, 'to': CHANGED}
            notes.append(f'a new secret: the old one keeps working for {apikeys.DEFAULT_GRACE_MINUTES} minutes, as a '
                         'rotation does')
        return {'kind': kind, 'name': name, 'action': 'update', 'fields': found, 'notes': notes} if found else None
    else:
        ignore = ()
    if kind == 'descriptions':
        before = {_label(*k): v for k, v in (before or {}).items()}
        after = {_label(*k): v for k, v in after.items()}
        found = {label: {'from': before.get(label), 'to': text} for label, text in sorted(after.items())
                 if before.get(label) != text}
        if prune_items:  # the file lists this connection's descriptions in full: the rest go
            found.update({label: {'from': text, 'to': None} for label, text in sorted(before.items())
                          if label not in after})
        if not found:
            return None
        return {'kind': kind, 'name': name, 'action': 'update' if before else 'create', 'fields': found,
                'notes': notes}
    if before is None:
        return {'kind': kind, 'name': name, 'action': 'create', 'notes': notes,
                'fields': _changes({}, after, ignore=ignore)}
    found = _changes(before, after, ignore=ignore)
    if not found:
        return None
    if found.get('password', {}).get('from') == SECRET and _env_ref(found['password']['to']):
        notes.append(f"the password moves out of the store: set {found['password']['to'][2:-1]} where the server "
                     'runs first')
    if kind == 'queries':
        definition = sorted(set(found) - {'collection'})
        notes.insert(0, 'a new version, published' if definition else 'moved to another collection')
    return {'kind': kind, 'name': name, 'action': 'update', 'fields': found, 'notes': notes}


def _label(table, column):
    return (table or '(connection)') + (f'.{column}' if column else '')


def _unlabel(label):
    table, _, column = label.partition('.')
    return (None if table == '(connection)' else table), (column or None)


def _shown(value):
    if value is None:
        return '(none)'
    if value in (SECRET, CHANGED):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _long(value):
    return isinstance(value, str) and ('\n' in value or len(value) > 60)


def render(result):
    """The diff as a reviewer reads it: + for created, ~ for changed, - for deleted, each field's old and new value -
    long text as a line diff."""
    lines = []
    for change in result['changes']:
        mark = {'create': '+', 'update': '~', 'delete': '-'}[change['action']]
        lines.append(f"{mark} {change['kind']}[{change['name']}]")
        for note in change['notes']:
            lines.append(f'    ({note})')
        for field, values in change['fields'].items():
            old, new = values['from'], values['to']
            if change['action'] == 'create':
                if new in (None, [], {}, False, ''):
                    continue  # the default: nothing to review
                if _long(new):
                    lines.append(f'    {field}:')
                    lines += [f'      {line}' for line in new.splitlines()]
                else:
                    lines.append(f'    {field}: {_shown(new)}')
            elif _long(old) or _long(new):
                lines.append(f'    {field}:')
                for line in difflib.unified_diff((old or '').splitlines(), (new or '').splitlines(), lineterm='',
                                                 n=2):
                    if not line.startswith(('---', '+++')):
                        lines.append(f'      {line}')
            else:
                lines.append(f'    {field}: {_shown(old)} -> {_shown(new)}')
    counts = {action: sum(1 for c in result['changes'] if c['action'] == action)
              for action in ('create', 'update', 'delete')}
    if result['not_in_file']:
        lines.append('')
        lines.append('On the server but not in the file - applying leaves them alone:')
        for item in result['not_in_file']:
            lines.append(f"    {item['kind']}[{item['name']}]")
    if lines:
        lines.append('')
    if result['changes']:
        lines.append(f"{counts['create']} to create, {counts['update']} to change"
                     + (f", {counts['delete']} to delete." if counts['delete'] else '.'))
    else:
        lines.append('No changes: the server matches the file.')
    return '\n'.join(lines)


# --------------------------------------------------------------------------------------
# Applying a file
# --------------------------------------------------------------------------------------

def file_hash(doc):
    """The SHA-256 of a document, as its canonical JSON - what the audit log and the managed marks name it by."""
    canonical = json.dumps(doc, sort_keys=True, separators=(',', ':'), ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def apply(doc, prune=False, actor='cli'):
    """Make the store match `doc`, all or nothing, and return what changed (diff()'s shape) with `applied`.

    Everything happens in one transaction, holding the store's write lock: the file is checked and compared against
    the store as it is then, every change is made through the functions the API uses - each audited, `via: config`
    - and the objects the file names are marked managed. Any problem, at any step, rolls all of it back; the error
    names the object. One `config_applied` entry, by `actor`, records the file's hash and the counts. A file that
    changes nothing writes nothing."""
    digest = file_hash(doc)
    with db.transaction():
        plan = _plan(desired(doc), prune)
        result, wanted, have = plan['result'], plan['wanted'], plan['have']
        entries = {kind: _objects(doc, kind, _Problems()) for kind in wanted if kind != 'descriptions'}
        managed = _managed()
        unmarked = [(kind, name) for kind in wanted for name in wanted[kind] if (kind, name) not in managed]
        if not result['changes'] and not unmarked:
            return {**result, 'applied': False, 'file_sha256': digest}
        with store.audit_via('config'):
            for change in result['changes']:
                kind, name = change['kind'], change['name']
                try:
                    _APPLY[kind](change, entries.get(kind, {}).get(name), wanted[kind].get(name),
                                 have[kind].get(name), actor, digest)
                except ApiError as error:
                    raise ConfigInvalid([f'{kind}[{name}]: {error.message}']) from None
        conn = db.connection()
        timestamp = store.now()
        for change in result['changes']:
            if change['action'] == 'delete':
                conn.execute('DELETE FROM config_managed WHERE kind = ? AND name = ?', (change['kind'], change['name']))
        for kind in wanted:
            for name in wanted[kind]:
                conn.execute('INSERT INTO config_managed (kind, name, applied_at, file_hash) VALUES (?, ?, ?, ?) '
                             'ON CONFLICT(kind, name) DO UPDATE SET applied_at = excluded.applied_at, '
                             'file_hash = excluded.file_hash', (kind, name, timestamp, digest))
        counts = {action: sum(1 for c in result['changes'] if c['action'] == action)
                  for action in ('create', 'update', 'delete')}
        store.record_audit(actor, 'config_applied', digest[:12], {
            'file_sha256': digest, 'created': counts['create'], 'changed': counts['update'],
            'deleted': counts['delete'], 'newly_managed': len(unmarked), 'pruned': prune})
    return {**result, 'applied': True, 'file_sha256': digest}


def _cleared(after, before, keep=()):
    """An update's body: every field the file sets, and None for each it no longer has - which the API removes."""
    return {**after, **{k: None for k in (before or {}) if k not in after and k not in keep}}


def _apply_connection(change, entry, after, before, actor, digest):
    from .services import connections
    if change['action'] == 'create':
        connections.create(entry, actor)
    elif change['action'] == 'update':
        body = _cleared(after, before, keep=('db', 'active'))
        if body.get('password') == SECRET:
            del body['password']
        connections.update(change['name'], body, actor)
    else:
        connections.delete(change['name'], f'removed by configuration file {digest[:12]}', actor)


def _apply_destination(change, entry, after, before, actor, digest):
    from .services import destinations as destination_service
    if change['action'] == 'create':
        destination_service.create(entry, actor)
    elif change['action'] == 'update':
        body = _cleared(after, before, keep=('storage',))
        if body.get('password') == SECRET:
            del body['password']
        destination_service.update(change['name'], body, actor)
    else:
        destination_service.delete(change['name'], actor, used_by=exports.using_destination(change['name']))


def _apply_query(change, entry, after, before, actor, digest):
    from . import collection_admin
    name = change['name']
    if change['action'] == 'delete':
        store.delete_saved(name)
        store.record_audit(actor, 'delete_query', name, None)
        return
    body = {k: v for k, v in entry.items() if k != 'collection'}
    if change['action'] == 'create':
        query_service.create({**body, 'collection': after['collection'], 'publish': True}, actor)
        return
    if set(change['fields']) - {'collection'}:
        query_service.add_version(name, {**{k: v for k, v in body.items() if k != 'name'}, 'publish': True}, actor)
    stored = store.read_collection(store.load_versions(name, with_history=False))
    if after['collection'] != stored:
        access_change = collection_admin.access_change(stored, after['collection'])
        previous = store.set_collection(name, after['collection'])
        store.record_audit(actor, 'move_query', name, {
            'collection': {'from': previous, 'to': after['collection']},
            'keys_gaining_access': access_change['keys']['gain'], 'keys_losing_access': access_change['keys']['lose']})


def _apply_role(change, entry, after, before, actor, digest):
    if change['action'] == 'create':
        access.create_role(entry, actor)
    elif change['action'] == 'update':
        access.update_role(change['name'], after, actor)
    else:
        access.delete_role(change['name'], actor)


def _apply_key(change, entry, after, before, actor, digest):
    name = change['name']
    if change['action'] == 'delete':
        access.delete_key(name, actor)
        return
    if change['action'] == 'create':
        body = {'name': name, 'expires_at': after['expires_at']}
        body.update({'role': after['role']} if after['role'] else after['grants'])
        access.create_key(body, actor)  # with a random secret, replaced at once: it never works
        apikeys.set_secret_hash(name, after['secret_sha256'], grace_minutes=0)
        if not after['active']:
            access.update_key(name, {'active': False}, actor)
        store.record_audit(actor, 'set_key_secret', name, {'grace_minutes': 0})
        return
    fields = set(change['fields']) - {'secret'}
    if fields:
        access.update_key(name, {**after['grants'], 'active': after['active'], 'expires_at': after['expires_at']},
                          actor)
    if 'secret' in change['fields']:
        until = apikeys.set_secret_hash(name, after['secret_sha256'])
        store.record_audit(actor, 'set_key_secret', name, {'grace_minutes': apikeys.DEFAULT_GRACE_MINUTES,
                                                           'previous_expires_at': until})


def _apply_descriptions(change, entry, after, before, actor, digest):
    from .services import descriptions as description_service
    if change['action'] == 'delete':
        fields = {label: None for label in (_label(*k) for k in before)}
    else:
        fields = {label: values['to'] for label, values in change['fields'].items()}
    items = [{'table': table, 'column': column, 'description': text}
             for (table, column), text in ((_unlabel(label), text) for label, text in fields.items())]
    description_service.update(change['name'], {'items': items}, actor)


def _apply_export(change, entry, after, before, actor, digest):
    from .services import exports as export_service
    if change['action'] == 'create':
        export_service.create(entry, actor)
    elif change['action'] == 'update':
        export_service.update(change['name'], _cleared(after, before), actor)
    else:
        export_service.delete(change['name'], actor)


_APPLY = {'connections': _apply_connection, 'destinations': _apply_destination, 'queries': _apply_query,
          'roles': _apply_role, 'api_keys': _apply_key, 'descriptions': _apply_descriptions,
          'exports': _apply_export}


# --------------------------------------------------------------------------------------
# The store, as a file
# --------------------------------------------------------------------------------------

def _variable(*parts):
    return 'QAG_' + _VAR_UNSAFE_RE.sub('_', '_'.join(parts).upper()).strip('_')


def _secret(value, owner, variable, variables):
    """A stored password as the file holds it: a ${VAR} reference as it is; a value, as a reference to a variable
    named for it, which the export lists - a value never leaves the store."""
    if value in (None, '') or _env_ref(value):
        return value
    variables.append({'name': variable, 'for': owner,
                      'stored': 'encrypted' if store._is_encrypted(value) else 'as plain text'})
    return '${' + variable + '}'


def _without_defaults(grants):
    """A grant as written in a file: every field left at the API's default dropped - but `connections`, whose
    default is every connection, always kept."""
    return {k: v for k, v in grants.items() if k == 'connections' or v not in (None, [], False)}


def _parameter(spec):
    rule = query_service.parameter_rule(spec)
    if rule.get('required') is True:
        del rule['required']
    if rule.get('type') is None:
        del rule['type']
    return rule['type'] if set(rule) == {'type'} else rule


def _query_entry(name, content):
    version = store.read_published(content)
    data = content[str(version)]
    shape = query_service.to_version(content, version, data)
    entry = {'name': name}
    if store.read_collection(content) is not None:
        entry['collection'] = store.read_collection(content)
    for field in ('connection_name', 'description', 'author', 'tags'):
        if shape.get(field) not in (None, '', []):
            entry[field] = shape[field]
    if shape['query_type'] == 'mongo':
        entry['query_type'] = 'mongo'
        entry.update(shape['mongo'])
    else:
        entry['sql'] = shape['sql']
    if data.get('query_parameters'):
        entry['parameters'] = {param: _parameter(spec) for param, spec in data['query_parameters'].items()}
    for field in ('cache_ttl', 'columns', 'agent_writes', 'max_affected_rows'):
        if shape.get(field) not in (None, {}, []):
            entry[field] = shape[field]
    return entry


def export(kinds=KINDS, examples=False):
    """The store as a configuration file: (document, variables, skipped). `variables` lists every ${VAR} the file
    names for a password the store held as a value - set each where the server runs before applying the file
    elsewhere. `skipped` names the saved queries with no published version, which a file can't describe."""
    unknown = sorted(set(kinds) - set(KINDS))
    if unknown:
        raise ApiError(f"Unknown kind(s): {', '.join(unknown)} (known: {', '.join(KINDS)})", code='invalid_config')
    doc = {'format': FORMAT, 'format_version': FORMAT_VERSION}
    variables, skipped = [], []
    if 'connections' in kinds:
        doc['connections'] = []
        for name, details in sorted(store.read_connections().items()):
            if details.get('example') is True and not examples:
                continue
            entry = {'name': name, 'db': details['db'], 'active': details['active']}
            for field in ('host', 'port', 'user', 'password', 'database'):
                if details.get(field) not in (None, ''):
                    entry[field] = details[field]
            if 'password' in entry:
                entry['password'] = _secret(entry['password'], f'connection {name}', _variable(name, 'password'),
                                            variables)
            entry.update({k: v for k, v in sorted(details.items())
                          if k not in (*entry, 'created_at', 'updated_at', 'example', 'password')})
            doc['connections'].append(entry)
    if 'destinations' in kinds:
        doc['destinations'] = []
        for destination in destinations.list_all():
            entry = {'name': destination['name'],
                     **{k: destination[k] for k in destinations.FIELDS if destination.get(k) is not None}}
            if 'password' in entry:
                entry['password'] = _secret(entry['password'], f"destination {entry['name']}",
                                            _variable('destination', entry['name'], 'password'), variables)
            doc['destinations'].append(entry)
    roles = apikeys.list_roles()
    if 'roles' in kinds:
        doc['roles'] = [{'name': name, **_without_defaults(access._grants(entry))}
                        for name, entry in sorted(roles.items()) if examples or entry.get('example') is not True]
    if 'queries' in kinds:
        doc['queries'] = []
        for name, content in store.iter_saved():
            if store.read_example(content) and not examples:
                continue
            if store.read_published(content) is None:
                skipped.append(name)
                continue
            doc['queries'].append(_query_entry(name, content))
    if 'api_keys' in kinds:
        doc['api_keys'] = []
        hashes = apikeys.secret_hashes()
        for name, entry in sorted(apikeys.list_keys().items()):
            if entry.get('example') is True and not examples:
                continue
            grants = access._grants(entry)
            role = entry.get('created_from_role')
            out = {'name': name}
            if role in roles and access._grants(roles[role]) == grants:
                out['role'] = role  # still exactly the role's grants: say so, and it follows the role from now on
            else:
                out.update(_without_defaults(grants))
            if entry.get('active') is False:
                out['active'] = False
            if entry.get('expires_at'):
                out['expires_at'] = entry['expires_at']
            out['secret_sha256'] = hashes[name]
            doc['api_keys'].append(out)
    if 'descriptions' in kinds:
        doc['descriptions'] = {}
        for name in sorted(store.read_connections()):
            items = [{'table': item['table'], **({'column': item['column']} if item['column'] else {}),
                      'description': item['description']} for item in descriptions.list_for(name)]
            if items:
                doc['descriptions'][name] = items
    if 'exports' in kinds:
        doc['exports'] = [{'name': e['name'], **_export_from_store(e)} for e in exports.list_all()]
    return doc, variables, skipped


class _Dumper(yaml.SafeDumper):
    """YAML a person reads in a review: SQL and other text over several lines as a block, keys in the order given."""


def _text(dumper, value):
    style = '|' if '\n' in value else None
    return dumper.represent_scalar('tag:yaml.org,2002:str', value, style=style)


_Dumper.add_representer(str, _text)


def dump(doc, variables=()):
    """The document as YAML, with the variables it names listed in a comment at the top."""
    header = [f'# QueryAPIGate configuration ({FORMAT} {FORMAT_VERSION}) - see ADR 0014.']
    if variables:
        header.append('# Set these where the server runs - passwords the store held as values, never written here:')
        header += [f"#   {v['name']}: {v['for']} (stored {v['stored']})" for v in variables]
    body = yaml.dump(doc, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=120, default_flow_style=False)
    return '\n'.join(header) + '\n' + body
