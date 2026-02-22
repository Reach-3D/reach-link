# reach-link

**reach-link** is the universal Python agent for [Reach 3D](https://reach3d.com/) that runs on Klipper/Moonraker-based 3D printers. It connects your printer to the Reach relay server, registers it, and sends periodic heartbeats so Reach 3D can monitor and control your printer remotely.

Works on **all platforms**: ARM64 (Raspberry Pi, etc.), x86_64 (Intel/AMD), MIPS (Creality K1/K1C), and more.

---

## Table of Contents

- [Features](#features)
- [Requirements](#requirements)
- [Configuration](#configuration)
- [Installation on a printer](#installation-on-a-printer)
- [Using Reach3DCommercial installer](#using-reach3dcommercial-installer)
- [Systemd service](#systemd-service-recommended)
- [Supervisor configuration](#supervisor-configuration)
- [Troubleshooting](#troubleshooting)
- [Development](#development)
- [Security notes](#security-notes)
- [Repository structure](#repository-structure)

---

## Features

- 🌍 **Universal:** Single Python script works on ARM64, x86_64, MIPS, and other Linux architectures
- 🔗 **Connects to relay server over HTTPS**
- 💓 **Registers and sends periodic heartbeats** to keep printer online
- 📊 **Sends telemetry:** temperatures, job progress, system health
- 📝 **Graceful shutdown** on `SIGTERM` / `Ctrl+C`
- 📋 **Structured logging** to stdout and/or log file
- 🔐 **Zero hardcoded secrets** — all configuration via environment variables
- ⚡ **Minimal dependencies** (requests + tenacity optional, falls back to stdlib)

---

## Requirements

- **Python 3.7+** (pre-installed on most modern printers)
- **Moonraker API** running on the printer (typically `http://127.0.0.1:7125`)
- ✅ Optional: `requests` library (if not available, script uses stdlib `urllib`)

---

## Configuration

All configuration is through environment variables:

| Variable                    | Required | Description                                           |
|-----------------------------|----------|-------------------------------------------------------|
| `REACH_LINK_RELAY`          | ✅        | HTTPS URL of the Reach relay server                   |
| `REACH_LINK_TOKEN`          | ✅        | Bearer token for authenticating with the relay        |
| `REACH_LINK_PRINTER_ID`     | ✅        | Unique identifier for this printer                    |
| `REACH_LINK_HEALTH_PORT`    | ❌        | Port for the `/health` endpoint (default: `8080`)     |
| `REACH_LINK_HEARTBEAT_INTERVAL` | ❌   | Heartbeat interval in seconds (default: `30`)         |
| `REACH_LINK_LOG_FILE`       | ❌        | Path to a log file (logs to stdout if unset)          |
| `RUST_LOG`                  | ❌        | Log filter level (default: `info`; e.g. `debug`, `reach_link=trace`) |

**Example:**

```env
REACH_LINK_RELAY=https://relay.reach3d.com
REACH_LINK_TOKEN=your-secret-token
REACH_LINK_PRINTER_ID=printer-abc123
REACH_LINK_HEALTH_PORT=8080
REACH_LINK_LOG_FILE=/var/log/reach-link.log
```

> ⚠️ Never commit secrets to source control. Use a `.env` file (already in `.gitignore`) or your system's secret manager.


## Building locally

```bash
# No build required! The Python script runs directly.
# To test locally, ensure Python 3.7+ and Moonraker are running:
python3 src/reach-link-agent.py

# Or set custom Moonraker URL:
REACH_LINK_MOONRAKER_URL=http://192.168.1.100:7125 python3 src/reach-link-agent.py
```

---

## Release process

Releases are published automatically by GitHub Actions when a version tag is pushed.

```bash
# Tag and push to trigger the release workflow
git tag v1.0.5
git push origin v1.0.5
```

The workflow (`.github/workflows/release.yml`) will:

1. Copy `src/reach-link-agent.py` → `reach-link.py`
2. Generate SHA-256 checksum
3. Create a GitHub Release with the Python script and checksum as assets
4. Include comprehensive usage instructions in the release body

**Result:** A single universal `reach-link.py` script ready for all platforms.


## Installation on a printer

### Quick start (manual)

```bash
# 1. Download the Python script (replace v1.0.5 with latest release)
curl -fsSL https://github.com/Reach-3D/reach-link/releases/download/v1.0.5/reach-link.py \
  -o /root/reach-link.py

# 2. Verify checksum (replace <hash> with SHA-256 from release page)
echo "<hash>  /root/reach-link.py" | sha256sum -c

# 3. Make executable
chmod +x /root/reach-link.py

# 4. Run (export your env vars first)
export REACH_LINK_RELAY=https://relay.reach3d.com
export REACH_LINK_TOKEN=your-secret-token
export REACH_LINK_PRINTER_ID=printer-abc123
python3 /root/reach-link.py
```

### Using Reach3DCommercial installer

The Reach 3D web app includes an automatic installer that:
- Detects your printer's architecture
- Downloads the latest reach-link.py
- Verifies checksums
- Deploys to your printer via SSH
- Auto-starts as a service (systemd/supervisor/cron)

**No manual downloading or setup needed!**

---

## Systemd service (recommended)

For most modern Linux distributions (Debian, Ubuntu, Fedora, etc.):

**1. Create `/etc/systemd/system/reach-link.service`:**

```ini
[Unit]
Description=Reach Link Printer Agent
After=network.target

[Service]
Type=simple
EnvironmentFile=/etc/reach-link/env
ExecStart=/usr/bin/python3 /root/reach-link.py
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

**2. Create `/etc/reach-link/env`:**

```env
REACH_LINK_RELAY=https://relay.reach3d.com
REACH_LINK_TOKEN=your-secret-token
REACH_LINK_PRINTER_ID=printer-abc123
REACH_LINK_MOONRAKER_URL=http://127.0.0.1:7125
REACH_LINK_HEARTBEAT_INTERVAL=30
REACH_LINK_TELEMETRY_INTERVAL=10
```

**3. Enable and start:**

```bash
systemctl daemon-reload
systemctl enable reach-link
systemctl start reach-link
systemctl status reach-link
```

**4. Check logs:**

```bash
journalctl -u reach-link -f
```

---

## Supervisor configuration

For printers using Supervisor (typical on Creality OS, K1/K1C, etc.):

**Create `/etc/supervisor/conf.d/reach-link.conf` (or equivalent path):**

```ini
[program:reach-link]
command=/usr/bin/python3 /root/reach-link.py
directory=/root
autostart=true
autorestart=true
redirect_stdout=true
redirect_stderr=true
stdout_logfile=/var/log/reach-link.log
stopasgroup=true
killasgroup=true
stopsignal=TERM
priority=999
```

**Apply changes:**

```bash
supervisorctl reread
supervisorctl update
supervisorctl start reach-link
supervisorctl status reach-link
```

---

## Troubleshooting

**"ModuleNotFoundError: No module named 'requests'"**

The script will automatically fall back to Python's built-in `urllib` library, but for better retries and error handling, install `requests`:

```bash
pip3 install requests==2.31.0 tenacity==8.2.3
```

**"Connection refused" to Moonraker (http://127.0.0.1:7125)**

- Verify Moonraker is running: `ps aux | grep moonraker`
- Check Moonraker's actual port: look at `moonraker.conf` or visit `http://<printer-ip>:7125/api/`
- Set `REACH_LINK_MOONRAKER_URL` environment variable if using a different port

**"Python 3 not found"**

Install Python 3:

```bash
# Debian/Ubuntu
apt-get update && apt-get install -y python3

# Alpine
apk add python3

# OpenWrt / similar
opkg install python3
```

**"Authentication failed" or "Invalid token"**

- Verify `REACH_LINK_TOKEN` is correct and not truncated
- Confirm `REACH_LINK_RELAY` URL is correct (e.g., `https://relay.reach-3d.com`)
- Check printer ID matches the one registered in Reach 3D

**"[Errno -2] Name or service not known"**

- Network connectivity issue; verify printer can reach the relay server:
  ```bash
  ping relay.reach-3d.com
  curl -I https://relay.reach-3d.com
  ```

---

## Development

To contribute or test the Python agent locally:

```bash
# Clone repo and install test dependencies
git clone https://github.com/Reach-3D/reach-link.git
cd reach-link
pip3 install requests==2.31.0 tenacity==8.2.3

# Test with a local Moonraker instance
export REACH_LINK_RELAY=https://relay.reach3d.com
export REACH_LINK_TOKEN=test-token
export REACH_LINK_PRINTER_ID=test-printer
export REACH_LINK_MOONRAKER_URL=http://localhost:7125
python3 src/reach-link-agent.py
```

### Troubleshooting

**"ModuleNotFoundError: No module named 'requests'"**
- The `requests` library is not installed. Try: `pip3 install requests` or use the pure-stdlib fallback.

**"Connection refused" when querying Moonraker**
- Verify Moonraker is running on the expected port (default: `http://127.0.0.1:7125`)
- Check `REACH_LINK_MOONRAKER_URL` environment variable

**Python 3 not found**
- K1C ships with Python 3 by default, but if missing, contact Creality support or see your distro's package manager (e.g., `opkg install python3`)

---

## Supported Architectures

| Architecture | Agent Type | Download |
|--------------|-----------|----------|
| ARM64 (Raspberry Pi 3/4/5, modern SBCs) | Rust binary | `reach-link-linux-arm64` |
| x86_64 (Intel/AMD systems) | Rust binary | `reach-link-linux-x86_64` |
| MIPS (Creality K1/K1C) | Python script | `reach-link-mips.py` |

All agents use the same protocol and configuration, ensuring consistent behavior across platforms.

---

## Security notes

- **No hardcoded secrets** — all sensitive values are read from environment variables at runtime.
- **HTTPS enforced** — the relay URL must start with `https://`; plain HTTP is rejected at startup.
- **Input validation** — all required env vars are validated at startup; the binary exits immediately on invalid configuration.
- **Static musl binary** — no shared library dependencies, minimising the attack surface.
- **Future hardening (optional):** TLS certificate pinning can be added to the `reqwest` client to pin the relay server's certificate, preventing MITM even with a compromised CA.

---

## Repository structure

```
reach-link/
├── src/
│   ├── main.rs                  # Core Rust agent (async / tokio)
│   └── reach-link-agent.py      # MIPS Python agent
├── build/
│   ├── cross-build.sh           # Local cross-compilation helper
│   └── artifacts/               # Cross-compiled binaries (git-ignored)
├── .github/
│   └── workflows/
│       └── release.yml          # CI/CD: build + publish GitHub Release (binaries + Python script)
├── Cargo.toml                   # Rust project manifest
├── Makefile                     # build / test / clean / cross targets
├── .gitignore
└── README.md
```
