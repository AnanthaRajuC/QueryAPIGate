//! The log lines a `/q/` call served here writes, in the Python server's formats (logging_setup.py): plain
//! (`%(asctime)s %(levelname)s %(name)s [%(request_id)s key=%(key)s]: %(message)s`, line breaks escaped) or JSON
//! (QUERYAPIGATE_JSON_LOGS), on stderr - and gunicorn's access line (accesslog.py), on stdout. Exact wording isn't
//! covered by the versioning policy; what a line costs is what matters for a fair comparison.

use std::io::Write;

use serde_json::{Map, Value as Json};

use crate::render::json_string;

pub struct Logger {
    pub json: bool,
}

impl Logger {
    pub fn line(
        &self,
        level: &str,
        request_id: &str,
        key: &str,
        message: &str,
        extra: Map<String, Json>,
    ) {
        let now = chrono::Local::now();
        let mut out = String::with_capacity(160 + message.len());
        if self.json {
            // json.dumps() of the payload: ', ' and ': ', ensure_ascii
            out.push_str("{\"time\": ");
            json_string(&now.format("%Y-%m-%dT%H:%M:%S").to_string(), &mut out);
            out.push_str(", \"level\": ");
            json_string(level, &mut out);
            out.push_str(", \"logger\": \"queryapigate\", \"request_id\": ");
            json_string(request_id, &mut out);
            out.push_str(", \"key\": ");
            json_string(key, &mut out);
            out.push_str(", \"message\": ");
            json_string(message, &mut out);
            for (name, value) in &extra {
                out.push_str(", ");
                json_string(name, &mut out);
                out.push_str(": ");
                match value {
                    Json::String(s) => json_string(s, &mut out),
                    other => out.push_str(&other.to_string()),
                }
            }
            out.push('}');
        } else {
            let escaped = message
                .replace('\\', "\\\\")
                .replace('\r', "\\r")
                .replace('\n', "\\n");
            out.push_str(&format!(
                "{} {level} queryapigate [{request_id} key={key}]: {escaped}",
                now.format("%Y-%m-%d %H:%M:%S,%3f")
            ));
        }
        out.push('\n');
        let _ = std::io::stderr().lock().write_all(out.as_bytes());
    }
}

/// gunicorn's access line, as gunicorn_conf.py configures it (accesslog._WITHOUT_QUERY).
#[allow(clippy::too_many_arguments)]
pub fn access(
    peer: &str,
    method: &str,
    path: &str,
    status: u16,
    bytes: usize,
    seconds: f64,
    request_id: &str,
    agent: &str,
) {
    let now = chrono::Local::now();
    let size = if bytes == 0 {
        "-".to_string()
    } else {
        bytes.to_string()
    };
    let line = format!(
        "{peer} - [{}] \"{method} {path} HTTP/1.1\" {status} {size} {seconds:.6}s rid={request_id} \"{agent}\"\n",
        now.format("%d/%b/%Y:%H:%M:%S %z")
    );
    let _ = std::io::stdout().lock().write_all(line.as_bytes());
}
