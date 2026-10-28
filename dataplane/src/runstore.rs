//! The run store (ADR 0029, a prototype): with QUERYAPIGATE_RUN_STORE_URL set, the runs this serves go straight into
//! ClickHouse's `events` table - Python no longer queues them, only counts them for /metrics - and ClickHouse fills
//! history and the usage rollups from it, as it does for Python's own runs (runstore.py).
//!
//! Each batch is stamped with event ids just before it is sent: milliseconds since 2026-01-01 UTC x 4096, plus a number
//! from the block of each millisecond Python leases this process (POST /internal/dataplane/lease) - so its ids never
//! collide with a Python process's - on ClickHouse's clock, as this process estimates it. Python's runstore.py is the
//! other half; the two share the layout, which the lease names.

use std::sync::Mutex;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use clickhouse::{Client, Row};
use serde::Serialize;
use serde_json::Value as Json;

use crate::forward::Forwarder;

/// How far a burst's ids may run ahead of the clock: runs past it are dropped, never stamped later (runstore.py's).
const MAX_LEAD_MS: i64 = 1000;
/// How often the offset to ClickHouse's clock is measured again.
const CLOCK_EVERY: Duration = Duration::from_secs(60);
/// How often the lease is renewed; Python lets it lapse a minute after the last renewal.
pub const RENEW_EVERY: Duration = Duration::from_secs(10);

/// QUERYAPIGATE_DATAPLANE_RUN_STORE_URL (an insert-only ClickHouse user, ideally) or QUERYAPIGATE_RUN_STORE_URL: None
/// when neither is set. The rules are config.py's: http(s), a host, https unless it is this machine.
pub fn url_from(get: impl Fn(&str) -> Option<String>) -> Result<Option<Target>, String> {
    let Some(raw) =
        get("QUERYAPIGATE_DATAPLANE_RUN_STORE_URL").or_else(|| get("QUERYAPIGATE_RUN_STORE_URL"))
    else {
        return Ok(None);
    };
    let raw = raw.trim();
    let bad = || {
        format!(
            "the run store's URL must be http(s)://user:password@host:port/database, not '{}'",
            redact(raw)
        )
    };
    let uri: hyper::Uri = raw.parse().map_err(|_| bad())?;
    let scheme = uri
        .scheme_str()
        .filter(|s| *s == "http" || *s == "https")
        .ok_or_else(bad)?;
    let authority = uri.authority().ok_or_else(bad)?;
    let host = authority.host();
    if host.is_empty() {
        return Err(bad());
    }
    // What follows the host, if anything, must be a port: `port()` alone is None for one out of range too
    let host_port = authority
        .as_str()
        .rsplit_once('@')
        .map_or(authority.as_str(), |(_, h)| h);
    let port = match host_port
        .strip_prefix(host)
        .and_then(|rest| rest.strip_prefix(':'))
    {
        None if host_port == host => {
            if scheme == "https" {
                8443
            } else {
                8123
            }
        }
        None => return Err(bad()),
        Some(port) => port
            .parse::<u16>()
            .ok()
            .filter(|p| *p > 0)
            .ok_or_else(bad)?,
    };
    let credentials = authority.as_str().rsplit_once('@').map_or("", |(c, _)| c);
    let (user, password) = credentials.split_once(':').unwrap_or((credentials, ""));
    if scheme == "http" && !matches!(host, "localhost" | "127.0.0.1" | "[::1]") {
        return Err("the run store's URL must be https:// unless ClickHouse is on this machine: it carries a password \
                    and every run"
            .into());
    }
    let path = uri.path();
    let database = path.trim_matches('/');
    let database = if database.is_empty() {
        "queryapigate"
    } else {
        database
    };
    if !database
        .chars()
        .all(|c| c.is_ascii_alphanumeric() || c == '_')
        || database.starts_with(|c: char| c.is_ascii_digit())
    {
        return Err(bad());
    }
    let decode = |s: &str| {
        percent_encoding::percent_decode_str(s)
            .decode_utf8_lossy()
            .into_owned()
    };
    Ok(Some(Target {
        url: format!("{scheme}://{host}:{port}"),
        user: if user.is_empty() {
            "default".into()
        } else {
            decode(user)
        },
        password: decode(password),
        database: database.to_string(),
    }))
}

fn redact(url: &str) -> String {
    match (url.split_once("://"), url.rsplit_once('@')) {
        (Some((scheme, _)), Some((_, host))) => format!("{scheme}://...@{host}"),
        _ => url.to_string(),
    }
}

pub struct Target {
    pub url: String,
    pub user: String,
    pub password: String,
    pub database: String,
}

/// One row of `events`, as runstore.py writes a run's: its columns are the table's, by name.
#[derive(Row, Serialize)]
pub struct EventRow {
    pub id: u64,
    #[serde(rename = "type")]
    pub kind: String,
    pub source: Option<String>,
    pub occurred_at: String,
    pub workspace: String,
    pub query_name: Option<String>,
    pub version: Option<u32>,
    pub key_name: Option<String>,
    pub connection_name: Option<String>,
    pub status: Option<String>,
    pub in_history: bool,
    pub rows: u64,
    pub seconds: f32,
    pub entry: String,
    pub payload_json: String,
}

/// The block of ids Python leased this process, and the layout it names.
struct Lease {
    block: u64,
    size: u64,
    epoch_ms: i64,
    per_ms: u64,
    sample_rate: f64,
}

struct Ids {
    last_ms: i64,
    seq: u64,
}

struct Clock {
    offset_ms: f64,
    measured: Option<Instant>,
}

pub struct RunStore {
    client: Client,
    holder: String,
    lease: Mutex<Option<Lease>>,
    ids: Mutex<Ids>,
    clock: Mutex<Clock>,
}

fn now_ms() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64() * 1000.0)
        .unwrap_or(0.0)
}

impl RunStore {
    pub fn new(target: Target) -> RunStore {
        // RowBinary as the struct is laid out: the schema isn't fetched first, which an insert-only user couldn't
        let client = Client::default()
            .with_url(target.url)
            .with_user(target.user)
            .with_password(target.password)
            .with_database(target.database)
            .with_validation(false);
        RunStore {
            client,
            holder: format!("dataplane:{}:{:x}", std::process::id(), rand_u64()),
            lease: Mutex::new(None),
            ids: Mutex::new(Ids {
                last_ms: -1,
                seq: 0,
            }),
            clock: Mutex::new(Clock {
                offset_ms: 0.0,
                measured: None,
            }),
        }
    }

    /// Lease a block from Python, or renew the one held.
    pub async fn keep_lease(&self, forward: &Forwarder) -> Result<(), String> {
        let held = self.lease.lock().unwrap().as_ref().map(|l| l.block);
        if let Some(block) = held {
            let body = serde_json::json!({"holder": self.holder, "block": block}).to_string();
            let (status, _) = forward.post_json("/internal/dataplane/lease", body).await?;
            if status.as_u16() == 200 {
                return Ok(());
            }
            // It lapsed: a new block, its numbers starting again
            *self.lease.lock().unwrap() = None;
        }
        let body = serde_json::json!({"holder": self.holder}).to_string();
        let (status, answer) = forward.post_json("/internal/dataplane/lease", body).await?;
        if status.as_u16() != 200 {
            return Err(format!(
                "Python refused a run store lease: {status} {answer}"
            ));
        }
        let answer: Json = serde_json::from_str(&answer).map_err(|e| e.to_string())?;
        let field = |name: &str| answer.get(name).and_then(Json::as_u64);
        let lease = Lease {
            block: field("block").ok_or("a lease without a block")?,
            size: field("size").ok_or("a lease without a size")?,
            epoch_ms: field("epoch_ms").ok_or("a lease without its epoch")? as i64,
            per_ms: field("per_ms").ok_or("a lease without its layout")?,
            sample_rate: answer
                .get("sample_rate")
                .and_then(Json::as_f64)
                .unwrap_or(1.0),
        };
        *self.ids.lock().unwrap() = Ids {
            last_ms: -1,
            seq: 0,
        };
        *self.lease.lock().unwrap() = Some(lease);
        Ok(())
    }

    async fn store_ms(&self, epoch_ms: i64) -> i64 {
        let stale = self
            .clock
            .lock()
            .unwrap()
            .measured
            .is_none_or(|at| at.elapsed() > CLOCK_EVERY);
        if stale {
            let before = now_ms();
            let server = self
                .client
                .query("SELECT toUnixTimestamp64Milli(now64(3))")
                .fetch_one::<i64>()
                .await;
            let after = now_ms();
            let mut clock = self.clock.lock().unwrap();
            if let Ok(server) = server {
                clock.offset_ms = server as f64 - (before + after) / 2.0;
            }
            clock.measured = Some(Instant::now());
        }
        let offset = self.clock.lock().unwrap().offset_ms;
        (now_ms() + offset) as i64 - epoch_ms
    }

    /// Up to `count` new ids, increasing - fewer when a burst would run more than MAX_LEAD_MS ahead.
    async fn mint(&self, count: usize) -> Result<Vec<u64>, String> {
        let (block, size, per_ms, epoch_ms) = {
            let lease = self.lease.lock().unwrap();
            let lease = lease.as_ref().ok_or("no run store lease held")?;
            (lease.block, lease.size, lease.per_ms, lease.epoch_ms)
        };
        let now = self.store_ms(epoch_ms).await;
        let mut ids = self.ids.lock().unwrap();
        let (mut ms, mut seq) = if ids.last_ms >= now {
            (ids.last_ms, ids.seq)
        } else {
            (now, 0)
        };
        let mut found = Vec::with_capacity(count);
        while found.len() < count {
            if seq >= size {
                ms += 1;
                seq = 0;
                if ms - now > MAX_LEAD_MS {
                    break;
                }
            }
            found.push(ms as u64 * per_ms + block + seq);
            seq += 1;
        }
        ids.last_ms = ms;
        ids.seq = seq;
        Ok(found)
    }

    /// Whether a run is kept in history: always a failure, otherwise QUERYAPIGATE_HISTORY_SAMPLE_RATE's share.
    pub fn keep(&self, status: &str) -> bool {
        let rate = self
            .lease
            .lock()
            .unwrap()
            .as_ref()
            .map_or(1.0, |l| l.sample_rate);
        status == "error" || rate >= 1.0 || (rand_u64() as f64 / u64::MAX as f64) < rate
    }

    /// Stamp `rows` and insert them as one block. Returns how many were written (a burst past the lease's ids drops
    /// the rest). A retry is stamped again unless the first attempt's rows turn out to have landed.
    pub async fn write(&self, mut rows: Vec<EventRow>) -> Result<usize, String> {
        for attempt in 1..=2 {
            let ids = self.mint(rows.len()).await?;
            rows.truncate(ids.len());
            for (row, id) in rows.iter_mut().zip(&ids) {
                row.id = *id;
            }
            let Some(&first) = ids.first() else {
                return Ok(0);
            };
            match self
                .insert(&rows, &format!("{}:{first}", self.holder))
                .await
            {
                Ok(()) => return Ok(rows.len()),
                Err(error) if attempt == 2 => return Err(error),
                Err(_) => {
                    let landed = self
                        .client
                        .query("SELECT count() FROM events WHERE id = ?")
                        .bind(first)
                        .fetch_one::<u64>()
                        .await
                        .unwrap_or(0);
                    if landed > 0 {
                        return Ok(rows.len());
                    }
                }
            }
        }
        unreachable!()
    }

    async fn insert(&self, rows: &[EventRow], token: &str) -> Result<(), String> {
        let mut insert = self
            .client
            .insert::<EventRow>("events")
            .await
            .map_err(|e| e.to_string())?
            .with_setting("insert_deduplication_token", token);
        for row in rows {
            insert.write(row).await.map_err(|e| e.to_string())?;
        }
        insert.end().await.map_err(|e| e.to_string())
    }
}

fn rand_u64() -> u64 {
    let mut bytes = [0u8; 8];
    getrandom::fill(&mut bytes).unwrap_or_default();
    u64::from_le_bytes(bytes)
}

/// A served run's `events` row, from the run serve.rs reports (finish()): its id is stamped when it's sent.
pub fn event_row(run: &Json, keep: bool) -> Option<EventRow> {
    let text = |name: &str| run.get(name).and_then(Json::as_str).map(str::to_string);
    Some(EventRow {
        id: 0,
        kind: "query.run".into(),
        source: text("transport"),
        occurred_at: text("executed_at")?,
        workspace: "default".into(), // a call in another workspace is forwarded (ADR 0028 §4)
        query_name: text("query"),
        version: run
            .get("version")
            .and_then(Json::as_u64)
            .and_then(|v| u32::try_from(v).ok()),
        key_name: text("key"),
        connection_name: text("connection"),
        status: text("status"),
        in_history: keep,
        rows: run.get("rows").and_then(Json::as_u64).unwrap_or(0),
        seconds: run
            .get("query_seconds")
            .and_then(Json::as_f64)
            .unwrap_or(0.0) as f32,
        entry: text("entry")?,
        payload_json: String::new(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;

    fn url(pairs: &[(&str, &str)]) -> Result<Option<Target>, String> {
        let env: HashMap<String, String> = pairs
            .iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect();
        url_from(|name| env.get(name).cloned())
    }

    #[test]
    fn reads_the_url_python_reads_or_its_own() {
        assert!(url(&[]).unwrap().is_none());
        let target = url(&[(
            "QUERYAPIGATE_RUN_STORE_URL",
            "https://u:p%40ss@ch.internal:9443/runs",
        )])
        .unwrap()
        .unwrap();
        assert_eq!(
            (
                target.url.as_str(),
                target.user.as_str(),
                target.password.as_str(),
                target.database.as_str()
            ),
            ("https://ch.internal:9443", "u", "p@ss", "runs")
        );
        let own = url(&[
            ("QUERYAPIGATE_RUN_STORE_URL", "https://u:p@ch:8443/runs"),
            (
                "QUERYAPIGATE_DATAPLANE_RUN_STORE_URL",
                "https://writer:w@ch:8443/runs",
            ),
        ])
        .unwrap()
        .unwrap();
        assert_eq!(own.user, "writer");
        let local = url(&[("QUERYAPIGATE_RUN_STORE_URL", "http://localhost")])
            .unwrap()
            .unwrap();
        assert_eq!(
            (local.url.as_str(), local.database.as_str()),
            ("http://localhost:8123", "queryapigate")
        );
    }

    #[test]
    fn refuses_what_config_py_refuses() {
        for bad in [
            "http://ch.internal:8123/runs",
            "clickhouse://h:9000/runs",
            "https:///runs",
            "https://h/runs-2026",
            "https://h:99999/runs",
        ] {
            assert!(
                url(&[("QUERYAPIGATE_RUN_STORE_URL", bad)]).is_err(),
                "{bad}"
            );
        }
        let error = url(&[(
            "QUERYAPIGATE_RUN_STORE_URL",
            "clickhouse://u:secret@h:9000/runs",
        )])
        .err()
        .unwrap();
        assert!(!error.contains("secret"), "{error}");
    }

    #[test]
    fn a_run_s_row_is_the_one_python_writes() {
        let run = serde_json::json!({"query": "by_id", "version": 2, "entry": "{\"rows\": 1}",
                                     "executed_at": "2026-10-28 12:00:00", "status": "success", "key": "app",
                                     "connection": "shop", "transport": "rest", "rows": 1, "query_seconds": 0.002});
        let row = event_row(&run, true).unwrap();
        assert_eq!(
            (
                row.query_name.as_deref(),
                row.version,
                row.key_name.as_deref(),
                row.source.as_deref()
            ),
            (Some("by_id"), Some(2), Some("app"), Some("rest"))
        );
        assert_eq!(
            (row.rows, row.entry.as_str(), row.workspace.as_str()),
            (1, "{\"rows\": 1}", "default")
        );
        assert!(row.in_history);
    }
}
