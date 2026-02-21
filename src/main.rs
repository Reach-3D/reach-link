use axum::{routing::get, Router};
use reqwest::Client;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::{env, net::SocketAddr, sync::Arc, time::Duration};
use tokio::{signal, sync::broadcast, time};
use tracing::{debug, error, info, warn};

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

#[derive(Debug, Clone)]
struct Config {
    relay_url: String,
    token: String,
    printer_id: String,
    moonraker_url: String,
    heartbeat_interval_secs: u64,
    telemetry_interval_secs: u64,
    log_file: Option<String>,
    health_addr: SocketAddr,
}

impl Config {
    fn from_env() -> Result<Self, String> {
        let relay_url = require_env("REACH_LINK_RELAY")?;
        let token = require_env("REACH_LINK_TOKEN")?;
        let printer_id = require_env_with_fallback("REACH_LINK_PRINTER_ID", "REACH_PRINTER_ID")?;
        let moonraker_url = env::var("REACH_LINK_MOONRAKER_URL")
            .unwrap_or_else(|_| "http://127.0.0.1:7125".to_string())
            .trim_end_matches('/')
            .to_string();
        let heartbeat_interval_secs: u64 = env::var("REACH_LINK_HEARTBEAT_INTERVAL")
            .unwrap_or_else(|_| "30".into())
            .parse()
            .unwrap_or(30);
        let telemetry_interval_secs: u64 = env::var("REACH_LINK_TELEMETRY_INTERVAL")
            .unwrap_or_else(|_| "10".into())
            .parse()
            .unwrap_or(10);
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
            moonraker_url,
            heartbeat_interval_secs,
            telemetry_interval_secs,
            log_file,
            health_addr,
        })
    }
}

fn require_env(name: &str) -> Result<String, String> {
    env::var(name).map_err(|_| format!("Required environment variable {} is not set", name))
}

fn require_env_with_fallback(primary: &str, fallback: &str) -> Result<String, String> {
    match env::var(primary) {
        Ok(value) => Ok(value),
        Err(_) => env::var(fallback).map_err(|_| {
            format!(
                "Required environment variable {} is not set (fallback {} also missing)",
                primary, fallback
            )
        }),
    }
}

// ---------------------------------------------------------------------------
// Relay client
// ---------------------------------------------------------------------------

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct RegisterPayload<'a> {
    printer_id: &'a str,
    token: &'a str,
    timestamp: i64,
    uptime: u64,
    version: &'static str,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct RegisterResponse {
    next_check_in: Option<u64>,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct TelemetryPayload<'a> {
    printer_id: &'a str,
    token: &'a str,
    timestamp: i64,
    temperatures: Option<Temperatures>,
    job: Option<Job>,
    system_health: Option<SystemHealth>,
    errors: Vec<TelemetryError>,
    log_tail: Vec<String>,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct TelemetryResponse {
    next_data_interval: Option<u64>,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct Temperatures {
    nozzle: Option<f64>,
    bed: Option<f64>,
    chamber: Option<f64>,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct Job {
    filename: Option<String>,
    progress: Option<f64>,
    eta: Option<u64>,
    elapsed_time: Option<u64>,
    state: &'static str,
    totaltime: Option<u64>,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct SystemHealth {
    cpu_percent: Option<f64>,
    memory_percent: Option<f64>,
    disk_percent: Option<f64>,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct TelemetryError {
    r#type: String,
    message: String,
    timestamp: i64,
    severity: &'static str,
}

#[derive(Default)]
struct MoonrakerSnapshot {
    temperatures: Option<Temperatures>,
    job: Option<Job>,
}

fn unix_timestamp_ms() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as i64
}

async fn register_printer(
    client: &Client,
    config: &Config,
    uptime_secs: u64,
) -> Result<Option<u64>, reqwest::Error> {
    let url = format!("{}/api/reach-link/register", config.relay_url);
    let payload = RegisterPayload {
        printer_id: &config.printer_id,
        token: &config.token,
        timestamp: unix_timestamp_ms(),
        uptime: uptime_secs,
        version: env!("CARGO_PKG_VERSION"),
    };

    let response = client
        .post(&url)
        .bearer_auth(&config.token)
        .json(&payload)
        .send()
        .await?;
    let status = response.status();

    if status.is_success() {
        let next_interval = response
            .json::<RegisterResponse>()
            .await
            .ok()
            .and_then(|payload| payload.next_check_in);

        info!(
            printer_id = %config.printer_id,
            status = %status,
            next_check_in = ?next_interval,
            "Printer registered successfully"
        );
        return Ok(next_interval);
    } else {
        let body = response.text().await.unwrap_or_default();
        warn!(
            printer_id = %config.printer_id,
            status = %status,
            body = %body,
            "Relay returned non-success status on registration"
        );
    }

    Ok(None)
}

fn map_job_state(value: Option<&str>) -> &'static str {
    match value.unwrap_or("unknown").to_ascii_lowercase().as_str() {
        "standby" | "ready" | "idle" | "complete" | "completed" => "idle",
        "printing" => "printing",
        "paused" => "paused",
        "error" | "cancelled" | "canceled" | "failed" => "error",
        _ => "unknown",
    }
}

async fn fetch_moonraker_snapshot(client: &Client, config: &Config) -> Result<MoonrakerSnapshot, reqwest::Error> {
    let url = format!(
        "{}/printer/objects/query?extruder&heater_bed&print_stats&display_status",
        config.moonraker_url
    );

    let response = client.get(&url).send().await?;
    if !response.status().is_success() {
        debug!(status = %response.status(), "Moonraker query failed");
        return Ok(MoonrakerSnapshot::default());
    }

    let payload: Value = response.json().await.unwrap_or(Value::Null);
    let status = payload
        .get("result")
        .and_then(|v| v.get("status"))
        .cloned()
        .unwrap_or(Value::Null);

    let nozzle = status
        .get("extruder")
        .and_then(|v| v.get("temperature"))
        .and_then(|v| v.as_f64());
    let bed = status
        .get("heater_bed")
        .and_then(|v| v.get("temperature"))
        .and_then(|v| v.as_f64());

    let progress_fraction = status
        .get("display_status")
        .and_then(|v| v.get("progress"))
        .and_then(|v| v.as_f64());
    let progress = progress_fraction.map(|p| (p * 100.0).clamp(0.0, 100.0));

    let filename = status
        .get("print_stats")
        .and_then(|v| v.get("filename"))
        .and_then(|v| v.as_str())
        .map(ToString::to_string);
    let print_duration = status
        .get("print_stats")
        .and_then(|v| v.get("print_duration"))
        .and_then(|v| v.as_f64())
        .map(|v| v.max(0.0) as u64);
    let total_duration = status
        .get("print_stats")
        .and_then(|v| v.get("total_duration"))
        .and_then(|v| v.as_f64())
        .map(|v| v.max(0.0) as u64);
    let state = map_job_state(
        status
            .get("print_stats")
            .and_then(|v| v.get("state"))
            .and_then(|v| v.as_str()),
    );

    let eta = match (print_duration, progress) {
        (Some(elapsed), Some(p)) if p > 0.0 && p < 100.0 => {
            let total = (elapsed as f64) * (100.0 / p);
            Some(total.max(0.0) as u64 - elapsed)
        }
        _ => None,
    };

    Ok(MoonrakerSnapshot {
        temperatures: Some(Temperatures {
            nozzle,
            bed,
            chamber: None,
        }),
        job: Some(Job {
            filename,
            progress,
            eta,
            elapsed_time: print_duration,
            state,
            totaltime: total_duration,
        }),
    })
}

async fn send_telemetry(
    client: &Client,
    config: &Config,
) -> Result<Option<u64>, reqwest::Error> {
    let snapshot = fetch_moonraker_snapshot(client, config)
        .await
        .unwrap_or_default();

    let payload = TelemetryPayload {
        printer_id: &config.printer_id,
        token: &config.token,
        timestamp: unix_timestamp_ms(),
        temperatures: snapshot.temperatures,
        job: snapshot.job,
        system_health: None,
        errors: vec![],
        log_tail: vec![],
    };

    let url = format!("{}/api/reach-link/printer-data", config.relay_url);
    let response = client
        .post(&url)
        .bearer_auth(&config.token)
        .json(&payload)
        .send()
        .await?;

    if response.status().is_success() {
        let next_interval = response
            .json::<TelemetryResponse>()
            .await
            .ok()
            .and_then(|payload| payload.next_data_interval);
        debug!(next_data_interval = ?next_interval, "Telemetry sent");
        return Ok(next_interval);
    }

    let status = response.status();
    let body = response.text().await.unwrap_or_default();
    warn!(status = %status, body = %body, "Telemetry endpoint returned non-success");
    Ok(None)
}

async fn heartbeat_loop(
    client: Client,
    config: Arc<Config>,
    mut shutdown_rx: broadcast::Receiver<()>,
) {
    let started_at = std::time::Instant::now();
    let mut next_wait = config.heartbeat_interval_secs;

    loop {
        let uptime_secs = started_at.elapsed().as_secs();
        match register_printer(&client, &config, uptime_secs).await {
            Ok(Some(server_interval)) if server_interval > 0 => {
                next_wait = server_interval;
            }
            Ok(_) => {}
            Err(e) => {
                error!(error = %e, "Failed to register heartbeat");
            }
        }

        tokio::select! {
            _ = shutdown_rx.recv() => {
                break;
            }
            _ = time::sleep(Duration::from_secs(next_wait.max(1))) => {}
        }
    }
}

async fn telemetry_loop(
    client: Client,
    config: Arc<Config>,
    mut shutdown_rx: broadcast::Receiver<()>,
) {
    let mut next_wait = config.telemetry_interval_secs;

    loop {
        match send_telemetry(&client, &config).await {
            Ok(Some(server_interval)) if server_interval > 0 => {
                next_wait = server_interval;
            }
            Ok(_) => {}
            Err(e) => {
                error!(error = %e, "Failed to send telemetry");
            }
        }

        tokio::select! {
            _ = shutdown_rx.recv() => {
                break;
            }
            _ = time::sleep(Duration::from_secs(next_wait.max(1))) => {}
        }
    }
}

// ---------------------------------------------------------------------------
// Health check HTTP server
// ---------------------------------------------------------------------------

async fn health_handler() -> &'static str {
    "OK"
}

async fn run_health_server(addr: SocketAddr, mut shutdown_rx: broadcast::Receiver<()>) {
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
        .with_graceful_shutdown(async move {
            let _ = shutdown_rx.recv().await;
        })
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
        env::remove_var("REACH_PRINTER_ID");
        env::remove_var("REACH_LINK_HEALTH_PORT");
        env::remove_var("REACH_LINK_HEARTBEAT_INTERVAL");
        env::remove_var("REACH_LINK_TELEMETRY_INTERVAL");
        env::remove_var("REACH_LINK_MOONRAKER_URL");
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
    fn test_printer_id_fallback_env() {
        let _lock = ENV_LOCK.lock().unwrap();
        env::set_var("REACH_LINK_RELAY", "https://relay.example.com");
        env::set_var("REACH_LINK_TOKEN", "test-token");
        env::remove_var("REACH_LINK_PRINTER_ID");
        env::set_var("REACH_PRINTER_ID", "printer-fallback");

        let config = Config::from_env();

        clear_env();
        env::remove_var("REACH_PRINTER_ID");

        assert!(config.is_ok());
        assert_eq!(config.unwrap().printer_id, "printer-fallback");
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
        moonraker = %config.moonraker_url,
        "reach-link starting"
    );

    let client = Client::builder()
        .timeout(Duration::from_secs(10))
        .build()
        .expect("Failed to build HTTP client");

    let shared_config = Arc::new(config);
    let (shutdown_tx, _) = broadcast::channel::<()>(4);

    let heartbeat_task = tokio::spawn(heartbeat_loop(
        client.clone(),
        Arc::clone(&shared_config),
        shutdown_tx.subscribe(),
    ));

    let telemetry_task = tokio::spawn(telemetry_loop(
        client.clone(),
        Arc::clone(&shared_config),
        shutdown_tx.subscribe(),
    ));

    let health_task = tokio::spawn(run_health_server(shared_config.health_addr, shutdown_tx.subscribe()));

    // Wait for OS signal, then trigger graceful shutdown.
    shutdown_signal().await;
    let _ = shutdown_tx.send(());

    // Give background tasks a brief window to exit cleanly.
    let _ = tokio::time::timeout(Duration::from_secs(3), heartbeat_task).await;
    let _ = tokio::time::timeout(Duration::from_secs(3), telemetry_task).await;
    let _ = tokio::time::timeout(Duration::from_secs(3), health_task).await;

    let health_addr = shared_config.health_addr;
    info!(addr = %health_addr, "reach-link stopped");
}
