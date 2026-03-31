#!/usr/bin/env python3
"""
reach-link Universal Agent
Cross-platform printer agent for Klipper/Moonraker 3D printers (Python version for all platforms).
Features: Heartbeat registration, telemetry collection, secure command polling via relay and RTDB.
Supports all architectures: MIPS, ARM64, x86_64, and others.
"""

import asyncio
import json
import logging
import os
import signal
import sys
import time
from typing import Any, Dict, Optional
from urllib.error import URLError, HTTPError
from urllib.parse import urljoin
from urllib.request import Request, urlopen
import ipaddress
import socket

# Import Firebase RTDB client
try:
    from firebase_rtdb_client import FirebaseRealtimeDatabaseClient
except ImportError:
    FirebaseRealtimeDatabaseClient = None  # Will be handled gracefully below

# Setup logging
def setup_logging(log_file: Optional[str] = None) -> None:
    """Configure logging to stdout and optional file."""
    log_level = logging.INFO
    log_format = "%(asctime)s [%(levelname)s] %(message)s"
    
    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(log_level)
    console_handler.setFormatter(logging.Formatter(log_format))
    
    logger = logging.getLogger()
    logger.setLevel(log_level)
    logger.addHandler(console_handler)
    
    # File handler (optional)
    if log_file:
        try:
            file_handler = logging.FileHandler(log_file)
            file_handler.setLevel(log_level)
            file_handler.setFormatter(logging.Formatter(log_format))
            logger.addHandler(file_handler)
        except Exception as e:
            print(f"Warning: Could not open log file {log_file}: {e}", file=sys.stderr)

logger = logging.getLogger(__name__)
AGENT_VERSION = "1.0.10"

# ============================================================================
# Configuration
# ============================================================================

class Config:
    """Load and validate configuration from environment."""
    
    def __init__(self):
        self.relay_url = self._require_env("REACH_LINK_RELAY")
        self.token = os.environ.get("REACH_LINK_TOKEN", "").strip()
        self.pairing_code = os.environ.get("REACH_LINK_PAIRING_CODE", "").strip()
        self.state_file = os.environ.get("REACH_LINK_STATE_FILE", "./.reach-link-state.json").strip()
        self.printer_id = os.environ.get("REACH_LINK_PRINTER_ID", "").strip() or os.environ.get("REACH_PRINTER_ID", "").strip()
        self.user_id = os.environ.get("REACH_LINK_USER_ID", "")
        self.printer_ip = os.environ.get("REACH_LINK_PRINTER_IP", "")
        self.moonraker_url = os.environ.get(
            "REACH_LINK_MOONRAKER_URL", "http://127.0.0.1:7125"
        ).rstrip("/")
        self.heartbeat_interval = int(
            os.environ.get("REACH_LINK_HEARTBEAT_INTERVAL", "30")
        )
        self.telemetry_interval = int(
            os.environ.get("REACH_LINK_TELEMETRY_INTERVAL", "10")
        )
        self.command_poll_interval = int(
            os.environ.get("REACH_LINK_COMMAND_POLL_INTERVAL", "25")
        )
        self.log_file = os.environ.get("REACH_LINK_LOG_FILE")
        
        # Firebase RTDB configuration (optional, for cloud command queue)
        self.firebase_database_url = os.environ.get("REACH_LINK_FIREBASE_DATABASE_URL", "")
        self.firebase_token = os.environ.get("REACH_LINK_FIREBASE_TOKEN", "")

        # Webcam snapshot configuration
        self.webcam_snapshot_interval = int(
            os.environ.get("REACH_LINK_WEBCAM_INTERVAL", "5")
        )
        self.webcam_viewer_timeout = int(
            os.environ.get("REACH_LINK_WEBCAM_VIEWER_TIMEOUT", "60")
        )

        self._load_persisted_state()
        
        # Validate
        if not self.relay_url.startswith("https://") and not self.relay_url.startswith("http://"):
            raise ValueError(f"REACH_LINK_RELAY must use HTTPS or HTTP, got: {self.relay_url}")
        if not self.token and not self.pairing_code:
            raise ValueError(
                "Bootstrap error: Neither REACH_LINK_TOKEN nor REACH_LINK_PAIRING_CODE is set.\n"
                "First setup: Run the wizard in Reach3D dashboard to create a pairing session, "
                "then run the setup command it provides.\n"
                "Existing setup: Set REACH_LINK_TOKEN to the token saved during first setup."
            )
        if self.token and not self.printer_id:
            raise ValueError("REACH_LINK_PRINTER_ID must not be empty when REACH_LINK_TOKEN is used")

    def _load_persisted_state(self):
        """Load persisted bootstrap credentials from disk if available."""
        if not self.state_file:
            return

        try:
            if not os.path.exists(self.state_file):
                return

            with open(self.state_file, "r", encoding="utf-8") as state_fp:
                data = json.load(state_fp)

            if not self.token:
                self.token = str(data.get("reachLinkToken", "") or data.get("token", "")).strip()
            if not self.printer_id:
                self.printer_id = str(data.get("printerId", "") or data.get("reachLinkPrinterId", "")).strip()
            if not self.user_id:
                self.user_id = str(data.get("userId", "") or data.get("reachLinkUserId", "")).strip()
            if data.get("relayUrl"):
                self.relay_url = str(data.get("relayUrl")).strip().rstrip("/")

            logger.info(f"Loaded persisted agent state from {self.state_file}")
        except Exception as error:
            logger.warning(f"Failed to load persisted state file {self.state_file}: {error}")

    def persist_state(self):
        """Persist active credentials to disk for restart/reboot resilience."""
        if not self.state_file:
            return

        payload = {
            "reachLinkToken": self.token,
            "printerId": self.printer_id,
            "userId": self.user_id,
            "relayUrl": self.relay_url,
            "savedAt": int(time.time()),
        }

        try:
            parent_dir = os.path.dirname(self.state_file)
            if parent_dir:
                os.makedirs(parent_dir, exist_ok=True)

            with open(self.state_file, "w", encoding="utf-8") as state_fp:
                json.dump(payload, state_fp)
            logger.info(f"Persisted agent state to {self.state_file}")
        except Exception as error:
            logger.warning(f"Failed to persist agent state to {self.state_file}: {error}")
    
    @staticmethod
    def _require_env(name: str) -> str:
        """Get required environment variable."""
        value = os.environ.get(name)
        if not value:
            raise ValueError(f"Required environment variable {name} is not set")
        return value
    
    @staticmethod
    def _require_env_with_fallback(primary: str, fallback: str) -> str:
        """Get environment variable with fallback."""
        value = os.environ.get(primary)
        if value:
            return value
        value = os.environ.get(fallback)
        if value:
            return value
        raise ValueError(
            f"Required environment variable {primary} is not set "
            f"(fallback {fallback} also missing)"
        )

# ============================================================================
# Subnet Detection (for local vs remote routing)
# ============================================================================

class SubnetDetector:
    """Detect if a user is on the same local network as the printer."""
    
    def __init__(self, printer_ip: str):
        self.printer_ip = printer_ip
    
    def is_same_subnet(self, user_ip: str, subnet_mask: int = 24) -> bool:
        """
        Check if user_ip and printer_ip are on the same subnet.
        Assumes /24 subnet (255.255.255.0) by default.
        """
        try:
            printer_addr = ipaddress.ip_address(self.printer_ip)
            user_addr = ipaddress.ip_address(user_ip)
            
            # Create /24 networks
            printer_net = ipaddress.ip_network(f"{self.printer_ip}/24", strict=False)
            user_net = ipaddress.ip_network(f"{user_ip}/24", strict=False)
            
            return printer_net == user_net
        except ValueError:
            # Invalid IP format, assume remote
            return False
    
    def get_local_ip(self) -> Optional[str]:
        """Get this machine's local IP (heuristic)."""
        try:
            # Connect to external host (doesn't actually send data)
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
            s.close()
            return local_ip
        except Exception:
            return None

# ============================================================================
# HTTP Client (stdlib-only, no external dependencies)
# ============================================================================

class HTTPClient:
    """Simple HTTP client using urllib."""
    
    @staticmethod
    def post_json(
        url: str,
        data: Dict[str, Any],
        token: Optional[str] = None,
        timeout: int = 10,
        max_retries: int = 3,
    ) -> Optional[Dict[str, Any]]:
        """POST JSON data with Bearer token auth; retry on failure."""
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        body = json.dumps(data).encode("utf-8")
        
        last_error = None
        for attempt in range(max_retries):
            try:
                req = Request(url, data=body, headers=headers, method="POST")
                with urlopen(req, timeout=timeout) as response:
                    response_body = response.read().decode("utf-8")
                    if response_body:
                        return json.loads(response_body)
                    return None
            except HTTPError as e:
                # 401 = token revoked; 403 = invalid token; 404 = not found.
                # None of these will succeed on retry — break immediately.
                if e.code == 401:
                    logger.error(f"Token revocation detected (HTTP 401): {e.reason}")
                    raise ValueError("TOKEN_REVOKED")
                if e.code in (403, 404):
                    logger.warning(f"HTTP POST received {e.code} (no retry): {e.reason}")
                    last_error = e
                    break

                last_error = e
                if attempt < max_retries - 1:
                    wait = 2 ** attempt
                    logger.debug(
                        f"HTTP POST failed with status {e.code} (attempt {attempt + 1}/{max_retries}); "
                        f"retrying in {wait}s"
                    )
                    time.sleep(wait)
            except (URLError, OSError) as e:
                last_error = e
                if attempt < max_retries - 1:
                    wait = 2 ** attempt
                    logger.debug(
                        f"HTTP POST failed (attempt {attempt + 1}/{max_retries}): {e}; "
                        f"retrying in {wait}s"
                    )
                    time.sleep(wait)
            except Exception as e:
                logger.error(f"Unexpected error in HTTP POST: {e}")
                return None
        
        logger.debug(f"HTTP POST failed after {max_retries} attempts: {last_error}")
        return None
    
    @staticmethod
    def get_json(
        url: str,
        timeout: int = 10,
        max_retries: int = 3,
    ) -> Optional[Dict[str, Any]]:
        """GET JSON data; retry on failure."""
        last_error = None
        for attempt in range(max_retries):
            try:
                with urlopen(url, timeout=timeout) as response:
                    response_body = response.read().decode("utf-8")
                    return json.loads(response_body)
            except (URLError, OSError) as e:
                last_error = e
                if attempt < max_retries - 1:
                    wait = 2 ** attempt
                    logger.debug(
                        f"HTTP GET failed (attempt {attempt + 1}/{max_retries}): {e}; "
                        f"retrying in {wait}s"
                    )
                    time.sleep(wait)
            except Exception as e:
                logger.error(f"Unexpected error in HTTP GET: {e}")
                return None
        
        logger.debug(f"HTTP GET failed after {max_retries} attempts: {last_error}")
        return None

# ============================================================================
# Moonraker Client
# ============================================================================

class MoonrakerClient:
    """Queries Moonraker API for printer state."""
    
    def __init__(self, url: str):
        self.url = url.rstrip("/")
    
    def get_status(self) -> Optional[Dict[str, Any]]:
        """
        Query Moonraker for temperatures, job, system health, fans, and motion.
        Provides rich telemetry for the RTDB live dashboard.
        """
        try:
            # Query printer objects: temperatures (nozzle, bed), job state, cpu/memory,
            # fan speed, gcode move (feed rate / flow rate factors), toolhead position.
            query_url = (
                f"{self.url}/printer/objects/query?"
                "extruder=temperature,target&"
                "heater_bed=temperature,target&"
                "print_stats=filename,total_duration,print_duration,filament_used,state&"
                "display_status=message&"
                "system_stats=cputime,memavail,cpu_percent,memory&"
                "fan=speed&"
                "gcode_move=speed,speed_factor,extrude_factor&"
                "toolhead=position&"
                "virtual_sdcard=progress,is_active,file_position"
            )
            
            response = HTTPClient.get_json(query_url, timeout=5)
            if not response or "result" not in response:
                logger.warning("Moonraker query returned invalid response")
                return None
            
            result = response.get("result", {})
            status = result.get("status", {})

            extruder = status.get("extruder", {})
            heater_bed = status.get("heater_bed", {})
            
            # Extract temperatures — include setpoint targets
            temperatures = {
                "nozzle": extruder.get("temperature"),
                "nozzleTarget": extruder.get("target"),
                "bed": heater_bed.get("temperature"),
                "bedTarget": heater_bed.get("target"),
                "chamber": None,  # K1C doesn't typically have a chamber sensor
            }

            # Extract fan speed (part cooling fan, 0.0–1.0)
            fan = status.get("fan", {})
            fans = {
                "partCooling": fan.get("speed"),
            }

            # Extract motion/positioning data
            gcode_move = status.get("gcode_move", {})
            toolhead = status.get("toolhead", {})
            position = toolhead.get("position", [None, None, None, None])
            motion = {
                "x": position[0] if len(position) > 0 else None,
                "y": position[1] if len(position) > 1 else None,
                "z": position[2] if len(position) > 2 else None,
                "speed": gcode_move.get("speed"),
                "speedFactor": gcode_move.get("speed_factor"),
                "extrudeFactor": gcode_move.get("extrude_factor"),
            }

            # Extract job info
            print_stats = status.get("print_stats", {})
            virtual_sdcard = status.get("virtual_sdcard", {})
            job_state = print_stats.get("state", "unknown")
            
            # Map Moonraker states to our enum
            state_map = {
                "standby": "idle",
                "printing": "printing",
                "paused": "paused",
                "error": "error",
            }
            job_state = state_map.get(job_state, "unknown")
            
            total_duration = print_stats.get("total_duration", 0)
            print_duration = print_stats.get("print_duration", 0)
            # Use Klipper's file-read-based progress (0.0–1.0) — more accurate
            # than the wall-clock ratio (print_duration / total_duration).
            sdcard_progress = virtual_sdcard.get("progress", 0.0) or 0.0
            progress = sdcard_progress * 100.0
            
            # Estimate remaining time from progress fraction and elapsed print time.
            estimated_time = None
            if sdcard_progress > 0.01 and print_duration > 0:
                total_estimated = print_duration / sdcard_progress
                estimated_time = int(max(0, total_estimated - print_duration))
            
            filament_used = print_stats.get("filament_used")
            
            job = {
                "filename": print_stats.get("filename"),
                "progress": min(progress, 100.0),
                "eta": estimated_time,
                "elapsedTime": int(print_duration),
                "state": job_state,
                "totaltime": int(total_duration),
                "filamentUsed": filament_used,
                "estimatedTime": estimated_time,
            }
            
            # Extract system health
            sys_stats = status.get("system_stats", {})
            system_health = {
                "cpuPercent": sys_stats.get("cpu_percent"),
                "memoryPercent": None,  # Would need total_memory to calculate
                "diskPercent": None,  # Moonraker doesn't expose disk usage via this endpoint
            }
            
            return {
                "temperatures": temperatures,
                "fans": fans,
                "motion": motion,
                "job": job,
                "system_health": system_health,
            }
        
        except Exception as e:
            logger.error(f"Error querying Moonraker: {e}")
            return None

    def get_webcam_snapshot(self) -> Optional[bytes]:
        """
        Fetch a JPEG snapshot from the local webcam.
        Crowsnest / mjpg-streamer serves snapshots at /webcam/?action=snapshot
        proxied through Moonraker.
        """
        try:
            snapshot_url = f"{self.url}/webcam/?action=snapshot"
            req = Request(snapshot_url, method="GET")
            with urlopen(req, timeout=10) as response:
                content_type = response.headers.get("Content-Type", "")
                if "image" not in content_type and "octet" not in content_type:
                    logger.debug(f"Webcam snapshot unexpected content type: {content_type}")
                    return None
                data = response.read()
                if len(data) < 100:
                    logger.debug(f"Webcam snapshot too small ({len(data)} bytes)")
                    return None
                return data
        except Exception as e:
            logger.debug(f"Failed to capture webcam snapshot: {e}")
            return None

# ============================================================================
# Reach3D Relay Client
# ============================================================================

class RelayClient:
    """Posts heartbeats and telemetry to Reach3D relay server."""
    
    def __init__(self, relay_url: str, token: str, printer_id: str):
        self.relay_url = relay_url.rstrip("/")
        self.token = token
        self.printer_id = printer_id
    
    def register_heartbeat(self, uptime_secs: int, version: str = "1.0.0") -> Optional[Dict[str, Any]]:
        """
        POST heartbeat to /api/reach-link/register.
        Returns response payload if successful.
        """
        url = urljoin(self.relay_url, "/api/reach-link/register")
        payload = {
            "printerId": self.printer_id,
            "token": self.token,
            "timestamp": int(time.time() * 1000),
            "uptime": uptime_secs,
            "version": version,
        }
        
        response = HTTPClient.post_json(url, payload, self.token, timeout=10)
        if response:
            logger.info(f"Heartbeat registered; next check-in: {response.get('nextCheckIn', '?')}s")
            return response
        return None
    
    def send_telemetry(self, moonraker_status: Dict[str, Any]) -> bool:
        """
        POST telemetry to /api/reach-link/printer-data.
        Returns True if successful.
        """
        url = urljoin(self.relay_url, "/api/reach-link/printer-data")
        payload = {
            "printerId": self.printer_id,
            "token": self.token,
            "timestamp": int(time.time() * 1000),
            "temperatures": moonraker_status.get("temperatures"),
            "fans": moonraker_status.get("fans"),
            "motion": moonraker_status.get("motion"),
            "job": moonraker_status.get("job"),
            "systemHealth": moonraker_status.get("system_health"),
            "errors": [],
            "logTail": [],
        }
        
        response = HTTPClient.post_json(url, payload, self.token, timeout=10)
        if response:
            logger.debug("Telemetry sent successfully")
            return True
        return False

    def send_webcam_snapshot(self, jpeg_data: bytes) -> bool:
        """
        POST webcam JPEG snapshot to /api/reach-link/webcam-snapshot.
        No retries — if one frame fails, the next capture will succeed.
        """
        url = urljoin(self.relay_url, "/api/reach-link/webcam-snapshot")
        headers = {
            "Content-Type": "image/jpeg",
            "Authorization": f"Bearer {self.token}",
            "X-Printer-Id": self.printer_id,
        }
        try:
            req = Request(url, data=jpeg_data, headers=headers, method="POST")
            with urlopen(req, timeout=15) as response:
                logger.debug("Webcam snapshot uploaded successfully")
                return True
        except HTTPError as e:
            logger.debug(f"Webcam snapshot upload failed (HTTP {e.code}): {e.reason}")
        except (URLError, OSError) as e:
            logger.debug(f"Webcam snapshot upload failed: {e}")
        except Exception as e:
            logger.error(f"Unexpected error uploading webcam snapshot: {e}")
        return False

    def pull_command(self) -> Optional[Dict[str, Any]]:
        """
        Poll relay for next queued command for this printer.
        The server holds the connection for up to 25 s (long-poll), so we
        allow a 30 s socket timeout to avoid premature disconnects.
        Returns command payload or None when queue is empty.
        """
        url = urljoin(self.relay_url, "/api/reach-link/commands/pull")
        payload = {
            "printerId": self.printer_id,
        }

        response = HTTPClient.post_json(url, payload, self.token, timeout=30)
        if not response:
            return None

        return response.get("command")

    def push_command_result(
        self,
        request_id: str,
        status: str,
        result: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
    ) -> bool:
        """
        Push command execution result back to relay.
        status must be "completed" or "failed".
        """
        url = urljoin(self.relay_url, "/api/reach-link/commands/push")
        payload: Dict[str, Any] = {
            "printerId": self.printer_id,
            "requestId": request_id,
            "status": status,
        }
        if result is not None:
            payload["result"] = result
        if error:
            payload["error"] = error

        response = HTTPClient.post_json(url, payload, self.token, timeout=10)
        return response is not None

# ============================================================================
# Main Agent
# ============================================================================

class ReachLinkAgent:
    """Main agent loop."""
    
    def __init__(self, config: Config):
        self.config = config
        self._bootstrap_credentials_if_needed()
        self.moonraker = MoonrakerClient(config.moonraker_url)
        self.relay = RelayClient(config.relay_url, config.token, config.printer_id)
        
        # Initialize Firebase RTDB client if configured
        self.firebase = None
        if config.firebase_database_url and config.firebase_token:
            if FirebaseRealtimeDatabaseClient:
                try:
                    self.firebase = FirebaseRealtimeDatabaseClient(
                        config.firebase_database_url,
                        config.firebase_token,
                        config.printer_id,
                    )
                    logger.info("Firebase RTDB client initialized (cloud command queue enabled)")
                except Exception as e:
                    logger.warning(f"Failed to initialize Firebase RTDB client: {e}")
                    self.firebase = None
            else:
                logger.debug("Firebase RTDB client not available (firebase_rtdb_client module not found)")
        else:
            logger.debug("Firebase RTDB not configured (env vars not set)")
        
        self.shutdown_event = asyncio.Event()
        self.start_time = time.time()
        self.last_heartbeat = 0.0
        self.last_telemetry = 0.0
        self.last_command_poll = 0.0
        self.last_webcam_capture = 0.0
        self.token_revoked = False

    def _bootstrap_credentials_if_needed(self):
        """Claim pairing session if token is not pre-provisioned."""
        if self.config.token and self.config.printer_id:
            return

        if not self.config.pairing_code:
            raise ValueError(
                "Missing credentials: set REACH_LINK_TOKEN or REACH_LINK_PAIRING_CODE"
            )

        claim_url = urljoin(self.config.relay_url, "/api/reach-link/pairing/claim")
        payload = {
            "pairingCode": self.config.pairing_code,
            "agentVersion": AGENT_VERSION,
            "moonrakerUrl": self.config.moonraker_url,
            "printerIPAddress": self.config.printer_ip or (SubnetDetector("127.0.0.1").get_local_ip() or ""),
            "hostname": socket.gethostname(),
        }

        logger.info("No REACH_LINK_TOKEN found, attempting pairing claim bootstrap...")
        response = HTTPClient.post_json(claim_url, payload, token=None, timeout=10, max_retries=3)

        if not response:
            raise ValueError("Pairing claim failed: no response from relay")

        token = str(response.get("reachLinkToken", "")).strip()
        printer_id = str(response.get("printerId", "")).strip()
        user_id = str(response.get("userId", "")).strip()
        relay_url = str(response.get("relayUrl", self.config.relay_url)).strip().rstrip("/")

        if not token or not printer_id:
            raise ValueError("Pairing claim failed: missing reachLinkToken or printerId")

        self.config.token = token
        self.config.printer_id = printer_id
        self.config.user_id = user_id or self.config.user_id
        self.config.relay_url = relay_url or self.config.relay_url
        self.config.persist_state()

        logger.info(f"Pairing claim successful. Printer registered as {self.config.printer_id}")
    
    def setup_signal_handlers(self):
        """Register SIGTERM/SIGINT handlers for graceful shutdown."""
        def signal_handler(signum, frame):
            logger.info(f"Received signal {signum}; shutting down...")
            self.shutdown_event.set()
        
        signal.signal(signal.SIGTERM, signal_handler)
        signal.signal(signal.SIGINT, signal_handler)
    
    def proxy_command_to_moonraker(self, command: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Proxy Moonraker API request to the printer's Moonraker instance.
        Routes relay command to local Moonraker instance on the printer.
        
        Example:
          command: "printer.gcode.script"
          params: { "script": "M109 S200" }
        
        Returns: { "result": {...} } or { "error": "..." }
        """
        try:
            moonraker_base = "http://127.0.0.1:7125"
            command_params = dict(params or {})
            method = str(command_params.pop("__method", "POST")).upper()
            query = command_params.pop("__query", {})
            
            # Construct Moonraker API endpoint
            # Most commands map directly: "printer.gcode" -> "/printer/gcode"
            path = "/" + command.replace(".", "/")
            url = f"{moonraker_base}{path}"
            if isinstance(query, dict) and query:
                from urllib.parse import urlencode
                query_string = urlencode(query)
                url = f"{url}?{query_string}"
            
            # Build request
            if method == "GET":
                req = Request(
                    url,
                    method="GET",
                    headers={"Content-Type": "application/json"}
                )
            else:
                body = json.dumps(command_params or {}).encode("utf-8")
                req = Request(
                    url,
                    data=body,
                    method=method,
                    headers={"Content-Type": "application/json"}
                )
            
            with urlopen(req, timeout=10) as response:
                response_data = json.loads(response.read().decode("utf-8"))
                logger.debug(f"Moonraker responded to {command}: {response.status}")
                return response_data
        
        except Exception as e:
            logger.error(f"Moonraker proxy error for {command}: {e}")
            return {"error": str(e), "errorCode": "moonraker_error"}
    
    def process_pending_firebase_commands(self) -> int:
        """
        Poll and process commands from Firebase RTDB
        (supplement to relay server polling)
        """
        if not self.firebase:
            return 0

        try:
            commands = self.firebase.get_queued_commands()
            if not commands:
                return 0

            processed_count = 0
            for command_id, command_data in commands.items():
                try:
                    command = command_data.get("command", "")
                    params = command_data.get("params", {})

                    if not command:
                        logger.warning(f"Firebase command {command_id} has no command field")
                        self.firebase.dequeue_command(command_id)
                        continue

                    logger.debug(f"Processing Firebase command {command_id}: {command}")

                    # Mark as executing
                    self.firebase.write_command_result(
                        command_id,
                        status="executing",
                    )

                    # Execute via Moonraker proxy
                    result = self.proxy_command_to_moonraker(command, params)

                    # Write result
                    if "error" in result:
                        self.firebase.write_command_result(
                            command_id,
                            status="failed",
                            error=str(result.get("error", "unknown")),
                        )
                    else:
                        self.firebase.write_command_result(
                            command_id,
                            status="completed",
                            result=result,
                        )

                    # Dequeue the command
                    self.firebase.dequeue_command(command_id)
                    processed_count += 1

                except Exception as e:
                    logger.error(f"Error processing Firebase command {command_id}: {e}")
                    try:
                        self.firebase.write_command_result(
                            command_id,
                            status="failed",
                            error=str(e),
                        )
                        self.firebase.dequeue_command(command_id)
                    except Exception:
                        pass

            return processed_count

        except Exception as e:
            logger.error(f"Error in Firebase command processing: {e}")
            return 0
    
    def process_pending_commands(self) -> int:
        """
        Drain the relay command queue: pull and execute commands until the queue
        is empty, then return the total number of commands processed.
        This prevents backlog build-up when the browser fires several requests
        in the same poll window (e.g. when the printer overview tab refreshes).
        Raises ValueError("TOKEN_REVOKED") if token has been revoked by server.
        """
        processed = 0
        try:
            while True:
                command_data = self.relay.pull_command()
                if not command_data:
                    # Queue is empty - done for this cycle.
                    break

                request_id = command_data.get("requestId", "")
                command = command_data.get("command", "")
                params = command_data.get("params", {})

                if not request_id or not command:
                    logger.warning("Received malformed relay command payload")
                    continue

                logger.info(f"[relay-command] Processing: id={request_id}, command={command}")
                result = self.proxy_command_to_moonraker(command, params)

                if "error" in result:
                    self.relay.push_command_result(
                        request_id=request_id,
                        status="failed",
                        result=result,
                        error=str(result.get("error", "moonraker_error")),
                    )
                else:
                    self.relay.push_command_result(
                        request_id=request_id,
                        status="completed",
                        result=result,
                    )

                processed += 1

            return processed
        except ValueError as e:
            if str(e) == "TOKEN_REVOKED":
                logger.critical("Token has been revoked by server. Agent will shut down.")
                logger.critical(
                    "Action required: Re-run printer setup to generate a new token and reinstall reach-link agent."
                )
                self.token_revoked = True
                self.shutdown_event.set()
                return processed
            raise
        except Exception as e:
            logger.error(f"Error processing relay commands: {e}")
            return processed
    
    
    # -----------------------------------------------------------------------
    # Self-update
    # -----------------------------------------------------------------------

    def _parse_version(self, version_str: str) -> tuple:
        """Parse a semver-like string into a comparable tuple, e.g. 'v1.0.7' -> (1, 0, 7)."""
        try:
            clean = version_str.lstrip("vV").strip()
            return tuple(int(x) for x in clean.split("."))
        except Exception:
            return (0,)

    def _check_for_update(self) -> None:
        """
        Check the Reach3D platform relay for a newer version of reach-link-agent.py.
        Downloads via the platform API (no GitHub access required on the printer).
        If a newer version is found: download, atomically replace current script,
        then exit so systemd/supervisor can restart with the new version.
        """
        try:
            import os as _os

            # Step 1 — Check version from platform relay (no auth required)
            version_url = f"{self.config.relay_url.rstrip('/')}/api/reach-link/version"
            req = Request(
                version_url,
                headers={"User-Agent": f"reach-link-agent/{AGENT_VERSION}"},
            )
            try:
                with urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
            except Exception as e:
                logger.debug(f"[auto-update] Version check failed: {e}")
                return

            latest_version_str = data.get("version", "")
            if not latest_version_str:
                return

            if self._parse_version(latest_version_str) <= self._parse_version(AGENT_VERSION):
                logger.info(f"[auto-update] Already up to date (v{AGENT_VERSION})")
                return

            logger.info(
                f"[auto-update] New version available: v{latest_version_str} "
                f"(current: v{AGENT_VERSION}). Downloading from platform..."
            )

            # Step 2 — Download the new agent script from platform (auth required)
            download_url = f"{self.config.relay_url.rstrip('/')}/api/reach-link/agent"
            dl_req = Request(
                download_url,
                headers={
                    "Authorization": f"Bearer {self.config.token}",
                    "X-Printer-Id": self.config.printer_id,
                    "User-Agent": f"reach-link-agent/{AGENT_VERSION}",
                },
            )

            current_script = _os.path.abspath(__file__)
            tmp_path = current_script + ".update_tmp"
            try:
                with urlopen(dl_req, timeout=30) as resp:
                    content = resp.read()
                if len(content) < 500:
                    logger.warning("[auto-update] Downloaded file too small — aborting update")
                    return
                with open(tmp_path, "wb") as fh:
                    fh.write(content)
            except Exception as e:
                logger.error(f"[auto-update] Download failed: {e}")
                try:
                    _os.remove(tmp_path)
                except Exception:
                    pass
                return

            # Step 3 — Atomic replace + restart
            try:
                _os.replace(tmp_path, current_script)
                logger.info(
                    f"[auto-update] Updated to v{latest_version_str}. "
                    "Exiting so the process manager can restart with the new version."
                )
                sys.exit(0)
            except Exception as e:
                logger.error(f"[auto-update] Failed to replace script: {e}")
                try:
                    _os.remove(tmp_path)
                except Exception:
                    pass

        except Exception as e:
            logger.warning(f"[auto-update] Unexpected error during update check: {e}")

    async def run(self):
        """Main agent loop."""
        logger.info(f"reach-link agent starting (version {AGENT_VERSION})")
        logger.info(
            f"relay_url={self.config.relay_url}, "
            f"printer_id={self.config.printer_id}, "
            f"user_id={self.config.user_id}, "
            f"moonraker_url={self.config.moonraker_url}"
        )
        logger.info(
            f"heartbeat_interval={self.config.heartbeat_interval}s, "
            f"telemetry_interval={self.config.telemetry_interval}s"
        )
        
        logger.info("Relay command queue mode enabled")

        # Check for updates before entering the main loop
        self._check_for_update()

        self.setup_signal_handlers()
        
        while not self.shutdown_event.is_set():
            try:
                now = time.time()
                uptime = int(now - self.start_time)
                
                # Heartbeat to HTTP relay
                if now - self.last_heartbeat >= self.config.heartbeat_interval:
                    if not self.token_revoked:
                        try:
                            heartbeat_payload = {
                                "printerId": self.config.printer_id,
                                "userId": self.config.user_id,
                                "uptime": uptime,
                                "version": AGENT_VERSION,
                            }
                            heartbeat_response = self.relay.register_heartbeat(uptime, version=AGENT_VERSION)
                            if heartbeat_response:
                                # Persist rotated token if the server issued one
                                new_token = str(heartbeat_response.get("rotatedToken", "")).strip()
                                if new_token:
                                    self.config.token = new_token
                                    self.relay.token = new_token
                                    self.config.persist_state()
                                    logger.info("Received and persisted rotated reach-link token after first heartbeat")
                                # Respect the server's requested check-in interval
                                next_check_in = heartbeat_response.get("nextCheckIn")
                                if next_check_in and isinstance(next_check_in, (int, float)) and int(next_check_in) > 0:
                                    self.config.heartbeat_interval = int(next_check_in)
                        except ValueError as e:
                            if str(e) == "TOKEN_REVOKED":
                                logger.critical("Token has been revoked by server. Agent will shut down.")
                                self.token_revoked = True
                                self.shutdown_event.set()
                    
                    self.last_heartbeat = now
                
                # Telemetry
                if now - self.last_telemetry >= self.config.telemetry_interval:
                    if not self.token_revoked:
                        try:
                            moonraker_status = self.moonraker.get_status()
                            if moonraker_status:
                                # Send to HTTP relay
                                self.relay.send_telemetry(moonraker_status)
                                
                                # Also update Firebase RTDB (cloud command queue)
                                if self.firebase:
                                    try:
                                        # Extract status fields from moonraker_status
                                        temperatures = moonraker_status.get("temperatures", {})
                                        job = moonraker_status.get("job")
                                        system_health = moonraker_status.get("system_health", {})
                                        
                                        # Determine printer state
                                        printer_state = "idle"
                                        if job and job.get("state") == "printing":
                                            printer_state = "printing"
                                        elif job and job.get("state") == "paused":
                                            printer_state = "paused"
                                        
                                        # Write to RTDB
                                        self.firebase.update_printer_status(
                                            state=printer_state,
                                            temperatures=temperatures,
                                            job=job,
                                            system_health=system_health,
                                        )
                                    except Exception as e:
                                        logger.debug(f"Failed to update Firebase RTDB: {e}")
                        except ValueError as e:
                            if str(e) == "TOKEN_REVOKED":
                                logger.critical("Token has been revoked by server. Agent will shut down.")
                                self.token_revoked = True
                                self.shutdown_event.set()
                    self.last_telemetry = now
                
                # Webcam snapshot (only when a viewer is active in the dashboard)
                if now - self.last_webcam_capture >= self.config.webcam_snapshot_interval:
                    if not self.token_revoked and self.firebase:
                        try:
                            viewer_ts = self.firebase.get_webcam_viewer_ts()
                            if viewer_ts and (now * 1000 - viewer_ts) < (self.config.webcam_viewer_timeout * 1000):
                                snapshot = self.moonraker.get_webcam_snapshot()
                                if snapshot:
                                    if self.relay.send_webcam_snapshot(snapshot):
                                        logger.debug(f"Webcam snapshot sent ({len(snapshot)} bytes)")
                        except Exception as e:
                            logger.debug(f"Webcam snapshot error: {e}")
                    self.last_webcam_capture = now

                # Process pending commands from relay queue.
                # The pull endpoint long-polls for up to 25 s so this loop
                # runs almost continuously — each call either returns a
                # dispatched command immediately or holds ~25 s then returns
                # empty, giving effectively real-time command delivery with
                # near-zero idle reads.
                if now - self.last_command_poll >= self.config.command_poll_interval:
                    if not self.token_revoked:
                        logger.debug(f"[relay-poll] Polling for commands (printerId={self.config.printer_id})")
                        n = self.process_pending_commands()
                        if n > 0:
                            logger.info(f"[relay-poll] Processed {n} command(s)")

                    self.last_command_poll = now
                
                # Sleep briefly to avoid busy-waiting
                await asyncio.sleep(1)
            
            except Exception as e:
                logger.error(f"Error in agent loop: {e}")
                await asyncio.sleep(5)
        
        logger.info("reach-link agent stopped")

# ============================================================================
# Entry Point
# ============================================================================

def main():
    """Entry point."""
    try:
        # Load config
        config = Config()
        
        # Setup logging
        setup_logging(config.log_file)
        
        # Run agent
        agent = ReachLinkAgent(config)
        asyncio.run(agent.run())
    
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        sys.exit(0)
    except ValueError as e:
        logger.error(f"Configuration error: {e}")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
