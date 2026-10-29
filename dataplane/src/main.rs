//! QueryAPIGate's data plane (ADR 0028, a prototype): listens on the public port in front of the Python server,
//! serves `/q/<name>` reads itself from the plans Python gives it (serve.rs), and forwards every other request to it
//! unchanged - the Console, the Management API, MCP, `/events`, everything.
//!
//! What it changes on a forwarded request's way through, and nothing else:
//! - removes the data plane's own headers from what a client sends (`X-QAG-Dataplane`, `X-QAG-Peer`,
//!   `X-QAG-Plan`), so only this process can speak for itself - the Console's `X-QAG-CSRF` passes;
//! - adds `X-QAG-Dataplane` (the shared secret) and `X-QAG-Peer` (the client's socket address), which Python takes
//!   together as the real client address - so its open-access loopback rule and a key's `allowed_ips` judge the client;
//! - drops hop-by-hop headers in both directions, as any proxy must;
//! - removes `X-QAG-Plan` from what Python answers.

mod auth;
mod config;
mod db;
mod forward;
mod log;
mod params;
mod pyfmt;
mod render;
mod respond;
mod runstore;
mod serve;
mod store;

use std::net::SocketAddr;
use std::sync::Arc;

use hyper::server::conn::http1;
use hyper::service::service_fn;
use hyper_util::rt::{TokioIo, TokioTimer};
use tokio::net::TcpListener;

fn main() {
    let args: Vec<String> = std::env::args().collect();
    if args.get(1).map(String::as_str) == Some("--probe-digests") && args.len() == 4 {
        // For the tests only: the digests of the rows a call for this secret and query would read
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .expect("a runtime");
        match runtime.block_on(probe_digests(&args[2], &args[3])) {
            Ok(found) => println!("{found}"),
            Err(error) => {
                eprintln!("queryapigate-dataplane: {error}");
                std::process::exit(1);
            }
        }
        return;
    }
    if args.get(1).map(String::as_str) == Some("--probe-query") {
        // For the tests only: run the requests on stdin, in order, over one connection - see probe_query()
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .expect("a runtime");
        let mut input = String::new();
        std::io::Read::read_to_string(&mut std::io::stdin(), &mut input).expect("stdin");
        println!("{}", runtime.block_on(probe_query(&input)));
        return;
    }
    let config = match config::Config::from_env() {
        Ok(config) => config,
        Err(message) => {
            eprintln!("queryapigate-dataplane: {message}");
            std::process::exit(2);
        }
    };
    let runtime = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(config.threads)
        .enable_all()
        .build()
        .expect("the Tokio runtime");
    runtime.block_on(serve(config));
}

async fn serve(config: config::Config) {
    let listener = match TcpListener::bind(config.listen).await {
        Ok(listener) => listener,
        Err(error) => {
            eprintln!(
                "queryapigate-dataplane: can't listen on {}: {error}",
                config.listen
            );
            std::process::exit(2);
        }
    };
    let get = |name: &str| {
        std::env::var(name)
            .ok()
            .filter(|value| !value.trim().is_empty())
    };
    let settings = serve::Settings::from(get);
    // Without the store every call is forwarded: the front still works, the data plane serves nothing
    let store = match store::Store::open(get, config.threads as u32 * 2).await {
        Ok(store) => Some(store),
        Err(error) => {
            eprintln!("queryapigate-dataplane: forwarding everything - {error}");
            None
        }
    };
    // A misconfigured run store fails startup, as Python's does: running would record its runs nowhere
    let run_store = match runstore::url_from(get) {
        Ok(target) => target.map(runstore::RunStore::new),
        Err(error) => {
            eprintln!("queryapigate-dataplane: {error}");
            std::process::exit(2);
        }
    };
    if run_store.is_some() {
        eprintln!("queryapigate-dataplane: writing its runs to the run store (ADR 0029)");
    }
    if let Some(reason) = settings.forward_everything {
        eprintln!("queryapigate-dataplane: forwarding everything - {reason} is set");
    }
    eprintln!(
        "queryapigate-dataplane: listening on {}, in front of {} ({} threads)",
        config.listen, config.upstream, config.threads
    );
    let app = serve::App::new(forward::Forwarder::new(&config), settings, store, run_store);
    let shutdown = shutdown_signal();
    tokio::pin!(shutdown);
    loop {
        let (stream, peer) = tokio::select! {
            accepted = listener.accept() => match accepted {
                Ok(accepted) => accepted,
                Err(error) => {
                    eprintln!("queryapigate-dataplane: accept failed: {error}");
                    continue;
                }
            },
            () = &mut shutdown => break,
        };
        let _ = stream.set_nodelay(true);
        tokio::spawn(connection(app.clone(), stream, peer));
    }
    app.drain().await;
    app.release_lease().await;
}

async fn connection(app: Arc<serve::App>, stream: tokio::net::TcpStream, peer: SocketAddr) {
    let service = service_fn(move |request| {
        let app = app.clone();
        async move { Ok::<_, std::convert::Infallible>(app.handle(request, peer).await) }
    });
    let served = http1::Builder::new()
        .timer(TokioTimer::new())
        .keep_alive(true)
        .serve_connection(TokioIo::new(stream), service)
        .await;
    if let Err(error) = served
        && !error.is_incomplete_message()
    {
        eprintln!("queryapigate-dataplane: connection from {peer}: {error}");
    }
}

async fn shutdown_signal() {
    let terminate = async {
        if let Ok(mut signal) =
            tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
        {
            signal.recv().await;
        }
    };
    tokio::select! {
        _ = tokio::signal::ctrl_c() => {},
        () = terminate => {},
    }
}

async fn probe_digests(secret: &str, query: &str) -> Result<String, String> {
    let get = |name: &str| {
        std::env::var(name)
            .ok()
            .filter(|value| !value.trim().is_empty())
    };
    let store = store::Store::open(get, 1).await?;
    let error = |e: sqlx::Error| e.to_string();
    let key = store.key(&auth::hash(secret)).await.map_err(error)?;
    let saved = store.query("default", query).await.map_err(error)?;
    let published = saved
        .get(0, "published_version")
        .and_then(store::Value::int)
        .ok_or("not published")?;
    let fields = (0..saved.rows.len())
        .find(|&i| saved.get(i, "version").and_then(store::Value::int) == Some(published))
        .and_then(|i| saved.get(i, "fields_json").and_then(store::Value::text))
        .ok_or("no published version")?;
    let fields: serde_json::Value = serde_json::from_str(fields).map_err(|e| e.to_string())?;
    let connection = fields["connection_name"].as_str().ok_or("no connection")?;
    let connection = store
        .connection("default", connection)
        .await
        .map_err(error)?;
    Ok(serde_json::json!({
        "api_key": key.digest(), "saved_query": saved.digest(), "connection": connection.digest(),
    })
    .to_string())
}

/// `{"dialect", "details", "requests": [{"statement", "values", "limit", "offset", "timeout", "search_path"}]}` ->
/// one result per request: `{"columns": [[name, type]], "rows": [[text|null]]}` or `{"error": ...}`. Values are
/// tagged as in tests/params_vectors.jsonl.
async fn probe_query(input: &str) -> String {
    use serde_json::{Value, json};
    let input: Value = serde_json::from_str(input).expect("a JSON request");
    let keys: Vec<String> = std::env::var("QUERYAPIGATE_SECRET_KEY")
        .unwrap_or_default()
        .split(',')
        .map(|k| k.trim().to_string())
        .filter(|k| !k.is_empty())
        .collect();
    let Some(target) = db::target(
        input["dialect"].as_str().unwrap(),
        input["details"].as_str().unwrap(),
        &keys,
    ) else {
        return json!({"error": "declined"}).to_string();
    };
    let pool = match db::open(&target, 1).await {
        Ok(pool) => pool,
        Err(failure) => return json!({"error": format!("{failure:?}")}).to_string(),
    };
    let mut results = Vec::new();
    for request in input["requests"].as_array().unwrap() {
        let statement: db::Statement =
            serde_json::from_value(request["statement"].clone()).unwrap();
        let mut values = std::collections::HashMap::new();
        for (name, tag) in request["values"].as_object().unwrap() {
            let tag = tag.as_array().unwrap();
            let value = match tag[0].as_str().unwrap() {
                "null" => params::Param::Null,
                "bool" => params::Param::Bool(tag[1].as_bool().unwrap()),
                "int" => params::Param::Int(tag[1].as_str().unwrap().parse().unwrap()),
                "float" => params::Param::Float(tag[1].as_str().unwrap().parse().unwrap()),
                _ => params::Param::Text(tag[1].as_str().unwrap().to_string()),
            };
            values.insert(name.clone(), params::Supplied::Value(value));
        }
        let run = db::run(
            &pool,
            &statement,
            &values,
            request["limit"].as_i64().unwrap(),
            request["offset"].as_i64().unwrap(),
            request["timeout"].as_f64(),
            request["search_path"].as_str(),
        )
        .await;
        results.push(match run {
            Ok(output) => json!({
                "columns": output.columns,
                "rows": output.rows.iter().map(|row| row.iter().map(|cell| cell.as_ref()
                    .map(|bytes| String::from_utf8_lossy(bytes).into_owned())).collect::<Vec<_>>()).collect::<Vec<_>>(),
                // the same, exactly as sent - invalid UTF-8 included
                "hex": output.rows.iter().map(|row| row.iter().map(|cell| cell.as_ref().map(hex::encode))
                    .collect::<Vec<_>>()).collect::<Vec<_>>(),
            }),
            Err(failure) => json!({"error": format!("{failure:?}")}),
        });
        tokio::time::sleep(std::time::Duration::from_millis(20)).await; // MySQL's ROLLBACK AND CHAIN, spawned
    }
    Value::Array(results).to_string()
}
