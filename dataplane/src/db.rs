//! Running a plan's statement on PostgreSQL or MySQL as Python's runner does (runners.py, ADR 0027) - one message per
//! query on PostgreSQL, read-only per transaction on MySQL - with each parameter's value written into the SQL as a
//! literal, as both of Python's drivers do client-side.
//!
//! Where this differs from the drivers, it is to stay safe without the session state they track: psycopg2 and
//! mysql-connector quote a string for the session's current `standard_conforming_strings`, encoding or `sql_mode`, which
//! a `SELECT set_config(...)` can change for the next call on the same connection. This quotes so that none of those
//! matter - PostgreSQL strings as pure-ASCII `E'...'` with `\u` escapes, MySQL strings as `_utf8mb4 X'...'` - and ends
//! each PostgreSQL message with `RESET ALL`, so the next message is parsed with nothing a query left behind.

use std::collections::HashMap;
use std::str::FromStr;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde::Deserialize;
use serde_json::{Map, Value as Json};
use sqlx::mysql::{MySqlConnectOptions, MySqlPool, MySqlPoolOptions};
use sqlx::postgres::{PgConnectOptions, PgPool, PgPoolOptions};
use sqlx::{Column, Executor, Row, TypeInfo, ValueRef};

use crate::params::{Param, Supplied};
use crate::pyfmt;

/// config.CONNECT_TIMEOUT
const CONNECT_TIMEOUT: Duration = Duration::from_secs(10);
/// A connection's TLS settings (runners.POSTGRES_TLS, MYSQL_TLS): the prototype leaves connections with any to Python.
const TLS_KEYS: [&str; 12] = [
    "sslmode",
    "sslrootcert",
    "sslcert",
    "sslkey",
    "sslcrl",
    "ssl_ca",
    "ssl_cert",
    "ssl_key",
    "ssl_verify_cert",
    "ssl_verify_identity",
    "ssl_disabled",
    "auth_plugin",
];
/// runners._MYSQL_TIMEOUT_ERRNOS: MySQL's MAX_EXECUTION_TIME, MariaDB's max_statement_time.
const MYSQL_TIMEOUT_ERRNOS: [&str; 2] = ["3024", "1969"];

#[derive(Debug)]
pub enum Failure {
    /// Something this doesn't do as Python would: forward the call.
    Decline,
    /// The time limit cancelled it: Python's 504.
    Timeout,
    /// The database refused or failed - forwarded, so Python answers it as it would. The text is for the probe.
    Database(#[allow(dead_code)] String),
}

// --------------------------------------------------------------------------------------
// Where to connect
// --------------------------------------------------------------------------------------

#[derive(Clone, Debug, PartialEq)]
pub struct Target {
    pub dialect: Dialect,
    pub host: String,
    pub port: u16,
    pub user: String,
    pub password: Option<String>,
    pub database: Option<String>,
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Dialect {
    Postgres,
    Mysql,
}

/// A stored connection's details (`connections.details_json`) as the runner connects with them (runners._connect_args),
/// or None for any the prototype leaves to Python: a `${...}` reference, a credential source, TLS settings, a
/// password it can't decrypt.
pub fn target(dialect: &str, details: &str, secret_keys: &[String]) -> Option<Target> {
    let dialect = match dialect {
        "postgres" => Dialect::Postgres,
        "mysql" => Dialect::Mysql,
        _ => return None,
    };
    let details: Map<String, Json> = serde_json::from_str(details).ok()?;
    if details.contains_key("credentials") || TLS_KEYS.iter().any(|key| details.contains_key(*key))
    {
        return None;
    }
    if details
        .values()
        .any(|value| value.as_str().is_some_and(|text| text.contains("${")))
    {
        return None;
    }
    let text = |key: &str| -> Option<Option<String>> {
        match details.get(key) {
            None | Some(Json::Null) => Some(None),
            Some(Json::String(text)) if text.is_empty() => Some(None),
            Some(Json::String(text)) => Some(Some(text.clone())),
            Some(_) => None,
        }
    };
    let host = text("host")??;
    let user = text("user")??; // without one libpq and mysql-connector each pick their own default
    let port = match details.get("port") {
        None | Some(Json::Null) => {
            if dialect == Dialect::Postgres {
                5432
            } else {
                3306
            }
        }
        Some(Json::String(text)) if text.is_empty() => {
            if dialect == Dialect::Postgres {
                5432
            } else {
                3306
            }
        }
        Some(Json::String(text)) => text.parse().ok()?,
        Some(Json::Number(n)) => n.to_string().parse().ok()?,
        Some(_) => return None,
    };
    let password = match text("password")? {
        Some(stored) => match stored.strip_prefix("enc:") {
            Some(token) => Some(decrypt(token, secret_keys)?),
            None => Some(stored),
        },
        None => None,
    };
    Some(Target {
        dialect,
        host,
        port,
        user,
        password,
        database: text("database")?,
    })
}

/// store._decrypt_password(): Fernet, with whichever of QUERYAPIGATE_SECRET_KEY's keys opens it.
fn decrypt(token: &str, keys: &[String]) -> Option<String> {
    keys.iter().find_map(|key| {
        let fernet = fernet::Fernet::new(key)?;
        String::from_utf8(fernet.decrypt(token).ok()?).ok()
    })
}

// --------------------------------------------------------------------------------------
// The statement, per call
// --------------------------------------------------------------------------------------

#[derive(Clone, Debug, Deserialize)]
#[serde(untagged)]
pub enum Segment {
    Text(String),
    Param { param: String },
}

#[derive(Clone, Debug, Deserialize)]
#[serde(untagged)]
pub enum Operand {
    Value { value: i64 },
    Param { param: String },
}

/// A plan's statement (dataplane.statement() in Python).
#[derive(Clone, Debug, Deserialize)]
pub struct Statement {
    pub segments: Vec<Segment>,
    pub paginated: bool,
    pub limit: Option<Operand>,
    pub offset: Option<Operand>,
    pub hint_at: Option<usize>,
}

/// A value as SQL text for `dialect` - or None for one Python couldn't bind either (sqltools._check_value()), or a
/// string PostgreSQL can't hold (a NUL), which then forwards.
pub fn literal(value: &Param, dialect: Dialect) -> Option<String> {
    Some(match value {
        Param::Null => "NULL".into(),
        Param::Bool(b) => match (dialect, b) {
            (Dialect::Postgres, true) => "true".into(),
            (Dialect::Postgres, false) => "false".into(),
            (Dialect::Mysql, true) => "True".into(),
            (Dialect::Mysql, false) => "False".into(),
        },
        // A space before a negative number, as psycopg2 writes it, so `x-:n` can never become `x--5`, a comment
        Param::Int(n) if *n < 0 => format!(" {n}"),
        Param::Int(n) => n.to_string(),
        Param::Float(f) if !f.is_finite() => return None,
        Param::Float(f) if f.is_sign_negative() => format!(" {}", pyfmt::repr(*f)),
        Param::Float(f) => pyfmt::repr(*f),
        Param::Text(text) => match dialect {
            Dialect::Postgres => postgres_string(text)?,
            Dialect::Mysql => mysql_string(text),
        },
    })
}

/// `E'...'` in ASCII alone: quotes and backslashes doubled, every other character outside printable ASCII a `\u`
/// escape - read the same whatever the session's standard_conforming_strings and client_encoding.
fn postgres_string(text: &str) -> Option<String> {
    let mut out = String::with_capacity(text.len() + 4);
    out.push_str("E'");
    for c in text.chars() {
        match c {
            '\0' => return None, // a PostgreSQL text value can't hold one: psycopg2 refuses it too
            '\'' => out.push_str("''"),
            '\\' => out.push_str("\\\\"),
            ' '..='~' => out.push(c),
            c if (c as u32) <= 0xFFFF => out.push_str(&format!("\\u{:04X}", c as u32)),
            c => out.push_str(&format!("\\U{:08X}", c as u32)),
        }
    }
    out.push('\'');
    Some(out)
}

/// `_utf8mb4 X'...'`: the text's bytes in hex - read the same whatever the session's sql_mode and character set - as a
/// utf8mb4 string, with its default collation (checked equal to the connection's when the pool starts).
fn mysql_string(text: &str) -> String {
    format!("_utf8mb4 X'{}'", hex::encode_upper(text.as_bytes()))
}

/// sqltools._window_value(): a window operand as a non-negative integer - a literal, or a bound value that is an
/// integer or a string of ASCII digits. Anything else Python refuses, or reads as this doesn't (`'٣'`): forward.
fn window_value(operand: &Operand, values: &HashMap<String, Supplied>) -> Option<i64> {
    match operand {
        Operand::Value { value } => Some(*value),
        Operand::Param { param } => match values.get(param)? {
            Supplied::Value(Param::Int(n)) if *n >= 0 => Some(*n),
            Supplied::Value(Param::Text(t))
                if !t.is_empty() && t.bytes().all(|b| b.is_ascii_digit()) =>
            {
                t.parse().ok()
            }
            _ => None,
        },
    }
}

/// The text the runner sends for this call (runners._prepare(), the MySQL hint, sqltools.paginate()) - before
/// PostgreSQL's prefix. `hint`: what goes after MySQL's leading SELECT.
pub fn build(
    statement: &Statement,
    values: &HashMap<String, Supplied>,
    dialect: Dialect,
    limit: i64,
    offset: i64,
    hint: Option<&str>,
) -> Option<String> {
    let mut sql = String::new();
    for (i, segment) in statement.segments.iter().enumerate() {
        match segment {
            Segment::Param { param } => match values.get(param)? {
                Supplied::Value(value) => sql.push_str(&literal(value, dialect)?),
                Supplied::Unsupported => return None,
            },
            Segment::Text(text) if i == 0 && hint.is_some() => {
                let at = statement.hint_at?;
                if !text.is_char_boundary(at) {
                    return None;
                }
                sql.push_str(&text[..at]);
                sql.push(' ');
                sql.push_str(hint.expect("checked"));
                sql.push_str(&text[at..]);
            }
            Segment::Text(text) => sql.push_str(text),
        }
    }
    if statement.paginated {
        let window = limit + 1; // one more row than the page: has_more
        let own_limit = match &statement.limit {
            Some(operand) => Some(window_value(operand, values)?),
            None => None,
        };
        let own_offset = match &statement.offset {
            Some(operand) => window_value(operand, values)?,
            None => 0,
        };
        let window = match own_limit {
            None => window,
            Some(own) => 0.max(window.min(own - offset)),
        };
        sql.push_str(&format!("\nLIMIT {window} OFFSET {}", own_offset + offset));
    }
    Some(sql)
}

/// runners._millis()
fn millis(timeout: f64) -> i64 {
    1.max((timeout * 1000.0) as i64)
}

/// runners._postgres_prefix(), and what this adds: the session's own TimeZone and DateStyle, which sqlx's startup
/// replaces with UTC and `ISO, MDY` (so `RESET ALL` alone would bring those back), set as psycopg2's session has them.
fn postgres_prefix(defaults: &Defaults, timeout: Option<f64>, search_path: Option<&str>) -> String {
    let quote = |text: &str| format!("'{}'", text.replace('\'', "''"));
    let mut prefix = format!(
        "RESET ALL; SET LOCAL TimeZone = {}; SET LOCAL DateStyle = {}; SET LOCAL DateStyle = ISO; \
         SET TRANSACTION READ ONLY; ",
        quote(&defaults.time_zone),
        quote(&defaults.date_style)
    );
    if let Some(timeout) = timeout.filter(|t| *t > 0.0) {
        prefix.push_str(&format!(
            "SET LOCAL statement_timeout = {}; ",
            millis(timeout)
        ));
    }
    if let Some(path) = search_path {
        prefix.push_str(&format!(
            "SET LOCAL search_path = \"{}\"; ",
            path.replace('"', "\"\"")
        ));
    }
    prefix
}

// --------------------------------------------------------------------------------------
// Connections
// --------------------------------------------------------------------------------------

/// What a PostgreSQL session has before sqlx's startup changes it, learnt once per pool over a connection that sends
/// nothing but client_encoding - as libpq, and so psycopg2, does.
#[derive(Clone, Debug)]
pub struct Defaults {
    pub time_zone: String,
    pub date_style: String,
}

pub enum Pool {
    Postgres { pool: PgPool, defaults: Defaults },
    Mysql { pool: MySqlPool, mariadb: bool },
}

/// One pool per connection, as its row stands: a changed connection gets a new pool.
#[derive(Default)]
pub struct Pools {
    pools: Mutex<HashMap<String, (Target, Arc<Pool>)>>,
}

impl Pools {
    pub async fn get(&self, name: &str, target: &Target, size: u32) -> Result<Arc<Pool>, Failure> {
        if let Some((known, pool)) = self.pools.lock().expect("pools").get(name)
            && known == target
        {
            return Ok(pool.clone());
        }
        let pool = Arc::new(open(target, size).await?);
        self.pools
            .lock()
            .expect("pools")
            .insert(name.to_string(), (target.clone(), pool.clone()));
        Ok(pool)
    }
}

pub async fn open(target: &Target, size: u32) -> Result<Pool, Failure> {
    let failed = |error: sqlx::Error| Failure::Database(error.to_string());
    match target.dialect {
        Dialect::Postgres => {
            let defaults = postgres_defaults(target).await?;
            let mut options = PgConnectOptions::new()
                .host(&target.host)
                .port(target.port)
                .username(&target.user)
                .extra_float_digits(None);
            if let Some(password) = &target.password {
                options = options.password(password);
            }
            if let Some(database) = &target.database {
                options = options.database(database);
            }
            let pool = PgPoolOptions::new()
                .max_connections(size)
                .test_before_acquire(false)
                .acquire_timeout(CONNECT_TIMEOUT)
                .connect_with(options)
                .await
                .map_err(failed)?;
            Ok(Pool::Postgres { pool, defaults })
        }
        Dialect::Mysql => {
            let mut options = MySqlConnectOptions::new()
                .host(&target.host)
                .port(target.port)
                .username(&target.user)
                .charset("utf8mb4")
                .pipes_as_concat(false)
                .no_engine_substitution(false)
                .timezone(None);
            if let Some(password) = &target.password {
                options = options.password(password);
            }
            if let Some(database) = &target.database {
                options = options.database(database);
            }
            let pool = MySqlPoolOptions::new()
                .max_connections(size)
                .test_before_acquire(false)
                .acquire_timeout(CONNECT_TIMEOUT)
                .after_connect(|conn, _| {
                    Box::pin(async move {
                        // The session mysql-connector has: sqlx's handshake asks for IGNORE_SPACE, which adds it to
                        // sql_mode (making function names reserved words), and mysql-connector turns autocommit off
                        conn.execute("SET SESSION sql_mode = @@GLOBAL.sql_mode")
                            .await?;
                        conn.execute("SET autocommit = 0").await?;
                        // Read-only per transaction (runners._MySQL.connect(), ADR 0027)
                        conn.execute("SET SESSION TRANSACTION READ ONLY").await?;
                        conn.execute("START TRANSACTION READ ONLY").await?;
                        Ok(())
                    })
                })
                .connect_with(options)
                .await
                .map_err(failed)?;
            let row = sqlx::query(
                "SELECT VERSION(), @@collation_connection, COLLATION(_utf8mb4'a'), @@character_set_connection",
            )
            .fetch_one(&pool)
            .await
            .map_err(failed)?;
            let text = |i: usize| row.try_get::<String, _>(i).unwrap_or_default();
            // `_utf8mb4 X'..'` has utf8mb4's default collation; a plain literal, the connection's - they must agree
            if text(1) != text(2) || text(3) != "utf8mb4" {
                return Err(Failure::Decline);
            }
            Ok(Pool::Mysql {
                pool,
                mariadb: text(0).to_ascii_lowercase().contains("mariadb"),
            })
        }
    }
}

async fn postgres_defaults(target: &Target) -> Result<Defaults, Failure> {
    let mut config = tokio_postgres::Config::new();
    config
        .host(&target.host)
        .port(target.port)
        .user(&target.user)
        .connect_timeout(CONNECT_TIMEOUT);
    if let Some(password) = &target.password {
        config.password(password);
    }
    if let Some(database) = &target.database {
        config.dbname(database);
    }
    // Without TLS: a server that requires it is left to Python for now
    let (client, connection) = config
        .connect(tokio_postgres::NoTls)
        .await
        .map_err(|_| Failure::Decline)?;
    let driver = tokio::spawn(connection);
    let show = |name: &'static str| {
        let client = &client;
        async move {
            let rows = client
                .simple_query(&format!("SHOW {name}"))
                .await
                .map_err(|_| Failure::Decline)?;
            rows.iter()
                .find_map(|message| match message {
                    tokio_postgres::SimpleQueryMessage::Row(row) => row.get(0).map(str::to_string),
                    _ => None,
                })
                .ok_or(Failure::Decline)
        }
    };
    let defaults = Defaults {
        time_zone: show("TimeZone").await?,
        date_style: show("DateStyle").await?,
    };
    drop(client);
    let _ = driver.await;
    Ok(defaults)
}

// --------------------------------------------------------------------------------------
// Running it
// --------------------------------------------------------------------------------------

/// A result as the database sent it: column names and type names, each value's text (both protocols send text).
#[derive(Debug, Default)]
pub struct Output {
    pub columns: Vec<(String, String)>,
    pub rows: Vec<Vec<Option<Vec<u8>>>>,
}

/// Column names and types, and each value's bytes as `bytes` reads them from the row's raw value.
fn collect<R: Row>(
    rows: Vec<R>,
    bytes: impl Fn(<R::Database as sqlx::Database>::ValueRef<'_>) -> Option<Vec<u8>>,
) -> Output
where
    usize: sqlx::ColumnIndex<R>,
{
    let columns = rows
        .first()
        .map(|row| {
            row.columns()
                .iter()
                .map(|c| (c.name().to_string(), c.type_info().name().to_string()))
                .collect()
        })
        .unwrap_or_default();
    let rows = rows
        .iter()
        .map(|row| {
            (0..row.columns().len())
                .map(|i| {
                    let raw = row.try_get_raw(i).ok()?;
                    if raw.is_null() { None } else { bytes(raw) }
                })
                .collect()
        })
        .collect();
    Output { columns, rows }
}

/// A PostgreSQL value's text, as the simple-query protocol sent it.
fn postgres_bytes(raw: sqlx::postgres::PgValueRef<'_>) -> Option<Vec<u8>> {
    raw.as_bytes().ok().map(<[u8]>::to_vec)
}

/// A MySQL value's text, as the text protocol sent it: `&[u8]`'s decoding hands the bytes over as they are, whatever
/// the column's type (it is only `try_get` that checks the type).
fn mysql_bytes(raw: sqlx::mysql::MySqlValueRef<'_>) -> Option<Vec<u8>> {
    <&[u8] as sqlx::Decode<sqlx::MySql>>::decode(raw)
        .ok()
        .map(<[u8]>::to_vec)
}

/// One page of the plan's statement: at most `limit + 1` rows from `offset`, as runners._fetch_page() returns them.
pub async fn run(
    pool: &Pool,
    statement: &Statement,
    values: &HashMap<String, Supplied>,
    limit: i64,
    offset: i64,
    timeout: Option<f64>,
    search_path: Option<&str>,
) -> Result<Output, Failure> {
    let mut output = match pool {
        Pool::Postgres { pool, defaults } => {
            let sql = build(statement, values, Dialect::Postgres, limit, offset, None)
                .ok_or(Failure::Decline)?;
            // ...and RESET ALL after it, in the same transaction: the next message is parsed with nothing set
            let message = format!(
                "{}{sql}; RESET ALL",
                postgres_prefix(defaults, timeout, search_path)
            );
            let rows = sqlx::raw_sql(sqlx::AssertSqlSafe(message))
                .fetch_all(pool)
                .await
                .map_err(|error| {
                    match error.as_database_error().and_then(|e| e.code()) {
                        Some(code) if code == "57014" => Failure::Timeout, // query_canceled
                        _ => Failure::Database(error.to_string()),
                    }
                })?;
            collect(rows, postgres_bytes)
        }
        Pool::Mysql { pool, mariadb } => {
            let timeout = timeout.filter(|t| *t > 0.0);
            let (sql, wrap) = match (timeout, *mariadb) {
                (None, _) => (
                    build(statement, values, Dialect::Mysql, limit, offset, None),
                    None,
                ),
                (Some(t), false) => {
                    let hint = format!("/*+ MAX_EXECUTION_TIME({}) */", millis(t));
                    (
                        build(
                            statement,
                            values,
                            Dialect::Mysql,
                            limit,
                            offset,
                            Some(&hint),
                        ),
                        None,
                    )
                }
                (Some(t), true) => (
                    build(statement, values, Dialect::Mysql, limit, offset, None),
                    Some(format!(
                        "SET STATEMENT max_statement_time = {} FOR ",
                        pyfmt::general(t)
                    )),
                ),
            };
            let sql = sql.ok_or(Failure::Decline)?;
            let sql = match wrap {
                Some(wrap) => format!("{wrap}{sql}"),
                None => sql,
            };
            let mut conn = pool
                .acquire()
                .await
                .map_err(|e| Failure::Database(e.to_string()))?;
            let result = sqlx::raw_sql(sqlx::AssertSqlSafe(sql))
                .fetch_all(&mut *conn)
                .await;
            // Ends the transaction and starts the next read-only one (runners._MySQL.reset()), off the response's path
            tokio::spawn(async move {
                if conn.execute("ROLLBACK AND CHAIN").await.is_err() {
                    let _ = conn.detach(); // not returned to the pool in an unknown state
                }
            });
            let rows =
                result.map_err(
                    |error| match error.as_database_error().and_then(|e| e.code()) {
                        Some(code) if MYSQL_TIMEOUT_ERRNOS.contains(&code.as_ref()) => {
                            Failure::Timeout
                        }
                        _ => Failure::Database(error.to_string()),
                    },
                )?;
            collect(rows, mysql_bytes)
        }
    };
    if !statement.paginated {
        // fetchmany(offset + limit + 1)[offset:], for a statement the runner doesn't page itself
        let keep = (offset + limit + 1) as usize;
        output.rows.truncate(keep);
        output
            .rows
            .drain(..(offset as usize).min(output.rows.len()));
    }
    Ok(output)
}

impl FromStr for Dialect {
    type Err = ();
    fn from_str(text: &str) -> Result<Dialect, ()> {
        match text {
            "postgres" => Ok(Dialect::Postgres),
            "mysql" => Ok(Dialect::Mysql),
            _ => Err(()),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn values(pairs: &[(&str, Param)]) -> HashMap<String, Supplied> {
        pairs
            .iter()
            .map(|(k, v)| (k.to_string(), Supplied::Value(v.clone())))
            .collect()
    }

    #[test]
    fn postgres_strings_are_ascii_and_read_alike_whatever_the_session() {
        assert_eq!(postgres_string("O'Brien").unwrap(), "E'O''Brien'");
        assert_eq!(postgres_string("a\\b").unwrap(), "E'a\\\\b'");
        assert_eq!(
            postgres_string("é😀\n").unwrap(),
            "E'\\u00E9\\U0001F600\\u000A'"
        );
        assert!(postgres_string("nul\0").is_none());
    }

    #[test]
    fn mysql_strings_are_hex() {
        assert_eq!(mysql_string("O'Brien"), "_utf8mb4 X'4F27427269656E'");
        assert_eq!(mysql_string(""), "_utf8mb4 X''");
    }

    #[test]
    fn numbers_as_the_drivers_write_them() {
        let pg = |v: Param| literal(&v, Dialect::Postgres).unwrap();
        assert_eq!(pg(Param::Int(-5)), " -5");
        assert_eq!(pg(Param::Float(1e16)), "1e+16");
        assert_eq!(pg(Param::Float(-0.0)), " -0.0");
        assert_eq!(pg(Param::Bool(true)), "true");
        assert_eq!(
            literal(&Param::Bool(false), Dialect::Mysql).unwrap(),
            "False"
        );
        assert_eq!(pg(Param::Null), "NULL");
    }

    #[test]
    fn builds_the_page_within_the_statement_s_own_window() {
        let statement: Statement = serde_json::from_value(serde_json::json!({
            "segments": ["SELECT id FROM t WHERE a = ", {"param": "a"}, " ORDER BY id"],
            "paginated": true, "limit": {"param": "n", "clause": "LIMIT"}, "offset": {"value": 2}, "hint_at": 6,
        }))
        .unwrap();
        let v = values(&[("a", Param::Text("x".into())), ("n", Param::Int(5))]);
        assert_eq!(
            build(
                &statement,
                &v,
                Dialect::Mysql,
                10,
                3,
                Some("/*+ MAX_EXECUTION_TIME(500) */")
            )
            .unwrap(),
            "SELECT /*+ MAX_EXECUTION_TIME(500) */ id FROM t WHERE a = _utf8mb4 X'78' ORDER BY id\nLIMIT 2 OFFSET 5"
        );
        let unsafe_window = values(&[
            ("a", Param::Text("x".into())),
            ("n", Param::Text("٣".into())),
        ]);
        assert!(build(&statement, &unsafe_window, Dialect::Mysql, 10, 0, None).is_none());
    }

    #[test]
    fn a_connection_the_prototype_leaves_to_python() {
        let keys: Vec<String> = Vec::new();
        assert!(target("postgres", r#"{"host": "h", "user": "u"}"#, &keys).is_some());
        for details in [
            r#"{"host": "h", "user": "u", "password": "${DB_PASSWORD}"}"#,
            r#"{"host": "h", "user": "u", "sslmode": "require"}"#,
            r#"{"host": "h", "user": "u", "credentials": {"source": "vault-database"}}"#,
            r#"{"host": "h", "user": "u", "password": "enc:not-a-token"}"#,
            r#"{"user": "u"}"#,
            r#"{"host": "h"}"#,
        ] {
            assert!(target("postgres", details, &keys).is_none(), "{details}");
        }
        assert!(target("sqlite", r#"{"host": "h", "user": "u"}"#, &keys).is_none());
    }

    #[test]
    fn decrypts_a_password_python_encrypted() {
        // cryptography.fernet.Fernet(key).encrypt(b"s3cret") in Python
        let key = "kM2dxV3DnnqfDBOwT2wGvbjDKr2HzHJH7-QYG1rSd4Q=".to_string();
        let token = include_str!("../tests/fernet_vector.txt").trim();
        let details = format!(r#"{{"host": "h", "user": "u", "password": "enc:{token}"}}"#);
        let found = target(
            "postgres",
            &details,
            &["wrong-but-valid-AAAAAAAAAAAAAAAAAAAAAAAAAAAA=".into(), key],
        )
        .unwrap();
        assert_eq!(found.password.as_deref(), Some("s3cret"));
    }
}
