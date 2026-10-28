//! Reading QueryAPIGate's own store - SQLite (queryapigate.db in QUERYAPIGATE_HOME) or PostgreSQL
//! (QUERYAPIGATE_DATABASE_URL) - for the three row sets a plan names: the key a secret belongs to, the saved query,
//! and the connection. Read only, with the same SELECTs Python runs, so their digests (`digest`) can be compared
//! with the ones Python put in the plan (`metacache.digest_rows`).

use std::path::PathBuf;
use std::str::FromStr;
use std::time::Duration;

use sha2::{Digest, Sha256};
use sqlx::postgres::{PgConnectOptions, PgPool, PgPoolOptions};
use sqlx::sqlite::{SqliteConnectOptions, SqlitePool, SqlitePoolOptions};
use sqlx::{Column, Row, TypeInfo, ValueRef};

/// One stored value, as the digest sees it - the store's columns are INTEGER or TEXT.
#[derive(Clone, Debug, PartialEq)]
pub enum Value {
    Null,
    Int(i64),
    Text(String),
    /// Anything else: a digest that can never match Python's, so a plan is never used for it.
    Other,
}

impl Value {
    pub fn text(&self) -> Option<&str> {
        match self {
            Value::Text(text) => Some(text),
            _ => None,
        }
    }
    pub fn int(&self) -> Option<i64> {
        match self {
            Value::Int(n) => Some(*n),
            _ => None,
        }
    }
}

/// Rows of one SELECT, values in column order.
#[derive(Clone, Debug, Default)]
pub struct Rows {
    pub columns: Vec<String>,
    pub rows: Vec<Vec<Value>>,
}

impl Rows {
    pub fn get(&self, row: usize, column: &str) -> Option<&Value> {
        let i = self.columns.iter().position(|c| c == column)?;
        self.rows.get(row)?.get(i)
    }

    /// SHA-256 over the values, each tagged with its type and length-prefixed - `metacache.digest_rows`.
    pub fn digest(&self) -> String {
        digest(&self.rows)
    }
}

pub fn digest(rows: &[Vec<Value>]) -> String {
    let mut hash = Sha256::new();
    for row in rows {
        hash.update(b"r");
        for value in row {
            let (tag, data): (&[u8], Vec<u8>) = match value {
                Value::Null => (b"n", Vec::new()),
                Value::Int(n) => (b"i", n.to_string().into_bytes()),
                Value::Text(text) => (b"s", text.as_bytes().to_vec()),
                Value::Other => (b"?", b"never matches".to_vec()),
            };
            hash.update(tag);
            hash.update((data.len() as u64).to_be_bytes());
            hash.update(&data);
        }
    }
    hex::encode(hash.finalize())
}

const KEY_SQL: &str = "SELECT workspace, name, hash, active, expires_at, created_at, details_json, previous_hash, \
    previous_expires_at FROM api_keys WHERE hash = ? OR previous_hash = ?";
const QUERY_SQL: &str = "SELECT q.collection, q.example, q.published_version, v.version, v.uuid, v.status, \
    v.created_at, v.last_modified_at, v.fields_json FROM saved_queries q LEFT JOIN saved_query_versions v \
    ON v.workspace = q.workspace AND v.query_name = q.name WHERE q.workspace = ? AND q.name = ? \
    ORDER BY v.version";
const CONNECTION_SQL: &str = "SELECT db, active, created_at, updated_at, details_json FROM connections \
    WHERE workspace = ? AND name = ?";
const SECRET_SQL: &str = "SELECT value FROM server_secrets WHERE name = ?";

pub enum Store {
    Sqlite(SqlitePool),
    Postgres(PgPool),
}

/// `?` placeholders numbered for PostgreSQL, as db.py's _pg_sql() rewrites them.
fn numbered(sql: &str) -> String {
    let mut out = String::with_capacity(sql.len() + 8);
    let mut n = 0;
    for c in sql.chars() {
        if c == '?' {
            n += 1;
            out.push_str(&format!("${n}"));
        } else {
            out.push(c);
        }
    }
    out
}

impl Store {
    /// The store the Python server uses, from the same environment.
    pub async fn open(
        get: impl Fn(&str) -> Option<String>,
        connections: u32,
    ) -> Result<Store, String> {
        if let Some(url) = get("QUERYAPIGATE_DATABASE_URL") {
            let options = PgConnectOptions::from_str(url.trim())
                .map_err(|error| format!("QUERYAPIGATE_DATABASE_URL: {error}"))?;
            let pool = PgPoolOptions::new()
                .max_connections(connections)
                .test_before_acquire(false)
                .acquire_timeout(Duration::from_secs(5))
                .connect_with(options)
                .await
                .map_err(|error| format!("the PostgreSQL store: {error}"))?;
            return Ok(Store::Postgres(pool));
        }
        let home = match get("QUERYAPIGATE_HOME") {
            Some(home) => PathBuf::from(home),
            None => std::env::current_dir().map_err(|error| error.to_string())?,
        };
        let path = std::fs::canonicalize(&home)
            .map_err(|error| format!("QUERYAPIGATE_HOME {}: {error}", home.display()))?
            .join("queryapigate.db");
        // Never created here: the Python server makes the store, this only reads it
        let options = SqliteConnectOptions::new()
            .filename(&path)
            .create_if_missing(false)
            .read_only(true);
        let pool = SqlitePoolOptions::new()
            .max_connections(connections)
            .test_before_acquire(false)
            .connect_with(options)
            .await
            .map_err(|error| format!("the store {}: {error}", path.display()))?;
        Ok(Store::Sqlite(pool))
    }

    async fn select(&self, sql: &'static str, args: &[&str]) -> Result<Rows, sqlx::Error> {
        match self {
            Store::Sqlite(pool) => {
                let mut query = sqlx::query(sql);
                for arg in args {
                    query = query.bind(*arg);
                }
                let rows = query.fetch_all(pool).await?;
                Ok(to_rows(&rows, sql))
            }
            Store::Postgres(pool) => {
                // One of the constant SELECTs above, renumbered - nothing from a request is in the text
                let sql = numbered(sql);
                let mut query = sqlx::query(sqlx::AssertSqlSafe(sql.clone()));
                for arg in args {
                    query = query.bind(*arg);
                }
                let rows = query.fetch_all(pool).await?;
                Ok(to_rows(&rows, &sql))
            }
        }
    }

    pub async fn key(&self, hash: &str) -> Result<Rows, sqlx::Error> {
        self.select(KEY_SQL, &[hash, hash]).await
    }

    pub async fn query(&self, workspace: &str, name: &str) -> Result<Rows, sqlx::Error> {
        self.select(QUERY_SQL, &[workspace, name]).await
    }

    pub async fn connection(&self, workspace: &str, name: &str) -> Result<Rows, sqlx::Error> {
        self.select(CONNECTION_SQL, &[workspace, name]).await
    }

    pub async fn server_secret(&self, name: &str) -> Result<Option<String>, sqlx::Error> {
        let rows = self.select(SECRET_SQL, &[name]).await?;
        Ok(rows
            .get(0, "value")
            .and_then(|v| v.text())
            .map(str::to_string))
    }
}

/// Column names from the first row, or - for none - from the SELECT list itself, so an empty result still has them.
fn to_rows<R: Row>(rows: &[R], sql: &str) -> Rows
where
    for<'r> i64: sqlx::Decode<'r, R::Database> + sqlx::Type<R::Database>,
    for<'r> i32: sqlx::Decode<'r, R::Database> + sqlx::Type<R::Database>,
    for<'r> String: sqlx::Decode<'r, R::Database> + sqlx::Type<R::Database>,
    for<'r> bool: sqlx::Decode<'r, R::Database> + sqlx::Type<R::Database>,
    usize: sqlx::ColumnIndex<R>,
{
    let columns = match rows.first() {
        Some(row) => row.columns().iter().map(|c| c.name().to_string()).collect(),
        None => select_list(sql),
    };
    let rows = rows
        .iter()
        .map(|row| (0..row.columns().len()).map(|i| value(row, i)).collect())
        .collect();
    Rows { columns, rows }
}

fn value<R: Row>(row: &R, i: usize) -> Value
where
    for<'r> i64: sqlx::Decode<'r, R::Database> + sqlx::Type<R::Database>,
    for<'r> i32: sqlx::Decode<'r, R::Database> + sqlx::Type<R::Database>,
    for<'r> String: sqlx::Decode<'r, R::Database> + sqlx::Type<R::Database>,
    for<'r> bool: sqlx::Decode<'r, R::Database> + sqlx::Type<R::Database>,
    usize: sqlx::ColumnIndex<R>,
{
    let Ok(raw) = row.try_get_raw(i) else {
        return Value::Other;
    };
    if raw.is_null() {
        return Value::Null;
    }
    // SQLite reports a value's own storage class; PostgreSQL the column's type
    let kind = raw.type_info().name().to_ascii_uppercase();
    match kind.as_str() {
        "INTEGER" | "INT8" | "BIGINT" => row
            .try_get::<i64, _>(i)
            .map(Value::Int)
            .unwrap_or(Value::Other),
        "INT4" | "INT" | "INT2" => row
            .try_get::<i32, _>(i)
            .map(|n| Value::Int(n.into()))
            .unwrap_or(Value::Other),
        "BOOL" | "BOOLEAN" => row
            .try_get::<bool, _>(i)
            .map(|b| Value::Int(b.into()))
            .unwrap_or(Value::Other),
        "TEXT" | "VARCHAR" | "NAME" | "BPCHAR" => row
            .try_get::<String, _>(i)
            .map(Value::Text)
            .unwrap_or(Value::Other),
        _ => Value::Other,
    }
}

/// The output names of `SELECT a, t.b, ... FROM` - for an empty result, whose rows carry none.
fn select_list(sql: &str) -> Vec<String> {
    let start = sql.find("SELECT ").map(|i| i + 7).unwrap_or(0);
    let end = sql.find(" FROM ").unwrap_or(sql.len());
    sql[start..end]
        .split(',')
        .map(|c| c.trim().rsplit('.').next().unwrap_or("").to_string())
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn digests_as_python_does() {
        // metacache.digest_rows([{'a': 1, 'b': 'x', 'c': None}, {'a': -2, 'b': 'é', 'c': 'y'}]) in Python
        let rows = vec![
            vec![Value::Int(1), Value::Text("x".into()), Value::Null],
            vec![
                Value::Int(-2),
                Value::Text("é".into()),
                Value::Text("y".into()),
            ],
        ];
        assert_eq!(
            digest(&rows),
            include_str!("../tests/digest_vector.txt").trim()
        );
        assert_eq!(
            digest(&[]),
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        );
    }

    #[test]
    fn numbers_placeholders_for_postgresql() {
        assert_eq!(numbered("a = ? OR b = ?"), "a = $1 OR b = $2");
    }

    #[test]
    fn names_an_empty_result_s_columns() {
        assert_eq!(
            select_list(CONNECTION_SQL),
            vec!["db", "active", "created_at", "updated_at", "details_json"]
        );
        assert_eq!(select_list(QUERY_SQL)[0], "collection");
    }
}
