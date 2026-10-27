//! QueryAPIGate's data plane (ADR 0028, a prototype): listens on the public port in front of the Python server and
//! forwards every request to it unchanged - the Console, the Management API, MCP, `/events`, everything. Serving
//! `/q/<name>` itself from Python's plans comes next; until then this is the front alone.
//!
//! What it changes on the way through, and nothing else:
//! - removes the data plane's own headers from what a client sends (`X-QAG-Dataplane`, `X-QAG-Peer`,
//!   `X-QAG-Plan`), so only this process can speak for itself - the Console's `X-QAG-CSRF` passes;
//! - adds `X-QAG-Dataplane` (the shared secret) and `X-QAG-Peer` (the client's socket address), which Python takes
//!   together as the real client address - so its open-access loopback rule and a key's `allowed_ips` judge the client;
//! - drops hop-by-hop headers in both directions, as any proxy must;
//! - removes `X-QAG-Plan` from what Python answers.

mod auth;
mod config;
mod forward;
mod params;
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
    eprintln!(
        "queryapigate-dataplane: listening on {}, forwarding to {} ({} threads)",
        config.listen, config.upstream, config.threads
    );
    let state = Arc::new(forward::State::new(config));
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
        let state = state.clone();
        tokio::spawn(connection(state, stream, peer));
    }
}

async fn connection(state: Arc<forward::State>, stream: tokio::net::TcpStream, peer: SocketAddr) {
    let service = service_fn(move |request| {
        let state = state.clone();
        async move { Ok::<_, std::convert::Infallible>(state.handle(request, peer).await) }
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
