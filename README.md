# reach-link

**reach-link** is the cross-platform printer agent for [Reach 3D](https://reach3d.com/) that runs on Klipper/Moonraker-based 3D printers. It connects your printer to the Reach relay server, registers it, and sends periodic heartbeats so Reach 3D can monitor and control your printer remotely.

---

## Table of Contents

- [Features](#features)
- [Requirements](#requirements)
- [Configuration](#configuration)
- [Building locally](#building-locally)
- [Release process](#release-process)
- [Installation on a printer](#installation-on-a-printer)
- [Supported architectures](#supported-architectures)
- [MIPS build instructions](#mips-build-instructions)
- [Security notes](#security-notes)
- [Repository structure](#repository-structure)

---

## Features

- Connects to a configurable relay server over **HTTPS**
- Registers the printer and sends periodic heartbeats
- Exposes a `/health` endpoint for local monitoring
- Graceful shutdown on `SIGTERM` / `Ctrl+C`
- Structured logging to stdout and/or a log file
- Zero hardcoded secrets — all configuration via environment variables
- Single static binary (musl), no runtime dependencies

---

## Requirements

- Rust stable (≥ 1.75) — install via [rustup](https://rustup.rs/)
- [`cross`](https://github.com/cross-rs/cross) for cross-compilation (ARM64, x86_64)
- Docker (required by `cross`)

> **MIPS Note:** Creality K1/K1C (MIPS) requires native compilation on-device. See [MIPS Build Instructions](#mips-build-instructions) below.

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

---

## Building locally

```bash
# Build a release binary for the host architecture
make build

# Run tests
make test

# Cross-compile for ARM64 and x86_64 (requires `cross` + Docker)
make cross

# Remove build artifacts
make clean
```

The host release binary is written to `target/release/reach-link`.  
Cross-compiled artifacts are written to `build/artifacts/`.

---

## Release process

Releases are built and published automatically by GitHub Actions when a version tag is pushed.

```bash
# Tag and push to trigger the release workflow
git tag v1.0.0
git push origin v1.0.0
```

The workflow (`.github/workflows/release.yml`) will:

1. Cross-compile binaries for `linux-arm64` and `linux-x86_64`
2. Compute SHA-256 checksums
3. Create a GitHub Release with all binaries and checksums attached
4. Include usage instructions in the release body

**Supported Architectures:**
- `linux-arm64` - ARM 64-bit (Raspberry Pi 3/4/5, most modern SBCs)
- `linux-x86_64` - x86 64-bit (Intel/AMD systems)

**MIPS Support:** Creality K1/K1C and other MIPS printers require native compilation (see below).

---

## Installation on a printer

```bash
# Download the arm64 binary (replace <version> with the release tag, e.g. v1.0.0)
curl -fsSL https://github.com/Reach-3D/reach-link/releases/download/<version>/reach-link-linux-arm64 \
  -o /usr/local/bin/reach-link

# Verify checksum (replace <hash> with the SHA-256 from the release page)
echo "<hash>  /usr/local/bin/reach-link" | sha256sum -c

# Make executable
chmod +x /usr/local/bin/reach-link

# Run (export your env vars first)
export REACH_LINK_RELAY=https://relay.reach3d.com
export REACH_LINK_TOKEN=your-secret-token
export REACH_LINK_PRINTER_ID=printer-abc123
/usr/local/bin/reach-link
```

### Systemd service (recommended)

Create `/etc/systemd/system/reach-link.service`:

```ini
[Unit]
Description=Reach Link Printer Agent
After=network.target

[Service]
EnvironmentFile=/etc/reach-link/env
ExecStart=/usr/local/bin/reach-link
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Create `/etc/reach-link/env`:

```env
REACH_LINK_RELAY=https://relay.reach3d.com
REACH_LINK_TOKEN=your-secret-token
REACH_LINK_PRINTER_ID=printer-abc123
```

```bash
systemctl enable --now reach-link
```

---

## MIPS Build Instructions

**For Creality K1/K1C and other MIPS printers:**

As of v1.0.4, MIPS support is provided via a **Python-based agent** (`reach-link-mips.py`) published in each release. This avoids the complexity of cross-compilation for Tier-3 Rust targets.

### Installation via Reach3DCommercial

The Reach 3D installer automatically detects your printer's architecture and deploys the correct agent (Rust binary for ARM64/x86_64, Python script for MIPS).

### Manual Installation on K1C

1. **Download the Python script:**
   ```bash
   wget https://github.com/Reach-3D/reach-link/releases/download/v1.0.4/reach-link-mips.py
   chmod +x reach-link-mips.py
   ```

2. **Verify the SHA-256 checksum:**
   ```bash
   echo "<hash from release page>  reach-link-mips.py" | sha256sum -c
   ```

3. **Install dependencies (if not present):**
   The Python agent requires Python 3.7+ and the `requests` library.
   ```bash
   # Check Python version
   python3 --version
   
   # Install requests (if pip is available)
   pip3 install requests==2.31.0 tenacity==8.2.3
   ```

4. **Create a `.env` file with configuration:**
   ```bash
   mkdir -p /root/reach-link
   cat > /root/reach-link/.env << EOF
   REACH_LINK_RELAY=https://relay.reach3d.com
   REACH_LINK_TOKEN=your-secret-token
   REACH_LINK_PRINTER_ID=printer-abc123
   REACH_LINK_MOONRAKER_URL=http://127.0.0.1:7125
   REACH_LINK_HEARTBEAT_INTERVAL=30
   REACH_LINK_TELEMETRY_INTERVAL=10
   EOF
   ```

5. **Run the script:**
   ```bash
   python3 /root/reach-link/reach-link-mips.py
   ```

6. **Set up as a service (supervisor/systemd):**
   Create a supervisor config or systemd unit to auto-start the Python agent. See [supervisor.conf example](#supervisor-config-example) below.

### Supervisor Config Example

If your K1C uses supervisor (typical for Creality OS):

```ini
[program:reach-link]
command=/usr/bin/python3 /root/reach-link/reach-link-mips.py
autostart=true
autorestart=true
stopasgroup=true
```

Place in `/usr/data/printer_data/config/supervisor/conf.d/reach-link.conf` and run:
```bash
supervisorctl reread
supervisorctl update
supervisorctl start reach-link
```

### Python Agent Features

- **Same protocol as Rust binary:** heartbeats and telemetry payloads are identical
- **Pure Python stdlib fallback:** uses `urllib` if `requests` is not available (no external dependencies required)
- **Graceful restart:** SIGTERM handling for clean shutdown
- **Moonraker API queries:** reads temperatures, job state, and system health from Moonraker's introspection API
- **Configurable intervals:** heartbeat (default 30s) and telemetry (default 10s)

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
