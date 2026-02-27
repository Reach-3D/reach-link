# Reach-Link Helper Bridge

A lightweight Windows utility that enables Reach3D printer setup from Vercel-hosted web origins.

**Problem**: Vercel (serverless hosting) cannot access user LAN private IPs. Reach-Link needs both LAN-based first-time setup (SSH) and cloud-based remote control.

**Solution**: Local helper bridge runs on user's Windows machine, handles LAN discovery and SSH installation, communicates results back to browser wizard via localhost HTTP.

## Architecture

```
Browser (Reach3DJanus)
    ↓
HelperClient (http://localhost:5900)
    ↓
Helper Bridge (this process)
    ├── /health → status check
    └── /execute → step execution
        ├── discover → mDNS + direct probe
        ├── validate_moonraker → HTTP check
        └── install_reach_link → SSH execution
```

## Build

**Requirements:**
- Python 3.7+
- pip

**Steps:**

```bash
cd reach-link/helper
python build.py
```

This will:
1. Install dependencies (paramiko)
2. Install PyInstaller
3. Build Windows executable (~15-20MB)
4. Output: `dist/reach-link-helper.exe`

## Usage

**Option 1: Manual start**
```bash
reach-link-helper.exe
# Output: Listening on http://127.0.0.1:5900 (or next available port 5900-5920)
```

**Option 2: Auto-start via registry (optional)**
- Add `reach-link-helper.exe` to Windows startup folder
- Or create scheduled task

## How It Works

### Discovery (LAN scan)
1. Probes common subnets (192.168.*, 10.*)
2. Tests known mDNS hostnames (mainsail.local, fluidd.local)
3. Returns list of Moonraker instances found

### Validation (HTTP probe)
1. Connects to Moonraker HTTP API
2. Validates version and state
3. Returns connection details

### Installation (SSH)
1. Accepts SSH credentials (password or private key)
2. Generates installation script with env vars
3. Uploads script to printer
4. Executes with optional sudo
5. Streams logs back to browser in real-time
6. Cleans up after completion

## Protocol

### Request
```json
POST /execute
{
  "sessionId": "session-123",
  "userId": "user-456",
  "step": "discover",
  "payload": {},
  "nonce": "random-hex",
  "timestamp": 1709028123456,
  "signature": "hmac-sha256-hex"
}
```

### Response
```json
{
  "success": true,
  "data": {
    "discovered": [
      { "hostname": "mainsail.local", "host": "192.168.1.100", "port": 7125, "ssl": false }
    ]
  },
  "logs": [
    { "timestamp": "2024-02-25T10:00:00Z", "level": "info", "source": "discover", "message": "Found Moonraker at 192.168.1.100:7125" }
  ]
}
```

## Files

- `main.py` - HTTP server and request handler
- `ssh_executor.py` - SSH operations and reach-link installation
- `lan_discovery.py` - LAN scanning and Moonraker validation
- `requirements.txt` - Python dependencies
- `reach-link-helper.spec` - PyInstaller configuration
- `build.py` - Build script

## Logging

Logs are printed to console and sent back in HTTP responses with structured format:
```
[timestamp] [level] [source] message
```

- **level**: info, debug, warn, error
- **source**: discover, validate, install, system

## Security Notes

- Runs only on localhost (127.0.0.1)
- Request signature validation (HMAC-SHA256)
- Nonce freshness check (±5 seconds)
- SSH credentials not persisted (only in request)
- Credentials filtered from logs

## Troubleshooting

**Helper not detected by browser**
- Ensure helper is running: `reach-link-helper.exe`
- Check Windows Firewall allows localhost
- Try ports 5900-5920 are available: `netstat -ano | findstr :590`

**SSH connection fails**
- Verify printer IP/hostname is correct
- Check SSH username (usually 'pi' for Raspberry Pi)
- Ensure password or private key is correct
- Try direct SSH first: `ssh pi@printer-ip` to verify

**Moonraker not found during discovery**
- Ensure printer is on same network as Windows machine
- Verify mDNS works: `ping mainsail.local`
- Try direct IP instead of hostname

**Installation hangs**
- Check SSH connection is stable
- Increase timeout in code if printers are slow
- Check printer has sufficient disk space

## Development

Run without building:
```bash
cd reach-link/helper
pip install -r requirements.txt
python main.py
```

## Future Enhancements

- [ ] mDNS discovery (currently manual/broadcast only)
- [ ] TLS certificate generation for Moonraker
- [ ] Auto-updater for helper binary
- [ ] Supervisor-based service management (alternative to systemd)
- [ ] macOS and Linux support (separate executables)
- [ ] GUI for manual monitoring and debugging

## License

Same as Reach-Link project (see root LICENSE)
