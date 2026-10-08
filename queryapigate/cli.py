"""Command line entry point: ``queryapigate serve``, ``init``, ``migrate-to-postgres``, ``export`` and
``collection export|import``."""
import argparse
import contextlib
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta

from . import (
    __version__,
    adminroles,
    admins,
    bundle,
    config,
    db,
    examples,
    experimental,
    instances,
    logging_setup,
    postman,
    store,
)
from .app import create_app
from .errors import ApiError

LOOPBACK_HOSTS = ('127.0.0.1', 'localhost', '::1')


def _serve(args):
    try:
        app = create_app()
    except ValueError as error:  # a malformed setting, e.g. QUERYAPIGATE_RATE_LIMIT
        print(f'queryapigate: {error}', file=sys.stderr)
        return 2
    args.port = args.port or config.port_setting('QUERYAPIGATE_PORT', 5000)  # checked by create_app() above
    if args.host not in LOOPBACK_HOSTS and not config.api_key():
        logging.getLogger('queryapigate').warning(
            'Listening on %s without QUERYAPIGATE_API_KEY set: anyone who can reach this port can run SQL '
            'on your active connections.', args.host)
    logging.getLogger('queryapigate').info('Using %s (metadata: %s)', config.home(), db.describe())
    app.run(host=args.host, port=args.port, debug=args.debug)
    return 0


def _mcp(args):
    """`queryapigate mcp`: an MCP server exposing read-only saved queries as tools (BACKLOG #42), on its own
    port, separate from `queryapigate serve` - see mcp_server.py's own module docstring for why. Needs the
    optional `queryapigate[mcp]` extra (not a core dependency), imported lazily so `queryapigate serve`/other
    commands never need it installed."""
    try:
        app = create_app()
    except ValueError as error:
        print(f'queryapigate: {error}', file=sys.stderr)
        return 2
    try:
        from .mcp_server import run
    except ImportError:
        print('queryapigate: the "mcp" package is not installed - run `pip install "queryapigate[mcp]"`',
             file=sys.stderr)
        return 2
    instances.register('mcp')  # create_app() recorded it as 'serve'
    args.port = args.port or config.mcp_port()
    logging.getLogger('queryapigate').info('Serving MCP tools for %s at http://%s:%s/mcp',
                                           config.home(), args.host, args.port)
    run(app, host=args.host, port=args.port)
    return 0


def _migrate_to_postgres(args):
    """`queryapigate migrate-to-postgres`: copy this home's queryapigate.db into the PostgreSQL database
    QUERYAPIGATE_DATABASE_URL names - see db.migrate_sqlite_to_postgres()."""
    source = args.source or str(config.db_file())
    try:
        copied = db.migrate_sqlite_to_postgres(source)
    except ValueError as error:
        print(f'queryapigate: {error}', file=sys.stderr)
        return 2
    print(f'Copied {source} into {db.describe()}:')
    for table, count in copied.items():
        print(f'  {table}: {count}')
    print(f'{source} was not changed. Keep QUERYAPIGATE_DATABASE_URL set from now on - every instance that '
          'shares it shares these connections, saved queries and keys.')
    return 0


def _events(args):
    """`queryapigate events`: the standalone Server-Sent Events server for many clients - see events.py."""
    try:
        config.check_settings()
    except ValueError as error:
        print(f'queryapigate: {error}', file=sys.stderr)
        return 2
    logging_setup.configure(logging.getLogger('queryapigate'))
    experimental.warn_in_use(logging.getLogger('queryapigate'), os.environ, store.read_connections(),
                             also=('live_events',))
    db.init_schema()
    instances.register('events')
    args.port = args.port or config.events_port()
    from .events import run
    run(host=args.host, port=args.port)
    return 0


def _init(args):
    """Scaffold a fresh home: just queryapigate.db (with the example connection templates seeded in, all
    inactive) now that connections are SQLite-backed - no more db_connections.json/saved_sql/ for a new
    install. On a home whose store already has connections, this is a no-op - "already exists - left
    untouched"."""
    home = config.home()
    home.mkdir(parents=True, exist_ok=True)
    if db.connection().execute('SELECT 1 FROM connections LIMIT 1').fetchone() is not None:
        print(f'{db.describe()} already has connections - left untouched')
        return 0
    store.update_connections(config.EXAMPLE_CONNECTIONS['connections'])
    print(f'Created {db.describe()}\nEdit it (queryapigate serve, then the admin UI, or PATCH '
          '/api/v1/connections/<name>) - set "active": true on the connections you want, then run: queryapigate serve')
    return 0


def _backup(args):
    """`queryapigate backup FILE`: a consistent copy of the SQLite store, safe while the server runs. A PostgreSQL
    store is backed up with pg_dump, like the rest of that database - this prints the command for it."""
    if db.is_postgres():
        schema = db.postgres_schema()
        print('queryapigate: the store is PostgreSQL - back it up with pg_dump, as the rest of that database '
              '(see DEPLOYMENT.md, Backups and restores):\n'
              f'  pg_dump --format=custom --schema={schema} --no-owner --no-privileges '
              f'--file={args.file} "$QUERYAPIGATE_DATABASE_URL"', file=sys.stderr)
        return 2
    destination = os.path.abspath(args.file)
    if os.path.exists(destination) and not args.force:
        print(f'queryapigate: {destination} already exists - pass --force to replace it', file=sys.stderr)
        return 2
    if os.path.abspath(str(config.db_file())) == destination:
        print('queryapigate: that is the store itself - back it up to another file', file=sys.stderr)
        return 2
    size = db.backup_sqlite(destination)
    print(f'Backed up {db.describe()} to {destination} ({size} bytes)')
    return 0


def _resolve_export_path(template, name):
    """Fill in a --out template's {date}/{name} placeholders. Deliberately just these two, not a general
    strftime/templating facility - the exact shape #31 was scoped to."""
    try:
        return template.format(date=datetime.now().strftime('%Y-%m-%d'), name=name)
    except (KeyError, IndexError) as error:
        raise ValueError(f'--out has an unknown placeholder ({error}) - only {{date}} and {{name}} are '
                         'supported') from None


def _export(args):
    """``queryapigate export <query> --format csv --out /path/{date}.csv`` - the last small step a
    cron/systemd/Kubernetes CronJob needs to turn the existing ?stream=true export into a scheduled file
    drop, without becoming a scheduler itself (see BACKLOG.md #31 for why that stays out of scope). Runs
    entirely in-process against QUERYAPIGATE_HOME - no server needs to be running, no HTTP round trip, no API
    key: this is a trusted local operator with the same reach the admin key already has."""
    try:
        config.check_settings()
    except ValueError as error:
        print(f'queryapigate export: {error}', file=sys.stderr)
        return 2

    from .engine import stream_sql
    from .errors import ApiError
    from .formats import STREAM_FORMATTERS, iter_stream_chunks

    tmp_path = None
    try:
        if args.format not in STREAM_FORMATTERS:
            raise ApiError(f"--format must be one of: {', '.join(sorted(STREAM_FORMATTERS))}")
        try:
            raw_params = dict(p.split('=', 1) for p in args.param)
        except ValueError:
            raise ApiError('--param must look like name=value') from None

        from . import exports
        path, version, connection_name, sql, values = exports.prepare(args.query, raw_params, args.connection)
        name = store.query_name(path)

        if args.to:
            return _export_to_destination(args, path, name, version, connection_name, sql, values)
        out_path = _resolve_export_path(args.out, name)
        out_dir = os.path.dirname(out_path) or '.'
        os.makedirs(out_dir, exist_ok=True)
        tmp_path = os.path.join(out_dir, f'.{os.path.basename(out_path)}.part')

        started = time.monotonic()
        entry = {'executed_at': store.now(), 'connection_name': connection_name, 'request_id': None,
                 'key_name': 'cli'}
        try:
            columns, rows = stream_sql(sql, connection_name, values, config.effective_timeout(args.timeout))
        except ApiError as error:
            store.record_execution(path, version,
                                   {**entry, 'status': 'error', 'error': error.message, 'code': error.code})
            raise
        row_count = 0

        def counted(row_iter):
            nonlocal row_count
            for row in row_iter:
                row_count += 1
                yield row

        if args.format == 'parquet':
            from . import parquet
            parquet.write(columns, counted(rows), tmp_path)
        else:
            with open(tmp_path, 'w', newline='') as f:
                for chunk in iter_stream_chunks(args.format, columns, counted(rows)):
                    f.write(chunk)
        os.replace(tmp_path, out_path)
        # The CLI runs outside the API's grants (a local operator is already trusted), but what it exports is
        # still on the record: run history, with `cli` as the caller.
        store.record_execution(path, version, {**entry, 'status': 'success', 'rows': row_count,
                                               'duration_ms': round((time.monotonic() - started) * 1000, 1)})
    except ApiError as error:
        print(f'queryapigate export: {error.message}', file=sys.stderr)
        return 1
    except (OSError, ValueError) as error:
        print(f'queryapigate export: {error}', file=sys.stderr)
        return 1
    finally:
        if tmp_path is not None and os.path.exists(tmp_path):
            os.remove(tmp_path)  # only ever left behind by a failed run - a success already renamed it away

    print(f'Wrote {row_count} row{"" if row_count == 1 else "s"} to {out_path}')
    return 0


def _export_to_destination(args, path, name, version, connection_name, sql, values):
    """`queryapigate export QUERY --to DESTINATION --out PATH`: the same run, written by DuckDB under a destination's
    prefix (ADR 0004) - a bucket or a folder - instead of a local file. Raises ApiError; the caller reports it."""
    from . import destinations, exporting
    from .engine import stream_sql
    if args.format not in exporting.FORMATS:
        raise ApiError(f"--format must be one of {', '.join(exporting.FORMATS)} with --to")
    destination = destinations.get(args.to)
    run_id = exporting.new_run_id()
    relative = exporting.render_path(args.out, name, values, run_id)
    started = time.monotonic()
    entry = {'executed_at': store.now(), 'connection_name': connection_name, 'request_id': None, 'key_name': 'cli',
             'transport': 'export', 'destination': args.to, 'run_id': run_id}
    try:
        columns, rows = stream_sql(sql, connection_name, values, config.effective_timeout(args.timeout))
        with contextlib.closing(rows):  # a failed write must still end the query and give its connection back
            result = exporting.deliver(destination, relative, columns, rows, args.format)
    except ApiError as error:
        store.record_execution(path, version, {**entry, 'status': 'error', 'error': error.message, 'code': error.code})
        raise
    store.record_execution(path, version, {**entry, 'status': 'success', 'rows': result['rows'],
                                           'object': result['object'], 'bytes': result['bytes'],
                                           'duration_ms': round((time.monotonic() - started) * 1000, 1)})
    count = result['rows']
    print(f'Wrote {count} row{"" if count == 1 else "s"} to {result["object"]}')
    return 0


def _collection_command(action):
    """Shared shell for the collection subcommands: validate settings first (a leftover SQL2API_ variable must
    stop these too, like every other command), then turn a clean ApiError/OSError into a one-line message and
    exit code 1 instead of a traceback."""
    def run(args):
        try:
            config.check_settings()
        except ValueError as error:
            print(f'queryapigate collection {args.action}: {error}', file=sys.stderr)
            return 2
        try:
            return action(args)
        except ApiError as error:
            print(f'queryapigate collection {args.action}: {error.message}', file=sys.stderr)
            return 1
        except (OSError, ValueError) as error:
            print(f'queryapigate collection {args.action}: {error}', file=sys.stderr)
            return 1
    return run


@_collection_command
def _collection_export(args):
    if args.format == 'postman':
        document = postman.build_collection(args.name, args.base_url)
    else:
        document = bundle.export_bundle(args.name)
    document = json.dumps(document, indent=2) + '\n'
    if args.out is None:
        sys.stdout.write(document)
        return 0
    out_dir = os.path.dirname(args.out) or '.'
    os.makedirs(out_dir, exist_ok=True)
    tmp_path = os.path.join(out_dir, f'.{os.path.basename(args.out)}.part')
    try:
        with open(tmp_path, 'w') as f:
            f.write(document)
        os.replace(tmp_path, args.out)  # a failed run never leaves a partial file at the destination
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
    print(f'Wrote collection {args.name} to {args.out}', file=sys.stderr)
    return 0


@_collection_command
def _collection_import(args):
    try:
        with open(args.file) as f:
            document = json.load(f)
    except json.JSONDecodeError as error:
        raise ApiError(f'{args.file} is not valid JSON: {error}') from None
    result = bundle.import_bundle(document, on_conflict=args.on_conflict, collection_override=args.collection,
                                  dry_run=args.dry_run)
    for line in bundle.summary_lines(result, args.dry_run):
        print(line)
    return 0


def _examples_command(action):
    """Shared shell for the examples subcommands - the same settings check and clean-error handling as the
    collection ones."""
    def run(args):
        try:
            config.check_settings()
        except ValueError as error:
            print(f'queryapigate examples {args.action}: {error}', file=sys.stderr)
            return 2
        try:
            return action(args)
        except ApiError as error:
            print(f'queryapigate examples {args.action}: {error.message}', file=sys.stderr)
            return 1
        except (OSError, ValueError) as error:
            print(f'queryapigate examples {args.action}: {error}', file=sys.stderr)
            return 1
    return run


@_examples_command
def _examples_load(args):
    added = examples.load()
    if added['connection'] or added['queries'] or added['roles'] or added['key_secrets']:
        store.record_audit('cli', 'load_examples', 'examples', examples.redact_for_audit(added))
        print(f"Loaded the example APIs: {len(added['queries'])} queries in "
              f"{len(examples.SCENARIOS)} collections, {len(added['roles'])} roles, "
              f"and the '{examples.CONNECTION}' connection.")
        if added['key_secrets']:
            print('Example API keys (store these now - they cannot be shown again):')
            for name, secret in sorted(added['key_secrets'].items()):
                print(f'  {name}: {secret}')
        print('Try:  queryapigate serve   then open /console, or  curl http://127.0.0.1:5000/q/example_top_films '
             "-H 'X-API-Key: <one of the secrets above>'")
        print("Watch it live:  curl -N http://127.0.0.1:5000/events -H 'X-API-Key: <one of the secrets above>' "
             '(then call a query in another terminal to see the event arrive)')
        print('Remove them again with:  queryapigate examples unload')
    else:
        print('The example APIs are already loaded - nothing changed.')
    return 0


@_examples_command
def _examples_unload(args):
    removed = examples.unload()
    if removed['connection'] or removed['queries'] or removed['roles'] or removed['keys']:
        store.record_audit('cli', 'unload_examples', 'examples', removed)
        print(f"Removed {len(removed['queries'])} example queries, {len(removed['roles'])} roles, "
              f"{len(removed['keys'])} API keys"
              f"{' and the connection' if removed['connection'] else ''}.")
        if removed['keys_still_granted']:
            print('These keys were granted an example collection, which no longer exists, so that grant now '
                  'reaches nothing: ' + ', '.join(removed['keys_still_granted']))
    else:
        print('No example APIs are loaded - nothing changed.')
    return 0


@_examples_command
def _examples_status(args):
    state = examples.status()
    if state['loaded']:
        print(f"Loaded: {len(state['queries'])} queries in {', '.join(state['collections'])}; "
              f"roles {', '.join(state['roles'])}; connection '{state['connection']}'.")
    elif state['partial']:
        print('Partly loaded (an interrupted load) - run `queryapigate examples load` to finish it, or '
              '`queryapigate examples unload` to remove what is there.')
    else:
        print('Not loaded. Run `queryapigate examples load`.')
    return 0


DEFAULT_TOKEN_DAYS = 90


def _token_expiry(value):
    """--expires: a date, 'never', or (default) DEFAULT_TOKEN_DAYS from today."""
    if value == 'never':
        return None
    if value is None:
        return (datetime.now() + timedelta(days=DEFAULT_TOKEN_DAYS)).strftime('%Y-%m-%d')
    return value


def _print_token(token, secret):
    expiry = f"expires {token['expires_at']}" if token['expires_at'] else 'never expires'
    print(f"Admin token {token['id']} for {token['admin']} ({expiry}) - store it now, it cannot be shown again:")
    print(f'  {secret}')
    print("Use it as the X-API-Key header, or paste it into the Console's key box.")


def _admins_command(action):
    """`queryapigate admins ...` - runs against the store directly, with no server and no key, so the first owner
    can be created (and a locked-out one given a new token) by whoever runs commands on the server."""
    def run(args):
        try:
            config.check_settings()
            return action(args)
        except ApiError as error:
            print(f'queryapigate admins {args.action}: {error.message}', file=sys.stderr)
            return 1
        except (OSError, ValueError) as error:
            print(f'queryapigate admins {args.action}: {error}', file=sys.stderr)
            return 2
    return run


@_admins_command
def _admins_create(args):
    admin = admins.create_admin(args.name, args.role, email=args.email, created_by='cli')
    store.record_audit('cli', 'create_admin', admin['name'], {'role': admin['role'], 'email': admin['email']})
    token, secret = admins.issue_token(admin['name'], label=args.label, expires_at=_token_expiry(args.expires))
    store.record_audit('cli', 'issue_admin_token', admin['name'], {'token': token['id'], 'label': token['label'],
                                                                    'expires_at': token['expires_at']})
    print(f"Created administrator {admin['name']} ({admin['role']}).")
    _print_token(token, secret)
    return 0


@_admins_command
def _admins_token(args):
    token, secret = admins.issue_token(args.name, label=args.label, expires_at=_token_expiry(args.expires))
    store.record_audit('cli', 'issue_admin_token', args.name, {'token': token['id'], 'label': token['label'],
                                                               'expires_at': token['expires_at']})
    _print_token(token, secret)
    return 0


@_admins_command
def _admins_list(args):
    found = admins.list_admins()
    if not found:
        print('No administrators yet. Create the first owner with:  queryapigate admins create NAME --role owner')
        return 0
    for admin in found:
        state = '' if admin['active'] else '  (inactive)'
        email = f"  <{admin['email']}>" if admin['email'] else ''
        seen = admin['last_seen_at'] or 'never'
        print(f"{admin['name']}  {admin['role']}{email}  last seen {seen}{state}")
    return 0


def _exports_command(action):
    """`queryapigate exports ...` - straight against the store, no server: what cron on the host runs."""
    def run(args):
        try:
            config.check_settings()
            return action(args)
        except ApiError as error:
            print(f'queryapigate exports {args.action}: {error.message}', file=sys.stderr)
            return 1
        except (OSError, ValueError) as error:
            print(f'queryapigate exports {args.action}: {error}', file=sys.stderr)
            return 1
    return run


@_exports_command
def _exports_run(args):
    from . import exports
    run = exports.run(args.name, 'cli')
    rows = run['rows']
    if run['object'] is None:
        print(f"No rows - nothing written (skip_empty). Run {run['id']}.")
    else:
        print(f"Wrote {rows} row{'' if rows == 1 else 's'} to {run['object']} (run {run['id']}).")
    if run['watermark']:
        print(f"Watermark: {run['watermark']['from']} -> {run['watermark']['to']}")
    return 0


@_exports_command
def _exports_list(args):
    from . import exports
    found = exports.list_all()
    if not found:
        print('No exports. Define one with POST /api/v1/exports, or in the Console.')
    for export in found:
        incremental = f"  watermark {export['watermark']}" if export.get('incremental') else ''
        running = '  (running)' if export['running'] else ''
        print(f"{export['name']}  {export['query']} -> {export['destination']}:{export['path']}  "
              f"{export.get('format', 'parquet')}{incremental}{running}")
    return 0


def build_parser():
    parser = argparse.ArgumentParser(prog='queryapigate', description='Expose SQL databases as a REST API.')
    parser.add_argument('--version', action='version', version=f'queryapigate {__version__}')
    commands = parser.add_subparsers(dest='command')

    serve = commands.add_parser('serve', help='start the HTTP server (default)')
    serve.add_argument('--host', default=os.environ.get('QUERYAPIGATE_HOST', '127.0.0.1'))
    serve.add_argument('--port', type=int, default=None)  # QUERYAPIGATE_PORT, or 5000 - read in _serve()
    serve.add_argument('--debug', action='store_true', default=config.env_flag('QUERYAPIGATE_DEBUG'),
                       help='Flask debug mode - never use on a reachable host')
    serve.set_defaults(func=_serve)

    mcp_parser = commands.add_parser('mcp', help='start an MCP server exposing read-only saved queries as '
                                                 'tools (needs `pip install "queryapigate[mcp]"`)')
    mcp_parser.add_argument('--host', default=os.environ.get('QUERYAPIGATE_HOST', '127.0.0.1'))
    mcp_parser.add_argument('--port', type=int, default=None)  # QUERYAPIGATE_MCP_PORT, or 5001
    mcp_parser.set_defaults(func=_mcp)

    events_parser = commands.add_parser('events', help='serve GET /events to many clients at once - apps, phones, '
                                                       'dashboards - with resume (Last-Event-ID) and every '
                                                       "instance's runs; run beside `queryapigate serve`")
    events_parser.add_argument('--host', default=os.environ.get('QUERYAPIGATE_HOST', '127.0.0.1'))
    events_parser.add_argument('--port', type=int, default=None)  # QUERYAPIGATE_EVENTS_PORT, or 5002
    events_parser.set_defaults(func=_events)

    backup = commands.add_parser('backup', help='copy queryapigate.db to FILE, consistently, while the server '
                                                'runs (a PostgreSQL store: prints the pg_dump command)')
    backup.add_argument('file', metavar='FILE')
    backup.add_argument('--force', action='store_true', help='replace FILE if it exists')
    backup.set_defaults(func=_backup)

    init = commands.add_parser('init', help='create queryapigate.db in the home folder, with an inactive template '
                                            'connection per database type')
    init.set_defaults(func=_init)

    migrate = commands.add_parser('migrate-to-postgres',
                                  help='copy queryapigate.db (connections, saved queries and their history, '
                                       'API keys, roles, audit log) into the empty PostgreSQL database '
                                       'QUERYAPIGATE_DATABASE_URL names')
    migrate.add_argument('--from', dest='source', metavar='PATH',
                         help='the queryapigate.db to copy (default: the one in the home folder)')
    migrate.set_defaults(func=_migrate_to_postgres)

    export = commands.add_parser('export', help='run a saved query and write the full result to a file '
                                               '(for cron/systemd/Kubernetes CronJob, not a scheduler itself)')
    export.add_argument('query', help='saved query name (as used in a GET /q/<name> request)')
    export.add_argument('--format', choices=('csv', 'tsv', 'ndjson', 'parquet'), default='csv')
    export.add_argument('--connection', help='overrides the saved query\'s own default connection')
    export.add_argument('--out', required=True,
                        help='output path; {date} (YYYY-MM-DD) and {name} are filled in, e.g. '
                             '/exports/{name}_{date}.csv - written to a temp file and renamed into place '
                             'only on success, so a failed run never leaves a partial or missing file there. '
                             'With --to, a path inside the destination, which may also use {time}, {run} and '
                             '{a_parameter}: orders/{date}/orders_{run}.parquet')
    export.add_argument('--to', metavar='DESTINATION',
                        help='write under this destination (an s3://, gs://, r2:// prefix or a folder, defined with '
                             '/api/v1/destinations) instead of a local path; --format parquet, csv or ndjson')
    export.add_argument('--param', action='append', default=[], metavar='name=value',
                        help='a query parameter, e.g. --param customer_id=42 (repeatable)')
    export.add_argument('--timeout', type=float, help='seconds allowed for the query (default: the server '
                                                       'setting, QUERYAPIGATE_QUERY_TIMEOUT)')
    export.set_defaults(func=_export)

    collection = commands.add_parser('collection', help='export or import a collection of saved queries as one '
                                                        'portable JSON bundle')
    collection.set_defaults(func=lambda a: collection.print_help() or 2)
    collection_actions = collection.add_subparsers(dest='action')
    collection_export = collection_actions.add_parser(
        'export', help="write a collection's queries (latest version of each: SQL, description, tags, parameters, "
                       'default connection - no history, keys or credentials) as a JSON bundle')
    collection_export.add_argument('name', help='collection name')
    collection_export.add_argument('--format', choices=('bundle', 'postman'), default='bundle',
                                   help='bundle (default): re-importable with `collection import`. postman: a '
                                        'Postman Collection v2.1 file with one request per query (export only)')
    collection_export.add_argument('--base-url', default=postman.DEFAULT_BASE_URL,
                                   help='the {{baseUrl}} variable of a postman export (default: %(default)s)')
    collection_export.add_argument('--out', help='write to this file instead of stdout (written to a temp file '
                                                 'and renamed into place, so a failure leaves nothing behind)')
    collection_export.set_defaults(func=_collection_export)
    collection_import = collection_actions.add_parser(
        'import', help='save every query in a bundle into this home folder, into its collection - validated '
                       'exactly like saving a query, and all-or-nothing before anything is written')
    collection_import.add_argument('file', help='a bundle written by `collection export`')
    collection_import.add_argument('--on-conflict', choices=bundle.POLICIES, default='fail',
                                   help='a query name that already exists: fail (default - import nothing), '
                                        'skip it, or save the bundle\'s as a new version of it')
    collection_import.add_argument('--collection', help="import into this collection instead of the bundle's own")
    collection_import.add_argument('--dry-run', action='store_true', help='report what would happen; write nothing')
    collection_import.set_defaults(func=_collection_import)

    example_parser = commands.add_parser('examples', help='load, remove or check the bundled example APIs '
                                                          '(reporting, dashboard, export, partner)')
    example_parser.set_defaults(func=lambda a: example_parser.print_help() or 2)
    example_actions = example_parser.add_subparsers(dest='action')
    examples_load = example_actions.add_parser(
        'load', help='install four worked example scenarios - queries in collections, roles, and a small generated '
                     'SQLite database - all marked as examples; idempotent, never overwrites anything of yours')
    examples_load.set_defaults(func=_examples_load)
    examples_unload = example_actions.add_parser(
        'unload', help='remove exactly what `examples load` installed (and nothing else)')
    examples_unload.set_defaults(func=_examples_unload)
    examples_status = example_actions.add_parser('status', help='say whether the examples are loaded')
    examples_status.set_defaults(func=_examples_status)

    admins_parser = commands.add_parser('admins', help='create named administrators and issue their admin tokens, '
                                                       'straight in the store - no server or key needed')
    admins_parser.set_defaults(func=lambda a: admins_parser.print_help() or 2)
    admin_actions = admins_parser.add_subparsers(dest='action')
    admins_create = admin_actions.add_parser('create', help='create an administrator and print its first token')
    admins_create.add_argument('name')
    admins_create.add_argument('--role', required=True, choices=adminroles.ROLE_NAMES)
    admins_create.add_argument('--email')
    admins_token = admin_actions.add_parser('token', help="issue another token for an administrator (one who lost "
                                                          'theirs, or a new CI job)')
    admins_token.add_argument('name')
    for sub in (admins_create, admins_token):
        sub.add_argument('--label', help="what the token is for, e.g. 'laptop' or 'ci'")
        sub.add_argument('--expires', help=f"YYYY-MM-DD, or 'never' (default: {DEFAULT_TOKEN_DAYS} days from today)")
    admins_list = admin_actions.add_parser('list', help='list administrators')
    admins_create.set_defaults(func=_admins_create)
    admins_token.set_defaults(func=_admins_token)
    admins_list.set_defaults(func=_admins_list)

    exports_parser = commands.add_parser('exports', help='run or list saved exports (deliveries of a saved query to a '
                                                         'destination) - for cron on the host, no server needed')
    exports_parser.set_defaults(func=lambda a: exports_parser.print_help() or 2)
    export_actions = exports_parser.add_subparsers(dest='action')
    exports_run = export_actions.add_parser('run', help='run a saved export once, now')
    exports_run.add_argument('name')
    exports_run.set_defaults(func=_exports_run)
    exports_list = export_actions.add_parser('list', help='list saved exports')
    exports_list.set_defaults(func=_exports_list)

    for sub in (serve, init, export, collection_export, collection_import, examples_load, examples_unload,
                examples_status, admins_create, admins_token, admins_list, exports_run, exports_list):
        sub.add_argument('--home', help='folder holding queryapigate.db '
                                        '(default: $QUERYAPIGATE_HOME or the current directory)')
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        args = parser.parse_args(['serve', *(argv or [])])
    if getattr(args, 'home', None):
        os.environ['QUERYAPIGATE_HOME'] = args.home
    try:
        config.check_database_url()
        db.init_schema()
        store.refuse_legacy_home()
    except ValueError as error:
        print(f'queryapigate: {error}', file=sys.stderr)
        return 2
    except db.Error as error:  # e.g. QUERYAPIGATE_DATABASE_URL names a server that isn't reachable
        print(f'queryapigate: cannot open the metadata database {db.describe()}: {error}'.rstrip(), file=sys.stderr)
        return 2
    return args.func(args)
