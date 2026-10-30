//! The client behind trusted proxies (`QUERYAPIGATE_TRUST_PROXY`, ADR 0030 section 5): its address and whether it
//! arrived over HTTPS, as werkzeug's `ProxyFix(x_for=hops, x_proto=hops)` finds them for Python.
//!
//! `ProxyFix` takes the hops-th value from the right of `X-Forwarded-For` and of `X-Forwarded-Proto`, split as
//! `parse_list_header` splits them, and keeps the socket's when there are fewer. This reproduces that for the headers
//! whose reading is plain - one line, no quotes, no empty item, an address written as Python and Rust both write it -
//! and says `None` for any other, so the call is forwarded and Python reads it as it always has. A key's
//! `allowed_ips` is checked against the address found here, so agreeing with Python is an access decision: only the
//! shapes both read the same are served.
//!
//! The host is checked too, though nothing here uses it: werkzeug answers 400 to a host it can't encode as IDNA
//! (`Map.bind_to_environ`) - `Host`, or behind proxies `X-Forwarded-Host` as `ProxyFix(x_host=hops)` takes it.

use std::net::IpAddr;

use hyper::header::{self, HeaderMap, HeaderName};

const FORWARDED_FOR: HeaderName = HeaderName::from_static("x-forwarded-for");
const FORWARDED_PROTO: HeaderName = HeaderName::from_static("x-forwarded-proto");
const FORWARDED_HOST: HeaderName = HeaderName::from_static("x-forwarded-host");

#[derive(Debug, PartialEq)]
pub struct Client {
    /// The address Python's REMOTE_ADDR holds after ProxyFix
    pub address: IpAddr,
    /// Whether Python's `request.is_secure` holds: the scheme is `https`
    pub secure: bool,
}

/// The client of a call that reached this process from `peer`, with `hops` trusted proxies in front; `None` when its
/// headers are any shape this doesn't read exactly as Python does.
pub fn client(headers: &HeaderMap, peer: IpAddr, hops: usize) -> Option<Client> {
    let forwarded_host = if hops == 0 {
        None
    } else {
        chosen(headers, FORWARDED_HOST, hops)?
    };
    let host = match forwarded_host {
        Some(host) => Some(host),
        None => one_line(headers, header::HOST)?,
    };
    if !host.is_none_or(encodes_as_idna) {
        return None;
    }
    if hops == 0 {
        return Some(Client {
            address: peer,
            secure: false,
        });
    }
    let address = match chosen(headers, FORWARDED_FOR, hops)? {
        None => peer,
        Some(text) => {
            let address: IpAddr = text.parse().ok()?;
            // Python keeps the text as it came: serve only the spelling that is the address's own, and never an
            // IPv4 address written as IPv6, which the two sides could compare against allowed_ips differently
            if address.to_string() != text || address.to_canonical() != address {
                return None;
            }
            address
        }
    };
    // Any other scheme is Python's to make sense of: werkzeug refuses `wss` on a request that isn't a WebSocket's
    let secure = match chosen(headers, FORWARDED_PROTO, hops)? {
        None | Some("http") => false,
        Some("https") => true,
        Some(_) => return None,
    };
    Some(Client { address, secure })
}

/// Whether werkzeug can encode `host` for routing: Python's `idna` codec on the name before any `:`, whose ASCII path
/// refuses an empty label but the last and any label of 64 characters or more. Anything not ASCII is left to Python.
fn encodes_as_idna(host: &str) -> bool {
    let name = host.split(':').next().unwrap_or_default();
    if !name.is_ascii() {
        return false;
    }
    let labels: Vec<&str> = name.split('.').collect();
    let (last, rest) = labels.split_last().expect("split gives one at least");
    rest.iter().all(|label| (1..64).contains(&label.len())) && last.len() < 64
}

/// A header's one line, as text: `Some(None)` when it's absent, `None` when there are several or it isn't visible
/// ASCII.
fn one_line(headers: &HeaderMap, name: HeaderName) -> Option<Option<&str>> {
    let mut lines = headers.get_all(name).iter();
    let Some(line) = lines.next() else {
        return Some(None);
    };
    if lines.next().is_some() {
        return None; // several lines: gunicorn joins them, and the join is its own
    }
    line.to_str().ok().map(Some)
}

/// The hops-th value from the right of a list header: `Some(None)` when it is absent or has fewer values (Python
/// keeps what it had), `None` when its reading isn't plain.
fn chosen(headers: &HeaderMap, name: HeaderName, hops: usize) -> Option<Option<&str>> {
    let Some(text) = one_line(headers, name)? else {
        return Some(None);
    };
    if text.contains('"') {
        return None; // parse_list_header unquotes
    }
    let values: Vec<&str> = text
        .split(',')
        .map(|v| v.trim_matches([' ', '\t']))
        .collect();
    if values.iter().any(|v| v.is_empty()) {
        return None; // empty items: kept or dropped by position, and an empty header is no value at all
    }
    Some(values.len().checked_sub(hops).map(|i| values[i]))
}

#[cfg(test)]
mod tests {
    use super::*;
    use hyper::header::HeaderValue;

    const PEER: &str = "192.0.2.9";

    fn find(pairs: &[(&str, &str)], hops: usize) -> Option<Client> {
        let mut headers = HeaderMap::new();
        for (name, value) in pairs {
            headers.append(
                HeaderName::from_bytes(name.as_bytes()).unwrap(),
                HeaderValue::from_str(value).unwrap(),
            );
        }
        client(&headers, PEER.parse().unwrap(), hops)
    }

    fn at(address: &str, secure: bool) -> Option<Client> {
        Some(Client {
            address: address.parse().unwrap(),
            secure,
        })
    }

    #[test]
    fn without_trusted_proxies_the_headers_are_not_read() {
        let headers = [
            ("x-forwarded-for", "10.0.0.1"),
            ("x-forwarded-proto", "https"),
        ];
        assert_eq!(find(&headers, 0), at(PEER, false));
    }

    #[test]
    fn takes_the_value_as_many_hops_from_the_right() {
        let header = [("x-forwarded-for", "10.0.0.1, 10.0.0.2,10.0.0.3")];
        assert_eq!(find(&header, 1), at("10.0.0.3", false));
        assert_eq!(find(&header, 2), at("10.0.0.2", false));
        assert_eq!(find(&header, 3), at("10.0.0.1", false));
        assert_eq!(
            find(&[("x-forwarded-for", " 10.0.0.1\t")], 1),
            at("10.0.0.1", false)
        );
    }

    #[test]
    fn keeps_the_socket_address_with_fewer_values_than_hops() {
        assert_eq!(find(&[("x-forwarded-for", "10.0.0.1")], 2), at(PEER, false));
        assert_eq!(find(&[], 1), at(PEER, false));
    }

    #[test]
    fn https_is_secure_http_isn_t_and_any_other_scheme_is_left_to_python() {
        let with = |proto| find(&[("x-forwarded-proto", proto)], 1);
        assert_eq!(with("https"), at(PEER, true));
        assert_eq!(with(" https "), at(PEER, true)); // parse_list_header strips each value
        assert_eq!(with("http"), at(PEER, false));
        for proto in ["wss", "ws", "HTTPS", "ftp"] {
            assert_eq!(with(proto), None, "{proto}");
        }
        assert_eq!(
            find(&[("x-forwarded-proto", "http, https")], 1),
            at(PEER, true)
        );
        assert_eq!(find(&[("x-forwarded-proto", "https")], 2), at(PEER, false));
    }

    #[test]
    fn ipv6_in_its_own_spelling_is_read() {
        assert_eq!(
            find(&[("x-forwarded-for", "2001:db8::7")], 1),
            at("2001:db8::7", false)
        );
    }

    #[test]
    fn any_shape_python_could_read_otherwise_is_left_to_it() {
        for value in [
            "",
            " ",
            ",",
            "10.0.0.1,",
            ",10.0.0.1",
            "10.0.0.1,,10.0.0.2",
            "\"10.0.0.1\"",
            "unknown",
            "10.0.0.1:8080",
            "[2001:db8::7]",
            "2001:DB8::7",
            "2001:db8:0::7",
            "::ffff:10.0.0.1",
            "010.0.0.1",
            "10.0.0.1 10.0.0.2",
            "fe80::1%eth0",
        ] {
            assert_eq!(find(&[("x-forwarded-for", value)], 1), None, "{value:?}");
        }
        let twice = [
            ("x-forwarded-for", "10.0.0.1"),
            ("x-forwarded-for", "10.0.0.2"),
        ];
        assert_eq!(find(&twice, 1), None);
        assert_eq!(find(&[("x-forwarded-proto", "\"https\"")], 1), None);
        assert_eq!(find(&[("x-forwarded-proto", "https,")], 1), None);
    }

    #[test]
    fn a_host_werkzeug_can_t_encode_is_left_to_python() {
        // Map.bind_to_environ: a 400 for a host whose name isn't IDNA - Host, or the forwarded one behind proxies
        let long = format!("{}.com", "x".repeat(64));
        for host in ["a..b", "...", ".", "..:80", "é.example", long.as_str()] {
            assert_eq!(find(&[("host", host)], 0), None, "{host:?}");
            let forwarded = format!("{host}, lb");
            assert_eq!(
                find(&[("x-forwarded-host", &forwarded)], 2),
                None,
                "{host:?}"
            );
        }
        let fine = format!("{}.com", "x".repeat(63));
        for host in [
            "api.example.com",
            "a.b.",
            "x:99999",
            ":80",
            "[::1]:80",
            "a b",
            "",
            fine.as_str(),
        ] {
            assert_eq!(find(&[("host", host)], 0), at(PEER, false), "{host:?}");
        }
        // behind proxies the forwarded host replaces Host, and with fewer values Host stays
        let replaced = [("host", "a..b"), ("x-forwarded-host", "api.example.com")];
        assert_eq!(find(&replaced, 1), at(PEER, false));
        assert_eq!(find(&replaced, 0), None);
        assert_eq!(find(&replaced, 2), None);
    }

    #[test]
    fn a_value_left_of_the_chosen_one_still_has_to_be_plain() {
        // Python would read it the same, but its position would move: the whole header is plain or none of it is
        assert_eq!(find(&[("x-forwarded-for", "\"x\", 10.0.0.2")], 1), None);
    }
}
