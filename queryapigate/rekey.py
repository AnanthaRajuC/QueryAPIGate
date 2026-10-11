"""Rotating the key passwords are encrypted with at rest (ADR 0009, BACKLOG #90).

QUERYAPIGATE_SECRET_KEY may list several keys, comma-separated: the first encrypts, every one decrypts
(store._get_fernet()). `report()` says which listed key each stored value is under; `rekey()` re-encrypts every value
under the first key, so the others can then be removed:

1. QUERYAPIGATE_SECRET_KEY=new,old on every instance, restarted one by one;
2. `queryapigate rekey` (or POST /api/v1/settings/rekey);
3. QUERYAPIGATE_SECRET_KEY=new.

What's covered is every `enc:` value the store holds - ENCRYPTED, by table and field. A new encrypted field joins by
being listed there. Hashed secrets (API keys, admin tokens) aren't encrypted, so rotation doesn't touch them.
"""
import json

from . import config, db, store
from .errors import ApiError

# (table, kind shown to people, field in details_json) - each table keyed by `name`
ENCRYPTED = (('connections', 'connection', 'password'), ('destinations', 'destination', 'password'))


def _fernets():
    from cryptography.fernet import Fernet
    return [Fernet(key.encode('utf-8')) for key in config.secret_keys()]


def _key_index(fernets, value):
    """Which listed key decrypts `value` (0 = the first), or None when none does."""
    from cryptography.fernet import InvalidToken
    token = value[len(store._ENC_PREFIX):].encode('ascii')
    for i, fernet in enumerate(fernets):
        try:
            fernet.decrypt(token)
            return i
        except InvalidToken:
            continue
    return None


def _values(conn):
    """Every encrypted value stored: (table, kind, name, field, details, value)."""
    for table, kind, field in ENCRYPTED:
        for row in conn.execute(f'SELECT name, details_json FROM {table} ORDER BY name').fetchall():
            details = json.loads(row['details_json'])
            if store._is_encrypted(details.get(field)):
                yield table, kind, row['name'], field, details, details[field]


def _require_keys():
    if not config.secret_keys():
        raise ApiError('QUERYAPIGATE_SECRET_KEY is not set - there is no key to encrypt with', 409,
                       code='secret_key_required')


def report(conn=None):
    """How many stored values are under each listed key, and which no listed key decrypts. Without a key at all,
    every encrypted value is one nothing decrypts."""
    conn = conn or db.connection()
    fernets = _fernets() if config.secret_keys() else []
    by_key = [0] * len(fernets)
    undecryptable = []
    for _table, kind, name, _field, _details, value in _values(conn):
        index = _key_index(fernets, value)
        if index is None:
            undecryptable.append({'type': kind, 'name': name})
        else:
            by_key[index] += 1
    return {'keys': len(fernets), 'values': sum(by_key) + len(undecryptable), 'by_key': by_key,
            'under_old_keys': sum(by_key[1:]), 'undecryptable': undecryptable}


def _refuse_undecryptable(found):
    if found:
        names = ', '.join(f"{v['type']} '{v['name']}'" for v in found)
        raise ApiError(f'No key in QUERYAPIGATE_SECRET_KEY decrypts the password of {names} - nothing was changed. '
                       'Add the key it was encrypted with (after the first), or enter that password again, then run '
                       'rekey again', 409, code='password_undecryptable', values=found)


def rekey(actor, via=None):
    """Re-encrypt every stored value under the first key, all in one transaction - or, when any value can't be
    decrypted by a listed key, change nothing and say which. A value already under the first key is left as it is,
    so running it again is harmless. Returns report() after, with `rekeyed`: how many values moved."""
    _require_keys()
    from cryptography.fernet import Fernet
    fernets = _fernets()
    first = Fernet(config.secret_keys()[0].encode('utf-8'))
    moved = 0
    # db.transaction() holds the store's write lock (BEGIN IMMEDIATE, or the advisory lock on PostgreSQL), so a
    # connection or destination saved meanwhile waits for this, and nothing read here changes before it's written.
    with db.transaction() as conn:
        pending = []
        undecryptable = []
        for table, kind, name, field, details, value in _values(conn):
            index = _key_index(fernets, value)
            if index is None:
                undecryptable.append({'type': kind, 'name': name})
            elif index > 0:
                pending.append((table, name, field, details, value, index))
        _refuse_undecryptable(undecryptable)
        for table, name, field, details, value, index in pending:
            plain = fernets[index].decrypt(value[len(store._ENC_PREFIX):].encode('ascii'))
            details[field] = store._ENC_PREFIX + first.encrypt(plain).decode('ascii')
            conn.execute(f'UPDATE {table} SET details_json = ? WHERE name = ?', (json.dumps(details), name))
            moved += 1
        after = report(conn)
    store.record_audit(actor, 'rekey', 'secret_key', {'rekeyed': moved, 'values': after['values'],
                                                      'keys': after['keys']}, via=via)
    return {**after, 'rekeyed': moved}


def check():
    """report(), for `rekey --check` and POST /api/v1/settings/rekey with `check` - refusing without any key."""
    _require_keys()
    return report()
