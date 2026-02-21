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
- [`cross`](https://github.com/cross-rs/cross) for cross-compilation (arm64, x86_64, MIPS)
- Docker (required by `cross`)

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

# Cross-compile for arm64, x86_64, and MIPS (requires `cross` + Docker)
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

1. Cross-compile binaries for `linux-arm64`, `linux-x86_64`, `linux-mips`, and `linux-mipsel`
2. Compute SHA-256 checksums
3. Create a GitHub Release with all binaries and checksums attached
4. Include usage instructions in the release body

**Supported Architectures:**
- `linux-arm64` - ARM 64-bit (Raspberry Pi 3/4/5, most modern SBCs)
- `linux-x86_64` - x86 64-bit (Intel/AMD systems)
- `linux-mips` - MIPS 32-bit big-endian (some embedded systems)
- `linux-mipsel` - MIPS 32-bit little-endian (Creality K1/K1C and similar)

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
│   └── main.rs                  # Core binary (async Rust / tokio)
├── build/
│   ├── cross-build.sh           # Local cross-compilation helper
│   └── artifacts/               # Cross-compiled binaries (git-ignored)
├── .github/
│   └── workflows/
│       └── release.yml          # CI/CD: build + publish GitHub Release
├── Cargo.toml                   # Rust project manifest
├── Makefile                     # build / test / clean / cross targets
├── .gitignore
└── README.md
```
