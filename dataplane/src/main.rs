//! QueryAPIGate's data plane (ADR 0028, a prototype): listens on the public port in front of the Python server and
//! forwards every request to it unchanged - the Console, the Management API, MCP, `/events`, everything. Serving
//! `/q/<name>` itself from Python's plans comes next; until then this is the front alone.
//!
//! What it changes on the way through, and nothing else:
//! - removes every `X-QAG-*` header a client sent, so only this process can speak for itself;
//! - adds `X-QAG-Dataplane` (the shared secret) and `X-QAG-Peer` (the client's socket address), which Python takes
//!   together as the real client address - so its open-access loopback rule and a key's `allowed_ips` judge the client;
//! - drops hop-by-hop headers in both directions, as any proxy must;
//! - removes `X-QAG-Plan` from what Python answers.

mod config;
mod forward;

use std::net::SocketAddr;
use std::sync::Arc;

use hyper::server::conn::http1;
use hyper::service::service_fn;
use hyper_util::rt::{TokioIo, TokioTimer};
use tokio::net::TcpListener;

fn main() {
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
