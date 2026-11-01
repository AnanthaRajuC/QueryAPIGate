//! Serving `/q/<name>` from Python's plans (ADR 0028), and forwarding everything else.
//!
//! A call is served here only when every one of these holds; otherwise it is forwarded, unchanged, and Python
//! answers it as it always has:
//! - nothing about the server or the request is outside what the prototype does (tracing, an Origin, a bearer token,
//!   a stream, another format, a version or connection chosen per call), and behind trusted proxies their headers
//!   read plainly (proxy.rs);
//! - the X-API-Key is a scoped key that authenticates (auth.rs), and its use was recorded within the last minute;
//! - a plan is held for exactly the rows read for this call - the key, the saved query, the connection - by digest;
//! - the parameters resolve as Python would resolve them (params.rs), the query runs (db.rs), and every value in the
//!   page renders as Python would render it (render.rs).
//!
//! With Redis, a key's rate limit and the server-wide one are counted here, in Python's buckets (ratelimit.rs,
//! ADR 0032): once every check that could forward the call before the query has passed, so a call is counted by one
//! process only. One forwarded after that says what it was counted in (X-QAG-Rate-Counted), and Python counts it no
//! more. A refusal is Python's 429, reported with the runs for /metrics and the alerts.
//!
//! A plan arrives on the response to a forwarded call: Python makes one for a call the data plane could serve
//! (dataplane.py) and this keeps it, by the digests of the rows it read, until it expires. Served runs are reported
//! to Python in batches (POST /internal/dataplane/runs), which records them as its own.

use std::collections::{BTreeSet, HashMap};
use std::net::{IpAddr, SocketAddr};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use base64::Engine;
use bytes::Bytes;
use http_body_util::{BodyExt, Full};
use hyper::body::Incoming;
use hyper::header::{self, HeaderMap};
use hyper::{Method, Request, Response};
use serde::Deserialize;
use serde_json::{Map, Value as Json, json};
use tokio::sync::{Semaphore, mpsc};

use crate::auth::{self, Clock};
use crate::db::{self, Dialect, Failure};
use crate::forward::{self, Body, Forwarder, PLAN_HEADER};
use crate::log::{self, Logger};
use crate::params::{self, Supplied};
use crate::proxy;
use crate::ratelimit::{self, Limit, Limiter};
use crate::render::{self, Action};
use crate::respond;
use crate::runstore::{self, RunStore};
use crate::store::{Rows, Store, Value};

/// apikeys._USE_RECORD_INTERVAL: a key's call is forwarded at least this often, so Python records its last use.
const USE_RECORD_INTERVAL: Duration = Duration::from_secs(60);
/// An unmasked plan depends on no schema; it is still let go of after this long.
const UNMASKED_PLAN_TTL: Duration = Duration::from_secs(3600);
/// How long a plan the data plane couldn't apply (a type it doesn't render, a connection it doesn't open) is left
/// to Python before it is tried again.
const PYTHON_ONLY_TTL: Duration = Duration::from_secs(300);
const MAX_PLANS: usize = 50_000;
const RESERVED: [&str; 7] = [
    "format",
    "page",
    "page_size",
    "connection_name",
    "version",
    "timeout",
    "stream",
];

// --------------------------------------------------------------------------------------
// Settings, read as the Python server reads them, from the same environment
// --------------------------------------------------------------------------------------

pub struct Settings {
    /// Why every call is forwarded, if one is
    pub forward_everything: Option<&'static str>,
    /// QUERYAPIGATE_RATE_LIMIT: counted here when Redis is set, else every call is forwarded
    pub client_limit: Option<Limit>,
    /// QUERYAPIGATE_REDIS_URL, when it is one this reads as Python does: where the rate limits are counted
    pub redis_url: Option<String>,
    /// QUERYAPIGATE_TRUST_PROXY: the client is found behind this many proxies (proxy.rs)
    pub proxy_hops: usize,
    /// QUERYAPIGATE_HSTS_MAX_AGE: sent on a call that arrived over HTTPS, as secheaders.apply() sends it
    pub hsts_max_age: Option<u64>,
    pub admin_key: Option<String>,
    pub secret_keys: Vec<String>,
    pub mask_key: Option<Vec<u8>>,
    pub query_timeout: Option<f64>,
    pub max_page_size: i64,
    pub log_requests: bool,
    pub access_log: bool,
    pub sql_text_logged: bool,
    pub slow_query: Option<f64>,
    pub pool_size: u32,
    pub flush_every: Duration,
    pub json_logs: bool,
    /// QUERYAPIGATE_METADATA_CACHE_TTL: how long the key, query and connection rows a call reads are reused, as
    /// metacache.py reuses them - this process is one more instance, so a change is seen up to this much later
    pub metadata_ttl: Duration,
}

fn off(value: Option<String>) -> bool {
    value.is_some_and(|v| {
        matches!(
            v.trim().to_ascii_lowercase().as_str(),
            "0" | "false" | "no" | "off"
        )
    })
}

/// A whole number as Python's int() reads a setting: Ok(None) when it is unset or isn't one (int() raises), Err for
/// what int() could read otherwise - `1_0`, other scripts' digits, a number past i128 - which this leaves to Python.
fn whole(value: Option<String>) -> Result<Option<i128>, ()> {
    let Some(value) = value else { return Ok(None) };
    let text = value.trim();
    if !text.is_ascii() || text.contains('_') {
        return Err(());
    }
    let digits = text.strip_prefix(['+', '-']).unwrap_or(text);
    if digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_digit()) {
        return Ok(None);
    }
    text.parse::<i128>().map(Some).map_err(|_| ())
}

/// A positive number of seconds as config.py reads one: `default` when unset, invalid or negative, None for 0.
fn seconds(value: Option<String>, default: f64) -> Option<f64> {
    match value.map(|v| v.trim().parse::<f64>()) {
        Some(Ok(v)) if v > 0.0 => Some(v),
        Some(Ok(v)) if v.abs() < f64::MIN_POSITIVE => None, // 0 turns it off
        _ => Some(default),
    }
}

impl Settings {
    pub fn from(get: impl Fn(&str) -> Option<String>) -> Settings {
        // config.proxy_hops(): max(0, int(value)), 0 when it isn't a number
        let hops = whole(get("QUERYAPIGATE_TRUST_PROXY"));
        let proxy_hops = match hops {
            Ok(n) => usize::try_from(n.unwrap_or(0).max(0)).ok(),
            Err(()) => None,
        };
        // config.hsts_max_age(): the digits of a positive number, else none
        let hsts = get("QUERYAPIGATE_HSTS_MAX_AGE");
        let hsts_max_age = match &hsts {
            Some(v) if !v.is_ascii() => Err(()), // isdigit() is true of other scripts' digits too
            Some(v) if v.trim().bytes().all(|b| b.is_ascii_digit()) => whole(hsts.clone())
                .and_then(|n| n.map(u64::try_from).transpose().map_err(|_| ()))
                .map(|n| n.filter(|&n| n > 0)),
            _ => Ok(None),
        };
        // config.rate_limit(), and where it is counted: one budget for both processes, so only in Redis (ADR 0032)
        let client_limit = get("QUERYAPIGATE_RATE_LIMIT").map(|v| ratelimit::parse(v.trim()));
        let redis_url = get("QUERYAPIGATE_REDIS_URL")
            .map(|v| v.trim().to_string())
            .filter(|url| ratelimit::readable(url));
        let forward_everything = if client_limit.is_some_and(|limit| limit.is_none()) {
            Some("QUERYAPIGATE_RATE_LIMIT is a limit only Python reads")
        } else if client_limit.is_some() && redis_url.is_none() {
            // without Redis each process counts its own: Python counts them all
            Some(
                "QUERYAPIGATE_RATE_LIMIT is set without a QUERYAPIGATE_REDIS_URL the data plane reads",
            )
        } else if get("OTEL_EXPORTER_OTLP_ENDPOINT").is_some() {
            Some("OTEL_EXPORTER_OTLP_ENDPOINT is set")
        } else if hops.is_err() || proxy_hops.is_none() {
            Some("QUERYAPIGATE_TRUST_PROXY is a number only Python reads") // int() may read it otherwise
        } else if hsts_max_age.is_err() {
            Some("QUERYAPIGATE_HSTS_MAX_AGE is a number only Python reads")
        } else {
            None
        };
        let flag = |name: &str| {
            get(name).is_some_and(|v| {
                matches!(
                    v.trim().to_ascii_lowercase().as_str(),
                    "1" | "true" | "yes" | "on"
                )
            })
        };
        Settings {
            forward_everything,
            client_limit: client_limit.flatten(),
            redis_url,
            proxy_hops: proxy_hops.unwrap_or(0),
            hsts_max_age: hsts_max_age.unwrap_or(None),
            admin_key: get("QUERYAPIGATE_API_KEY")
                .map(|k| k.trim().to_string())
                .filter(|k| !k.is_empty()),
            secret_keys: get("QUERYAPIGATE_SECRET_KEY")
                .unwrap_or_default()
                .split(',')
                .map(|k| k.trim().to_string())
                .filter(|k| !k.is_empty())
                .collect(),
            mask_key: get("QUERYAPIGATE_MASK_KEY")
                .map(|k| k.trim().as_bytes().to_vec())
                .filter(|k| !k.is_empty()),
            query_timeout: seconds(get("QUERYAPIGATE_QUERY_TIMEOUT"), 30.0),
            max_page_size: get("QUERYAPIGATE_MAX_PAGE_SIZE")
                .and_then(|v| v.trim().parse::<i64>().ok())
                .map(|v| v.max(1))
                .unwrap_or(1000),
            log_requests: !off(get("QUERYAPIGATE_LOG_REQUESTS")),
            access_log: !off(get("QUERYAPIGATE_ACCESS_LOG")),
            sql_text_logged: get("QUERYAPIGATE_HISTORY_ADHOC_SQL")
                .is_none_or(|v| v.trim().to_lowercase() == "text"),
            slow_query: seconds(get("QUERYAPIGATE_SLOW_QUERY_THRESHOLD"), 1.0),
            pool_size: get("QUERYAPIGATE_DATAPLANE_POOL_SIZE")
                .and_then(|v| v.trim().parse().ok())
                .unwrap_or(16),
            flush_every: get("QUERYAPIGATE_HISTORY_FLUSH_INTERVAL")
                .and_then(|v| v.trim().parse::<f64>().ok())
                .filter(|v| v.is_finite()) // NaN.max(0.0) is 0.0
                .and_then(|v| Duration::try_from_secs_f64(v.max(0.0)).ok())
                .unwrap_or(Duration::from_secs(1)),
            json_logs: flag("QUERYAPIGATE_JSON_LOGS"),
            metadata_ttl: get("QUERYAPIGATE_METADATA_CACHE_TTL")
                .and_then(|v| v.trim().parse::<f64>().ok())
                .filter(|v| v.is_finite()) // NaN.max(0.0) is 0.0
                .and_then(|v| Duration::try_from_secs_f64(v.max(0.0)).ok())
                .unwrap_or(Duration::ZERO),
        }
    }
}

// --------------------------------------------------------------------------------------
// Plans
// --------------------------------------------------------------------------------------

#[derive(Debug, Deserialize, PartialEq, Eq, Hash, Clone)]
struct Digests {
    api_key: String,
    saved_query: String,
    connection: String,
}

#[derive(Debug, Deserialize)]
struct PlanKeyName {
    workspace: String,
    name: String,
}

#[derive(Debug, Deserialize)]
struct PlanQuery {
    name: String,
    version: i64,
}

#[derive(Debug, Deserialize)]
struct PlanConnection {
    name: String,
    dialect: String,
}

#[derive(Debug, Deserialize)]
struct MaskPlan {
    columns: usize,
    actions: Vec<Option<String>>,
    fired: Json,
}

/// dataplane.build()'s plan.
#[derive(Debug, Deserialize)]
struct Plan {
    v: u32,
    rows: Digests,
    key: PlanKeyName,
    query: PlanQuery,
    connection: PlanConnection,
    statement: db::Statement,
    search_path: Option<String>,
    mask: Option<MaskPlan>,
    expires_in: Option<f64>,
    used: Vec<String>,
    sql: String,
    /// The key's own rate limit, `[count, period]`: counted here, in Redis
    #[serde(default)]
    rate_limit: Option<(u64, u64)>,
}

struct Ready {
    plan: Plan,
    dialect: Dialect,
    actions: Option<Vec<Option<Action>>>,
    used: BTreeSet<String>,
    rate_limit: Option<Limit>,
}

enum Held {
    Plan(Arc<Ready>, Instant),
    /// Python's to answer, until then
    Python(Instant),
}

/// What a call read, for keeping the plan its forwarded response may carry.
struct Capture {
    digests: Digests,
    key: String,
    query: String,
    version: i64,
    connection: String,
    dialect: String,
}

/// Store rows held for the metadata cache: until when, and the rows.
type Reused = (Instant, Arc<Rows>);

enum Outcome {
    Served(Response<Body>),
    Forward(Option<Capture>),
    /// Forwarded after its rate limits were counted here: Python mustn't count them again
    Recounted(Counted),
}

use Outcome::{Forward, Served};

/// The rate-limit buckets a call was counted in here, as `(limit, remaining)`: what its answer's X-RateLimit-*
/// headers say, Python's or this process's.
#[derive(Default, Clone, Copy)]
struct Counted {
    client: Option<(u64, i64)>,
    key: Option<(u64, i64)>,
}

impl Counted {
    /// The call forwarded: saying what it was counted in, if anything.
    fn forward(self) -> Outcome {
        if self.client.is_none() && self.key.is_none() {
            Forward(None)
        } else {
            Outcome::Recounted(self)
        }
    }

    /// X-QAG-Rate-Counted: `client=<limit>,<remaining>; key=<limit>,<remaining>`, dataplane.counted()'s reading.
    fn header(&self) -> hyper::header::HeaderValue {
        let parts: Vec<String> = [("client", self.client), ("key", self.key)]
            .into_iter()
            .filter_map(|(bucket, counted)| {
                counted.map(|(limit, remaining)| format!("{bucket}={limit},{remaining}"))
            })
            .collect();
        hyper::header::HeaderValue::from_str(&parts.join("; ")).expect("digits")
    }

    /// after_request's X-RateLimit-* headers, as respond::rate_limits() adds them.
    fn headers(&self) -> respond::RateLimits {
        respond::RateLimits {
            client: self.client,
            key: self.key,
        }
    }
}

/// A 429 answered here, for its runs report: which bucket refused, whose, and the call's method.
#[derive(PartialEq, Eq, Hash)]
struct Refusal {
    scope: &'static str,
    /// The client's address for the server-wide limit, the key's name for its own
    who: String,
    method: String,
}

/// What the data plane queues for its reports to Python.
enum Queued {
    Run(Json),
    /// A 429 was noted (App::refused): the report it waits for is sent
    Refused,
}

// --------------------------------------------------------------------------------------
// The data plane
// --------------------------------------------------------------------------------------

pub struct App {
    forward: Forwarder,
    settings: Settings,
    logger: Logger,
    store: Option<Store>,
    pools: db::Pools,
    plans: Mutex<HashMap<Digests, Held>>,
    used: Mutex<HashMap<(String, String, bool), Instant>>,
    store_mask_key: Mutex<Option<Vec<u8>>>,
    runs: mpsc::Sender<Queued>,
    /// With Redis: where rate limits are counted (ADR 0032)
    limiter: Option<Limiter>,
    /// The 429s answered since the last report: the seconds each took, by refusal
    refused: Mutex<HashMap<Refusal, Vec<f64>>>,
    /// Runs served but not queued for Python (the queue was full): sent with the next report, for its /metrics
    dropped: AtomicU64,
    /// Reports being sent, at most REPORTS_IN_FLIGHT: all of its permits free means none is.
    in_flight: Arc<Semaphore>,
    /// (kind, key) -> (until, rows): the store rows metacache.py would reuse, for QUERYAPIGATE_METADATA_CACHE_TTL
    rows: Mutex<HashMap<(char, String), Reused>>,
    /// With a run store (ADR 0029): where its runs are written, by this process, before Python is told of them
    run_store: Option<Arc<RunStore>>,
}

impl App {
    pub fn new(
        forward: Forwarder,
        settings: Settings,
        store: Option<Store>,
        run_store: Option<RunStore>,
    ) -> Arc<App> {
        let (runs, queue) = mpsc::channel(10_000);
        let logger = Logger {
            json: settings.json_logs,
        };
        let limiter = settings
            .redis_url
            .as_deref()
            .and_then(|url| match Limiter::new(url) {
                Ok(limiter) => Some(limiter),
                Err(error) => {
                    eprintln!(
                        "queryapigate-dataplane: rate-limited calls are forwarded - Redis: {error}"
                    );
                    None
                }
            });
        let app = Arc::new(App {
            forward,
            settings,
            logger,
            store,
            pools: db::Pools::default(),
            plans: Mutex::new(HashMap::new()),
            used: Mutex::new(HashMap::new()),
            store_mask_key: Mutex::new(None),
            runs,
            limiter,
            refused: Mutex::new(HashMap::new()),
            dropped: AtomicU64::new(0),
            in_flight: Arc::new(Semaphore::new(REPORTS_IN_FLIGHT)),
            rows: Mutex::new(HashMap::new()),
            run_store: run_store.map(Arc::new),
        });
        tokio::spawn(report(app.clone(), queue));
        if app.run_store.is_some() {
            tokio::spawn(keep_lease(app.clone()));
        }
        app
    }

    pub async fn handle(&self, request: Request<Incoming>, peer: SocketAddr) -> Response<Body> {
        let started = Instant::now();
        if !candidate(&request) {
            let (parts, body) = request.into_parts();
            return forward::relay(
                self.forward
                    .send(parts, body.boxed(), peer.ip(), false, None)
                    .await,
            );
        }
        let (parts, body) = request.into_parts();
        let body = match body.collect().await {
            Ok(collected) => collected.to_bytes(),
            Err(_) => {
                return forward::error_response(
                    hyper::StatusCode::BAD_REQUEST,
                    "The request body could not be read",
                    "invalid_body",
                );
            }
        };
        let outcome = self.serve(&parts, &body, peer.ip(), started).await;
        match outcome {
            Served(response) => response,
            Outcome::Recounted(counted) => {
                let full = Full::new(body).map_err(|never| match never {}).boxed();
                forward::relay(
                    self.forward
                        .send(parts, full, peer.ip(), false, Some(counted.header()))
                        .await,
                )
            }
            Forward(capture) => {
                let full = Full::new(body).map_err(|never| match never {}).boxed();
                let response = self
                    .forward
                    .send(parts, full, peer.ip(), capture.is_some(), None)
                    .await;
                if let (Some(capture), Ok(response)) = (capture, &response) {
                    let digests = capture.digests.clone();
                    let carried = response.headers().contains_key(PLAN_HEADER);
                    self.keep_plan(capture, response.headers());
                    if !carried && response.status() == hyper::StatusCode::OK {
                        // Answered, with no plan at all: a call Python keeps (a write, a cached query) - stop asking a
                        // while. A plan that came but didn't fit (the rows changed in between) is just not kept.
                        self.python_only(&digests);
                    }
                }
                forward::relay(response)
            }
        }
    }

    /// Keep the plan Python's answer carries, if it is one for exactly this call - whether it did.
    fn keep_plan(&self, capture: Capture, headers: &HeaderMap) -> bool {
        let Some(value) = headers.get(PLAN_HEADER).and_then(|v| v.to_str().ok()) else {
            return false;
        };
        let Ok(bytes) = base64::engine::general_purpose::URL_SAFE.decode(value) else {
            return false;
        };
        let Ok(plan) = serde_json::from_slice::<Plan>(&bytes) else {
            return false;
        };
        // Only for exactly the rows this call read, and what they say
        let matches = plan.v == 1
            && plan.rows == capture.digests
            && plan.key.workspace == "default"
            && plan.key.name == capture.key
            && plan.query.name == capture.query
            && plan.query.version == capture.version
            && plan.connection.name == capture.connection
            && plan.connection.dialect == capture.dialect;
        let Ok(dialect) = plan.connection.dialect.parse::<Dialect>() else {
            return false;
        };
        if !matches {
            return false;
        }
        let actions = match &plan.mask {
            None => None,
            Some(mask) => match mask
                .actions
                .iter()
                .map(|a| Action::named(a.as_deref()))
                .collect::<Result<Vec<_>, _>>()
            {
                Ok(actions) if actions.len() == mask.columns => Some(actions),
                _ => return false,
            },
        };
        let ttl = match (&plan.mask, plan.expires_in) {
            (Some(_), Some(seconds)) => Duration::from_secs_f64(seconds.max(0.0)),
            (Some(_), None) => return false,
            (None, _) => UNMASKED_PLAN_TTL,
        };
        let rate_limit = match plan.rate_limit {
            None => None,
            Some(pair) => match Limit::from_plan(pair) {
                Some(limit) => Some(limit),
                None => return false,
            },
        };
        let used = plan.used.iter().cloned().collect();
        let ready = Arc::new(Ready {
            plan,
            dialect,
            actions,
            used,
            rate_limit,
        });
        let mut plans = self.plans.lock().expect("plans");
        if plans.len() >= MAX_PLANS {
            plans.clear();
        }
        plans.insert(capture.digests, Held::Plan(ready, Instant::now() + ttl));
        true
    }

    fn python_only(&self, digests: &Digests) {
        self.plans.lock().expect("plans").insert(
            digests.clone(),
            Held::Python(Instant::now() + PYTHON_ONLY_TTL),
        );
    }

    async fn mask_key(&self) -> Option<Vec<u8>> {
        if let Some(key) = &self.settings.mask_key {
            return Some(key.clone());
        }
        if let Some(key) = self.store_mask_key.lock().expect("key").clone() {
            return Some(key);
        }
        // The store's key, made once by Python and never changed (masking.hash_key()); none yet: Python makes it
        let key = self
            .store
            .as_ref()?
            .server_secret("mask_hash_key")
            .await
            .ok()??
            .into_bytes();
        *self.store_mask_key.lock().expect("key") = Some(key.clone());
        Some(key)
    }

    /// A store read, reused for QUERYAPIGATE_METADATA_CACHE_TTL when it is set - or None when it fails.
    async fn cached<F, Fut>(&self, kind: char, key: &str, load: F) -> Option<Arc<Rows>>
    where
        F: FnOnce() -> Fut,
        Fut: std::future::Future<Output = Result<Rows, sqlx::Error>>,
    {
        let ttl = self.settings.metadata_ttl;
        if ttl.is_zero() {
            return load().await.ok().map(Arc::new);
        }
        let now = Instant::now();
        if let Some((until, rows)) = self
            .rows
            .lock()
            .expect("rows")
            .get(&(kind, key.to_string()))
            && *until > now
        {
            return Some(rows.clone());
        }
        let rows = Arc::new(load().await.ok()?);
        let mut cache = self.rows.lock().expect("rows");
        if cache.len() >= MAX_PLANS {
            cache.clear();
        }
        cache.insert((kind, key.to_string()), (now + ttl, rows.clone()));
        Some(rows)
    }

    async fn serve(
        &self,
        parts: &hyper::http::request::Parts,
        body: &Bytes,
        peer: IpAddr,
        started: Instant,
    ) -> Outcome {
        if self.settings.forward_everything.is_some() {
            return Forward(None);
        }
        // The client as Python finds it behind trusted proxies: who allowed_ips, the log and the run name
        let Some(client) = proxy::client(&parts.headers, peer, self.settings.proxy_hops) else {
            return Forward(None);
        };
        let peer = client.address;
        let hsts = self.settings.hsts_max_age.filter(|_| client.secure);
        let Some(store) = &self.store else {
            return Forward(None);
        };
        let Some(name) = query_name(parts.uri.path()) else {
            return Forward(None);
        };
        let Some(request) = Call::read(parts, body, &self.settings) else {
            return Forward(None);
        };

        // The key and the saved query, read together; then the connection the query names
        let hash = auth::hash(&request.key);
        let (key_rows, query_rows) = tokio::join!(
            self.cached('k', &hash, || store.key(&hash)),
            self.cached('q', &name, || store.query("default", &name)),
        );
        let (Some(key_rows), Some(query_rows)) = (key_rows, query_rows) else {
            return Forward(None);
        };
        let clock = Clock::now();
        let Some(caller) = auth::authenticate(
            &request.key,
            self.settings.admin_key.as_deref(),
            &key_rows,
            Some(peer),
            &clock,
        ) else {
            return Forward(None);
        };
        if caller.workspace != "default" {
            return Forward(None);
        }
        let Some((version, fields)) = published(&query_rows) else {
            return Forward(None);
        };
        let Some(connection_name) = fields
            .get("connection_name")
            .and_then(Json::as_str)
            .map(str::to_string)
        else {
            return Forward(None);
        };
        let Some(connection_rows) = self
            .cached('c', &connection_name, || {
                store.connection("default", &connection_name)
            })
            .await
        else {
            return Forward(None);
        };
        if connection_rows.rows.len() != 1
            || connection_rows.get(0, "active").and_then(Value::int) != Some(1)
        {
            return Forward(None);
        }
        let dialect_name = connection_rows
            .get(0, "db")
            .and_then(Value::text)
            .unwrap_or("")
            .to_string();
        let digests = Digests {
            api_key: key_rows.digest(),
            saved_query: query_rows.digest(),
            connection: connection_rows.digest(),
        };
        let capture = Capture {
            digests: digests.clone(),
            key: caller.name.clone(),
            query: name.clone(),
            version,
            connection: connection_name.clone(),
            dialect: dialect_name.clone(),
        };
        {
            let use_key = (
                caller.workspace.clone(),
                caller.name.clone(),
                caller.previous,
            );
            let mut used = self.used.lock().expect("used");
            let now = Instant::now();
            if used
                .get(&use_key)
                .is_none_or(|at| now.duration_since(*at) >= USE_RECORD_INTERVAL)
            {
                used.insert(use_key, now);
                return Forward(Some(capture)); // Python records this key's use
            }
        }
        let ready = {
            let mut plans = self.plans.lock().expect("plans");
            match plans.get(&digests) {
                Some(Held::Plan(ready, until)) if *until > Instant::now() => ready.clone(),
                Some(Held::Python(until)) if *until > Instant::now() => return Forward(None),
                Some(_) => {
                    plans.remove(&digests);
                    return Forward(Some(capture));
                }
                None => return Forward(Some(capture)),
            }
        };
        let plan = &ready.plan;
        if plan.key.name != caller.name
            || plan.query.name != name
            || plan.query.version != version
            || plan.connection.name != connection_name
            || plan.connection.dialect != dialect_name
        {
            return Forward(Some(capture));
        }
        // A limited key's budget is Redis's: without a connection to it here, Python's to count
        if ready.rate_limit.is_some() && self.limiter.is_none() {
            return Forward(None);
        }

        // Parameters, as Python would resolve them; a `from_claim` one is Python's (app.bind_claims())
        let declared = fields.get("query_parameters");
        if let Some(Json::Object(specs)) = declared
            && specs.values().any(|spec| spec.get("from_claim").is_some())
        {
            return Forward(None);
        }
        let Ok(values) = params::resolve(declared, &request.params, &ready.used) else {
            return Forward(None);
        };

        // The connection
        let details = connection_rows
            .get(0, "details_json")
            .and_then(Value::text)
            .unwrap_or("");
        let Some(target) = db::target(&dialect_name, details, &self.settings.secret_keys) else {
            self.python_only(&digests);
            return Forward(None);
        };
        let pool = match self
            .pools
            .get(&connection_name, &target, self.settings.pool_size)
            .await
        {
            Ok(pool) => pool,
            Err(Failure::Decline) => {
                self.python_only(&digests);
                return Forward(None);
            }
            Err(_) => return Forward(None),
        };
        let mask_key = match &plan.mask {
            Some(_) => match self.mask_key().await {
                Some(key) => Some(key),
                None => return Forward(None),
            },
            None => None,
        };
        let timeout = match (request.timeout, self.settings.query_timeout) {
            (None, server) => server,
            (Some(asked), None) => Some(asked),
            (Some(asked), Some(server)) => Some(asked.min(server)),
        };
        // Python's offset is unbounded, and the database refuses one this large: forward it, and Python answers
        let Some(offset) = (request.page - 1).checked_mul(request.page_size) else {
            return Forward(None);
        };
        let limit = request.page_size;
        let request_id = respond::request_id(request.request_id.as_deref());

        // The rate limits, now that nothing before the query forwards the call: the client's, then the key's, as
        // Python checks them - so a refusal by the client's leaves the key's bucket alone, as in Python
        let mut counted = Counted::default();
        for (scope, limit) in [
            ("client", self.settings.client_limit),
            ("key", ready.rate_limit),
        ] {
            let (Some(limit), Some(limiter)) = (limit, &self.limiter) else {
                continue;
            };
            let bucket = if scope == "client" {
                forward::peer_text(peer)
            } else {
                format!("default~{}", caller.name) // ratelimit.budget_name()
            };
            let Some(verdict) = limiter.hit(scope, limit, &bucket).await else {
                return counted.forward(); // Redis failed: what wasn't counted here, Python counts
            };
            let mine = Some((limit.count, verdict.remaining));
            if scope == "client" {
                counted.client = mine;
            } else {
                counted.key = mine;
            }
            if !verdict.allowed {
                let (message, logged_key, who) = if scope == "client" {
                    ("Rate limit exceeded", "-", bucket)
                } else {
                    (
                        "Rate limit exceeded for this API key",
                        caller.name.as_str(),
                        caller.name.clone(),
                    )
                };
                let response =
                    respond::too_many(message, verdict.retry_after, &request_id, counted.headers());
                let response = respond::hsts(response, hsts);
                let refusal = Refusal {
                    scope,
                    who,
                    method: parts.method.to_string(),
                };
                self.refuse(
                    parts,
                    refusal,
                    &request_id,
                    logged_key,
                    started,
                    &response,
                    peer,
                );
                return Served(response);
            }
        }

        let executed_at = clock.stored.clone();
        if self.settings.log_requests {
            self.executing(
                &request_id,
                &caller.name,
                &connection_name,
                &dialect_name,
                limit,
                offset,
                timeout,
                &plan.sql,
            );
        }

        // The query
        let query_started = Instant::now();
        let output = db::run(
            &pool,
            &plan.statement,
            &values,
            limit,
            offset,
            timeout,
            plan.search_path.as_deref(),
        )
        .await;
        let query_seconds = query_started.elapsed().as_secs_f64();
        self.slow(
            &request_id,
            &caller.name,
            &connection_name,
            &dialect_name,
            query_seconds,
            &plan.sql,
        );
        let entry = |fields: Json| {
            let mut entry = Map::new();
            entry.insert("executed_at".into(), json!(executed_at));
            entry.insert("connection_name".into(), json!(connection_name));
            entry.insert("request_id".into(), json!(request_id));
            entry.insert("key_name".into(), json!(caller.name));
            entry.insert("transport".into(), json!("rest"));
            if let Json::Object(more) = fields {
                entry.extend(more);
            }
            Json::Object(entry)
        };
        let output = match output {
            Ok(output) => output,
            Err(Failure::Timeout) => {
                let limit_seconds = timeout.unwrap_or(0.0);
                let (response, message) = respond::timed_out(limit_seconds, &request_id);
                let response =
                    respond::rate_limits(respond::hsts(response, hsts), counted.headers());
                let entry =
                    entry(json!({"status": "error", "error": message, "code": "query_timeout"}));
                self.finish(
                    parts,
                    &request_id,
                    &caller.name,
                    plan,
                    entry,
                    &dialect_name,
                    query_seconds,
                    0.0,
                    504,
                    started,
                    &response,
                    peer,
                );
                return Served(response);
            }
            Err(Failure::Decline) => {
                self.python_only(&digests);
                return counted.forward();
            }
            Err(Failure::Unsent) => return counted.forward(), // this call's values only: the plan stays
            Err(Failure::Database(_)) => return counted.forward(), // Python runs it again, and answers as it does
        };

        // The page
        let serialize_started = Instant::now();
        let has_more = output.rows.len() as i64 > limit;
        let rows = output.rows.len().min(limit as usize);
        let actions = ready
            .actions
            .as_ref()
            .zip(plan.mask.as_ref())
            .map(|(a, m)| (a.as_slice(), m.columns));
        let Ok(body) = render::body(ready.dialect, &output, rows, actions, mask_key.as_deref())
        else {
            self.python_only(&digests);
            return counted.forward();
        };
        let serialization_seconds = serialize_started.elapsed().as_secs_f64();
        let response = respond::page(
            body,
            request.page,
            request.page_size,
            rows,
            has_more,
            &request_id,
        );
        let response = respond::rate_limits(respond::hsts(response, hsts), counted.headers());
        let mut fields = json!({
            "status": "success", "rows": rows, "duration_ms": (query_seconds * 1000.0).round() as i64,
            "serialization_ms": (serialization_seconds * 10000.0).round() / 10.0,
        });
        if let Some(mask) = &plan.mask
            && mask.fired.as_array().is_some_and(|f| !f.is_empty())
        {
            fields["masked"] = mask.fired.clone();
        }
        let entry = entry(fields);
        self.finish(
            parts,
            &request_id,
            &caller.name,
            plan,
            entry,
            &dialect_name,
            query_seconds,
            serialization_seconds,
            200,
            started,
            &response,
            peer,
        );
        Served(response)
    }

    #[allow(clippy::too_many_arguments)]
    fn executing(
        &self,
        rid: &str,
        key: &str,
        connection: &str,
        dialect: &str,
        limit: i64,
        offset: i64,
        timeout: Option<f64>,
        sql: &str,
    ) {
        let timeout_text = timeout
            .map(crate::pyfmt::repr)
            .unwrap_or_else(|| "None".into());
        let logged = self.logged_sql(sql);
        let mut extra = Map::new();
        extra.insert("connection".into(), json!(connection));
        extra.insert("dialect".into(), json!(dialect));
        extra.insert("limit".into(), json!(limit));
        extra.insert("offset".into(), json!(offset));
        extra.insert(
            "timeout".into(),
            timeout.map(|t| json!(t)).unwrap_or(Json::Null),
        );
        extra.insert("sql_hash".into(), json!(sql_hash(sql)));
        self.logger.line("INFO", rid, key, &format!(
            "Executing on {connection} ({dialect}), limit={limit} offset={offset} timeout={timeout_text}: {logged}"
        ), extra);
    }

    fn slow(&self, rid: &str, key: &str, connection: &str, dialect: &str, seconds: f64, sql: &str) {
        let Some(threshold) = self.settings.slow_query else {
            return;
        };
        if seconds < threshold {
            return;
        }
        let mut extra = Map::new();
        extra.insert("connection".into(), json!(connection));
        extra.insert("dialect".into(), json!(dialect));
        extra.insert(
            "duration_ms".into(),
            json!((seconds * 10000.0).round() / 10.0),
        );
        self.logger.line(
            "WARNING",
            rid,
            key,
            &format!(
                "Slow query on {connection} ({dialect}): {:.1}ms - {}",
                seconds * 1000.0,
                self.logged_sql(sql)
            ),
            extra,
        );
    }

    fn logged_sql(&self, sql: &str) -> String {
        if self.settings.sql_text_logged {
            sql.to_string()
        } else {
            format!("(text not logged; sha256 {})", sql_hash(sql))
        }
    }

    /// The run's report to Python, and the request's own log lines.
    #[allow(clippy::too_many_arguments)]
    fn finish(
        &self,
        parts: &hyper::http::request::Parts,
        rid: &str,
        key: &str,
        plan: &Plan,
        entry: Json,
        dialect: &str,
        query_seconds: f64,
        serialization_seconds: f64,
        status: u16,
        started: Instant,
        response: &Response<Body>,
        peer: IpAddr,
    ) {
        let seconds = started.elapsed().as_secs_f64();
        // The entry as Python stores it (json.dumps), and what /metrics counts the run by: Python only queues it
        let field = |name: &str| entry.get(name).cloned().unwrap_or(Json::Null);
        let run = json!({
            "query": plan.query.name, "version": plan.query.version, "entry": crate::pyfmt::dumps(&entry),
            "executed_at": field("executed_at"), "status": field("status"), "key": field("key_name"),
            "connection": field("connection_name"), "transport": field("transport"), "rows": field("rows"),
            "dialect": dialect, "method": parts.method.as_str(), "http_status": status, "seconds": seconds,
            "query_seconds": query_seconds, "serialization_seconds": serialization_seconds,
            "code": field("code"), "masked": entry.get("masked").and_then(Json::as_array).is_some_and(|m| !m.is_empty()),
        });
        if self.runs.try_send(Queued::Run(run)).is_err() {
            // full: dropped, as Python's own history queue drops when it can't keep up - and counted the same way
            self.dropped.fetch_add(1, Ordering::Relaxed);
        }
        self.logged(
            parts,
            rid,
            key,
            status,
            seconds,
            Some(serialization_seconds),
            response,
            peer,
        );
    }

    /// A 429 this answered (ADR 0032): noted for the next runs report - Python counts it in /metrics and the alerts,
    /// as one of its own - and logged as Python logs one. It isn't a run.
    #[allow(clippy::too_many_arguments)]
    fn refuse(
        &self,
        parts: &hyper::http::request::Parts,
        refusal: Refusal,
        rid: &str,
        key: &str,
        started: Instant,
        response: &Response<Body>,
        peer: IpAddr,
    ) {
        let seconds = started.elapsed().as_secs_f64();
        let first = {
            let mut refused = self.refused.lock().expect("refused");
            let first = refused.is_empty();
            refused.entry(refusal).or_default().push(seconds);
            first
        };
        if first {
            // The report that takes it: when the queue is full, one is coming anyway
            let _ = self.runs.try_send(Queued::Refused);
        }
        self.logged(parts, rid, key, 429, seconds, None, response, peer);
    }

    /// A call's request log line and access log line, as Python's after_request and gunicorn write them.
    #[allow(clippy::too_many_arguments)]
    fn logged(
        &self,
        parts: &hyper::http::request::Parts,
        rid: &str,
        key: &str,
        status: u16,
        seconds: f64,
        serialization_seconds: Option<f64>,
        response: &Response<Body>,
        peer: IpAddr,
    ) {
        let path = percent_encoding::percent_decode_str(parts.uri.path())
            .decode_utf8_lossy()
            .into_owned();
        if self.settings.log_requests {
            let mut extra = Map::new();
            extra.insert("method".into(), json!(parts.method.as_str()));
            extra.insert("path".into(), json!(path));
            extra.insert("status".into(), json!(status));
            extra.insert(
                "duration_ms".into(),
                json!((seconds * 10000.0).round() / 10.0),
            );
            if let Some(serialization_seconds) = serialization_seconds.filter(|_| status == 200) {
                extra.insert(
                    "serialization_ms".into(),
                    json!((serialization_seconds * 10000.0).round() / 10.0),
                );
            }
            self.logger.line(
                "INFO",
                rid,
                key,
                &format!(
                    "{} {path} -> {status} in {:.1}ms",
                    parts.method,
                    seconds * 1000.0
                ),
                extra,
            );
        }
        if self.settings.access_log {
            // A body built here has no Content-Length yet (hyper adds it as it sends): its size is exact all the same
            let bytes = response
                .headers()
                .get(header::CONTENT_LENGTH)
                .and_then(|v| v.to_str().ok()?.parse().ok())
                .or_else(|| hyper::body::Body::size_hint(response.body()).exact())
                .and_then(|n| usize::try_from(n).ok());
            let agent = parts
                .headers
                .get(header::USER_AGENT)
                .and_then(|v| v.to_str().ok())
                .unwrap_or("-");
            log::access(
                &forward::peer_text(peer),
                parts.method.as_str(),
                &path,
                status,
                bytes.unwrap_or(0),
                seconds,
                rid,
                agent,
            );
        }
    }
}

/// Reports at once, at most: one at a time kept up with about 8,600 runs/s - well short of what this serves.
const REPORTS_IN_FLIGHT: usize = 4;

/// Runs in one insert into the run store at most (ADR 0029 §4): one interval's at 100,000 calls a second.
const RUN_STORE_BATCH: usize = 100_000;

/// How long a shutdown waits for the runs still queued to reach Python.
const DRAIN_FOR: Duration = Duration::from_secs(10);

impl App {
    /// On shutdown, after drain(): the run store's lease given back.
    pub async fn release_lease(&self) {
        if let Some(run_store) = &self.run_store {
            run_store.release(&self.forward).await;
        }
    }

    /// On shutdown: every run still queued sent to Python, as Python's own history writer flushes at exit - for up to
    /// DRAIN_FOR. Runs it can't send by then are lost, as Python's are on a hard kill.
    pub async fn drain(&self) {
        let until = tokio::time::Instant::now() + DRAIN_FOR;
        while self.runs.capacity() < self.runs.max_capacity() && tokio::time::Instant::now() < until
        {
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
        // The batch being gathered is sent at its interval's end, then every report finishes
        tokio::time::sleep_until(
            (tokio::time::Instant::now() + self.settings.flush_every).min(until),
        )
        .await;
        let _ =
            tokio::time::timeout_at(until, self.in_flight.acquire_many(REPORTS_IN_FLIGHT as u32))
                .await;
    }
}

/// Sends served runs to Python in batches, every QUERYAPIGATE_HISTORY_FLUSH_INTERVAL (each at once for 0), up to
/// REPORTS_IN_FLIGHT at once, each with how many runs were dropped since the one before - a report Python didn't
/// take included.
async fn report(app: Arc<App>, mut queue: mpsc::Receiver<Queued>) {
    let every = app.settings.flush_every;
    // A run store wants few, large inserts: each is a part ClickHouse later merges, through three views
    let most = if app.run_store.is_some() {
        RUN_STORE_BATCH
    } else {
        500
    };
    loop {
        let Some(first) = queue.recv().await else {
            return;
        };
        let mut batch = Vec::new();
        if let Queued::Run(run) = first {
            batch.push(run);
        }
        if !every.is_zero() {
            let deadline = tokio::time::Instant::now() + every;
            while batch.len() < most {
                match tokio::time::timeout_at(deadline, queue.recv()).await {
                    Ok(Some(Queued::Run(run))) => batch.push(run),
                    Ok(Some(Queued::Refused)) => {}
                    Ok(None) | Err(_) => break,
                }
            }
        }
        // The 429s since the last report, each set with its calls' seconds: governance.note_refused()'s
        let rate_limited: Vec<Json> = std::mem::take(&mut *app.refused.lock().expect("refused"))
            .into_iter()
            .map(|(refusal, seconds)| {
                let mut refused =
                    json!({"scope": refusal.scope, "method": refusal.method, "seconds": seconds});
                if refusal.scope == "client" {
                    refused["client"] = json!(refusal.who);
                } else {
                    refused["workspace"] = json!("default");
                    refused["key"] = json!(refusal.who);
                }
                refused
            })
            .collect();
        if batch.is_empty() && rate_limited.is_empty() {
            continue;
        }
        let Ok(permit) = app.in_flight.clone().acquire_owned().await else {
            return;
        };
        let (app, count) = (app.clone(), batch.len());
        tokio::spawn(async move {
            // With a run store this process writes the runs itself; Python is told only to count them
            let recorded = match &app.run_store {
                Some(run_store) if !batch.is_empty() => Some(record(run_store, &batch).await),
                _ => None,
            };
            let carried = app.dropped.swap(0, Ordering::Relaxed);
            let mut dropped = carried;
            let in_store = matches!(recorded, Some(Ok(_)));
            let mut body = match recorded {
                Some(Ok((kept, sampled_out))) => {
                    json!({"runs": batch, "dropped": dropped, "recorded": true,
                                                        "kept": kept, "sampled_out": sampled_out})
                }
                Some(Err(error)) => {
                    eprintln!(
                        "queryapigate-dataplane: writing {count} runs to the run store failed: {error}"
                    );
                    dropped += count as u64;
                    json!({"runs": batch, "dropped": dropped, "recorded": true, "kept": 0, "sampled_out": 0})
                }
                None => json!({"runs": batch, "dropped": dropped}),
            };
            if !rate_limited.is_empty() {
                body["rate_limited"] = json!(rate_limited);
            }
            let body = body.to_string();
            let failed = match app.forward.post("/internal/dataplane/runs", body).await {
                Ok(status) if status.as_u16() == 204 => None,
                Ok(status) => Some(format!("got {status}")),
                Err(error) => Some(format!("failed: {error}")),
            };
            if let Some(why) = failed {
                // Not sent again (Python may have recorded some), but counted: as dropped, with the next report -
                // unless they're in the run store already, when only their counts in /metrics are lost
                let lost = if in_store {
                    carried
                } else {
                    dropped.max(count as u64 + carried)
                };
                app.dropped.fetch_add(lost, Ordering::Relaxed);
                eprintln!("queryapigate-dataplane: reporting {count} runs {why}");
            }
            drop(permit);
        });
    }
}

/// A batch of served runs into the run store: (kept in history, sampled out of it).
async fn record(run_store: &RunStore, batch: &[Json]) -> Result<(u64, u64), String> {
    let rows: Vec<_> = batch
        .iter()
        .filter_map(|run| {
            let status = run.get("status").and_then(Json::as_str).unwrap_or("");
            runstore::event_row(run, run_store.keep(status))
        })
        .collect();
    let kept = rows.iter().filter(|row| row.in_history).count() as u64;
    let total = rows.len() as u64;
    let written = run_store.write(rows).await? as u64;
    if written < total {
        return Err(format!(
            "{} runs past what its lease allows in a burst",
            total - written
        ));
    }
    Ok((kept, total - kept))
}

/// The run store's lease on its ids, kept: leased at startup - retried every second until Python answers - and
/// renewed every RENEW_EVERY.
async fn keep_lease(app: Arc<App>) {
    let Some(run_store) = app.run_store.clone() else {
        return;
    };
    loop {
        let wait = match run_store.keep_lease(&app.forward).await {
            Ok(()) => runstore::RENEW_EVERY,
            Err(error) => {
                eprintln!("queryapigate-dataplane: run store lease: {error}");
                Duration::from_secs(1)
            }
        };
        tokio::time::sleep(wait).await;
    }
}

fn sql_hash(sql: &str) -> String {
    use sha2::{Digest, Sha256};
    hex::encode(Sha256::digest(sql.as_bytes()))
}

/// Whether a request could be one this serves - worth reading its body for. Everything else streams straight through.
fn candidate(request: &Request<Incoming>) -> bool {
    (request.method() == Method::GET || request.method() == Method::POST)
        && request.uri().path().starts_with("/q/")
        && request.headers().contains_key("x-api-key")
        && small_body(request.headers())
}

/// The largest body read before the key is checked: a `/q/` call's parameters are a few hundred bytes.
const MAX_READ_BODY: u64 = 64 * 1024;

/// Whether a request's body is one this may read whole before the key is checked: no body, or a Content-Length of
/// at most MAX_READ_BODY. Anything else - chunked, or larger - streams through to Python, which reads it as it would.
fn small_body(headers: &HeaderMap) -> bool {
    if headers.contains_key(header::TRANSFER_ENCODING) {
        return false;
    }
    match headers.get(header::CONTENT_LENGTH) {
        None => true,
        Some(value) => value
            .to_str()
            .ok()
            .and_then(|v| v.trim().parse::<u64>().ok())
            .is_some_and(|n| n <= MAX_READ_BODY),
    }
}

/// `/q/<name>`: the one path segment, percent-decoded as Werkzeug decodes it - or None.
fn query_name(path: &str) -> Option<String> {
    let raw = path.strip_prefix("/q/")?;
    if raw.is_empty() || raw.contains('/') {
        return None;
    }
    let name = percent_encoding::percent_decode_str(raw)
        .decode_utf8()
        .ok()?
        .into_owned();
    if name.is_empty() || name.contains('/') {
        None
    } else {
        Some(name)
    }
}

/// The published version's number and fields (store.select_version()), from the rows store.query() read.
fn published(rows: &Rows) -> Option<(i64, Map<String, Json>)> {
    let published = rows.get(0, "published_version").and_then(Value::int)?;
    let at = (0..rows.rows.len())
        .find(|&i| rows.get(i, "version").and_then(Value::int) == Some(published))?;
    let fields: Map<String, Json> =
        serde_json::from_str(rows.get(at, "fields_json").and_then(Value::text)?).ok()?;
    if !fields.get("sql_query").is_some_and(Json::is_string)
        || fields.get("query_type").is_some_and(|t| t != "sql")
    {
        return None;
    }
    Some((published, fields))
}

/// What a request asks for, read as app.py reads it - or None for anything this leaves to Python.
struct Call {
    key: String,
    request_id: Option<String>,
    params: HashMap<String, Supplied>,
    page: i64,
    page_size: i64,
    timeout: Option<f64>,
}

impl Call {
    fn read(
        parts: &hyper::http::request::Parts,
        body: &Bytes,
        settings: &Settings,
    ) -> Option<Call> {
        let headers = &parts.headers;
        // CORS, a bearer token, a session: Python's
        if headers.contains_key(header::ORIGIN) || headers.contains_key(header::AUTHORIZATION) {
            return None;
        }
        let mut keys = headers.get_all("x-api-key").iter();
        let key = keys.next()?.to_str().ok()?.to_string();
        if keys.next().is_some() || key.is_empty() {
            return None;
        }
        let request_id = headers
            .get("x-request-id")
            .and_then(|v| v.to_str().ok())
            .map(str::to_string);

        // The query string: the first value of each name, as request.args.get() and .items() give
        let mut args: Vec<(String, String)> = Vec::new();
        if let Some(query) = parts.uri.query() {
            for (name, value) in form_urlencoded::parse(query.as_bytes()) {
                if !args.iter().any(|(n, _)| *n == name) {
                    args.push((name.into_owned(), value.into_owned()));
                }
            }
        }
        let arg = |name: &str| {
            args.iter()
                .find(|(n, _)| n == name)
                .map(|(_, v)| v.as_str())
        };
        if arg("connection_name").is_some() || arg("version").is_some() || arg("stream").is_some() {
            return None;
        }

        // The body: JSON when it says so (request.is_json), an object - else nothing Python reads either
        let mut object = Map::new();
        if parts.method == Method::POST {
            let content_type = headers
                .get(header::CONTENT_TYPE)
                .and_then(|v| v.to_str().ok())
                .unwrap_or("");
            let mimetype = content_type
                .split(';')
                .next()
                .unwrap_or("")
                .trim()
                .to_ascii_lowercase();
            let is_json = mimetype == "application/json"
                || (mimetype.starts_with("application/") && mimetype.ends_with("+json"));
            if is_json && !body.iter().all(u8::is_ascii_whitespace) {
                match serde_json::from_slice::<Json>(body).ok()? {
                    Json::Object(map) => object = map,
                    _ => return None,
                }
            } else if !is_json && !body.is_empty() {
                return None; // what Python does with a body it doesn't read is its own to say
            }
        }
        if object.contains_key("version") || object.contains_key("connection_name") {
            return None;
        }

        // format: json only (get_output_format())
        let format = match arg("format").filter(|f| !f.is_empty()) {
            Some(f) => f.to_string(),
            None => match object.get("format") {
                None | Some(Json::Null) | Some(Json::Bool(false)) => "json".into(),
                Some(Json::String(s)) if s.is_empty() => "json".into(),
                Some(Json::String(s)) => s.clone(),
                Some(_) => return None,
            },
        };
        if !format.eq_ignore_ascii_case("json") {
            return None;
        }
        // timeout (get_timeout()): may lower the server's limit, never raise it
        let timeout = match arg("timeout") {
            Some("") => None,
            Some(text) => Some(plain_seconds(text)?),
            None => match object.get("timeout") {
                None | Some(Json::Null) => None,
                Some(Json::String(s)) if s.is_empty() => None,
                Some(Json::String(s)) => Some(plain_seconds(s)?),
                Some(Json::Number(n)) => Some(
                    n.to_string()
                        .parse::<f64>()
                        .ok()
                        .filter(|t| t.is_finite() && *t > 0.0)?,
                ),
                Some(_) => return None,
            },
        };
        // page and page_size (get_pagination())
        let int_arg = |name: &str| -> Option<Option<i64>> {
            match arg(name) {
                None | Some("") => Some(None),
                Some(text) => Some(Some(params::plain_integer(text)?)),
            }
        };
        let page = int_arg("page")?.unwrap_or(1);
        let page_size = int_arg("page_size")?.unwrap_or(10);
        if page < 1 || page_size < 1 || page_size > settings.max_page_size {
            return None;
        }
        // The parameters: the URL's, then the body's params, then its placeholders, each overriding the last
        let mut params: HashMap<String, Supplied> = HashMap::new();
        for (name, value) in &args {
            if !RESERVED.contains(&name.as_str()) {
                params.insert(name.clone(), params::from_url(value));
            }
        }
        for field in ["params", "placeholders"] {
            match object.get(field) {
                None | Some(Json::Null) => {}
                Some(Json::Object(given)) => {
                    for (name, value) in given {
                        params.insert(name.clone(), params::from_json(value));
                    }
                }
                Some(_) => return None,
            }
        }
        Some(Call {
            key,
            request_id,
            params,
            page,
            page_size,
            timeout,
        })
    }
}

/// A timeout's seconds as float() reads plain decimals, and only positive finite ones (get_timeout()).
fn plain_seconds(text: &str) -> Option<f64> {
    if !text.bytes().all(|b| b.is_ascii_digit() || b == b'.')
        || text.matches('.').count() > 1
        || text == "."
    {
        return None;
    }
    text.parse::<f64>()
        .ok()
        .filter(|t| t.is_finite() && *t > 0.0)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_query_name_is_one_decoded_segment() {
        assert_eq!(query_name("/q/by_id").as_deref(), Some("by_id"));
        assert_eq!(query_name("/q/caf%C3%A9").as_deref(), Some("café"));
        for path in ["/q/", "/q/a/b", "/q/a%2Fb", "/w/x/q/a", "/q/%FF"] {
            assert!(query_name(path).is_none(), "{path}");
        }
    }

    #[test]
    fn settings_as_config_py_reads_them() {
        let settings = Settings::from(|name| match name {
            "QUERYAPIGATE_QUERY_TIMEOUT" => Some("0".into()),
            "QUERYAPIGATE_LOG_REQUESTS" => Some("Off".into()),
            "QUERYAPIGATE_MAX_PAGE_SIZE" => Some("-5".into()),
            _ => None,
        });
        assert_eq!(settings.query_timeout, None);
        assert!(!settings.log_requests && settings.access_log);
        assert_eq!(settings.max_page_size, 1);
        assert!(settings.forward_everything.is_none());
        assert_eq!(Settings::from(|_| None).query_timeout, Some(30.0));
        let limited =
            Settings::from(|name| (name == "QUERYAPIGATE_RATE_LIMIT").then(|| "10/minute".into()));
        assert!(limited.forward_everything.is_some());
    }

    #[test]
    fn a_server_wide_limit_is_counted_here_only_with_redis() {
        let read = |limit: Option<&str>, redis: Option<&str>| {
            let (limit, redis) = (limit.map(str::to_string), redis.map(str::to_string));
            Settings::from(move |name| match name {
                "QUERYAPIGATE_RATE_LIMIT" => limit.clone(),
                "QUERYAPIGATE_REDIS_URL" => redis.clone(),
                _ => None,
            })
        };
        let redis = Some("redis://127.0.0.1:6379/0");
        let counted = read(Some(" 10 / minute "), redis);
        assert!(counted.forward_everything.is_none());
        assert_eq!(
            counted.client_limit,
            Some(Limit {
                count: 10,
                period: 60
            })
        );
        assert_eq!(counted.redis_url.as_deref(), redis);
        // Redis without a server-wide limit: keys' own limits are counted, nothing else changes
        let keys_only = read(None, redis);
        assert!(keys_only.forward_everything.is_none() && keys_only.client_limit.is_none());
        assert!(keys_only.redis_url.is_some());
        // Python's to count: no Redis, a URL with options only redis-py reads, a limit only Python reads
        for (limit, redis) in [
            (Some("10/minute"), None),
            (
                Some("10/minute"),
                Some("redis://127.0.0.1:6379/0?socket_timeout=2"),
            ),
            (Some("10/fortnight"), redis),
            (Some("١٠/minute"), redis),
        ] {
            assert!(
                read(limit, redis).forward_everything.is_some(),
                "{limit:?} {redis:?}"
            );
        }
    }

    #[test]
    fn trusted_proxies_and_hsts_as_config_py_reads_them() {
        let read = |hops: &str, hsts: &str| {
            let (hops, hsts) = (hops.to_string(), hsts.to_string());
            let settings = Settings::from(move |name| match name {
                "QUERYAPIGATE_TRUST_PROXY" => Some(hops.clone()),
                "QUERYAPIGATE_HSTS_MAX_AGE" => Some(hsts.clone()),
                _ => None,
            });
            (
                settings.forward_everything.is_some(),
                settings.proxy_hops,
                settings.hsts_max_age,
            )
        };
        // max(0, int(value)), 0 when it isn't one; the digits of a positive number, else none
        assert_eq!(read("2", "600"), (false, 2, Some(600)));
        assert_eq!(read(" +1 ", " 007 "), (false, 1, Some(7)));
        assert_eq!(read("-3", "0"), (false, 0, None));
        assert_eq!(read("", "+5"), (false, 0, None));
        assert_eq!(read("x", "1.5"), (false, 0, None));
        // what int() or isdigit() read some other way is left to Python: every call forwarded
        for (hops, hsts) in [
            ("1_0", ""),
            ("٣", ""),
            ("99999999999999999999999", ""),
            ("1", "٣"),
        ] {
            assert!(read(hops, hsts).0, "{hops:?} {hsts:?}");
        }
    }

    #[test]
    fn intervals_that_aren_t_durations_fall_back_to_the_defaults() {
        for text in ["inf", "1e300", "nan", "x"] {
            let settings = Settings::from(|name| {
                (name == "QUERYAPIGATE_HISTORY_FLUSH_INTERVAL"
                    || name == "QUERYAPIGATE_METADATA_CACHE_TTL")
                    .then(|| text.into())
            });
            assert_eq!(settings.flush_every, Duration::from_secs(1), "{text}");
            assert_eq!(settings.metadata_ttl, Duration::ZERO, "{text}");
        }
    }

    #[test]
    fn only_a_small_body_of_known_size_is_read_before_the_key_is_checked() {
        let headers = |pairs: &[(&'static str, &str)]| {
            let mut map = HeaderMap::new();
            for (name, value) in pairs {
                map.insert(*name, value.parse().unwrap());
            }
            map
        };
        assert!(small_body(&headers(&[])));
        assert!(small_body(&headers(&[("content-length", "120")])));
        assert!(!small_body(&headers(&[("content-length", "65537")])));
        assert!(!small_body(&headers(&[("content-length", "x")])));
        assert!(!small_body(&headers(&[("transfer-encoding", "chunked")])));
    }

    #[test]
    fn a_timeout_must_be_plain_and_positive() {
        assert_eq!(plain_seconds("2.5"), Some(2.5));
        for text in ["0", "-1", " 1", "inf", "1e3", "1_0", "."] {
            assert!(plain_seconds(text).is_none(), "{text}");
        }
    }
}
