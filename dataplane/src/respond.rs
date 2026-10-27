//! The response around a body, as Python sends it for a `/q/` call: render()'s paging headers, after_request's request
//! ID and security headers (secheaders.py), and the 504 a time limit ends in. CORS and HSTS are never needed here: a
//! request with an Origin, or one that could have arrived over HTTPS with HSTS on, is forwarded.

use bytes::Bytes;
use http_body_util::{BodyExt, Full};
use hyper::header::{self, HeaderName, HeaderValue};
use hyper::{Response, StatusCode};

use crate::forward::Body;
use crate::pyfmt;
use crate::render::json_string;

/// secheaders.API_CSP
const API_CSP: &str = "default-src 'none'; frame-ancestors 'none'";

/// app.new_request_id(): the caller's X-Request-Id when it has the accepted shape (`[A-Za-z0-9._:-]{1,64}`), else
/// twelve random hex digits, as `uuid4().hex[:12]` gives.
pub fn request_id(supplied: Option<&str>) -> String {
    if let Some(id) = supplied
        && (1..=64).contains(&id.len())
        && id
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"._:-".contains(&b))
    {
        return id.to_string();
    }
    let mut random = [0u8; 6];
    getrandom::fill(&mut random).expect("the system's random source");
    hex::encode(random)
}

fn with_headers(status: StatusCode, body: String, request_id: &str) -> Response<Body> {
    let mut response = Response::new(
        Full::new(Bytes::from(body))
            .map_err(|never| match never {})
            .boxed(),
    );
    *response.status_mut() = status;
    let headers = response.headers_mut();
    headers.insert(
        header::CONTENT_TYPE,
        HeaderValue::from_static("application/json"),
    );
    headers.insert(
        HeaderName::from_static("x-request-id"),
        HeaderValue::from_str(request_id).expect("checked"),
    );
    headers.insert(
        header::X_CONTENT_TYPE_OPTIONS,
        HeaderValue::from_static("nosniff"),
    );
    headers.insert(
        header::CONTENT_SECURITY_POLICY,
        HeaderValue::from_static(API_CSP),
    );
    headers.insert(
        header::REFERRER_POLICY,
        HeaderValue::from_static("no-referrer"),
    );
    headers.insert(header::CACHE_CONTROL, HeaderValue::from_static("no-store"));
    response
}

/// A page: the body, and render()'s X-Page, X-Page-Size, X-Row-Count and X-Has-More.
pub fn page(
    body: String,
    page: i64,
    page_size: i64,
    rows: usize,
    has_more: bool,
    request_id: &str,
) -> Response<Body> {
    let mut response = with_headers(StatusCode::OK, body, request_id);
    let headers = response.headers_mut();
    let number = |n: String| HeaderValue::from_str(&n).expect("digits");
    headers.insert(HeaderName::from_static("x-page"), number(page.to_string()));
    headers.insert(
        HeaderName::from_static("x-page-size"),
        number(page_size.to_string()),
    );
    headers.insert(
        HeaderName::from_static("x-row-count"),
        number(rows.to_string()),
    );
    headers.insert(
        HeaderName::from_static("x-has-more"),
        HeaderValue::from_static(if has_more { "true" } else { "false" }),
    );
    response
}

/// runners._timed_out(), as handle_api_error() answers it: error_body()'s keys in its order.
pub fn timed_out(timeout: f64, request_id: &str) -> (Response<Body>, String) {
    let message = format!(
        "The query exceeded the time limit of {} seconds and was cancelled",
        pyfmt::general(timeout)
    );
    let mut body = String::from("{\"error\":");
    json_string(&message, &mut body);
    body.push_str(",\"code\":\"query_timeout\",\"timeout\":");
    body.push_str(&pyfmt::repr(timeout));
    body.push_str(",\"request_id\":");
    json_string(request_id, &mut body);
    body.push_str("}\n");
    (
        with_headers(StatusCode::GATEWAY_TIMEOUT, body, request_id),
        message,
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn keeps_an_acceptable_request_id_and_replaces_any_other() {
        assert_eq!(request_id(Some("trace-1.2:3")), "trace-1.2:3");
        for bad in [
            None,
            Some(""),
            Some("has space"),
            Some("é"),
            Some(&"x".repeat(65)[..]),
        ] {
            let made = request_id(bad);
            assert_eq!(made.len(), 12);
            assert!(made.bytes().all(|b| b.is_ascii_hexdigit()));
        }
    }

    #[test]
    fn a_timeout_as_python_answers_it() {
        let (response, _) = timed_out(0.5, "rid");
        assert_eq!(response.status(), 504);
        // jsonify(error_body('The query exceeded ...', 504, {'timeout': 0.5}, 'query_timeout')) in Python
        let python = "{\"error\":\"The query exceeded the time limit of 0.5 seconds and was cancelled\",\
                      \"code\":\"query_timeout\",\"timeout\":0.5,\"request_id\":\"rid\"}\n";
        let body = futures_body(response);
        assert_eq!(body, python);
        let (thirty, _) = timed_out(30.0, "rid");
        assert!(
            futures_body(thirty)
                .contains("limit of 30 seconds and was cancelled\",\"code\":\"query_timeout\",\"timeout\":30.0,")
        );
    }

    fn futures_body(response: Response<Body>) -> String {
        let runtime = tokio::runtime::Builder::new_current_thread()
            .build()
            .unwrap();
        let bytes = runtime
            .block_on(response.into_body().collect())
            .unwrap()
            .to_bytes();
        String::from_utf8(bytes.to_vec()).unwrap()
    }

    #[test]
    fn the_security_headers_python_adds() {
        let response = page("[]\n".into(), 2, 10, 0, false, "rid");
        let headers = response.headers();
        assert_eq!(headers["x-content-type-options"], "nosniff");
        assert_eq!(headers["content-security-policy"], API_CSP);
        assert_eq!(headers["referrer-policy"], "no-referrer");
        assert_eq!(headers["cache-control"], "no-store");
        assert_eq!(
            (
                &headers["x-page"],
                &headers["x-page-size"],
                &headers["x-has-more"]
            ),
            (
                &"2".parse().unwrap(),
                &"10".parse().unwrap(),
                &"false".parse().unwrap()
            )
        );
    }
}
