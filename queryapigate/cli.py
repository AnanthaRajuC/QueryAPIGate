"""Command line entry point: ``queryapigate serve``, ``init``, ``export`` and ``collection export|import``."""
import argparse
import json
import logging
import os
import sys
from datetime import datetime

from . import __version__, apikeys, bundle, config, db, examples, postman, store
from .app import create_app
from .errors import ApiError

LOOPBACK_HOSTS = ('127.0.0.1', 'localhost', '::1')


def _serve(args):
    try:
        app = create_app()
    except ValueError as error:  # a malformed setting, e.g. QUERYAPIGATE_RATE_LIMIT
        print(f'queryapigate: {error}', file=sys.stderr)
        return 2
    if args.host not in LOOPBACK_HOSTS and not config.api_key():
        logging.getLogger('queryapigate').warning(
            'Listening on %s without QUERYAPIGATE_API_KEY set: anyone who can reach this port can run SQL '
            'on your active connections.', args.host)
    logging.getLogger('queryapigate').info('Using %s (connections: %s)', config.home(), config.connections_file().name)
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
    logging.getLogger('queryapigate').info('Serving MCP tools for %s at http://%s:%s/mcp',
                                           config.home(), args.host, args.port)
    run(app, host=args.host, port=args.port)
    return 0


def _init(args):
    """Scaffold a fresh home: just queryapigate.db (with the example connection templates seeded in, all
    inactive) now that connections are SQLite-backed - no more db_connections.json/saved_sql/ for a new
    install. On an existing home, main()'s own db.init_schema()/import_legacy_data_if_empty() already ran
    before this - if that found real data (a fresh table freshly imported from a legacy file, or one
    already populated from a previous run), this is a no-op, the same "already exists - left untouched"
    result `init` always gave."""
    home = config.home()
    home.mkdir(parents=True, exist_ok=True)
    if db.connection().execute('SELECT 1 FROM connections LIMIT 1').fetchone() is not None:
        print(f'{config.db_file()} already has connections - left untouched')
        return 0
    store.update_connections(config.EXAMPLE_CONNECTIONS['connections'])
    print(f'Created {config.db_file()}\nEdit it (queryapigate serve, then the admin UI, or PATCH /connections) - '
          'set "active": true on the connections you want, then run: queryapigate serve')
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
    from .params import resolve as resolve_params
    from .sqltools import fill_placeholders, placeholder_names

    tmp_path = None
    try:
        if args.format not in STREAM_FORMATTERS:
            raise ApiError(f"--format must be one of: {', '.join(sorted(STREAM_FORMATTERS))}")
        try:
            raw_params = dict(p.split('=', 1) for p in args.param)
        except ValueError:
            raise ApiError('--param must look like name=value') from None

        path = store.resolve_saved_file(args.query)
        name = store.query_name(path)
        _, saved = store.select_version(store.load_versions(path), None)
        connection_name = args.connection or saved.get('connection_name')
        if not connection_name:
            raise ApiError('Connection name is missing - pass --connection or set one on the saved query')
        sql = saved.get('sql_query')
        if not isinstance(sql, str):
            raise ApiError('Saved query has no SQL', 500)
        used = set(placeholder_names(sql))
        values = resolve_params(saved.get('query_parameters'), raw_params, used=used)
        sql = fill_placeholders(sql, values)

        out_path = _resolve_export_path(args.out, name)
        out_dir = os.path.dirname(out_path) or '.'
        os.makedirs(out_dir, exist_ok=True)
        tmp_path = os.path.join(out_dir, f'.{os.path.basename(out_path)}.part')

        columns, rows = stream_sql(sql, connection_name, values, config.effective_timeout(args.timeout))
        row_count = 0

        def counted(row_iter):
            nonlocal row_count
            for row in row_iter:
                row_count += 1
                yield row

        with open(tmp_path, 'w', newline='') as f:
            for chunk in iter_stream_chunks(args.format, columns, counted(rows)):
                f.write(chunk)
        os.replace(tmp_path, out_path)
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
        print('Try:  queryapigate serve   then open /ui, or  curl http://127.0.0.1:5000/q/example_top_films '
             "-H 'X-API-Key: <one of the secrets above>'")
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


def build_parser():
    parser = argparse.ArgumentParser(prog='queryapigate', description='Expose SQL databases as a REST API.')
    parser.add_argument('--version', action='version', version=f'queryapigate {__version__}')
    commands = parser.add_subparsers(dest='command')

    serve = commands.add_parser('serve', help='start the HTTP server (default)')
    serve.add_argument('--host', default=os.environ.get('QUERYAPIGATE_HOST', '127.0.0.1'))
    serve.add_argument('--port', type=int, default=int(os.environ.get('QUERYAPIGATE_PORT', 5000)))
    serve.add_argument('--debug', action='store_true', default=config.env_flag('QUERYAPIGATE_DEBUG'),
                       help='Flask debug mode - never use on a reachable host')
    serve.set_defaults(func=_serve)

    mcp_parser = commands.add_parser('mcp', help='start an MCP server exposing read-only saved queries as '
                                                 'tools (needs `pip install "queryapigate[mcp]"`)')
    mcp_parser.add_argument('--host', default=os.environ.get('QUERYAPIGATE_HOST', '127.0.0.1'))
    mcp_parser.add_argument('--port', type=int, default=config.mcp_port())
    mcp_parser.set_defaults(func=_mcp)

    init = commands.add_parser('init', help='create db_connections.json and saved_sql/ in the home folder')
    init.set_defaults(func=_init)

    export = commands.add_parser('export', help='run a saved query and write the full result to a file '
                                               '(for cron/systemd/Kubernetes CronJob, not a scheduler itself)')
    export.add_argument('query', help='saved query name (as used in a GET /q/<name> request)')
    export.add_argument('--format', choices=('csv', 'tsv', 'ndjson'), default='csv')
    export.add_argument('--connection', help='overrides the saved query\'s own default connection')
    export.add_argument('--out', required=True,
                        help='output path; {date} (YYYY-MM-DD) and {name} are filled in, e.g. '
                             '/exports/{name}_{date}.csv - written to a temp file and renamed into place '
                             'only on success, so a failed run never leaves a partial or missing file there')
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

    for sub in (serve, init, export, collection_export, collection_import, examples_load, examples_unload,
                examples_status):
        sub.add_argument('--home', help='folder holding db_connections.json and saved_sql/ '
                                        '(default: $QUERYAPIGATE_HOME or the current directory)')
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        args = parser.parse_args(['serve', *(argv or [])])
    if getattr(args, 'home', None):
        os.environ['QUERYAPIGATE_HOME'] = args.home
    db.init_schema()
    store.import_legacy_data_if_empty()
    apikeys.import_legacy_keys_if_empty()
    apikeys.import_legacy_roles_if_empty()
    return args.func(args)
