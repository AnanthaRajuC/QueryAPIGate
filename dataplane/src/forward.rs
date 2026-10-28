//! Forwarding a request to the Python server, and its answer back - bodies streamed both ways, never buffered, so
//! `/events` and streamed exports pass through as they are produced.

use std::net::IpAddr;
use std::time::Duration;

use bytes::Bytes;
use http_body_util::combinators::BoxBody;
use http_body_util::{BodyExt, Full};
use hyper::body::Incoming;
use hyper::header::{self, HeaderMap, HeaderName, HeaderValue};
use hyper::{Request, Response, StatusCode, Uri};
use hyper_util::client::legacy::Client;
use hyper_util::client::legacy::connect::HttpConnector;
use hyper_util::rt::TokioExecutor;

use crate::config::Config;

pub type Body = BoxBody<Bytes, hyper::Error>;

pub const SECRET_HEADER: HeaderName = HeaderName::from_static("x-qag-dataplane");
pub const PEER_HEADER: HeaderName = HeaderName::from_static("x-qag-peer");
pub const PLAN_HEADER: HeaderName = HeaderName::from_static("x-qag-plan");
/// Sent only with a call whose plan the data plane would keep: Python reads that call's rows past its metadata cache
/// to digest them, and every other forwarded call keeps the cache.
pub const WANTED_HEADER: HeaderName = HeaderName::from_static("x-qag-plan-wanted");

/// Headers that describe one connection, not the message: never passed on (RFC 9110, 7.6.1), nor any the
/// `Connection` header names.
const HOP_BY_HOP: [HeaderName; 8] = [
    header::CONNECTION,
    HeaderName::from_static("keep-alive"),
    header::PROXY_AUTHENTICATE,
    header::PROXY_AUTHORIZATION,
    header::TE,
    header::TRAILER,
    header::TRANSFER_ENCODING,
    header::UPGRADE,
];

/// The Python server behind the data plane: requests forwarded to it, and the data plane's own reports.
pub struct Forwarder {
    upstream: hyper::http::uri::Authority,
    secret: HeaderValue,
    client: Client<HttpConnector, Body>,
}

impl Forwarder {
    pub fn new(config: &Config) -> Forwarder {
        let mut connector = HttpConnector::new();
        connector.set_nodelay(true);
        connector.set_connect_timeout(Some(Duration::from_secs(5)));
        let client = Client::builder(TokioExecutor::new())
            // Under gunicorn's own keep-alive (2 s by default), so the pool lets a connection go before gunicorn does
            .pool_idle_timeout(Duration::from_millis(1500))
            .pool_max_idle_per_host(1024)
            .build(connector);
        Forwarder {
            upstream: config
                .upstream
                .authority()
                .expect("checked by Config")
                .clone(),
            secret: config.secret.clone(),
            client,
        }
    }

    /// Send a request on to Python as the client sent it - its body streamed through, or already read - with the
    /// secret and the client's address. Python's answer comes back as it is: `relay()` it to the client.
    pub async fn send(
        &self,
        mut parts: hyper::http::request::Parts,
        body: Body,
        peer: IpAddr,
        want_plan: bool,
    ) -> Result<Response<Incoming>, Box<Response<Body>>> {
        let path = parts
            .uri
            .path_and_query()
            .map(|p| p.as_str())
            .unwrap_or("/");
        parts.uri = Uri::builder()
            .scheme("http")
            .authority(self.upstream.clone())
            .path_and_query(path)
            .build()
            .map_err(|_| {
                Box::new(error_response(
                    StatusCode::BAD_REQUEST,
                    "The request's path is not valid",
                    "invalid_request",
                ))
            })?;
        clean(&mut parts.headers);
        parts.headers.insert(SECRET_HEADER, self.secret.clone());
        if want_plan {
            parts
                .headers
                .insert(WANTED_HEADER, HeaderValue::from_static("1"));
        }
        parts.headers.insert(
            PEER_HEADER,
            HeaderValue::from_str(&peer_text(peer)).expect("an IP address is ASCII"),
        );
        self.client
            .request(Request::from_parts(parts, body))
            .await
            .map_err(|error| {
                eprintln!("queryapigate-dataplane: forwarding failed: {error}");
                Box::new(error_response(
                    StatusCode::BAD_GATEWAY,
                    "The server behind the data plane did not answer",
                    "upstream_unavailable",
                ))
            })
    }

    /// POST a JSON body to one of Python's internal endpoints, as the data plane itself.
    pub async fn post(&self, path: &str, json: String) -> Result<StatusCode, String> {
        Ok(self.post_json(path, json).await?.0)
    }

    /// post(), with Python's answer: its status and body.
    pub async fn post_json(
        &self,
        path: &str,
        json: String,
    ) -> Result<(StatusCode, String), String> {
        let request = Request::post(format!("http://{}{path}", self.upstream))
            .header(header::CONTENT_TYPE, "application/json")
            .header(SECRET_HEADER, self.secret.clone())
            .header(PEER_HEADER, "127.0.0.1")
            .body(
                Full::new(Bytes::from(json))
                    .map_err(|never| match never {})
                    .boxed(),
            )
            .map_err(|e| e.to_string())?;
        let response = self
            .client
            .request(request)
            .await
            .map_err(|e| e.to_string())?;
        let status = response.status();
        let body = response
            .into_body()
            .collect()
            .await
            .map(|b| String::from_utf8_lossy(&b.to_bytes()).into_owned())
            .unwrap_or_default();
        Ok((status, body))
    }
}

/// Python's answer, on its way to the client: hop-by-hop headers and the data plane's own removed.
pub fn relay(response: Result<Response<Incoming>, Box<Response<Body>>>) -> Response<Body> {
    match response {
        Ok(response) => {
            let (mut parts, body) = response.into_parts();
            clean(&mut parts.headers);
            Response::from_parts(parts, body.boxed())
        }
        Err(response) => *response,
    }
}

/// An address as Python writes it: an IPv4 client on a dual-stack socket as plain IPv4, not `::ffff:a.b.c.d`.
pub fn peer_text(peer: IpAddr) -> String {
    peer.to_canonical().to_string()
}

/// The headers only the data plane and Python say to each other: removed from what a client sends, so only this
/// process can speak for itself, and from what Python answers. Python's own `X-QAG-CSRF` passes, as any other.
const OURS: [HeaderName; 4] = [SECRET_HEADER, PEER_HEADER, PLAN_HEADER, WANTED_HEADER];

/// Remove hop-by-hop headers, and the data plane's own (`OURS`).
pub fn clean(headers: &mut HeaderMap) {
    let named: Vec<HeaderName> = headers
        .get_all(header::CONNECTION)
        .iter()
        .filter_map(|value| value.to_str().ok())
        .flat_map(|value| value.split(','))
        .filter_map(|name| HeaderName::from_bytes(name.trim().as_bytes()).ok())
        .collect();
    for name in named.iter().chain(HOP_BY_HOP.iter()).chain(OURS.iter()) {
        headers.remove(name);
    }
}

/// A JSON error in the shape the Python server answers with, for what only the data plane can say.
pub fn error_response(status: StatusCode, message: &str, code: &str) -> Response<Body> {
    let mut body = String::from(r#"{"error":"#);
    crate::render::json_string(message, &mut body);
    body.push_str(r#","code":"#);
    crate::render::json_string(code, &mut body);
    body.push('}');
    let mut response = Response::new(
        Full::new(Bytes::from(body))
            .map_err(|never| match never {})
            .boxed(),
    );
    *response.status_mut() = status;
    response.headers_mut().insert(
        header::CONTENT_TYPE,
        HeaderValue::from_static("application/json"),
    );
    response
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_client_never_sends_the_data_plane_s_own_headers() {
        let mut headers = HeaderMap::new();
        for name in ["x-qag-dataplane", "X-QAG-Peer", "x-qag-plan"] {
            headers.insert(
                HeaderName::from_bytes(name.as_bytes()).unwrap(),
                HeaderValue::from_static("spoofed"),
            );
        }
        headers.insert(header::ACCEPT, HeaderValue::from_static("application/json"));
        clean(&mut headers);
        assert_eq!(headers.len(), 1);
        assert!(headers.contains_key(header::ACCEPT));
    }

    #[test]
    fn the_console_s_csrf_header_passes() {
        let mut headers = HeaderMap::new();
        headers.insert("x-qag-csrf", HeaderValue::from_static("token"));
        clean(&mut headers);
        assert_eq!(headers.get("x-qag-csrf").unwrap(), "token");
    }

    #[test]
    fn hop_by_hop_headers_and_those_connection_names_are_dropped() {
        let mut headers = HeaderMap::new();
        headers.insert(
            header::CONNECTION,
            HeaderValue::from_static("keep-alive, x-custom-hop"),
        );
        headers.insert("x-custom-hop", HeaderValue::from_static("1"));
        headers.insert("keep-alive", HeaderValue::from_static("timeout=5"));
        headers.insert(
            header::TRANSFER_ENCODING,
            HeaderValue::from_static("chunked"),
        );
        headers.insert("x-api-key", HeaderValue::from_static("secret"));
        clean(&mut headers);
        assert_eq!(
            headers.keys().map(|k| k.as_str()).collect::<Vec<_>>(),
            vec!["x-api-key"]
        );
    }

    #[test]
    fn an_ipv4_client_on_a_dual_stack_socket_is_written_as_ipv4() {
        let mapped: IpAddr = "::ffff:203.0.113.9".parse().unwrap();
        assert_eq!(peer_text(mapped), "203.0.113.9");
        assert_eq!(peer_text("2001:db8::1".parse().unwrap()), "2001:db8::1");
    }

    #[test]
    fn an_error_s_text_is_valid_json() {
        let response = error_response(StatusCode::BAD_GATEWAY, "a \"b\" \\ \n é", "upstream");
        let body = response.into_body();
        let bytes = futures_body(body);
        assert_eq!(
            bytes,
            r#"{"error":"a \"b\" \\ \n \u00e9","code":"upstream"}"#
        );
    }

    fn futures_body(body: Body) -> String {
        let collected = tokio::runtime::Builder::new_current_thread()
            .build()
            .unwrap()
            .block_on(body.collect())
            .unwrap();
        String::from_utf8(collected.to_bytes().to_vec()).unwrap()
    }
}
