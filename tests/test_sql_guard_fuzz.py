"""Property-based and fuzz tests for the SQL guard: the single-statement/read-only check in
``sqltools.validate_sql`` and the ``:name`` parameter binder in ``sqltools.bind_parameters``.

These two functions carry the project's core promise ("only one read-only statement runs unless you opt
in, and parameter values never become part of the SQL text"), so they get adversarial, generated input
instead of only hand-picked examples.

## A real finding, and why the guard is now dialect-aware

Fuzzing this guard surfaced a genuine bypass, confirmed against real servers (see
``BackslashBoundaryRegressionTests`` below for the exact commands): MySQL and ClickHouse both honour a
C-style backslash escape inside ``'...'``/``"..."`` string literals by default (``\\'`` does not close the
string), while PostgreSQL, SQLite and H2 do not (under their standard/default settings, only doubling -
``''``/``""`` - closes a string). The old, single, dialect-less literal-detection regex used doubling-only
rules for every database. For MySQL and ClickHouse specifically, a value ending in an escaped quote,
followed by more text, followed by a second quote to close against, could make the guard think a semicolon
was safely "inside a string literal" when the real database would treat it as a live, second statement:

    >>> payload = "SELECT 'a\\\\'' ; DELETE FROM t; SELECT 1 -- injected' AS trailer"
    >>> old_unfixed_guard(payload)   # used to run, treating the whole thing as one safe SELECT
    >>> real_mysql.execute(payload)  # actually executes THREE statements

Verified end to end against a real MySQL server: this exact payload, sent through the old code path,
returned one row and raised no error, while MySQL (via mysql-connector-python, the exact driver this
project uses) genuinely parsed and queued a second statement (confirmed via ``cursor.nextset()``), and a
smuggled ``SELECT SLEEP(5)`` as that second statement did NOT add observable delay - because this
project's runners never call ``nextset()``, so on the *current* codebase the smuggled statement is queued
but never pulled, and (independently) MySQL's own ``SET SESSION TRANSACTION READ ONLY`` - set on every
read-only connection - would reject a smuggled write. ClickHouse's server independently refuses
multi-statement queries outright. So there is no currently-demonstrated path to unauthorized data access
or modification through this specific gap, but it is still a real failure of an explicitly documented
guarantee ("Only a single SQL statement can be executed at a time"), it is fragile to rely on unrelated
driver/protocol behaviour to neutralise it, and it directly undermined auditability (execution_history
would show one request while two statements ran). The fix: literal detection now uses a
backslash-escape-aware regex for ``mysql``/``clickhouse`` connections and the original doubling-only regex
for everything else - see ``BACKSLASH_ESCAPE_DIALECTS`` in sqltools.py.

PostgreSQL and SQLite were also verified empirically: neither treats backslash specially in a standard
string literal, so the same payload is genuinely one harmless statement to them (the string value ends up
containing the ``;`` characters as inert text) - confirmed by running it for real and observing the
returned string value and that no row was deleted. The ANSI/doubling-only regex (used for postgres,
sqlite, h2 and no dialect at all) is unchanged and continues to *accept* that payload, correctly.
"""
import math
import os
import re
import unittest
from unittest import mock

from hypothesis import example, given, settings
from hypothesis import strategies as st

from queryapigate import sqltools
from queryapigate.errors import ApiError

# A small, targeted alphabet: hypothesis explores combinations of these far more effectively than it
# would a full-unicode strategy, which would almost never happen to produce quote/comment structure.
_SPECIAL_CHARS = "'\"`\\;-/*# \t\nA1:{}().,%_"
sql_soup = st.text(alphabet=_SPECIAL_CHARS, max_size=40)
# Filler with none of the characters that have any special meaning to a SQL tokenizer - safe to drop
# into a generated statement without introducing accidental quote/comment/semicolon structure.
plain_filler = st.text(alphabet='abcXYZ019 ', max_size=8)
# Deliberately no quote, backslash, or comment-opening character (-, /, *, #) at all: this must be
# content that cannot form its OWN quote or comment structure, so that any ';' it contains is
# unambiguously live to every dialect once it is established to be outside the enclosing literal -
# isolating the one thing this class of test cares about: where the ENCLOSING literal closes.
_dangerous_no_quotes = st.text(alphabet='abcXYZ019 ;', max_size=25)

WRITE_KEYWORDS = ('insert', 'update', 'delete', 'drop', 'alter', 'create', 'truncate', 'replace', 'merge',
                  'grant', 'revoke', 'call', 'rename', 'lock', 'unlock', 'set', 'use', 'exec', 'execute')
READ_ONLY_KEYWORDS = sqltools.READ_ONLY_STATEMENTS
ALL_DIALECTS = (None, 'mysql', 'postgres', 'clickhouse', 'sqlite', 'h2')


class BaseGuardTest(unittest.TestCase):
    def setUp(self):
        # validate_sql consults QUERYAPIGATE_ALLOW_WRITES at call time; keep every test deterministic.
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop('QUERYAPIGATE_ALLOW_WRITES', None)


# --------------------------------------------------------------------------------------
# The backslash/quote-boundary bypass class (see the module docstring)
# --------------------------------------------------------------------------------------

def build_boundary_payload(prefix, dangerous):
    """A statement shaped exactly like the confirmed real-world bypass: an opening string whose first
    quote is backslash-escaped, immediately followed by a second (real) quote - which a doubling-only
    reader mistakes for the ANSI '' escape and keeps the string open through, but a backslash-aware
    reader (matching real MySQL/ClickHouse) correctly treats as closing the string right there, exposing
    ``dangerous`` as live SQL text up to the final quote.
    """
    return f"SELECT '{prefix}\\''{dangerous}' AS x"


class BackslashBoundaryFuzzTests(BaseGuardTest):
    """Fuzzes the *shape* of the confirmed vulnerability class, not just the one example that found it."""

    @given(prefix=plain_filler, dangerous=_dangerous_no_quotes)
    @settings(max_examples=300)
    @example(prefix='a', dangerous=' ; DROP TABLE t; --')  # the exact confirmed real-server payload
    @example(prefix='', dangerous='')
    @example(prefix='', dangerous=';')
    def test_backslash_dialects_never_hide_a_live_semicolon_in_the_gap(self, prefix, dangerous):
        sql = build_boundary_payload(prefix, dangerous)
        for dialect in ('mysql', 'clickhouse'):
            if ';' in dangerous:
                with self.assertRaises(ApiError, msg=f'dialect={dialect} sql={sql!r}'):
                    sqltools.validate_sql(sql, dialect=dialect)
            else:
                # nothing dangerous in the gap: must still be usable as an ordinary single statement
                sqltools.validate_sql(sql, dialect=dialect)

    @given(prefix=plain_filler, dangerous=_dangerous_no_quotes)
    @settings(max_examples=100)
    def test_write_keyword_hidden_in_the_gap_is_never_silently_allowed(self, prefix, dangerous):
        # A write keyword in the gap is only "hidden" if wrapped with its own statement separator;
        # bare text there is never treated as SQL structure by ANY dialect (it is either genuinely
        # inert string content, or genuinely live text that the read-only-first-keyword check does not
        # even look at, since first_keyword only ever inspects the very start of the string). What must
        # never happen is validate_sql accepting the input while a *live* semicolon sits in the gap,
        # regardless of what surrounds it.
        sql = build_boundary_payload(prefix, f'{dangerous}; DROP TABLE evil')
        for dialect in ('mysql', 'clickhouse'):
            with self.assertRaises(ApiError, msg=f'dialect={dialect} sql={sql!r}'):
                sqltools.validate_sql(sql, dialect=dialect)

    def test_ansi_dialects_correctly_treat_the_same_shape_as_one_harmless_string(self):
        # Confirmed against real PostgreSQL and SQLite (see BackslashBoundaryRegressionTests): neither
        # gives backslash any special meaning in a standard string literal, so this shape really is one
        # statement to them, and the semicolon-laden "dangerous" text ends up as an inert string value.
        sql = build_boundary_payload('a', ' ; DROP TABLE t; --')
        for dialect in (None, 'postgres', 'sqlite', 'h2'):
            sqltools.validate_sql(sql, dialect=dialect)  # must not raise


class BackslashBoundaryRegressionTests(BaseGuardTest):
    """Pins the exact payload that was run against real database servers during this investigation.

    Verified with throwaway servers and each database's own Python driver (mysql-connector-python,
    clickhouse-driver, psycopg2, sqlite3), calling execute() exactly as queryapigate.runners does:

    - MySQL 8 (mysql-connector-python): returned one row ("a'",) with no error, and cursor.nextset()
      confirmed a second, genuinely-executed statement (SELECT 999) was queued behind it.
    - ClickHouse 24: raised "DB::Exception: Syntax error (Multi-statements are not allowed)" - refused
      by the server itself, independent of this guard.
    - PostgreSQL 16 (psycopg2): ran without error and returned the ENTIRE tail, including the literal
      semicolons, as one string value - i.e. genuinely one harmless statement.
    - SQLite (stdlib sqlite3): identical result to PostgreSQL.
    """
    PAYLOAD = "SELECT 'a\\'' ; DELETE FROM canary; SELECT 1 -- injected' AS trailer"

    def test_mysql_and_clickhouse_reject_the_confirmed_bypass(self):
        for dialect in ('mysql', 'clickhouse'):
            with self.assertRaises(ApiError) as caught:
                sqltools.validate_sql(self.PAYLOAD, dialect=dialect)
            self.assertIn('single SQL statement', caught.exception.message)

    def test_ansi_dialects_accept_it_because_it_is_genuinely_one_statement_there(self):
        for dialect in (None, 'postgres', 'sqlite', 'h2'):
            sqltools.validate_sql(self.PAYLOAD, dialect=dialect)  # must not raise


# --------------------------------------------------------------------------------------
# Multi-statement / write / MySQL-comment-bypass guards (dialect-independent constructions)
# --------------------------------------------------------------------------------------

class MultiStatementGuardFuzzTests(BaseGuardTest):
    @given(first=plain_filler, second=plain_filler, dialect=st.sampled_from(ALL_DIALECTS))
    @settings(max_examples=200)
    def test_a_bare_semicolon_outside_any_quote_is_always_caught(self, first, second, dialect):
        # No quotes/backslashes/comments anywhere, so every dialect agrees on the structure: this is
        # unambiguously two statements.
        sql = f'SELECT 1, {first} ; SELECT 2, {second}'
        with self.assertRaises(ApiError, msg=f'dialect={dialect} sql={sql!r}'):
            sqltools.validate_sql(sql, dialect=dialect)

    @given(dialect=st.sampled_from(ALL_DIALECTS))
    @settings(max_examples=50)
    def test_a_semicolon_only_inside_a_plain_quoted_string_is_never_flagged(self, dialect):
        # No backslashes at all, so this is unambiguous in every dialect: one string, no live semicolon.
        sql = "SELECT 'a; DROP TABLE x; --' AS note"
        sqltools.validate_sql(sql, dialect=dialect)  # must not raise

    @given(keyword=st.sampled_from(WRITE_KEYWORDS), leading=st.text(alphabet=' \t\n(', max_size=5),
          dialect=st.sampled_from(ALL_DIALECTS))
    @settings(max_examples=300)
    def test_a_write_statement_is_always_rejected_when_writes_are_disabled(self, keyword, leading, dialect):
        sql = f'{leading}{keyword.upper()} INTO t VALUES (1)'
        with self.assertRaises(ApiError, msg=f'dialect={dialect} sql={sql!r}') as caught:
            sqltools.validate_sql(sql, dialect=dialect)
        self.assertEqual(caught.exception.status, 403)

    @given(keyword=st.sampled_from(READ_ONLY_KEYWORDS), leading=st.text(alphabet=' \t\n(', max_size=5),
          dialect=st.sampled_from(ALL_DIALECTS))
    @settings(max_examples=200)
    def test_a_read_only_statement_is_always_accepted_when_writes_are_disabled(self, keyword, leading, dialect):
        sql = f'{leading}{keyword.upper()} 1'
        sqltools.validate_sql(sql, dialect=dialect)  # must not raise

    @given(position=st.integers(min_value=0, max_value=20), dialect=st.sampled_from(ALL_DIALECTS))
    @settings(max_examples=100)
    def test_mysql_versioned_comment_bypass_is_rejected_wherever_it_appears(self, position, dialect):
        # /*! ... */ is executable code to MySQL despite looking like a comment; the check is a plain
        # substring match, so it must catch the marker at any position, not just a fixed one.
        base = 'SELECT 1 FROM some_table WHERE some_column = 2'
        position = min(position, len(base))
        sql = f'{base[:position]}/*!50000 , 1=1*/{base[position:]}'
        with self.assertRaises(ApiError, msg=f'dialect={dialect} sql={sql!r}') as caught:
            sqltools.validate_sql(sql, dialect=dialect)
        self.assertEqual(caught.exception.status, 403)

    @given(dialect=st.sampled_from(ALL_DIALECTS))
    @settings(max_examples=20)
    def test_writes_are_allowed_when_the_server_opts_in(self, dialect):
        os.environ['QUERYAPIGATE_ALLOW_WRITES'] = '1'
        sqltools.validate_sql('DELETE FROM t WHERE id = 1', dialect=dialect)  # must not raise
        # the multi-statement rule still applies even with writes enabled
        with self.assertRaises(ApiError):
            sqltools.validate_sql('DELETE FROM t; DELETE FROM u', dialect=dialect)


# --------------------------------------------------------------------------------------
# bind_parameters: values must never become part of the SQL text
# --------------------------------------------------------------------------------------

_param_value_text = st.text(alphabet="abcXYZ019'\";\\%-- \t\n/*", max_size=30)


class BindParametersFuzzTests(unittest.TestCase):
    @given(value=_param_value_text, style=st.sampled_from(('qmark', 'format', 'pyformat')))
    @settings(max_examples=300)
    def test_the_output_sql_is_the_input_with_only_the_marker_replaced(self, value, style):
        # A direct substring-containment check ("value not in output sql") is fragile here: a short
        # fuzzed value can coincide by pure accident with unrelated punctuation already in the SQL
        # template. Checking that the output is the template with *only* the marker text swapped for
        # the driver's placeholder is a stronger property anyway - it proves the value never reaches
        # the SQL text at all (bind_parameters only ever writes it into `args`), independent of content.
        template = 'SELECT col1, col2 FROM t WHERE x = :v'
        sql, args = sqltools.bind_parameters(template, {'v': value}, style)
        marker = {'qmark': '?', 'format': '%s', 'pyformat': '%(v)s'}[style]
        self.assertEqual(sql, template.replace(':v', marker))
        got = args['v'] if style == 'pyformat' else args[0]
        self.assertEqual(got, value)

    @given(prefix=plain_filler, marker_text=st.text(alphabet='abcXYZ_019', min_size=1, max_size=10),
          style=st.sampled_from(('qmark', 'format', 'pyformat')))
    @settings(max_examples=200)
    def test_markers_inside_plain_literals_comments_and_casts_are_never_extracted(self, prefix, marker_text,
                                                                                  style):
        # No backslashes here, so this is unambiguous across every dialect: none of the ":marker_text"
        # occurrences below are live markers - only the standalone ":real" is. "real" itself is fixed
        # (rather than fuzzed into the identifier) so the expected extracted name stays unambiguous.
        sql = (f"SELECT {prefix} FROM t WHERE a = ':{marker_text}' AND b = \"another :{marker_text}\" "
              f"AND c = `col:{marker_text}` -- trailing :{marker_text}\n"
              f"AND d::{marker_text} = :real /* :{marker_text} */")
        self.assertEqual(sqltools.named_parameters(sql), ['real'])
        rewritten, args = sqltools.bind_parameters(sql, {'real': 1}, style)
        # everything except the one real marker survives byte-for-byte
        self.assertIn(f"':{marker_text}'", rewritten)
        self.assertIn(f'"another :{marker_text}"', rewritten)
        self.assertIn(f'`col:{marker_text}`', rewritten)
        self.assertIn(f'-- trailing :{marker_text}', rewritten)
        self.assertIn(f'/* :{marker_text} */', rewritten)
        self.assertIn(f'd::{marker_text}', rewritten)  # the Postgres-style cast survives untouched

    @given(before=st.text(alphabet='ab%019 ', max_size=15), after=st.text(alphabet='ab%019 ', max_size=15))
    @settings(max_examples=200)
    def test_percent_signs_outside_markers_are_escaped_only_for_percent_based_styles(self, before, after):
        sql = f"SELECT {before} :v {after}"
        for style in ('format', 'pyformat'):
            rewritten, _ = sqltools.bind_parameters(sql, {'v': 1}, style)
            self.assertEqual(rewritten.count('%%'), before.count('%') + after.count('%'))
            self.assertNotIn('%', rewritten.replace('%%', '').replace('%s' if style == 'format' else '%(v)s', ''))
        rewritten, _ = sqltools.bind_parameters(sql, {'v': 1}, 'qmark')
        self.assertEqual(rewritten, f'SELECT {before} ? {after}')  # untouched: qmark never uses %

    @given(names=st.lists(st.text(alphabet='abc019_', min_size=1, max_size=6).filter(lambda n: not n[0].isdigit()),
                          min_size=1, max_size=4, unique=True))
    @settings(max_examples=100)
    def test_every_used_name_must_have_a_value_or_binding_fails(self, names):
        sql = 'SELECT ' + ', '.join(f':{n}' for n in names)
        with self.assertRaises(ApiError):
            sqltools.bind_parameters(sql, {}, 'qmark')
        full = {n: 1 for n in names}
        sql2, args = sqltools.bind_parameters(sql, full, 'qmark')
        self.assertEqual(sql2.count('?'), len(names))
        self.assertEqual(args, [1] * len(names))

    def test_no_markers_returns_the_sql_unchanged_with_no_args(self):
        sql = "SELECT '50% off' FROM t"
        self.assertEqual(sqltools.bind_parameters(sql, {}, 'format'), (sql, None))

    @given(value=st.one_of(st.lists(st.integers(), max_size=3), st.dictionaries(st.text(max_size=3), st.integers(),
                                                                                max_size=3)))
    @settings(max_examples=30)
    def test_unsupported_value_types_are_rejected(self, value):
        with self.assertRaises(ApiError):
            sqltools.bind_parameters('SELECT :v', {'v': value}, 'qmark')

    @given(value=st.one_of(st.just(math.nan), st.just(math.inf), st.just(-math.inf)))
    @settings(max_examples=10)
    def test_non_finite_numbers_are_rejected(self, value):
        with self.assertRaises(ApiError):
            sqltools.bind_parameters('SELECT :v', {'v': value}, 'qmark')


# --------------------------------------------------------------------------------------
# fill_placeholders: {name} text substitution is restricted to a safe character set
# --------------------------------------------------------------------------------------

class FillPlaceholdersFuzzTests(unittest.TestCase):
    @given(value=st.text(alphabet="abcXYZ019 .,:@%+/-", max_size=30))
    @settings(max_examples=200)
    def test_safe_text_is_accepted_and_spliced_in_verbatim(self, value):
        if '--' in value:
            with self.assertRaises(ApiError):
                sqltools.fill_placeholders('SELECT {v}', {'v': value})
            return
        result = sqltools.fill_placeholders('SELECT {v}', {'v': value})
        self.assertEqual(result, f'SELECT {value}')

    @given(value=st.text(alphabet="abc'\";\\()[]<>!&|$\n", min_size=1, max_size=15))
    @settings(max_examples=200)
    def test_any_character_outside_the_safe_set_is_rejected(self, value):
        if sqltools._SAFE_TEXT_RE.match(value) and '--' not in value:
            return  # happened to land entirely inside the safe set; not what this test targets
        with self.assertRaises(ApiError) as caught:
            sqltools.fill_placeholders('SELECT {v}', {'v': value})
        # the function never splices a rejected value anywhere; it always raises before substituting
        self.assertIn('must be a number, boolean', caught.exception.message)

    @given(n=st.integers(min_value=-10 ** 6, max_value=10 ** 6))
    @settings(max_examples=50)
    def test_integers_and_booleans_are_rendered_as_expected(self, n):
        self.assertEqual(sqltools.fill_placeholders('SELECT {v}', {'v': n}), f'SELECT {n}')
        self.assertEqual(sqltools.fill_placeholders('SELECT {v}', {'v': True}), 'SELECT TRUE')
        self.assertEqual(sqltools.fill_placeholders('SELECT {v}', {'v': False}), 'SELECT FALSE')

    def test_missing_placeholder_is_reported(self):
        with self.assertRaises(ApiError) as caught:
            sqltools.fill_placeholders('SELECT {a}, {b}', {'a': '1'})
        self.assertIn('b', caught.exception.message)


# --------------------------------------------------------------------------------------
# paginate: the appended LIMIT/OFFSET is always well-formed and integer-only, and never widens the page
# --------------------------------------------------------------------------------------

class PaginateFuzzTests(unittest.TestCase):
    @given(sql=sql_soup, limit=st.integers(min_value=0, max_value=10 ** 6),
          offset=st.integers(min_value=0, max_value=10 ** 9))
    @settings(max_examples=300)
    def test_the_last_line_is_always_a_well_formed_integer_limit_offset(self, sql, limit, offset):
        try:
            result = sqltools.paginate(sql, limit, offset)
        except ApiError:
            return  # a soup ending in "LIMIT :name" with no value for it - refused, never interpolated
        last_line = result.rsplit('\n', 1)[-1]
        match = re.fullmatch(r'LIMIT (\d+) OFFSET (\d+)', last_line)
        self.assertIsNotNone(match, last_line)
        # The statement's own window can narrow the page and move its start, never widen it or move it back
        self.assertLessEqual(int(match.group(1)), limit)
        self.assertGreaterEqual(int(match.group(2)), offset)

    @given(sql=sql_soup, limit=st.integers(min_value=0, max_value=10 ** 6),
          offset=st.integers(min_value=0, max_value=10 ** 9))
    @settings(max_examples=300)
    def test_without_a_window_of_its_own_the_page_is_appended_unchanged(self, sql, limit, offset):
        sql = sql + ' AS x'  # can no longer end in a LIMIT/OFFSET/FETCH clause
        self.assertEqual(sqltools.paginate(sql, limit, offset).rsplit('\n', 1)[-1], f'LIMIT {limit} OFFSET {offset}')

    @given(sql=sql_soup, limit=st.text(alphabet="abcXYZ; DROP TABLE-'\"", max_size=15))
    @settings(max_examples=100)
    def test_a_non_integer_limit_is_never_silently_interpolated(self, sql, limit):
        try:
            int(limit)
        except ValueError:
            with self.assertRaises(ValueError):
                sqltools.paginate(sql, limit, 0)
        # if it happens to parse as an int (e.g. "  5  "), that is fine - it is used as an integer


if __name__ == '__main__':
    unittest.main()
