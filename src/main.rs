use axum::{routing::get, Router};
use reqwest::Client;
use serde::Serialize;
use std::{env, net::SocketAddr, time::Duration};
use tokio::{signal, time};
use tracing::{error, info, warn};

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

#[derive(Debug, Clone)]
struct Config {
    relay_url: String,
    token: String,
    printer_id: String,
    log_file: Option<String>,
    health_addr: SocketAddr,
}

impl Config {
    fn from_env() -> Result<Self, String> {
        let relay_url = require_env("REACH_LINK_RELAY")?;
        let token = require_env("REACH_LINK_TOKEN")?;
        let printer_id = require_env("REACH_LINK_PRINTER_ID")?;
        let log_file = env::var("REACH_LINK_LOG_FILE").ok();

        // Validate relay URL starts with https://
        if !relay_url.starts_with("https://") {
            return Err(format!(
                "REACH_LINK_RELAY must use HTTPS, got: {}",
                relay_url
            ));
        }

        // Validate token is non-empty
        if token.trim().is_empty() {
            return Err("REACH_LINK_TOKEN must not be empty".into());
        }

        // Validate printer_id is non-empty
        if printer_id.trim().is_empty() {
            return Err("REACH_LINK_PRINTER_ID must not be empty".into());
        }

        let health_port: u16 = env::var("REACH_LINK_HEALTH_PORT")
            .unwrap_or_else(|_| "8080".into())
            .parse()
            .map_err(|_| "REACH_LINK_HEALTH_PORT must be a valid port number")?;

        let health_addr = SocketAddr::from(([0, 0, 0, 0], health_port));

        Ok(Self {
            relay_url,
            token,
            printer_id,
            log_file,
            health_addr,
        })
    }
}

fn require_env(name: &str) -> Result<String, String> {
    env::var(name).map_err(|_| format!("Required environment variable {} is not set", name))
}

// ---------------------------------------------------------------------------
// Relay client
// ---------------------------------------------------------------------------

#[derive(Serialize)]
struct RegisterPayload<'a> {
    printer_id: &'a str,
}

async fn register_printer(client: &Client, config: &Config) -> Result<(), reqwest::Error> {
    let url = format!("{}/api/v1/register", config.relay_url);
    let payload = RegisterPayload {
        printer_id: &config.printer_id,
    };

    let response = client
        .post(&url)
        .bearer_auth(&config.token)
        .json(&payload)
        .send()
        .await?;

    if response.status().is_success() {
        info!(
            printer_id = %config.printer_id,
            status = %response.status(),
            "Printer registered successfully"
        );
    } else {
        warn!(
            printer_id = %config.printer_id,
            status = %response.status(),
            "Relay returned non-success status on registration"
        );
    }

    Ok(())
}

async fn heartbeat_loop(client: Client, config: Config) {
    let interval_secs: u64 = env::var("REACH_LINK_HEARTBEAT_INTERVAL")
        .unwrap_or_else(|_| "30".into())
        .parse()
        .unwrap_or(30);

    let mut interval = time::interval(Duration::from_secs(interval_secs));
    loop {
        interval.tick().await;
        if let Err(e) = register_printer(&client, &config).await {
            error!(error = %e, "Failed to reach relay server");
        }
    }
}

// ---------------------------------------------------------------------------
// Health check HTTP server
// ---------------------------------------------------------------------------

async fn health_handler() -> &'static str {
    "OK"
}

async fn run_health_server(addr: SocketAddr) {
    let app = Router::new().route("/health", get(health_handler));
    let listener = match tokio::net::TcpListener::bind(addr).await {
        Ok(l) => l,
        Err(e) => {
            error!(error = %e, addr = %addr, "Failed to bind health server");
            return;
        }
    };
    info!(addr = %addr, "Health check server listening");
    if let Err(e) = axum::serve(listener, app)
        .with_graceful_shutdown(shutdown_signal())
        .await
    {
        error!(error = %e, "Health server error");
    }
}

// ---------------------------------------------------------------------------
// Graceful shutdown
// ---------------------------------------------------------------------------

async fn shutdown_signal() {
    let ctrl_c = async {
        signal::ctrl_c()
            .await
            .expect("failed to install Ctrl+C handler");
    };

    #[cfg(unix)]
    let sigterm = async {
        signal::unix::signal(signal::unix::SignalKind::terminate())
            .expect("failed to install SIGTERM handler")
            .recv()
            .await;
    };

    #[cfg(not(unix))]
    let sigterm = std::future::pending::<()>();

    tokio::select! {
        _ = ctrl_c => { info!("Received Ctrl+C, shutting down"); },
        _ = sigterm => { info!("Received SIGTERM, shutting down"); },
    }
}

// ---------------------------------------------------------------------------
// Logging setup
// ---------------------------------------------------------------------------

fn setup_logging(log_file: Option<&str>) {
    use tracing_subscriber::{fmt, prelude::*, EnvFilter};

    let filter = EnvFilter::try_from_default_env()
        // Default to "info" level; override by setting RUST_LOG (e.g. RUST_LOG=debug)
        .unwrap_or_else(|_| EnvFilter::new("info"));

    let registry = tracing_subscriber::registry().with(filter);

    if let Some(path) = log_file {
        let file = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(path)
            .expect("Failed to open log file");

        let file_layer = fmt::layer().with_writer(file).with_ansi(false);
        let stdout_layer = fmt::layer().with_writer(std::io::stdout);
        registry.with(stdout_layer).with(file_layer).init();
    } else {
        let stdout_layer = fmt::layer().with_writer(std::io::stdout);
        registry.with(stdout_layer).init();
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use std::{env, sync::Mutex};

    // Serialize env-var tests to avoid race conditions between parallel test threads
    static ENV_LOCK: Mutex<()> = Mutex::new(());

    fn set_valid_env() {
        env::set_var("REACH_LINK_RELAY", "https://relay.example.com");
        env::set_var("REACH_LINK_TOKEN", "test-token");
        env::set_var("REACH_LINK_PRINTER_ID", "printer-001");
    }

    fn clear_env() {
        env::remove_var("REACH_LINK_RELAY");
        env::remove_var("REACH_LINK_TOKEN");
        env::remove_var("REACH_LINK_PRINTER_ID");
        env::remove_var("REACH_LINK_HEALTH_PORT");
    }

    #[test]
    fn test_valid_config() {
        let _lock = ENV_LOCK.lock().unwrap();
        set_valid_env();
        let config = Config::from_env();
        clear_env();
        assert!(config.is_ok());
        let c = config.unwrap();
        assert_eq!(c.relay_url, "https://relay.example.com");
        assert_eq!(c.printer_id, "printer-001");
    }

    #[test]
    fn test_missing_relay() {
        let _lock = ENV_LOCK.lock().unwrap();
        set_valid_env();
        env::remove_var("REACH_LINK_RELAY");
        let result = Config::from_env();
        clear_env();
        assert!(result.is_err());
        assert!(result.unwrap_err().contains("REACH_LINK_RELAY"));
    }

    #[test]
    fn test_http_relay_rejected() {
        let _lock = ENV_LOCK.lock().unwrap();
        set_valid_env();
        env::set_var("REACH_LINK_RELAY", "http://relay.example.com");
        let result = Config::from_env();
        clear_env();
        assert!(result.is_err());
        assert!(result.unwrap_err().contains("HTTPS"));
    }

    #[test]
    fn test_empty_token_rejected() {
        let _lock = ENV_LOCK.lock().unwrap();
        set_valid_env();
        env::set_var("REACH_LINK_TOKEN", "   ");
        let result = Config::from_env();
        clear_env();
        assert!(result.is_err());
        assert!(result.unwrap_err().contains("REACH_LINK_TOKEN"));
    }

    #[test]
    fn test_empty_printer_id_rejected() {
        let _lock = ENV_LOCK.lock().unwrap();
        set_valid_env();
        env::set_var("REACH_LINK_PRINTER_ID", "");
        let result = Config::from_env();
        clear_env();
        assert!(result.is_err());
        assert!(result.unwrap_err().contains("REACH_LINK_PRINTER_ID"));
    }

    #[test]
    fn test_invalid_health_port_rejected() {
        let _lock = ENV_LOCK.lock().unwrap();
        set_valid_env();
        env::set_var("REACH_LINK_HEALTH_PORT", "not-a-number");
        let result = Config::from_env();
        clear_env();
        assert!(result.is_err());
    }

    #[test]
    fn test_default_health_port() {
        let _lock = ENV_LOCK.lock().unwrap();
        set_valid_env();
        env::remove_var("REACH_LINK_HEALTH_PORT");
        let config = Config::from_env().unwrap();
        clear_env();
        assert_eq!(config.health_addr.port(), 8080);
    }
}

// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------

#[tokio::main]
async fn main() {
    // Parse config first (before logging) so we can pass log_file to setup
    let config = match Config::from_env() {
        Ok(c) => c,
        Err(e) => {
            eprintln!("Configuration error: {}", e);
            std::process::exit(1);
        }
    };

    setup_logging(config.log_file.as_deref());

    info!(
        version = env!("CARGO_PKG_VERSION"),
        printer_id = %config.printer_id,
        relay = %config.relay_url,
        "reach-link starting"
    );

    let client = Client::builder()
        .timeout(Duration::from_secs(10))
        .build()
        .expect("Failed to build HTTP client");

    // Initial registration
    if let Err(e) = register_printer(&client, &config).await {
        error!(error = %e, "Initial registration failed, will retry on next heartbeat");
    }

    // Run health server and heartbeat loop concurrently
    let health_addr = config.health_addr;
    tokio::select! {
        _ = run_health_server(health_addr) => {},
        _ = heartbeat_loop(client, config) => {},
        _ = shutdown_signal() => {
            info!("reach-link stopped");
        },
    }
}
