"""Worked example APIs you can load into a home folder, try, and remove again (``queryapigate examples ...``).

Four scenarios, each a *collection* of saved queries plus a role shaped like the key that would front it:

* ``examples-reporting`` - a read-only, rate-limited reporting API of aggregate queries;
* ``examples-dashboard`` - small queries meant to be polled, cached for 30 seconds;
* ``examples-export`` - a ~20,000-row table meant for ``?stream=true`` exports;
* ``examples-partner`` - a narrow, parameter-validated surface for an external client's key.

Plus a fifth role, ``example-executive``, granted both the reporting and dashboard collections at once - the
one thing none of the four scenarios above shows on its own: a role (and so a key created from it) reaching
several collections, not just one.

They run against a small generated SQLite database (``examples.db``, connection name ``examples``): no third-party
data is shipped, so there is nothing to license, and rentals are dated relative to the day it is generated so the
"today" and "overdue" queries always have something to show. Eight tables - ``film``, ``customer`` and ``rental``
plus ``category``, ``store``, ``staff``, ``address`` and ``payment`` - with real foreign keys throughout;
``example_all_rentals`` (the export scenario) joins six of them (``rental``, ``film``, ``customer``, ``payment``,
``staff``, ``store``).

Everything installed is *marked* ``example`` (a top-level ``"example": true`` on a query file, and on the role,
key and connection entries), and removal deletes exactly what is marked - never something of yours that merely
shares a name. Loading refuses to touch anything that is not marked, and is idempotent: running it again changes
nothing.

Loading also creates one real API key per role (``role=<name>``, the same "create key from role" path the admin
UI uses), so every scenario is immediately usable rather than just described. This is a deliberate reversal of
this module's own earlier design (a key used to be left for the reader to create by hand, specifically so a
server with none stayed open) - the moment these keys exist, the server requires a key for *every* request, not
just the example endpoints. A key's secret is shown exactly once, in ``load()``'s own return value
(``key_secrets``) - printed by the CLI, returned by ``POST /examples``, or logged a single time at startup for
``QUERYAPIGATE_LOAD_EXAMPLES`` (the only channel available there, since nothing interactive is watching); it is
never recoverable afterward, the same rule every API key already has. Weighed against staying open: a demo an
admin can actually call immediately, with real per-scenario keys to copy into a client, was judged worth losing
the "still open" default - see ``documentation/EXAMPLES.md`` for the full reasoning and how to use the keys.

Loading also seeds each fresh query's ``execution_history`` with a handful of realistic-looking synthetic runs
(``_seed_execution_history()``) - through ``store.record_execution()``, the exact function a real request already
uses - so the admin UI's History tab, Home tab and requests-per-day chart show something immediately instead of
staying empty until someone actually calls a query. Deliberately *not* extended to the in-memory ``/metrics``
counters or the API Keys/Connections "Usage" columns: those are documented as live-traffic counters, reset on
every restart, and synthesizing them would only last until the next one while quietly changing what they
honestly mean.
"""
import os
import random
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from . import apikeys, bundle, config, store
from .errors import ApiError

CONNECTION = 'examples'
DB_FILE = 'examples.db'
AUTHOR = 'queryapigate-examples'

CATEGORIES = ['Action', 'Comedy', 'Documentary', 'Drama', 'Family', 'Sci-Fi']
RATINGS = ['G', 'PG', 'PG-13', 'R']
FILM_COUNT = 60
CUSTOMER_COUNT = 200
RENTAL_COUNT = 20000
HISTORY_DAYS = 120

_ADJECTIVES = ['Crimson', 'Silent', 'Golden', 'Hidden', 'Electric', 'Midnight', 'Broken', 'Wild', 'Frozen', 'Distant']
_NOUNS = ['Harbor', 'Garden', 'Signal', 'Voyage', 'Mirror', 'Orchard', 'Citadel', 'Horizon', 'Lantern', 'Meadow']
_FIRST = ['Ava', 'Ben', 'Chloe', 'Dev', 'Elena', 'Farid', 'Grace', 'Hiro', 'Isla', 'Jonas', 'Kira', 'Liam']
_LAST = ['Adams', 'Brooks', 'Chen', 'Diaz', 'Evans', 'Fischer', 'Gupta', 'Hughes', 'Ito', 'Jensen', 'Khan', 'Lopez']
_COUNTRIES = ['Australia', 'Brazil', 'Canada', 'Germany', 'India', 'Japan', 'Kenya', 'Norway', 'Spain', 'USA']
# One representative city per _COUNTRIES entry, same index - used for `address` and `store` so a country's
# city is consistent rather than a random, unrelated pairing.
_CITIES = ['Sydney', 'Sao Paulo', 'Toronto', 'Berlin', 'Mumbai', 'Osaka', 'Nairobi', 'Oslo', 'Madrid', 'Austin']
_STORE_COUNTRY_INDEXES = [2, 3, 9]  # Toronto/Canada, Berlin/Germany, Austin/USA - the 3 example stores

# --------------------------------------------------------------------------------------
# The database
# --------------------------------------------------------------------------------------

_SCHEMA = '''
CREATE TABLE film (
    film_id     INTEGER PRIMARY KEY,
    title       TEXT NOT NULL,
    category    TEXT NOT NULL,
    rating      TEXT NOT NULL,
    length      INTEGER NOT NULL,
    rental_rate REAL NOT NULL
);
CREATE TABLE customer (
    customer_id INTEGER PRIMARY KEY,
    first_name  TEXT NOT NULL,
    last_name   TEXT NOT NULL,
    email       TEXT NOT NULL,
    country     TEXT NOT NULL
);
CREATE TABLE rental (
    rental_id   INTEGER PRIMARY KEY,
    film_id     INTEGER NOT NULL REFERENCES film(film_id),
    customer_id INTEGER NOT NULL REFERENCES customer(customer_id),
    rental_date TEXT NOT NULL,
    return_date TEXT,
    amount      REAL NOT NULL
);
CREATE INDEX rental_date_idx ON rental(rental_date);
CREATE INDEX rental_film_idx ON rental(film_id);
CREATE TABLE category (
    category_id INTEGER PRIMARY KEY,
    name        TEXT NOT NULL
);
CREATE TABLE store (
    store_id    INTEGER PRIMARY KEY,
    city        TEXT NOT NULL,
    country     TEXT NOT NULL
);
CREATE TABLE staff (
    staff_id    INTEGER PRIMARY KEY,
    first_name  TEXT NOT NULL,
    last_name   TEXT NOT NULL,
    store_id    INTEGER NOT NULL REFERENCES store(store_id)
);
CREATE TABLE address (
    address_id  INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customer(customer_id),
    city        TEXT NOT NULL,
    country     TEXT NOT NULL
);
CREATE TABLE payment (
    payment_id   INTEGER PRIMARY KEY,
    rental_id    INTEGER NOT NULL REFERENCES rental(rental_id),
    staff_id     INTEGER NOT NULL REFERENCES staff(staff_id),
    amount       REAL NOT NULL,
    payment_date TEXT NOT NULL
);
CREATE INDEX staff_store_idx ON staff(store_id);
CREATE INDEX address_customer_idx ON address(customer_id);
CREATE INDEX payment_rental_idx ON payment(rental_id);
CREATE INDEX payment_staff_idx ON payment(staff_id);
'''


def build_database(path, now=None, seed=42):
    """Create the example SQLite database at ``path`` (which must not exist). Deterministic for a given ``now`` and
    ``seed``. Dates are UTC, to match SQLite's own ``'now'`` that the example queries compare against."""
    rng = random.Random(seed)
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    titles = [f'{a} {n}' for a in _ADJECTIVES for n in _NOUNS]
    rng.shuffle(titles)
    films = [(i, titles[i - 1], CATEGORIES[i % len(CATEGORIES)], rng.choice(RATINGS), rng.randint(70, 170),
              rng.choice([0.99, 1.99, 2.99, 3.99])) for i in range(1, FILM_COUNT + 1)]
    customers = []
    for i in range(1, CUSTOMER_COUNT + 1):
        first, last = rng.choice(_FIRST), rng.choice(_LAST)
        customers.append((i, first, last, f'{first}.{last}{i}@example.com'.lower(), rng.choice(_COUNTRIES)))
    weights = [1 / (rank + 10) for rank in range(FILM_COUNT)]  # popular films exist, without one dwarfing the rest
    rentals = []
    for rental_id in range(1, RENTAL_COUNT + 1):
        film = films[rng.choices(range(FILM_COUNT), weights)[0]]
        rented = now - timedelta(days=rng.random() * HISTORY_DAYS, seconds=rng.randint(0, 3600))
        due_back = rented + timedelta(days=rng.randint(1, 6))
        # A rental is still out when it is not yet due back - or, for about 1 in 100, it never came back at all.
        returned = None if (due_back > now or rng.random() < 0.01) else due_back
        rentals.append((rental_id, film[0], rng.randint(1, CUSTOMER_COUNT), rented.strftime('%Y-%m-%d %H:%M:%S'),
                        returned.strftime('%Y-%m-%d %H:%M:%S') if returned else None, film[5]))

    # Five more tables, purely additive - film/customer/rental's own rows (and the rng draws that produced
    # them, above) are untouched, so this must only ever append new rng consumption after this point, never
    # interleave with it, or the rental rows a reseed would produce could silently change.
    categories = [(i, name) for i, name in enumerate(CATEGORIES, start=1)]
    stores = [(i, _CITIES[idx], _COUNTRIES[idx]) for i, idx in enumerate(_STORE_COUNTRY_INDEXES, start=1)]
    staff = []
    for store_id, _city, _country in stores:
        for _ in range(2):
            first, last = rng.choice(_FIRST), rng.choice(_LAST)
            staff.append((len(staff) + 1, first, last, store_id))
    addresses = []
    for i in range(1, CUSTOMER_COUNT + 1):
        idx = rng.randrange(len(_COUNTRIES))
        addresses.append((i, i, _CITIES[idx], _COUNTRIES[idx]))
    payments = [(rental[0], rental[0], rng.choice(staff)[0], rental[5], rental[3]) for rental in rentals]

    tmp_path = f'{path}.tmp'
    conn = sqlite3.connect(tmp_path)
    try:
        conn.executescript(_SCHEMA)
        conn.executemany('INSERT INTO film VALUES (?, ?, ?, ?, ?, ?)', films)
        conn.executemany('INSERT INTO customer VALUES (?, ?, ?, ?, ?)', customers)
        conn.executemany('INSERT INTO rental VALUES (?, ?, ?, ?, ?, ?)', rentals)
        conn.executemany('INSERT INTO category VALUES (?, ?)', categories)
        conn.executemany('INSERT INTO store VALUES (?, ?, ?)', stores)
        conn.executemany('INSERT INTO staff VALUES (?, ?, ?, ?)', staff)
        conn.executemany('INSERT INTO address VALUES (?, ?, ?, ?)', addresses)
        conn.executemany('INSERT INTO payment VALUES (?, ?, ?, ?, ?)', payments)
        conn.commit()
    finally:
        conn.close()
    os.replace(tmp_path, path)  # a half-written file is never left at the real name


# --------------------------------------------------------------------------------------
# The scenarios: bundle documents, so loading goes through exactly the validation saving a query does
# --------------------------------------------------------------------------------------

def _query(name, description, sql, parameters=None, cache_ttl=None, tags=()):
    entry = {'name': name, 'sql_query': sql, 'author': AUTHOR, 'description': description,
             'tags': ['example', *tags], 'connection_name': CONNECTION}
    if parameters:
        entry['query_parameters'] = parameters
    if cache_ttl:
        entry['cache_ttl'] = cache_ttl
    return entry


_FILM_JOIN = 'FROM rental r JOIN film f ON f.film_id = r.film_id'

SCENARIOS: dict[str, dict[str, Any]] = {
    'examples-reporting': {
        'title': 'Reporting API',
        'queries': [
            _query('example_monthly_revenue', 'Rentals and revenue per month, newest first',
                   "SELECT strftime('%Y-%m', rental_date) AS month, COUNT(*) AS rentals, "
                   'ROUND(SUM(amount), 2) AS revenue FROM rental '
                   "WHERE rental_date >= date('now', '-' || :months || ' months') GROUP BY month ORDER BY month DESC",
                   {'months': {'type': 'int', 'min': 1, 'max': 24, 'default': 6,
                               'description': 'How many months back to include'}}),
            _query('example_top_films', 'Most-rented films, optionally within one category',
                   'SELECT * FROM (SELECT f.title, f.category, f.rating, COUNT(*) AS rentals, '
                   f'RANK() OVER (ORDER BY COUNT(*) DESC) AS rank {_FILM_JOIN} '
                   'WHERE (:category IS NULL OR f.category = :category) GROUP BY f.film_id) '
                   'WHERE rank <= :top_n ORDER BY rank, title',
                   {'top_n': {'type': 'int', 'min': 1, 'max': 50, 'default': 10, 'description': 'How many films'},
                    'category': {'type': 'str', 'enum': CATEGORIES, 'required': False,
                                 'description': 'Only this category (omit for all)'}}),
            _query('example_revenue_by_category', 'Revenue per film category over the last N days',
                   f"SELECT f.category, COUNT(*) AS rentals, ROUND(SUM(r.amount), 2) AS revenue {_FILM_JOIN} "
                   "WHERE r.rental_date >= date('now', '-' || :days || ' days') GROUP BY f.category "
                   'ORDER BY revenue DESC',
                   {'days': {'type': 'int', 'min': 1, 'max': HISTORY_DAYS, 'default': 30,
                             'description': 'Window size in days'}}),
        ],
        'role': {'name': 'example-reporting', 'rate_limit': '200/hour'},
    },
    'examples-dashboard': {
        'title': 'Dashboard data API',
        'queries': [
            _query('example_kpi_rentals_today', 'Rentals and revenue so far today (UTC)',
                   'SELECT COUNT(*) AS rentals_today, ROUND(COALESCE(SUM(amount), 0), 2) AS revenue_today '
                   "FROM rental WHERE date(rental_date) = date('now')", cache_ttl=30, tags=('kpi',)),
            _query('example_kpi_active_rentals', 'Rentals that are currently out',
                   'SELECT COUNT(*) AS active_rentals FROM rental WHERE return_date IS NULL',
                   cache_ttl=30, tags=('kpi',)),
            _query('example_kpi_overdue', 'Rentals out for more than 7 days',
                   "SELECT COUNT(*) AS overdue FROM rental WHERE return_date IS NULL "
                   "AND rental_date < datetime('now', '-7 days')", cache_ttl=30, tags=('kpi',)),
            _query('example_recent_rentals', 'The latest rentals, for a live feed',
                   "SELECT r.rental_id, f.title, c.first_name || ' ' || c.last_name AS customer, r.rental_date "
                   f'{_FILM_JOIN} JOIN customer c ON c.customer_id = r.customer_id '
                   "WHERE r.rental_date >= datetime('now', '-' || :hours || ' hours') "
                   'ORDER BY r.rental_date DESC, r.rental_id DESC',
                   {'hours': {'type': 'int', 'min': 1, 'max': 168, 'default': 24,
                              'description': 'How many hours back'}}, cache_ttl=30),
        ],
        'role': {'name': 'example-dashboard', 'rate_limit': '600/minute'},
    },
    'examples-export': {
        'title': 'Data export API',
        'queries': [
            _query('example_all_rentals', 'Every rental with its payment, staff and store - about '
                   f'{RENTAL_COUNT:,} rows. Run it with ?stream=true&format=csv to export it in constant memory',
                   'SELECT r.rental_id, r.rental_date, r.return_date, r.amount, f.title, f.category, '
                   "c.first_name || ' ' || c.last_name AS customer, c.country, "
                   "s.first_name || ' ' || s.last_name AS staff, st.city AS store_city "
                   f'{_FILM_JOIN} JOIN customer c ON c.customer_id = r.customer_id '
                   'JOIN payment p ON p.rental_id = r.rental_id JOIN staff s ON s.staff_id = p.staff_id '
                   'JOIN store st ON st.store_id = s.store_id ORDER BY r.rental_id'),
            _query('example_rentals_since', 'Rentals from a date onward - for incremental exports',
                   "SELECT r.rental_id, r.rental_date, r.amount, f.title "
                   f'{_FILM_JOIN} WHERE date(r.rental_date) >= :since ORDER BY r.rental_id',
                   {'since': {'type': 'str', 'pattern': r'\d{4}-\d{2}-\d{2}', 'default': '2000-01-01',
                              'description': 'YYYY-MM-DD'}}),
        ],
        'role': {'name': 'example-export', 'rate_limit': '20/hour'},
    },
    'examples-partner': {
        'title': 'Partner / integration API',
        'queries': [
            _query('example_film_lookup', 'One film and how it is doing - for a partner integration',
                   'SELECT f.film_id, f.title, f.category, f.rating, f.length, '
                   "SUM(r.rental_date >= datetime('now', '-30 days')) AS rentals_last_30_days, "
                   'SUM(r.return_date IS NULL) AS currently_rented '
                   f'{_FILM_JOIN} WHERE f.film_id = :film_id GROUP BY f.film_id',
                   {'film_id': {'type': 'int', 'min': 1, 'description': 'The film to look up'}}),
            _query('example_film_search', 'Find films by part of the title',
                   "SELECT film_id, title, category, rating FROM film WHERE title LIKE '%' || :text || '%' "
                   'ORDER BY title',
                   {'text': {'type': 'str', 'min_length': 2, 'max_length': 30, 'pattern': '[A-Za-z ]+',
                             'description': 'Letters and spaces only'}}),
        ],
        'role': {'name': 'example-partner', 'rate_limit': '60/minute'},
    },
}

QUERY_NAMES = [q['name'] for scenario in SCENARIOS.values() for q in scenario['queries']]
# Which scenario's own key would realistically have run each query - used only to attribute synthetic
# execution_history entries to a plausible caller, see _seed_execution_history().
_KEY_BY_QUERY = {q['name']: scenario['role']['name'] for scenario in SCENARIOS.values() for q in scenario['queries']}

# A fifth role, deliberately not tied to one collection like the four above - it demonstrates that a role
# (and so a key created from it) can be granted several collections at once, not just one. Not part of
# SCENARIOS since nothing here iterates it 1:1 with a collection to create queries for; status()/unload()/
# _conflicts() already operate generically over "every role marked example: True", so they need no changes
# to pick this up - only load()'s role-creation loop needs a matching one for EXTRA_ROLES.
EXTRA_ROLES: list[dict[str, Any]] = [
    {'name': 'example-executive', 'collections': ['examples-reporting', 'examples-dashboard'],
     'rate_limit': '300/hour'},
]

ROLE_NAMES = [scenario['role']['name'] for scenario in SCENARIOS.values()] + [r['name'] for r in EXTRA_ROLES]
# One example API key per role, reusing the role's own name - created_from_role already links them, and
# same name in the API Keys tab vs. the Roles tab makes the pairing obvious without a second naming scheme.
KEY_NAMES = ROLE_NAMES


def _bundle(collection):
    return {'format': bundle.FORMAT, 'format_version': bundle.FORMAT_VERSION, 'collection': collection,
            'queries': SCENARIOS[collection]['queries']}


# --------------------------------------------------------------------------------------
# Load / unload / status
# --------------------------------------------------------------------------------------

def _marked_connection():
    return store.read_connections().get(CONNECTION)


def status():
    """What is installed right now. ``loaded`` means all of it (the connection, every query, every role and
    every key); a partly installed state - an interrupted load - reports ``partial`` so it is never mistaken
    for either extreme. ``keys`` lists example key *names* only, never secrets - a key's secret is shown
    once, at creation, in ``load()``'s own return value (``key_secrets``), and is gone forever after that,
    the same rule every API key already has."""
    connection = _marked_connection()
    has_connection = bool(connection and connection.get('example') is True)
    queries = sorted(store.example_query_names())
    roles = sorted(name for name, role in apikeys.list_roles().items() if role.get('example') is True)
    keys = sorted(name for name, key in apikeys.list_keys().items() if key.get('example') is True)
    collections = sorted({c for c, names in store.collection_members().items() if set(names) & set(queries)})
    complete = (has_connection and set(queries) == set(QUERY_NAMES) and set(roles) == set(ROLE_NAMES)
               and set(keys) == set(KEY_NAMES))
    installed_any = has_connection or bool(queries) or bool(roles) or bool(keys)
    return {'loaded': complete, 'partial': installed_any and not complete, 'connection': CONNECTION if has_connection
            else None, 'queries': queries, 'roles': roles, 'keys': keys, 'collections': collections}


def _conflicts():
    """Things already in the home under an example's name that are not marked as examples - never overwritten."""
    found = []
    connection = _marked_connection()
    if connection is not None and connection.get('example') is not True:
        found.append(f"connection '{CONNECTION}'")
    elif connection is None and os.path.exists(config.home() / DB_FILE):
        found.append(f'the file {DB_FILE}')
    marked = set(store.example_query_names())
    for name in QUERY_NAMES:
        if name not in marked and store.saved_query_exists(name):
            found.append(f"saved query '{name}'")
    roles = apikeys.list_roles()
    found += [f"role '{name}'" for name in ROLE_NAMES if name in roles and roles[name].get('example') is not True]
    keys = apikeys.list_keys()
    found += [f"API key '{name}'" for name in KEY_NAMES if name in keys and keys[name].get('example') is not True]
    return found


def _seed_execution_history(created_query_names, now=None, seed=42):
    """Synthetic, realistic-looking execution_history for freshly-loaded example queries (only ones actually
    created this call - never touches an existing query's real history, so a second load() adds nothing
    more, the same idempotency guarantee everything else here already has), so the admin UI's History tab,
    Home tab and requests-per-day chart show something immediately instead of staying empty until a real
    request happens. Deterministic for a given now/seed, the same reproducibility build_database() already
    commits to - a separate random.Random instance, so this never shares or disturbs that function's own RNG
    stream. Every entry is `status: "success"` - a fresh install's history shouldn't scatter fake errors -
    and goes through store.record_execution(), the exact function a real request already uses, so it is
    real, persisted execution_history in every respect except how it was produced."""
    rng = random.Random(seed)
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    for name in created_query_names:
        key_name = _KEY_BY_QUERY.get(name)
        if key_name is None:
            continue
        version, _data = store.select_version(store.load_versions(name), None)
        for _ in range(rng.randint(8, 20)):
            executed_at = now - timedelta(days=rng.random() * 7, seconds=rng.randint(0, 86400))
            entry = {
                'executed_at': executed_at.strftime('%Y-%m-%d %H:%M:%S'),
                'connection_name': CONNECTION,
                'key_name': key_name,
                'status': 'success',
                'rows': rng.randint(1, 50),
                'duration_ms': rng.randint(2, 40),
            }
            store.record_execution(name, version, entry, sample=False)  # seeded, not live traffic


def redact_for_audit(added):
    """`load()`'s own return value, safe to pass to store.record_audit(): `key_secrets` (real, plaintext,
    shown-once secrets) is replaced with just the sorted list of names a fresh key was created for - the
    audit log must record *that* a key was created, never its value, the same redaction connection passwords
    already get (masked/`"changed"`, never the literal secret) before anything persists it or the admin UI's
    Audit Log tab can ever display it."""
    return {**added, 'key_secrets': sorted(added['key_secrets'])}


def load(now=None):
    """Install the examples. Idempotent (what is already there is left alone, so re-running never adds versions) and
    completes an interrupted load. Refuses, changing nothing, if something of yours holds an example's name.
    Returns what was added."""
    with store.lock:
        conflicts = _conflicts()
        if conflicts:
            raise ApiError('Nothing loaded - these already exist and are not examples, so they will not be '
                           f"overwritten: {', '.join(conflicts)}. Rename or remove them first.", 409)
        added = {'connection': False, 'queries': [], 'roles': [], 'key_secrets': {}}
        config.home().mkdir(parents=True, exist_ok=True)
        db_path = config.home() / DB_FILE
        if _marked_connection() is None:
            if not db_path.exists():
                try:
                    build_database(str(db_path), now)
                except sqlite3.Error as error:  # SQLite's own error type - not an OSError, so callers would miss it
                    raise ApiError(f'Could not create the example database in {config.home()} ({error}). Is the data '
                                   'folder writable by the user QueryAPIGate runs as?', 500) from None
            store.update_connections({CONNECTION: {'db': 'sqlite', 'database': DB_FILE, 'active': True,
                                                   'example': True}})
            added['connection'] = True
        present = set(store.example_query_names())
        for collection, scenario in SCENARIOS.items():
            missing = [q for q in scenario['queries'] if q['name'] not in present]
            if missing:
                document = {**_bundle(collection), 'queries': missing}
                result = bundle.import_bundle(document, on_conflict='fail', example=True, audit=False)
                added['queries'] += result['created']
        existing_roles = apikeys.list_roles()
        for collection, scenario in SCENARIOS.items():
            role = scenario['role']
            if role['name'] not in existing_roles:
                apikeys.create_role(role['name'], connections=[], allow_writes=False, collections=[collection],
                                    rate_limit=role['rate_limit'], example=True)
                added['roles'].append(role['name'])
        for role in EXTRA_ROLES:
            if role['name'] not in existing_roles:
                apikeys.create_role(role['name'], connections=[], allow_writes=False, collections=role['collections'],
                                    rate_limit=role['rate_limit'], example=True)
                added['roles'].append(role['name'])
        existing_keys = apikeys.list_keys()
        for role_name in KEY_NAMES:
            if role_name not in existing_keys:
                added['key_secrets'][role_name] = apikeys.create_key(role_name, role=role_name, example=True)
        _seed_execution_history(added['queries'], now)
    return added


def unload():
    """Remove exactly what is marked as an example: its queries, roles, keys and connection (and the database
    file). Never touches anything unmarked. Reports keys that were granted an example collection, whose grant
    is now inert - a different thing from the example keys removed here, which are the ones *made from* an
    example role, not merely granted one."""
    with store.lock:
        before = status()
        removed = {'connection': False, 'queries': [], 'roles': [], 'keys': []}
        for name in before['queries']:
            store.delete_saved(name)
            removed['queries'].append(name)
        for name in before['roles']:
            apikeys.delete_role(name)
            removed['roles'].append(name)
        for name in before['keys']:
            apikeys.delete_key(name)
            removed['keys'].append(name)
        if before['connection']:
            store.delete_connection(CONNECTION)
            removed['connection'] = True
            try:
                os.remove(config.home() / DB_FILE)
            except FileNotFoundError:
                pass
        grants = apikeys.collection_grants()
        removed['keys_still_granted'] = sorted({key for c in before['collections']
                                                for key in grants.get(c, {}).get('keys', [])})
    return removed
