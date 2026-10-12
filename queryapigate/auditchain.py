"""The audit log as a hash chain (ADR 0012, BACKLOG #96): each entry carries `seq`, the previous entry's `hash` as its
`prev_hash`, and its own `hash` over both and the entry - so changing, removing, inserting or reordering an entry
breaks every hash after it.

The hash is HMAC-SHA256 under QUERYAPIGATE_AUDIT_KEY when that is set ("hmac-sha256:<hex>"): someone who can write to
the store but hasn't the key can't re-sign an edit. Without it, plain SHA-256 ("sha256:<hex>"): consistent, but anyone
who knows to can recompute it - a SIEM copy outside the store is what contradicts that. Several keys,
comma-separated, while rotating: the first signs, every one verifies. Once the chain is keyed, an unkeyed entry after
a keyed one is a break, not a change of mind.

Entries are appended inside the insert's transaction, which holds the store's write lock (db.transaction()), so
instances writing at once still make one chain. The rolling cap (QUERYAPIGATE_AUDIT_LOG_LIMIT) trims the oldest; the
anchor (audit_chain) remembers the newest entry trimmed, so the chain's first kept entry must follow it - a deletion
from the head looks different from a trim.
"""
import hashlib
import hmac
import json
import os

GENESIS = '0' * 64
ANCHOR = 'anchor'


def keys():
    """QUERYAPIGATE_AUDIT_KEY's keys, first to last - empty when unset."""
    return [k.strip() for k in os.environ.get('QUERYAPIGATE_AUDIT_KEY', '').split(',') if k.strip()]


def check_keys():
    """At startup: each key long enough to be one (32 characters or more), none twice."""
    found = keys()
    for i, key in enumerate(found):
        which = ('QUERYAPIGATE_AUDIT_KEY' if len(found) == 1
                 else f'Key {i + 1} of {len(found)} in QUERYAPIGATE_AUDIT_KEY')
        if len(key) < 32:
            raise ValueError(f'{which} must be at least 32 characters - e.g. from '
                             '`python -c "import secrets; print(secrets.token_hex(32))"`')
        if key in found[:i]:
            raise ValueError(f'{which} is the same as key {found.index(key) + 1} - list each key once')


def canonical(entry):
    """The entry as hashed: sorted keys, no whitespace - the same bytes from the dict written and the JSON read back."""
    return json.dumps(entry, sort_keys=True, separators=(',', ':'), default=str, ensure_ascii=False)


def _message(prev_hash, seq, entry):
    return f'{prev_hash}\n{seq}\n{canonical(entry)}'.encode()


def compute(prev_hash, seq, entry, key=None):
    if key is None:
        return 'sha256:' + hashlib.sha256(_message(prev_hash, seq, entry)).hexdigest()
    return 'hmac-sha256:' + hmac.new(key.encode(), _message(prev_hash, seq, entry), hashlib.sha256).hexdigest()


def _matches(stored, prev_hash, seq, entry, listed):
    """Whether `stored` is this entry's hash - under any listed key, or unkeyed. Returns (ok, keyed, known): `known`
    False for a keyed hash with no key to check it against."""
    if stored.startswith('sha256:'):
        return hmac.compare_digest(stored, compute(prev_hash, seq, entry)), False, True
    if stored.startswith('hmac-sha256:'):
        if not listed:
            return True, True, False
        return any(hmac.compare_digest(stored, compute(prev_hash, seq, entry, k)) for k in listed), True, True
    return False, False, True


def _head(conn):
    row = conn.execute('SELECT seq, hash FROM audit_log WHERE seq IS NOT NULL ORDER BY seq DESC LIMIT 1').fetchone()
    if row is not None:
        return row[0], row[1]
    anchor = conn.execute('SELECT seq, hash FROM audit_chain WHERE name = ?', (ANCHOR,)).fetchone()
    return (anchor[0], anchor[1]) if anchor is not None else (0, GENESIS)


def append(conn, entry, limit):
    """Insert `entry` as the chain's next link, inside the caller's transaction, and trim to `limit`, moving the anchor.
    Returns (seq, prev_hash, hash). Entries written meanwhile without a link - by an instance still on 0.24, during a
    rolling upgrade - are linked in first, in the order they were written."""
    chain_existing(conn)
    seq, prev_hash = _head(conn)
    seq += 1
    listed = keys()
    digest = compute(prev_hash, seq, entry, listed[0] if listed else None)
    conn.execute('INSERT INTO audit_log (timestamp, entry_json, seq, prev_hash, hash) VALUES (?, ?, ?, ?, ?)',
                 (entry['timestamp'], json.dumps(entry, default=str), seq, prev_hash, digest))
    cut = seq - limit  # keep the newest `limit`: everything up to `cut` goes, and `cut` becomes the anchor
    if cut > 0:
        row = conn.execute('SELECT seq, hash FROM audit_log WHERE seq = ?', (cut,)).fetchone()
        if row is not None:
            _set_anchor(conn, row[0], row[1])
            conn.execute('DELETE FROM audit_log WHERE seq <= ?', (cut,))
    return seq, prev_hash, digest


def _set_anchor(conn, seq, digest):
    if conn.execute('SELECT 1 FROM audit_chain WHERE name = ?', (ANCHOR,)).fetchone() is None:
        conn.execute('INSERT INTO audit_chain (name, seq, hash) VALUES (?, ?, ?)', (ANCHOR, seq, digest))
    else:
        conn.execute('UPDATE audit_chain SET seq = ?, hash = ? WHERE name = ?', (seq, digest, ANCHOR))


def chain_existing(conn):
    """Schema 14: link the entries already stored, oldest first, as they are now - the chain proves nothing about them
    before this moment, which the `audit_chain_started` entry that follows records."""
    rows = conn.execute('SELECT id, entry_json FROM audit_log WHERE seq IS NULL ORDER BY id').fetchall()
    seq, prev_hash = _head(conn)
    listed = keys()
    for row in rows:
        seq += 1
        entry = json.loads(row['entry_json'])
        digest = compute(prev_hash, seq, entry, listed[0] if listed else None)
        conn.execute('UPDATE audit_log SET seq = ?, prev_hash = ?, hash = ? WHERE id = ?',
                     (seq, prev_hash, digest, row['id']))
        prev_hash = digest
    return len(rows)


def _walk(links, anchor):
    """Verify `links` - (seq, prev_hash, hash, entry), in seq order - from `anchor` (seq, hash). Returns the report."""
    listed = keys()
    expected_seq, expected_prev = anchor
    keyed_seen = unchecked = 0
    for checked, (seq, prev_hash, stored, entry) in enumerate(links):
        where = {'seq': seq, 'timestamp': entry.get('timestamp'), 'action': entry.get('action')}
        if seq != expected_seq + 1:
            problem = 'missing' if seq > expected_seq + 1 else 'out_of_order'
            return {'intact': False, 'checked': checked, 'break': {**where, 'problem': problem,
                                                                    'expected_seq': expected_seq + 1}}
        if prev_hash != expected_prev:
            return {'intact': False, 'checked': checked, 'break': {**where, 'problem': 'changed_before'}}
        ok, keyed, known = _matches(stored or '', prev_hash, seq, entry, listed)
        if not ok:
            return {'intact': False, 'checked': checked, 'break': {**where, 'problem': 'changed'}}
        if keyed:
            keyed_seen += 1
            unchecked += 0 if known else 1
        elif keyed_seen:  # unkeyed after keyed: re-written without the key
            return {'intact': False, 'checked': checked, 'break': {**where, 'problem': 'unkeyed_after_keyed'}}
        expected_seq, expected_prev = seq, stored
    if listed and links and not (links[-1][2] or '').startswith('hmac-sha256:'):
        # With a key set the server signs every entry, and says so when it starts (audit_key_set) - so a newest entry
        # that isn't keyed means the chain was re-written without the key, from the start if need be.
        seq, _prev, _stored, entry = links[-1]
        return {'intact': False, 'checked': len(links), 'break': {'seq': seq, 'timestamp': entry.get('timestamp'),
                                                                   'action': entry.get('action'),
                                                                   'problem': 'unkeyed_after_keyed'}}
    return {'intact': True, 'entries': expected_seq - anchor[0], 'first_seq': anchor[0] + 1, 'last_seq': expected_seq,
            'keyed': bool(keyed_seen), 'unchecked_without_key': unchecked}


def verify(conn):
    """The stored chain, from the anchor: {'intact': True, ...} or {'intact': False, 'break': {...}}."""
    anchor = conn.execute('SELECT seq, hash FROM audit_chain WHERE name = ?', (ANCHOR,)).fetchone()
    rows = conn.execute('SELECT seq, prev_hash, hash, entry_json FROM audit_log ORDER BY seq').fetchall()
    links = [(r['seq'], r['prev_hash'], r['hash'], json.loads(r['entry_json'])) for r in rows]
    report = _walk(links, (anchor[0], anchor[1]) if anchor is not None else (0, GENESIS))
    report['entries_stored'] = len(links)
    return report


def verify_file(path):
    """An export file (QUERYAPIGATE_AUDIT_LOG_EXPORT_FILE), one entry per line with its chain fields - from its first
    line, whose `prev_hash` is taken as given (the file may have started after the chain did)."""
    links = []
    with open(path) as f:
        for number, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                data = json.loads(line)
            except ValueError:
                return {'intact': False, 'break': {'line': number, 'problem': 'not_json'}}
            if 'seq' not in data:
                continue  # written before the chain existed
            chain = (data.pop('seq'), data.pop('prev_hash', None), data.pop('hash', None))
            links.append((*chain, data))
    if not links:
        return {'intact': True, 'entries': 0}
    return _walk(links, (links[0][0] - 1, links[0][1]))


def head_keyed(conn):
    """Whether the newest entry is signed with a key at all (False for none, or an empty log)."""
    row = conn.execute('SELECT hash FROM audit_log ORDER BY seq DESC LIMIT 1').fetchone()
    return row is not None and (row['hash'] or '').startswith('hmac-sha256:')


def head_signed_by(conn):
    """Which listed key signed the newest entry - 0 for the first, None for none (unkeyed, or no key verifies it)."""
    row = conn.execute('SELECT seq, prev_hash, hash, entry_json FROM audit_log ORDER BY seq DESC LIMIT 1').fetchone()
    if row is None or not (row['hash'] or '').startswith('hmac-sha256:'):
        return None
    entry = json.loads(row['entry_json'])
    for i, key in enumerate(keys()):
        if hmac.compare_digest(row['hash'], compute(row['prev_hash'], row['seq'], entry, key)):
            return i
    return None


_checked = {'at': 0.0, 'report': None}


def cached_verify(conn, max_age=300.0):
    """verify(), at most once every `max_age` seconds per process - for the alert, which the Console asks for often."""
    import time
    if _checked['report'] is None or time.monotonic() - _checked['at'] >= max_age:
        _checked['report'], _checked['at'] = verify(conn), time.monotonic()
    return _checked['report']
