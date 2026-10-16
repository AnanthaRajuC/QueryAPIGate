"""Command line entry point: ``queryapigate serve``, ``init``, ``migrate-to-postgres``, ``export`` and
``collection export|import``."""
import argparse
import contextlib
import hashlib
import json
import logging
import os
import secrets
import signal
import sys
import time
from datetime import datetime, timedelta

from . import (
    __version__,
    adminroles,
    admins,
    bundle,
    config,
    configcode,
    db,
    examples,
    experimental,
    instances,
    logging_setup,
    postman,
    scope,
    store,
    workspaces,
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
    # SIGTERM - what a container runtime or `kill` sends - as a clean exit, so what runs at exit does: Vault leases
    # revoked (ADR 0015), the audit stream's lease given back. Python's default ends the process without it.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    app.run(host=args.host, port=args.port, debug=args.debug, request_handler=_handler())
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
    if config.mcp_oauth_enabled():
        from . import oidc
        log = logging.getLogger('queryapigate')
        log.info('MCP sign-in through %s, for tokens issued to %s (scope %s)', config.mcp_auth_server(),
                 config.mcp_resource_url(), config.mcp_scope())
        if oidc.jwks_uri(config.mcp_auth_server()) is None:
            log.warning('The MCP server will refuse every access token until that metadata can be fetched')
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
                             store.workspace_names())
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
    """_export_run(), in a span (tracing.py) - continuing TRACEPARENT, when the scheduler running it sets one."""
    from . import tracing
    with tracing.span(f'export {args.query}', attributes={'queryapigate.query': args.query},
                      parent=tracing.from_environment()):
        return _export_run(args)


def _export_run(args):
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

    from . import mongodocs
    from .engine import stream_mongo, stream_sql
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
        find = sql if isinstance(sql, mongodocs.Find) else None
        timeout = config.effective_timeout(args.timeout)
        try:
            if find is not None:
                columns, rows = None, stream_mongo(find.collection, find.filter, connection_name, None, timeout,
                                                   projection=find.projection, sort=find.sort)
            else:
                columns, rows = stream_sql(sql, connection_name, values, timeout)
        except ApiError as error:
            store.record_execution(path, version,
                                   {**entry, 'status': 'error', 'error': error.message, 'code': error.code})
            raise
        row_count = 0
        shaped = None

        def counted(row_iter):
            nonlocal row_count
            for row in row_iter:
                row_count += 1
                yield row

        with contextlib.closing(rows):  # a failed write must still end the query and give its connection back
            if find is not None:  # documents, shaped by the format (ADR 0022)
                body, shaped = mongodocs.chunks(args.format, counted(rows), find.projection)
                with open(tmp_path, 'wb' if args.format == 'parquet' else 'w',
                          **({} if args.format == 'parquet' else {'newline': ''})) as f:
                    for chunk in body:
                        f.write(chunk)
            elif args.format == 'parquet':
                from . import parquet
                parquet.write(columns, counted(rows), tmp_path)
            else:
                with open(tmp_path, 'w', newline='') as f:
                    for chunk in iter_stream_chunks(args.format, columns, counted(rows)):
                        f.write(chunk)
        os.replace(tmp_path, out_path)
        # The CLI runs outside the API's grants (a local operator is already trusted), but what it exports is
        # still on the record: run history, with `cli` as the caller.
        report = shaped.report() if shaped is not None else {}
        store.record_execution(path, version, {**entry, 'status': 'success', 'rows': row_count, **report,
                                               'duration_ms': round((time.monotonic() - started) * 1000, 1)})
        if report:
            print(f"queryapigate export: {report['fields_dropped']} field(s) had no column and were left out - a "
                  f'CSV takes its columns from the projection, or else the first {mongodocs.LOOKAHEAD} documents. '
                  'Name the fields in the projection, or export NDJSON or Parquet, to keep them', file=sys.stderr)
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
    from . import destinations, exporting, exports
    if args.format not in exporting.FORMATS:
        raise ApiError(f"--format must be one of {', '.join(exporting.FORMATS)} with --to")
    destination = destinations.get(args.to)
    run_id = exporting.new_run_id()
    relative = exporting.render_path(args.out, name, values, run_id)
    started = time.monotonic()
    entry = {'executed_at': store.now(), 'connection_name': connection_name, 'request_id': None, 'key_name': 'cli',
             'transport': 'export', 'destination': args.to, 'run_id': run_id}
    try:
        columns, rows = exports.open_stream(sql, connection_name, values, config.effective_timeout(args.timeout))
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


def _config_command(action):
    """Shared shell for the config subcommands (ADR 0014). Every failure exits 1, settings included: `config diff`
    exits 2 for "there are changes", so a script can tell the two apart."""
    def run(args):
        try:
            config.check_settings()
            return action(args)
        except configcode.ConfigInvalid as error:
            print(f'queryapigate config {args.action}: {len(error.problems)} problem(s) in the file:', file=sys.stderr)
            for problem in error.problems:
                print(f'  {problem}', file=sys.stderr)
            return 1
        except ApiError as error:
            print(f'queryapigate config {args.action}: {error.message}', file=sys.stderr)
            return 1
        except (OSError, ValueError) as error:
            print(f'queryapigate config {args.action}: {error}', file=sys.stderr)
            return 1
    return run


def _read_config_file(path):
    if path == '-':
        return configcode.load(sys.stdin.read())
    with open(path) as f:
        return configcode.load(f.read())


@_config_command
def _config_export(args):
    kinds = tuple(k.strip() for k in args.kinds.split(',')) if args.kinds else configcode.KINDS
    doc, variables, skipped = configcode.export(kinds, examples=args.examples)
    text = configcode.dump(doc, variables)
    if args.out is None:
        sys.stdout.write(text)
    else:
        out_dir = os.path.dirname(args.out) or '.'
        tmp_path = os.path.join(out_dir, f'.{os.path.basename(args.out)}.part')
        try:
            with open(tmp_path, 'w') as f:
                f.write(text)
            os.replace(tmp_path, args.out)  # a failed run never leaves a partial file at the destination
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        print(f'Wrote the configuration to {args.out}', file=sys.stderr)
    for variable in variables:
        print(f"Set {variable['name']} where the server runs: {variable['for']}'s password, stored "
              f"{variable['stored']} here", file=sys.stderr)
    for name in skipped:
        print(f'Left out saved query {name}: it has no published version', file=sys.stderr)
    return 0


def _remote(args, operation, **flags):
    """POST the file to a running server's /api/v1/config/<operation> (`--server`), with the key in QAG_API_KEY - an
    environment variable, so it never shows in a CI log or a process list."""
    import urllib.error
    import urllib.request
    key = os.environ.get('QAG_API_KEY', '').strip()
    if not key:
        raise ApiError('--server needs the API key in the QAG_API_KEY environment variable')
    doc = _read_config_file(args.file)
    query = '&'.join(f'{k}=true' for k, v in flags.items() if v)
    workspace = configcode.workspace_of(doc)  # the server applies a file only to the workspace it describes
    prefix = '' if workspace == db.DEFAULT_WORKSPACE else f'/w/{workspace}'
    url = args.server.rstrip('/') + f'{prefix}/api/v1/config/{operation}' + (f'?{query}' if query else '')
    request = urllib.request.Request(url, data=json.dumps(doc).encode(), method='POST',
                                     headers={'X-API-Key': key, 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        try:
            body = json.load(error)
        except ValueError:
            raise ApiError(f'{url} answered {error.code}') from None
        if body.get('code') == 'invalid_config':
            raise configcode.ConfigInvalid(body.get('problems') or [body.get('error')]) from None
        raise ApiError(f"{url} answered {error.code}: {body.get('error')}") from None
    except urllib.error.URLError as error:
        raise ApiError(f'{url}: {error.reason}') from None


@_config_command
def _config_diff(args):
    if args.server:
        result = _remote(args, 'diff', prune=args.prune)
    else:
        result = configcode.diff(_read_config_file(args.file), prune=args.prune)
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(configcode.render(result))
    return 2 if result['changes'] else 0


@_config_command
def _config_apply(args):
    if args.server:
        result = _remote(args, 'apply', prune=args.prune, dry_run=args.dry_run)
    elif args.dry_run:
        result = {**configcode.diff(_read_config_file(args.file), prune=args.prune), 'applied': False}
    else:
        result = configcode.apply(_read_config_file(args.file), prune=args.prune, actor='cli')
    if args.json:
        print(json.dumps(result, indent=2, default=str))
        return 0
    print(configcode.render(result))
    if args.dry_run:
        print('Dry run: nothing was changed.')
    elif result['applied']:
        print(f"Applied: the server matches the file (sha256 {result['file_sha256'][:12]}).")
    return 0


def _config_new_key(args):
    """A fresh API key secret and its SHA-256, for a key declared in a configuration file. Nothing is stored."""
    secret = 'sk_' + secrets.token_urlsafe(32)
    print(f'secret:        {secret}')
    print(f'secret_sha256: {hashlib.sha256(secret.encode()).hexdigest()}')
    print('Give the secret to whoever uses the key, keep it in your secrets manager, and put only secret_sha256 in '
          'the file.', file=sys.stderr)
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


def _describe_keys(found):
    keys, by_key = found['keys'], found['by_key']
    if not found['values']:
        return 'No encrypted passwords are stored.'
    parts = [f"{by_key[0]} under the first key"] + [f"{n} under key {i + 2}" for i, n in enumerate(by_key[1:])]
    if found['undecryptable']:
        parts.append(f"{len(found['undecryptable'])} under no listed key")
    return f"{found['values']} encrypted password(s), {keys} key(s) listed: " + ', '.join(parts) + '.'


def _handler():
    """Werkzeug's request handler, without its version banner (`Server: Werkzeug/3.x Python/3.12`, ADR 0010)."""
    from werkzeug.serving import WSGIRequestHandler

    from . import accesslog

    class Handler(WSGIRequestHandler):
        def version_string(self):
            return 'QueryAPIGate'

        def log_request(self, code='-', size='-'):
            """The access log line without the query string's values (accesslog.py, ADR 0016)."""
            line = accesslog.request_line(self.command, self.path, self.request_version)
            self.log('info', '"%s" %s %s', line, getattr(code, 'value', code), size)

    return Handler


def _history_forget(args):
    """`queryapigate history forget CALLER`: delete a person's runs, events and usage counts (ADR 0016)."""
    from . import history
    try:
        config.check_settings()
        found = history.forget(args.caller, 'cli', dry_run=args.dry_run)
    except (ApiError, ValueError) as error:
        print(f'queryapigate history forget: {getattr(error, "message", error)}', file=sys.stderr)
        return 1
    if args.dry_run:
        print(f"Would delete {found['runs']} run(s), {found['events']} event(s) and {found['usage_periods']} usage "
              f"period(s) of {args.caller}.")
    else:
        print(f"Deleted {found['runs']} run(s), {found['events']} event(s) and {found['usage_periods']} usage "
              f'period(s) of {args.caller}; recorded in the audit log by its hash, not its name.')
    return 0


def _audit_verify(args):
    """`queryapigate audit verify [--file PATH]`: walk the audit log's hash chain (ADR 0012) - the store's, or an export
    file's - and say whether it's intact, or where it first breaks. Exit 0 intact, 1 broken, 2 can't be checked
    (unreadable, or keyed with no key set)."""
    from . import auditchain
    try:
        auditchain.check_keys()
        report = auditchain.verify_file(args.file) if args.file else auditchain.verify(db.connection())
    except (OSError, ValueError) as error:
        print(f'queryapigate audit verify: {error}', file=sys.stderr)
        return 2
    if report['intact']:
        if not report.get('entries'):
            print('Intact: no chained entries yet.')
            return 0
        if report.get('unchecked_without_key') == report['entries']:
            print(f"Not checked: all {report['entries']} entries are keyed, and QUERYAPIGATE_AUDIT_KEY is not set - "
                  'set it to the key they were written with.', file=sys.stderr)
            return 2
        how = ('keyed with QUERYAPIGATE_AUDIT_KEY' if report.get('keyed')
               else 'unkeyed (plain SHA-256: set QUERYAPIGATE_AUDIT_KEY so an edit cannot be re-signed)')
        print(f"Intact: entries {report['first_seq']} to {report['last_seq']}, {how}.")
        if report.get('repeats'):
            print(f"  {report['repeats']} repeated entries dropped - the same seq and hash, as at-least-once delivery "
                  'can send them')
        if report.get('unchecked_without_key'):
            print(f"  {report['unchecked_without_key']} keyed entries not checked - QUERYAPIGATE_AUDIT_KEY is not set")
        return 0
    found = report['break']
    where = f"line {found['line']}" if 'line' in found else (
        f"entry {found['seq']} ({found.get('action')}, {found.get('timestamp')})")
    if found['problem'] == 'conflicting_copies':
        where += f" - lines {found['lines'][0]} and {found['lines'][1]} differ"
    print(f"BROKEN at {where}: {found['problem'].replace('_', ' ')}", file=sys.stderr)
    return 1


def _rekey(args):
    from . import rekey
    try:
        config.check_secret_keys()
        found = rekey.check() if args.check else rekey.rekey('cli')
    except ValueError as error:
        print(f'queryapigate rekey: {error}', file=sys.stderr)
        return 2
    except ApiError as error:
        print(f'queryapigate rekey: {error.message}', file=sys.stderr)
        return 1
    if not args.check:
        print(f"Re-encrypted {found['rekeyed']} password(s) under the first key.")
    print(_describe_keys(found))
    for value in found['undecryptable']:
        print(f"  no listed key decrypts {value['type']} '{value['name']}'")
    if found['undecryptable']:
        return 1
    if found['keys'] > 1 and not found['under_old_keys']:
        print('Nothing is left under the other keys: remove them from QUERYAPIGATE_SECRET_KEY on every instance.')
    elif args.check and found['under_old_keys']:
        print('Run `queryapigate rekey` to move them to the first key.')
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

    config_parser = commands.add_parser('config', help="the server's configuration as a file (ADR 0014, experimental): "
                                                       'export it, see what applying one would change, apply it')
    config_parser.set_defaults(func=lambda a: config_parser.print_help() or 2)
    config_actions = config_parser.add_subparsers(dest='action')
    config_export = config_actions.add_parser(
        'export', help='write connections, destinations, roles, saved queries, API keys, descriptions and exports as '
                       'one YAML file - passwords as ${VAR} references, keys as their hashes, never a secret')
    config_export.add_argument('--kinds', help=f"only these, comma-separated (of: {', '.join(configcode.KINDS)})")
    config_export.add_argument('--examples', action='store_true', help='include the bundled examples too')
    config_export.add_argument('--out', help='write to this file instead of stdout')
    config_export.set_defaults(func=_config_export)
    config_diff = config_actions.add_parser(
        'diff', help='what applying FILE would create and change, field by field - writes nothing. Exit 0: no '
                     'changes; 2: changes; 1: the file has problems')
    config_diff.add_argument('file', help='a configuration file (YAML or JSON), or - for stdin')
    config_diff.add_argument('--json', action='store_true', help='the changes as JSON, for a script')
    config_diff.add_argument('--prune', action='store_true', help='also show what --prune would delete')
    config_diff.add_argument('--server', metavar='URL', help="a running server's base URL, instead of this home's "
                                                             'store - the key in QAG_API_KEY (CI against prod)')
    config_diff.set_defaults(func=_config_diff)
    config_apply = config_actions.add_parser(
        'apply', help='make the server match FILE, all or nothing - every change through the same rules as the API, '
                      'audited; what the file does not name is left alone')
    config_apply.add_argument('file', help='a configuration file (YAML or JSON), or - for stdin')
    config_apply.add_argument('--prune', action='store_true',
                              help='also delete what a file manages (created, changed or named before) and this one '
                                   'no longer has - never what was made in the Console or the API')
    config_apply.add_argument('--dry-run', action='store_true', help='say what would change; change nothing')
    config_apply.add_argument('--json', action='store_true', help='the result as JSON, for a script')
    config_apply.add_argument('--server', metavar='URL', help="a running server's base URL, instead of this home's "
                                                              'store - the key in QAG_API_KEY (an owner\'s)')
    config_apply.set_defaults(func=_config_apply)
    config_new_key = config_actions.add_parser(
        'new-key', help="a fresh API key secret and its secret_sha256, for a key declared in a file - nothing stored")
    config_new_key.set_defaults(func=_config_new_key)

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

    audit_parser = commands.add_parser('audit', help="check the audit log's hash chain")
    audit_parser.set_defaults(func=lambda a: audit_parser.print_help() or 2)
    audit_actions = audit_parser.add_subparsers(dest='action')
    audit_verify = audit_actions.add_parser('verify', help='walk the chain and say whether it is intact, or where it '
                                                          'first breaks (exit 0 or 1)')
    audit_verify.add_argument('--file', help='a copy instead of the store: an export file '
                                             '(QUERYAPIGATE_AUDIT_LOG_EXPORT_FILE), or what a SIEM received - JSON or '
                                             'syslog lines, repeats allowed')
    audit_verify.set_defaults(func=_audit_verify)

    history_parser = commands.add_parser('history', help='run history: forget a person (ADR 0016)')
    history_parser.set_defaults(func=lambda a: history_parser.print_help() or 2)
    history_actions = history_parser.add_subparsers(dest='action')
    history_forget = history_actions.add_parser(
        'forget', help="delete every run by CALLER, its events and its usage counts - a person's data, on request; "
                       'recorded in the audit log by its hash, not its name')
    history_forget.add_argument('caller', help='a key name, or jwt:<subject> for a signed-in person')
    history_forget.add_argument('--dry-run', action='store_true', help='say how much would go; delete nothing')
    history_forget.set_defaults(func=_history_forget)

    rekey_parser = commands.add_parser('rekey', help='re-encrypt every stored password under the first key in '
                                                     'QUERYAPIGATE_SECRET_KEY, so the others can be removed (rotating '
                                                     'the key)')
    rekey_parser.add_argument('--check', action='store_true',
                              help='only report how many passwords are under each key - change nothing')
    rekey_parser.set_defaults(func=_rekey)

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
                examples_status, admins_create, admins_token, admins_list, exports_run, exports_list, rekey_parser,
                audit_verify):
        sub.add_argument('--home', help='folder holding queryapigate.db '
                                        '(default: $QUERYAPIGATE_HOME or the current directory)')
    # What a workspace owns (ADR 0023): these act in one. `config diff` and `config apply` take theirs from the file.
    for sub in (export, collection_export, collection_import, config_export, examples_load, examples_unload,
                examples_status, exports_run, exports_list):
        sub.add_argument('--workspace', metavar='NAME',
                         help='the workspace whose queries, connections, keys and exports this acts on '
                              '(default: default)')
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        args = parser.parse_args(['serve', *(argv or [])])
    if getattr(args, 'home', None):
        os.environ['QUERYAPIGATE_HOME'] = args.home
    if getattr(args, 'server', None) or getattr(args, 'func', None) is _config_new_key:
        return args.func(args)  # nothing of this home's: no store opened, so none made in a CI checkout
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
    workspace = getattr(args, 'workspace', None)
    if workspace is not None and not workspaces.exists(workspace):
        print(f"queryapigate: no workspace named '{workspace}' - create it first "
              '(POST /api/v1/workspaces, or the Console)', file=sys.stderr)
        return 2
    with scope.using(workspace):
        return args.func(args)
