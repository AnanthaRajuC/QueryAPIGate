//! The data plane's settings, from the environment - the same one the Python server reads.

use std::net::SocketAddr;

use hyper::Uri;
use hyper::header::HeaderValue;

/// The shortest QUERYAPIGATE_DATAPLANE_SECRET accepted: it is all that lets a request speak for a client's address.
const MIN_SECRET: usize = 16;

pub struct Config {
    /// QUERYAPIGATE_DATAPLANE_LISTEN: where clients connect (loopback by default, like the Python server's own).
    pub listen: SocketAddr,
    /// QUERYAPIGATE_DATAPLANE_UPSTREAM: the Python server, `http://host:port`.
    pub upstream: Uri,
    /// QUERYAPIGATE_DATAPLANE_SECRET, as the header value sent with every forwarded request.
    pub secret: HeaderValue,
    /// QUERYAPIGATE_DATAPLANE_THREADS: Tokio worker threads (the machine's cores by default).
    pub threads: usize,
}

impl Config {
    pub fn from_env() -> Result<Config, String> {
        Self::from(|name| {
            std::env::var(name)
                .ok()
                .filter(|value| !value.trim().is_empty())
        })
    }

    pub fn from(get: impl Fn(&str) -> Option<String>) -> Result<Config, String> {
        let listen =
            get("QUERYAPIGATE_DATAPLANE_LISTEN").unwrap_or_else(|| "127.0.0.1:5000".into());
        let listen: SocketAddr = listen.trim().parse().map_err(|_| {
            format!("QUERYAPIGATE_DATAPLANE_LISTEN must be an address and port, not '{listen}'")
        })?;
        let upstream = get("QUERYAPIGATE_DATAPLANE_UPSTREAM")
            .unwrap_or_else(|| "http://127.0.0.1:5001".into());
        let upstream: Uri = upstream
            .trim()
            .parse()
            .ok()
            .filter(|uri: &Uri| uri.scheme_str() == Some("http") && uri.authority().is_some())
            .filter(|uri: &Uri| uri.path() == "/" && uri.query().is_none())
            .ok_or_else(|| {
                format!(
                    "QUERYAPIGATE_DATAPLANE_UPSTREAM must be http://host:port, not '{upstream}'"
                )
            })?;
        let secret = get("QUERYAPIGATE_DATAPLANE_SECRET")
            .map(|s| s.trim().to_string())
            .unwrap_or_default();
        if secret.len() < MIN_SECRET {
            return Err(format!(
                "QUERYAPIGATE_DATAPLANE_SECRET must be set to at least {MIN_SECRET} characters, \
                 and to the same value as the Python server's"
            ));
        }
        let secret = HeaderValue::from_str(&secret)
            .map_err(|_| "QUERYAPIGATE_DATAPLANE_SECRET must be printable ASCII".to_string())?;
        let threads = match get("QUERYAPIGATE_DATAPLANE_THREADS") {
            None => std::thread::available_parallelism()
                .map(|n| n.get())
                .unwrap_or(1),
            Some(value) => value
                .trim()
                .parse::<usize>()
                .ok()
                .filter(|n| (1..=256).contains(n))
                .ok_or_else(|| {
                    format!("QUERYAPIGATE_DATAPLANE_THREADS must be 1 to 256, not '{value}'")
                })?,
        };
        Ok(Config {
            listen,
            upstream,
            secret,
            threads,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;

    fn config(pairs: &[(&str, &str)]) -> Result<Config, String> {
        let env: HashMap<String, String> = pairs
            .iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect();
        Config::from(|name| env.get(name).cloned())
    }

    const SECRET: (&str, &str) = ("QUERYAPIGATE_DATAPLANE_SECRET", "0123456789abcdef");

    #[test]
    fn defaults_to_loopback_in_front_of_a_local_upstream() {
        let config = config(&[SECRET]).unwrap();
        assert_eq!(config.listen.to_string(), "127.0.0.1:5000");
        assert_eq!(config.upstream.to_string(), "http://127.0.0.1:5001/");
    }

    #[test]
    fn refuses_to_start_without_a_long_enough_secret() {
        assert!(config(&[]).is_err());
        assert!(config(&[("QUERYAPIGATE_DATAPLANE_SECRET", "short")]).is_err());
    }

    #[test]
    fn refuses_an_upstream_that_is_not_plain_http_to_a_host() {
        for upstream in [
            "https://127.0.0.1:5001",
            "127.0.0.1:5001",
            "http://127.0.0.1:5001/api",
            "nonsense",
        ] {
            assert!(
                config(&[SECRET, ("QUERYAPIGATE_DATAPLANE_UPSTREAM", upstream)]).is_err(),
                "{upstream}"
            );
        }
    }

    #[test]
    fn refuses_threads_out_of_range() {
        for threads in ["0", "257", "many"] {
            assert!(
                config(&[SECRET, ("QUERYAPIGATE_DATAPLANE_THREADS", threads)]).is_err(),
                "{threads}"
            );
        }
        assert_eq!(
            config(&[SECRET, ("QUERYAPIGATE_DATAPLANE_THREADS", "4")])
                .unwrap()
                .threads,
            4
        );
    }
}
