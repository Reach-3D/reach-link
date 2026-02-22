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
# Main Agent
# ============================================================================

class ReachLinkAgent:
    """Main agent loop."""
    
    def __init__(self, config: Config):
        self.config = config
        self.moonraker = MoonrakerClient(config.moonraker_url)
        self.relay = RelayClient(config.relay_url, config.token, config.printer_id)
        self.shutdown_event = asyncio.Event()
        self.start_time = time.time()
        self.last_heartbeat = 0.0
        self.last_telemetry = 0.0
    
    def setup_signal_handlers(self):
        """Register SIGTERM/SIGINT handlers for graceful shutdown."""
        def signal_handler(signum, frame):
            logger.info(f"Received signal {signum}; shutting down...")
            self.shutdown_event.set()
        
        signal.signal(signal.SIGTERM, signal_handler)
        signal.signal(signal.SIGINT, signal_handler)
    
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
        
        while not self.shutdown_event.is_set():
            try:
                now = time.time()
                uptime = int(now - self.start_time)
                
                # Heartbeat
                if now - self.last_heartbeat >= self.config.heartbeat_interval:
                    self.relay.register_heartbeat(uptime)
                    self.last_heartbeat = now
                
                # Telemetry
                if now - self.last_telemetry >= self.config.telemetry_interval:
                    moonraker_status = self.moonraker.get_status()
                    if moonraker_status:
                        self.relay.send_telemetry(moonraker_status)
                    self.last_telemetry = now
                
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
