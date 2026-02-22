#!/usr/bin/env python3
"""
reach-link Universal Agent
Cross-platform printer agent for Klipper/Moonraker 3D printers (Python version for all platforms).
Features: Heartbeat registration, telemetry collection, command proxying via RTDB, local/remote routing.
Supports all architectures: MIPS, ARM64, x86_64, and others.
"""

import asyncio
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime
from typing import Any, Dict, Optional
from urllib.error import URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen
import ipaddress
import socket

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

# ============================================================================
# Configuration
# ============================================================================

class Config:
    """Load and validate configuration from environment."""
    
    def __init__(self):
        self.relay_url = self._require_env("REACH_LINK_RELAY")
        self.token = self._require_env("REACH_LINK_TOKEN")
        self.printer_id = self._require_env_with_fallback(
            "REACH_LINK_PRINTER_ID", "REACH_PRINTER_ID"
        )
        self.user_id = os.environ.get("REACH_LINK_USER_ID", "")
        self.firebase_token = os.environ.get("REACH_LINK_FIREBASE_TOKEN", "")
        self.printer_ip = os.environ.get("REACH_LINK_PRINTER_IP", "")
        self.moonraker_url = os.environ.get(
            "REACH_LINK_MOONRAKER_URL", "http://127.0.0.1:7125"
        ).rstrip("/")
        self.heartbeat_interval = int(
            os.environ.get("REACH_LINK_HEARTBEAT_INTERVAL", "60")
        )
        self.telemetry_interval = int(
            os.environ.get("REACH_LINK_TELEMETRY_INTERVAL", "10")
        )
        self.log_file = os.environ.get("REACH_LINK_LOG_FILE")
        
        # Validate
        if not self.relay_url.startswith("https://") and not self.relay_url.startswith("http://"):
            raise ValueError(f"REACH_LINK_RELAY must use HTTPS or HTTP, got: {self.relay_url}")
        if not self.token.strip():
            raise ValueError("REACH_LINK_TOKEN must not be empty")
        if not self.printer_id.strip():
            raise ValueError("REACH_LINK_PRINTER_ID must not be empty")
    
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
        token: str,
        timeout: int = 10,
        max_retries: int = 3,
    ) -> Optional[Dict[str, Any]]:
        """POST JSON data with Bearer token auth; retry on failure."""
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
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
            except (URLError, OSError) as e:
                last_error = e
                if attempt < max_retries - 1:
                    wait = 2 ** attempt  # Exponential backoff
                    logger.warning(
                        f"HTTP POST failed (attempt {attempt + 1}/{max_retries}): {e}; "
                        f"retrying in {wait}s"
                    )
                    time.sleep(wait)
            except Exception as e:
                logger.error(f"Unexpected error in HTTP POST: {e}")
                return None
        
        logger.error(f"HTTP POST failed after {max_retries} attempts: {last_error}")
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
        Query Moonraker for temperatures, job, system health.
        Mimics the Rust binary's snapshot structure.
        """
        try:
            # Query printer objects: temperatures (nozzle, bed), job state, cpu/memory
            query_url = (
                f"{self.url}/printer/objects/query?"
                "extruder=temperature,target&"
                "heater_bed=temperature,target&"
                "print_stats=filename,total_duration,print_duration,filament_used&"
                "display_status=message&"
                "system_stats=cputime,memavail,cpu_percent,memory"
            )
            
            response = HTTPClient.get_json(query_url, timeout=5)
            if not response or "result" not in response:
                logger.warning("Moonraker query returned invalid response")
                return None
            
            result = response.get("result", {})
            status = result.get("status", {})
            
            # Extract temperatures
            temperatures = {
                "nozzle": status.get("extruder", {}).get("temperature"),
                "bed": status.get("heater_bed", {}).get("temperature"),
                "chamber": None,  # K1C doesn't typically have chamber sensor
            }
            
            # Extract job info
            print_stats = status.get("print_stats", {})
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
            progress = 0.0
            if total_duration > 0:
                progress = (print_duration / total_duration) * 100
            
            job = {
                "filename": print_stats.get("filename"),
                "progress": min(progress, 100.0),
                "eta": None,  # Could calculate from remaining time if needed
                "elapsedTime": int(print_duration),
                "state": job_state,
                "totaltime": int(total_duration),
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
                "job": job,
                "system_health": system_health,
            }
        
        except Exception as e:
            logger.error(f"Error querying Moonraker: {e}")
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
    
    def register_heartbeat(self, uptime_secs: int, version: str = "1.0.0") -> bool:
        """
        POST heartbeat to /api/reach-link/register.
        Returns True if successful.
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
            return True
        return False
    
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

# ============================================================================
# Firebase Realtime Database Client (REST API, no external dependencies)
# ============================================================================

class FirebaseRTDB:
    """Firebase Realtime Database client using REST API."""
    
    COMMON_RTDB_URL = "https://reach3d-default-rtdb.firebaseio.com"
    
    def __init__(self, firebase_token: str, printer_id: str, relay_url: str):
        self.firebase_token = firebase_token
        self.printer_id = printer_id
        # Extract RTDB base URL from relay URL if available, otherwise use default
        self.rtdb_url = self._extract_rtdb_url(relay_url) or self.COMMON_RTDB_URL
    
    @staticmethod
    def _extract_rtdb_url(relay_url: str) -> Optional[str]:
        """
        Extract RTDB URL from relay_url if embedded, otherwise None.
        Example: 'https://service.com/api' might contain a config with RTDB URL.
        For now, returning None to use default.
        """
        return None
    
    def read_commands(self) -> Dict[str, Any]:
        """
        Read commands from /printers/{printerId}/QUERY
        Returns dict of { requestId: { command, params, ... } }
        """
        url = f"{self.rtdb_url}/printers/{self.printer_id}/QUERY.json?auth={self.firebase_token}"
        try:
            response = HTTPClient.get_json(url, timeout=5, max_retries=2)
            if response and isinstance(response, dict):
                logger.debug(f"Read {len(response)} pending commands")
                return response
            return {}
        except Exception as e:
            logger.debug(f"Error reading commands: {e}")
            return {}
    
    def delete_command(self, request_id: str) -> bool:
        """Delete a command after processing."""
        url = f"{self.rtdb_url}/printers/{self.printer_id}/QUERY/{request_id}.json?auth={self.firebase_token}"
        try:
            req = Request(url, method="DELETE")
            with urlopen(req, timeout=5) as response:
                if response.status == 200:
                    logger.debug(f"Deleted command {request_id}")
                    return True
        except Exception as e:
            logger.warning(f"Error deleting command {request_id}: {e}")
        return False
    
    def write_response(self, request_id: str, status: str, result: Any, error_code: Optional[str] = None) -> bool:
        """
        Write command response to /printers/{printerId}/RESPONSE/{requestId}
        """
        url = f"{self.rtdb_url}/printers/{self.printer_id}/RESPONSE/{request_id}.json?auth={self.firebase_token}"
        payload = {
            "requestId": request_id,
            "status": status,
            "result": result,
            "timestamp": int(time.time() * 1000),
        }
        if error_code:
            payload["errorCode"] = error_code
        
        body = json.dumps(payload).encode("utf-8")
        try:
            req = Request(url, data=body, method="PUT", headers={"Content-Type": "application/json"})
            with urlopen(req, timeout=5) as response:
                if response.status in (200, 201):
                    logger.debug(f"Wrote response for {request_id}")
                    return True
        except Exception as e:
            logger.error(f"Error writing response: {e}")
        return False
    
    def update_heartbeat(self, uptime: int, status: str = "online") -> bool:
        """Update heartbeat in RTDB."""
        url = f"{self.rtdb_url}/printers/{self.printer_id}/heartbeat.json?auth={self.firebase_token}"
        payload = {
            "connected": True,
            "lastHeartbeat": datetime.now().isoformat(),
            "uptime": uptime,
            "status": status,
        }
        body = json.dumps(payload).encode("utf-8")
        try:
            req = Request(url, data=body, method="PATCH", headers={"Content-Type": "application/json"})
            with urlopen(req, timeout=5) as response:
                if response.status in (200, 201):
                    return True
        except Exception as e:
            logger.debug(f"Error updating heartbeat in RTDB: {e}")
        return False

# ============================================================================
# Main Agent
# ============================================================================

class ReachLinkAgent:
    """Main agent loop."""
    
    def __init__(self, config: Config):
        self.config = config
        self.moonraker = MoonrakerClient(config.moonraker_url)
        self.relay = RelayClient(config.relay_url, config.token, config.printer_id)
        
        # Firebase RTDB client (only if credentials available)
        self.rtdb = None
        if config.firebase_token:
            self.rtdb = FirebaseRTDB(config.firebase_token, config.printer_id, config.relay_url)
            logger.info("Firebase RTDB client initialized for command proxying")
        
        self.subnet_detector = SubnetDetector(config.printer_ip) if config.printer_ip else None
        
        self.shutdown_event = asyncio.Event()
        self.start_time = time.time()
        self.last_heartbeat = 0.0
        self.last_telemetry = 0.0
        self.last_token_refresh = time.time()
        self.token_expires_at = 0.0
    
    def setup_signal_handlers(self):
        """Register SIGTERM/SIGINT handlers for graceful shutdown."""
        def signal_handler(signum, frame):
            logger.info(f"Received signal {signum}; shutting down...")
            self.shutdown_event.set()
        
        signal.signal(signal.SIGTERM, signal_handler)
        signal.signal(signal.SIGINT, signal_handler)
    
    def should_refresh_token(self) -> bool:
        """Check if token should be refreshed (before expiry)."""
        now = time.time()
        # Refresh 10 minutes before expiry (600 seconds)
        return (self.token_expires_at > 0) and (now > (self.token_expires_at - 600))
    
    def refresh_firebase_token(self) -> bool:
        """
        Call /api/reach-link/auth/refresh to get a new token.
        Returns True if successful.
        """
        if not self.config.user_id or not self.config.firebase_token:
            return False
        
        try:
            url = urljoin(self.config.relay_url, "/api/reach-link/auth/refresh")
            payload = {
                "printerId": self.config.printer_id,
                "userId": self.config.user_id,
                "expiredToken": self.config.firebase_token,
            }
            
            response = HTTPClient.post_json(url, payload, self.config.token, timeout=10)
            if response and response.get("token"):
                old_token = self.config.firebase_token
                self.config.firebase_token = response["token"]
                self.token_expires_at = response.get("expiresAt", 0) / 1000.0  # Convert ms to seconds
                
                # Update RTDB client with new token
                if self.rtdb:
                    self.rtdb.firebase_token = self.config.firebase_token
                
                logger.info(f"Firebase token refreshed (expires in {response.get('expiresIn', '?')} seconds)")
                return True
        except Exception as e:
            logger.error(f"Token refresh failed: {e}")
        
        return False
    
    def proxy_command_to_moonraker(self, command: str, params: Dict[str, Any], user_ip: Optional[str] = None) -> Dict[str, Any]:
        """
        Proxy Moonraker API request to the printer's Moonraker instance.
        Intelligently routes to localhost (127.0.0.1:7125) for local WiFi users,
        or to the printer's WiFi IP:7125 for remote users.
        
        Example:
          command: "printer.gcode.script"
          params: { "script": "M109 S200" }
          user_ip: "192.168.1.100" (optional)
        
        Returns: { "result": {...} } or { "error": "..." }
        """
        try:
            # Determine target Moonraker URL based on user location
            moonraker_base = "http://127.0.0.1:7125"  # Default to localhost
            
            # Check if user is on same WiFi (local) vs remote
            if user_ip and self.subnet_detector:
                is_local = self.subnet_detector.is_same_subnet(user_ip)
                if not is_local and self.config.printer_ip:
                    # User is remote: route through printer's WiFi IP
                    moonraker_base = f"http://{self.config.printer_ip}:7125"
                    logger.debug(f"Remote user {user_ip}: routing to printer IP {self.config.printer_ip}:7125")
                else:
                    logger.debug(f"Local user {user_ip}: routing to localhost 127.0.0.1:7125")
            else:
                logger.debug("No user IP or subnet detector: using default localhost routing")
            
            # Construct Moonraker API endpoint
            # Most commands map directly: "printer.gcode" -> "/printer/gcode"
            path = "/" + command.replace(".", "/")
            url = f"{moonraker_base}{path}"
            
            # Build request body
            body = json.dumps(params or {}).encode("utf-8")
            
            # POST to Moonraker
            req = Request(
                url,
                data=body,
                method="POST",
                headers={"Content-Type": "application/json"}
            )
            
            with urlopen(req, timeout=10) as response:
                response_data = json.loads(response.read().decode("utf-8"))
                logger.debug(f"Moonraker responded to {command}: {response.status}")
                return response_data
        
        except Exception as e:
            logger.error(f"Moonraker proxy error for {command}: {e}")
            return {"error": str(e), "errorCode": "moonraker_error"}
    
    def process_pending_commands(self) -> int:
        """
        Read pending commands from RTDB, execute them, and write responses.
        Intelligently routes to local or remote Moonraker based on user IP.
        Returns count of commands processed.
        """
        if not self.rtdb:
            return 0
        
        try:
            commands = self.rtdb.read_commands()
            processed_count = 0
            
            for request_id, command_data in commands.items():
                try:
                    # Extract command info
                    command = command_data.get("command", "")
                    params = command_data.get("params", {})
                    user_id = command_data.get("userId", "")
                    user_ip = command_data.get("userIp", "")  # Extract user IP for routing
                    
                    if not command:
                        logger.warning(f"Skipping command {request_id}: no command specified")
                        self.rtdb.delete_command(request_id)
                        continue
                    
                    logger.debug(f"Processing command {request_id}: {command} from user {user_id} (IP: {user_ip})")
                    
                    # Proxy to Moonraker with user IP for intelligent routing
                    result = self.proxy_command_to_moonraker(command, params, user_ip if user_ip else None)
                    
                    # Write response
                    if "error" in result:
                        self.rtdb.write_response(
                            request_id,
                            "error",
                            result,
                            error_code=result.get("errorCode", "unknown_error")
                        )
                    else:
                        self.rtdb.write_response(
                            request_id,
                            "success",
                            result
                        )
                    
                    # Delete command after processing
                    self.rtdb.delete_command(request_id)
                    processed_count += 1
                
                except Exception as e:
                    logger.error(f"Error processing command {request_id}: {e}")
                    # Still delete the command to avoid infinite retries
                    self.rtdb.delete_command(request_id)
            
            if processed_count > 0:
                logger.info(f"Processed {processed_count} commands from RTDB")
            
            return processed_count
        
        except Exception as e:
            logger.error(f"Error reading commands from RTDB: {e}")
            return 0
    
    
    async def run(self):
        """Main agent loop."""
        logger.info(f"reach-link agent starting (version 1.0.5)")
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
        
        # Log proxy/routing mode
        if self.config.firebase_token and self.config.printer_ip:
            logger.info("Firebase token and printer IP available - hybrid mode enabled (local + RTDB proxy)")
        elif self.config.firebase_token:
            logger.info("Firebase token available - RTDB proxy mode")
        else:
            logger.info("HTTP relay mode (legacy)")
        
        self.setup_signal_handlers()
        
        # Initialize token expiration if Firebase token is available
        if self.config.firebase_token:
            # Assume token was just generated (60 min TTL)
            self.token_expires_at = time.time() + 3600
            logger.info("Firebase token initialized (60 min TTL)")
        
        while not self.shutdown_event.is_set():
            try:
                now = time.time()
                uptime = int(now - self.start_time)
                
                # Token refresh (every 50 minutes, 10 min before expiry)
                if self.should_refresh_token():
                    logger.info("Refreshing Firebase token before expiry...")
                    if self.refresh_firebase_token():
                        self.last_token_refresh = now
                    else:
                        logger.warning("Token refresh failed; attempting to continue with current token")
                
                # Heartbeat to HTTP relay
                if now - self.last_heartbeat >= self.config.heartbeat_interval:
                    heartbeat_payload = {
                        "printerId": self.config.printer_id,
                        "userId": self.config.user_id,
                        "uptime": uptime,
                        "version": "1.0.5",
                    }
                    self.relay.register_heartbeat(uptime)
                    
                    # Also update RTDB heartbeat if available
                    if self.rtdb:
                        self.rtdb.update_heartbeat(uptime, "online")
                    
                    self.last_heartbeat = now
                
                # Telemetry
                if now - self.last_telemetry >= self.config.telemetry_interval:
                    moonraker_status = self.moonraker.get_status()
                    if moonraker_status:
                        self.relay.send_telemetry(moonraker_status)
                    self.last_telemetry = now
                
                # Process pending commands from RTDB (if available)
                if self.rtdb:
                    self.process_pending_commands()
                
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
