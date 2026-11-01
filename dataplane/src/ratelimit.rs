//! The rate limits Python counts in Redis (ratelimit.py, ADR 0032): a key's own `rate_limit`, and the server-wide
//! `QUERYAPIGATE_RATE_LIMIT` by client address - counted here in the same buckets, by the same script, so a caller has
//! one budget whichever process takes its call.
//!
//! Only with Redis: without it each process counts its own, so a limited call is forwarded and Python counts it. When
//! Redis fails, the call is forwarded uncounted - Python counts it, in Redis or, with its alert, in its own memory -
//! and Redis is left alone for RETRY_AFTER_FAILURE, as Python leaves it. A limit is never lifted here, and nothing is
//! refused that Redis didn't refuse.

use std::sync::Mutex;
use std::time::{Duration, Instant};

use redis::aio::{ConnectionManager, ConnectionManagerConfig};

/// ratelimit._TOKEN_BUCKET, byte for byte: EVALSHA names it by its SHA-1, so Python and this process run one script,
/// loaded once (a test compares it with Python's).
pub const TOKEN_BUCKET: &str = r#"
local count = tonumber(ARGV[1])
local period = tonumber(ARGV[2])
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
local state = redis.call('HMGET', KEYS[1], 'tokens', 'seen')
local tokens = tonumber(state[1])
local seen = tonumber(state[2])
if tokens == nil or seen == nil then
  tokens = count
  seen = now
end
tokens = math.min(count, tokens + math.max(0, now - seen) * count / period)
local allowed = 0
if tokens >= 1 then
  tokens = tokens - 1
  allowed = 1
end
redis.call('HSET', KEYS[1], 'tokens', tostring(tokens), 'seen', tostring(now))
redis.call('EXPIRE', KEYS[1], math.ceil(period) + 1)
local retry = 0
if allowed == 0 then
  retry = math.max(1, math.ceil((1 - tokens) * period / count - 0.000001))
end
return {allowed, math.floor(tokens), retry}
"#;

/// ratelimit._PREFIX
const PREFIX: &str = "qag:ratelimit:";
/// SharedRateLimiter.RETRY_AFTER_FAILURE: an outage costs one timeout every few seconds, not one per call
const RETRY_AFTER_FAILURE: Duration = Duration::from_secs(5);
/// redis.Redis.from_url(url, socket_timeout=0.5, socket_connect_timeout=0.5)
const TIMEOUT: Duration = Duration::from_millis(500);
/// How often a failure is logged, at most: an outage under load would otherwise flood the log, as Python says
const WARN_EVERY: Duration = Duration::from_secs(60);

/// config._RATE_PERIODS
const PERIODS: [(&str, u64); 4] = [
    ("second", 1),
    ("minute", 60),
    ("hour", 3600),
    ("day", 86400),
];

/// A limit as config.parse_rate_limit() gives it: `count` calls per `period` seconds.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Limit {
    pub count: u64,
    pub period: u64,
}

impl Limit {
    /// A plan's `[count, period]`, when it is one Python could have made.
    pub fn from_plan((count, period): (u64, u64)) -> Option<Limit> {
        (count >= 1 && PERIODS.iter().any(|&(_, seconds)| seconds == period))
            .then_some(Limit { count, period })
    }
}

/// config.parse_rate_limit()'s `^\s*(\d+)\s*/\s*(second|minute|hour|day)s?\s*$` (re.I), for the text that reads
/// the same in Rust: ASCII, spaces and tabs only, and a count up to u64. None for any other - Python's to read.
pub fn parse(text: &str) -> Option<Limit> {
    if !text
        .bytes()
        .all(|b| b.is_ascii_graphic() || b == b' ' || b == b'\t')
    {
        return None;
    }
    let (count, period) = text.split_once('/')?;
    let count = count.trim_matches([' ', '\t']);
    let period = period.trim_matches([' ', '\t']).to_ascii_lowercase();
    if count.is_empty() || !count.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    let count: u64 = count.parse().ok()?;
    let name = period.strip_suffix('s').unwrap_or(&period);
    let &(_, seconds) = PERIODS.iter().find(|&&(n, _)| n == name)?;
    (count >= 1).then_some(Limit {
        count,
        period: seconds,
    })
}

/// Whether QUERYAPIGATE_REDIS_URL is one this connects to as redis-py's `Redis.from_url()` does: `redis://`,
/// `rediss://` or `unix://`, with no query or fragment - their options are each client's own.
pub fn readable(url: &str) -> bool {
    let plain = ["redis://", "rediss://", "unix://"]
        .iter()
        .any(|scheme| url.starts_with(scheme));
    plain && !url.contains(['?', '#']) && redis::Client::open(url).is_ok()
}

/// What one count said: the script's `{allowed, remaining, retry}`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Verdict {
    pub allowed: bool,
    pub remaining: i64,
    pub retry_after: i64,
}

/// The buckets: counted in Redis, through one multiplexed connection, re-made after a failure.
pub struct Limiter {
    connection: ConnectionManager,
    script: redis::Script,
    /// After a failure, until when Redis isn't tried; when one was last logged
    failed: Mutex<(Option<Instant>, Option<Instant>)>,
}

impl Limiter {
    /// For a URL readable() accepts. Nothing is connected until the first count.
    pub fn new(url: &str) -> Result<Limiter, String> {
        let client = redis::Client::open(url).map_err(|e| e.to_string())?;
        let config = ConnectionManagerConfig::new()
            .set_connection_timeout(Some(TIMEOUT))
            .set_response_timeout(Some(TIMEOUT))
            .set_number_of_retries(0); // a call never waits on reconnecting: after a failure it is forwarded
        let connection =
            ConnectionManager::new_lazy_with_config(client, config).map_err(|e| e.to_string())?;
        Ok(Limiter {
            connection,
            script: redis::Script::new(TOKEN_BUCKET),
            failed: Mutex::new((None, None)),
        })
    }

    /// One call counted against `client`'s bucket in `scope` (`client` or `key`), as SharedRateLimiter.hit() counts
    /// it - or None when Redis didn't answer, or failed a moment ago: the call is then Python's to count.
    pub async fn hit(&self, scope: &str, limit: Limit, client: &str) -> Option<Verdict> {
        if self
            .failed
            .lock()
            .expect("failed")
            .0
            .is_some_and(|until| Instant::now() < until)
        {
            return None;
        }
        // `{period:g}`: Python writes 60, not 60.0 - the periods are whole numbers of seconds
        let key = format!("{PREFIX}{scope}:{}/{}:{client}", limit.count, limit.period);
        let mut invocation = self.script.prepare_invoke();
        invocation.key(key).arg(limit.count).arg(limit.period);
        let mut connection = self.connection.clone();
        // connecting and answering: each has its own TIMEOUT, as in Python
        let counted = tokio::time::timeout(
            2 * TIMEOUT,
            invocation.invoke_async::<(i64, i64, i64)>(&mut connection),
        )
        .await;
        match counted {
            Ok(Ok((allowed, remaining, retry_after))) => Some(Verdict {
                allowed: allowed == 1,
                remaining,
                retry_after,
            }),
            Ok(Err(error)) => self.failure(&error.to_string()),
            Err(_) => self.failure("it did not answer in time"),
        }
    }

    fn failure(&self, why: &str) -> Option<Verdict> {
        let now = Instant::now();
        let mut failed = self.failed.lock().expect("failed");
        failed.0 = Some(now + RETRY_AFTER_FAILURE);
        if failed
            .1
            .is_none_or(|at| now.duration_since(at) >= WARN_EVERY)
        {
            failed.1 = Some(now);
            eprintln!(
                "queryapigate-dataplane: rate-limited calls are forwarded to Python for {}s - Redis failed: {why}",
                RETRY_AFTER_FAILURE.as_secs()
            );
        }
        None
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_script_is_pythons() {
        let python = std::fs::read_to_string(
            std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../queryapigate/ratelimit.py"),
        )
        .unwrap();
        let opening = "_TOKEN_BUCKET = \"\"\"";
        let start = python.find(opening).unwrap() + opening.len();
        let end = start + python[start..].find("\"\"\"").unwrap();
        assert_eq!(&python[start..end], TOKEN_BUCKET);
    }

    #[test]
    fn a_limit_as_python_reads_one() {
        let limit = |count, period| Some(Limit { count, period });
        for (text, read) in [
            ("60/minute", limit(60, 60)),
            (" 10 / Seconds ", limit(10, 1)),
            ("1/DAY", limit(1, 86400)),
            ("060/hours", limit(60, 3600)),
            ("5/minutes", limit(5, 60)),
            ("\t3\t/\tsecond", limit(3, 1)),
            ("0/minute", None),
            ("-1/minute", None),
            ("+1/minute", None),
            ("1.5/minute", None),
            ("60/minutess", None),
            ("60/fortnight", None),
            ("60 per minute", None),
            ("60/minute/x", None),
            ("1_0/minute", None),
            ("\u{0661}/minute", None), // an Arabic-Indic 1: Python's \\d reads it, so it's Python's
            ("60/minute\u{a0}", None),
            ("60\n/minute", None),
            ("99999999999999999999999/minute", None),
            ("", None),
        ] {
            assert_eq!(parse(text), read, "{text:?}");
        }
    }

    #[test]
    fn only_the_limits_a_plan_can_carry() {
        assert_eq!(
            Limit::from_plan((3, 60)),
            Some(Limit {
                count: 3,
                period: 60
            })
        );
        assert_eq!(Limit::from_plan((0, 60)), None);
        assert_eq!(Limit::from_plan((3, 61)), None);
    }

    #[test]
    fn urls_both_clients_read_alike() {
        for url in [
            "redis://localhost:6379",
            "redis://:pw@10.0.0.5:6380/2",
            "rediss://user:pw@cache.internal:6380/0",
            "unix:///run/redis.sock",
        ] {
            assert!(readable(url), "{url}");
        }
        for url in [
            "redis://localhost:6379/0?socket_timeout=1",
            "rediss://cache.internal#insecure",
            "redis+unix:///run/redis.sock",
            "http://localhost:6379",
            "localhost:6379",
        ] {
            assert!(!readable(url), "{url}");
        }
    }
}
