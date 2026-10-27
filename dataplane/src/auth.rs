//! Authenticating a scoped API key as `apikeys.authenticate()` does - or declining to, so the call is forwarded and
//! Python answers it. Every way this can fail, including a key Python would accept but this can't vouch for, ends in
//! forwarding: the data plane never answers 401 itself, so it can never disagree with Python about one.

use std::net::IpAddr;
use std::str::FromStr;

use chrono::{NaiveDate, NaiveDateTime};
use ipnet::IpNet;
use serde_json::{Map, Value as Json};
use sha2::{Digest, Sha256};

use crate::store::{Rows, Value};

/// What an administrator's token starts with (admins.TOKEN_PREFIX) - never a scoped key, so always forwarded.
const ADMIN_TOKEN_PREFIX: &str = "qagadm_";

/// A scoped key that authenticated.
#[derive(Debug)]
pub struct Caller {
    pub workspace: String,
    pub name: String,
    /// Matched by its old secret, still in a rotation's grace period (ADR 0009).
    pub previous: bool,
    pub details: Map<String, Json>,
}

/// `apikeys._hash()`: the hex SHA-256 of the secret, what `api_keys.hash` holds.
pub fn hash(secret: &str) -> String {
    hex::encode(Sha256::digest(secret.as_bytes()))
}

/// Equal, in time that doesn't depend on where they first differ - as `hmac.compare_digest()`.
pub fn same(a: &[u8], b: &[u8]) -> bool {
    if a.len() != b.len() {
        return false;
    }
    a.iter().zip(b).fold(0u8, |acc, (x, y)| acc | (x ^ y)) == 0
}

pub struct Clock {
    /// Local wall-clock time: when a key's `expires_at` date ends (`apikeys.is_expired()`).
    pub local: NaiveDateTime,
    /// `store.now()`'s text, what `previous_expires_at` is compared with as a string.
    pub stored: String,
}

impl Clock {
    pub fn now() -> Clock {
        let local = chrono::Local::now().naive_local();
        Clock {
            stored: local.format("%Y-%m-%d %H:%M:%S").to_string(),
            local,
        }
    }
}

/// The scoped key `supplied` is, from the rows `store.key(hash(supplied))` read - or None: forward the call.
pub fn authenticate(
    supplied: &str,
    admin_key: Option<&str>,
    rows: &Rows,
    client: Option<IpAddr>,
    clock: &Clock,
) -> Option<Caller> {
    if supplied.is_empty() || supplied.starts_with(ADMIN_TOKEN_PREFIX) {
        return None;
    }
    if admin_key.is_some_and(|admin| same(supplied.as_bytes(), admin.as_bytes())) {
        return None; // the break-glass administrator: Python's
    }
    if rows.rows.len() != 1 {
        return None; // none: a wrong key, Python's 401; two: a secret both current and rotated out, Python's to sort
    }
    let supplied_hash = hash(supplied);
    let text = |column: &str| rows.get(0, column).and_then(Value::text);
    let row_hash = text("hash")?;
    let previous = row_hash != supplied_hash;
    if previous {
        let (Some(old), Some(until)) = (text("previous_hash"), text("previous_expires_at")) else {
            return None;
        };
        if until <= clock.stored.as_str() || !same(supplied_hash.as_bytes(), old.as_bytes()) {
            return None;
        }
    } else if !same(supplied_hash.as_bytes(), row_hash.as_bytes()) {
        return None;
    }
    let active = rows.get(0, "active").and_then(Value::int)? != 0;
    let details: Map<String, Json> = serde_json::from_str(text("details_json")?).ok()?;
    if !active
        || expired(text("expires_at"), clock)
        || !ip_allowed(details.get("allowed_ips"), client)?
    {
        return None;
    }
    Some(Caller {
        workspace: text("workspace")?.to_string(),
        name: text("name")?.to_string(),
        previous,
        details,
    })
}

/// `apikeys.is_expired()`: valid through the end of its date, local time; a date that can't be read counts as passed.
fn expired(expires_at: Option<&str>, clock: &Clock) -> bool {
    let Some(date) = expires_at.filter(|d| !d.is_empty()) else {
        return false;
    };
    if date.len() != 10 {
        return true;
    }
    match NaiveDate::parse_from_str(date, "%Y-%m-%d") {
        Ok(day) => clock.local > day.and_hms_opt(23, 59, 59).expect("a valid time"),
        Err(_) => true,
    }
}

/// `apikeys._ip_allowed()`. None when it can't say - an entry Python would fail on - so the call is forwarded.
fn ip_allowed(allowed: Option<&Json>, client: Option<IpAddr>) -> Option<bool> {
    let list = match allowed {
        None | Some(Json::Null) => return Some(true),
        Some(Json::Array(list)) => list,
        Some(_) => return None,
    };
    let Some(client) = client else {
        return Some(false);
    };
    let mut found = false;
    for value in list {
        let text = value.as_str()?;
        let network = match IpNet::from_str(text) {
            Ok(network) => network,
            Err(_) => IpNet::from(IpAddr::from_str(text).ok()?),
        };
        found |= network.contains(&client);
    }
    Some(found)
}

#[cfg(test)]
mod tests {
    use super::*;

    const SECRET: &str = "qag_test_secret";

    fn rows(active: i64, expires: Option<&str>, details: &str) -> Rows {
        let text = |s: &str| Value::Text(s.to_string());
        let opt = |s: Option<&str>| s.map(text).unwrap_or(Value::Null);
        Rows {
            columns: [
                "workspace",
                "name",
                "hash",
                "active",
                "expires_at",
                "created_at",
                "details_json",
                "previous_hash",
                "previous_expires_at",
            ]
            .map(String::from)
            .to_vec(),
            rows: vec![vec![
                text("default"),
                text("reporting"),
                text(&hash(SECRET)),
                Value::Int(active),
                opt(expires),
                text("2026-10-01 10:00:00"),
                text(details),
                Value::Null,
                Value::Null,
            ]],
        }
    }

    fn clock(at: &str) -> Clock {
        let local = NaiveDateTime::parse_from_str(at, "%Y-%m-%d %H:%M:%S").unwrap();
        Clock {
            stored: at.to_string(),
            local,
        }
    }

    fn ip(text: &str) -> Option<IpAddr> {
        Some(text.parse().unwrap())
    }

    #[test]
    fn a_scoped_key_with_its_right_secret() {
        let caller = authenticate(
            SECRET,
            Some("admin"),
            &rows(1, None, "{}"),
            ip("10.0.0.1"),
            &clock("2026-10-27 12:00:00"),
        );
        let caller = caller.unwrap();
        assert_eq!(
            (
                caller.workspace.as_str(),
                caller.name.as_str(),
                caller.previous
            ),
            ("default", "reporting", false)
        );
    }

    #[test]
    fn never_the_administrator_nor_an_administrator_s_token() {
        let at = clock("2026-10-27 12:00:00");
        assert!(authenticate(SECRET, Some(SECRET), &rows(1, None, "{}"), None, &at).is_none());
        assert!(authenticate("qagadm_token", None, &rows(1, None, "{}"), None, &at).is_none());
    }

    #[test]
    fn not_an_inactive_key_nor_a_wrong_secret_nor_unknown_rows() {
        let at = clock("2026-10-27 12:00:00");
        assert!(authenticate(SECRET, None, &rows(0, None, "{}"), None, &at).is_none());
        assert!(authenticate("other", None, &rows(1, None, "{}"), None, &at).is_none());
        assert!(authenticate(SECRET, None, &Rows::default(), None, &at).is_none());
        assert!(authenticate(SECRET, None, &rows(1, None, "not json"), None, &at).is_none());
    }

    #[test]
    fn expires_at_the_end_of_its_day_and_unreadable_dates_count_as_passed() {
        let key = rows(1, Some("2026-10-27"), "{}");
        assert!(authenticate(SECRET, None, &key, None, &clock("2026-10-27 23:59:59")).is_some());
        assert!(authenticate(SECRET, None, &key, None, &clock("2026-10-28 00:00:00")).is_none());
        assert!(
            authenticate(
                SECRET,
                None,
                &rows(1, Some("27/10/2026"), "{}"),
                None,
                &clock("2020-01-01 00:00:00")
            )
            .is_none()
        );
    }

    #[test]
    fn allowed_ips_judge_the_client_and_an_empty_list_allows_none() {
        let at = clock("2026-10-27 12:00:00");
        let office = rows(1, None, r#"{"allowed_ips": ["10.0.0.0/8", "2001:db8::1"]}"#);
        assert!(authenticate(SECRET, None, &office, ip("10.9.8.7"), &at).is_some());
        assert!(authenticate(SECRET, None, &office, ip("2001:db8::1"), &at).is_some());
        assert!(authenticate(SECRET, None, &office, ip("192.0.2.1"), &at).is_none());
        assert!(authenticate(SECRET, None, &office, None, &at).is_none());
        assert!(
            authenticate(
                SECRET,
                None,
                &rows(1, None, r#"{"allowed_ips": []}"#),
                ip("10.0.0.1"),
                &at
            )
            .is_none()
        );
        // a host address with a prefix, as ip_network(strict=False) reads it
        let host = rows(1, None, r#"{"allowed_ips": ["10.1.2.3/24"]}"#);
        assert!(authenticate(SECRET, None, &host, ip("10.1.2.200"), &at).is_some());
        assert!(
            authenticate(
                SECRET,
                None,
                &rows(1, None, r#"{"allowed_ips": ["nonsense"]}"#),
                ip("10.0.0.1"),
                &at
            )
            .is_none()
        );
    }

    #[test]
    fn an_old_secret_only_within_its_grace_period() {
        let mut key = rows(1, None, "{}");
        key.rows[0][2] = Value::Text(hash("the new one"));
        key.rows[0][7] = Value::Text(hash(SECRET));
        key.rows[0][8] = Value::Text("2026-10-27 13:00:00".into());
        let caller = authenticate(SECRET, None, &key, None, &clock("2026-10-27 12:59:59")).unwrap();
        assert!(caller.previous);
        assert!(authenticate(SECRET, None, &key, None, &clock("2026-10-27 13:00:00")).is_none());
    }
}
