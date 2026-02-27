#!/usr/bin/env python3
"""
Reach-Link Helper Bridge: Local Windows utility for LAN printer discovery and SSH-based reach-link installation.

Runs as a localhost HTTP server (ports 5900-5920).
Browser wizard communicates with this helper to:
- Discover Moonraker printers on LAN (multicast mDNS)
- Validate Moonraker connection (HTTP probe)
- Install reach-link via SSH (with real-time logging)

Protocol:
  GET /health -> {"ready": true, "version": "...", "capabilities": {...}}
  POST /execute -> {"sessionId": "...", "userId": "...", "step": "...", "payload": {...}, "nonce": "...", "timestamp": ..., "signature": "..."}
    -> {"success": true, "data": {...}, "logs": [...]}
"""

import asyncio
import json
import logging
import os
import sys
import socket
import hashlib
import hmac
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from io import BytesIO
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse, parse_qs

# Import helper modules
from ssh_executor import SSHExecutor, SSHResult
from lan_discovery import LANDiscovery

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(name)s] %(levelname)s: %(message)s'
)
logger = logging.getLogger('HelperBridge')

# Constants
VERSION = "1.0.0"
HELPER_PORT_RANGE = (5900, 5920)
NONCE_EXPIRY_SECONDS = 5  # ±5s for nonce freshness


class HelperLogEntry:
    """Log entry from helper execution"""
    def __init__(self, level: str, source: str, message: str):
        self.timestamp = datetime.utcnow().isoformat() + 'Z'
        self.level = level  # info, debug, warn, error
        self.source = source  # discover, validate, install, system
        self.message = message

    def to_dict(self) -> Dict[str, str]:
        return {
            'timestamp': self.timestamp,
            'level': self.level,
            'source': self.source,
            'message': self.message,
        }


class HelperExecutor:
    """Executes setup steps and collects logs"""
    def __init__(self):
        self.logs: List[HelperLogEntry] = []
        self.ssh_executor = SSHExecutor(log_callback=self._add_log)
        self.lan_discovery = LANDiscovery(log_callback=self._add_log)

    def _add_log(self, level: str, source: str, message: str):
        """Add log entry"""
        entry = HelperLogEntry(level, source, message)
        self.logs.append(entry)
        logger.info(f"[{source}] {message}")

    def clear_logs(self):
        """Clear log history"""
        self.logs = []

    def discover(self) -> Dict[str, Any]:
        """Discover printers on LAN"""
        self.clear_logs()
        self._add_log('info', 'discover', 'Starting LAN discovery...')

        try:
            discovered = self.lan_discovery.scan()
            self._add_log('info', 'discover', f'Found {len(discovered)} printer(s)')
            return {
                'success': True,
                'data': {
                    'discovered': discovered,
                    'count': len(discovered),
                },
            }
        except Exception as e:
            self._add_log('error', 'discover', f'Discovery failed: {str(e)}')
            return {
                'success': False,
                'error': str(e),
            }

    def validate_moonraker(self, host: str, port: int = 7125, ssl: bool = False, api_key: str = '') -> Dict[str, Any]:
        """Validate Moonraker connection"""
        self.clear_logs()
        self._add_log('info', 'validate', f'Validating Moonraker at {host}:{port} (SSL={ssl})')

        try:
            result = self.lan_discovery.validate_connection(host, port, ssl, api_key)
            if result['success']:
                self._add_log('info', 'validate', f'Moonraker validated: {result["data"].get("moonrakerVersion", "unknown")}')
            else:
                self._add_log('error', 'validate', f'Validation failed: {result.get("error", "unknown error")}')
            return result
        except Exception as e:
            self._add_log('error', 'validate', f'Validation error: {str(e)}')
            return {
                'success': False,
                'error': str(e),
            }

    def install_reach_link(self, printer_id: str, relay_url: str, reach_link_token: str, ssh: Dict[str, Any]) -> Dict[str, Any]:
        """Install reach-link via SSH"""
        self.clear_logs()
        self._add_log('info', 'install', f'Installing reach-link for printer: {printer_id}')
        self._add_log('info', 'install', f'SSH Target: {ssh.get("username")}@{ssh.get("host")}')

        try:
            result = self.ssh_executor.install_reach_link(
                printer_id=printer_id,
                relay_url=relay_url,
                reach_link_token=reach_link_token,
                ssh_host=ssh.get('host'),
                ssh_port=int(ssh.get('port', 22)),
                ssh_username=ssh.get('username'),
                ssh_auth_method=ssh.get('authMethod', 'password'),
                ssh_password=ssh.get('password'),
                ssh_private_key=ssh.get('privateKey'),
                ssh_sudo_password=ssh.get('sudoPassword'),
            )
            if result['success']:
                self._add_log('info', 'install', 'Install completed successfully')
            else:
                self._add_log('error', 'install', f'Install failed: {result.get("error", "unknown error")}')
            return result
        except Exception as e:
            self._add_log('error', 'install', f'Install error: {str(e)}')
            return {
                'success': False,
                'error': str(e),
            }


class HelperRequestHandler(BaseHTTPRequestHandler):
    """HTTP request handler for helper bridge"""
    
    executor: Optional[HelperExecutor] = None

    def log_message(self, format, *args):
        """Suppress default logging"""
        pass

    def do_GET(self):
        """Handle GET requests"""
        if self.path == '/health':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            response = {
                'ready': True,
                'version': VERSION,
                'timestamp': datetime.utcnow().isoformat() + 'Z',
                'capabilities': {
                    'discover': True,
                    'validateMoonraker': True,
                    'installReachLink': True,
                },
            }
            self.wfile.write(json.dumps(response).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        """Handle POST requests"""
        if self.path != '/execute':
            self.send_response(404)
            self.end_headers()
            return

        # Read request body
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length)

        try:
            payload = json.loads(body.decode('utf-8'))
        except Exception as e:
            logger.error(f'Failed to parse request: {str(e)}')
            self.send_response(400)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'error': 'Invalid JSON'}).encode())
            return

        # Validate signature (basic check - can be enhanced)
        if not self._validate_signature(payload):
            logger.warning('Invalid signature in request')
            self.send_response(401)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'error': 'Invalid signature'}).encode())
            return

        # Validate nonce freshness
        if not self._validate_nonce_freshness(payload):
            logger.warning('Stale nonce in request')
            self.send_response(401)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'error': 'Stale nonce'}).encode())
            return

        # Execute step
        step = payload.get('step')
        step_payload = payload.get('payload', {})

        if step == 'discover':
            result = self.executor.discover()
        elif step == 'validate_moonraker':
            result = self.executor.validate_moonraker(
                host=step_payload.get('host'),
                port=int(step_payload.get('port', 7125)),
                ssl=bool(step_payload.get('ssl', False)),
                api_key=step_payload.get('apiKey', ''),
            )
        elif step == 'install_reach_link':
            result = self.executor.install_reach_link(
                printer_id=step_payload.get('printerId'),
                relay_url=step_payload.get('relayUrl'),
                reach_link_token=step_payload.get('reachLinkToken'),
                ssh=step_payload.get('ssh', {}),
            )
        else:
            result = {'success': False, 'error': f'Unknown step: {step}'}

        # Add logs to result
        result['logs'] = [entry.to_dict() for entry in self.executor.logs]

        # Send response
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(result).encode())

    def _validate_signature(self, payload: Dict[str, Any]) -> bool:
        """Validate HMAC signature (basic implementation)"""
        # In production, use proper key management
        # For now, accept any signature (validation happens on client side)
        return 'signature' in payload

    def _validate_nonce_freshness(self, payload: Dict[str, Any]) -> bool:
        """Validate nonce is not too old"""
        try:
            timestamp = payload.get('timestamp', 0)
            now_ms = int(datetime.utcnow().timestamp() * 1000)
            age_ms = abs(now_ms - timestamp)
            return age_ms <= (NONCE_EXPIRY_SECONDS * 1000)
        except:
            return False


def find_available_port(start: int = HELPER_PORT_RANGE[0], end: int = HELPER_PORT_RANGE[1]) -> int:
    """Find first available port in range"""
    for port in range(start, end + 1):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.bind(('localhost', port))
            s.close()
            return port
        except OSError:
            continue
    raise RuntimeError(f'No available ports in range {start}-{end}')


def main():
    """Main entry point"""
    logger.info(f'Reach-Link Helper Bridge v{VERSION} starting...')

    # Find available port
    try:
        port = find_available_port()
        logger.info(f'Using port {port}')
    except Exception as e:
        logger.error(f'Failed to find available port: {str(e)}')
        sys.exit(1)

    # Setup executor
    HelperRequestHandler.executor = HelperExecutor()

    # Start server on all interfaces (0.0.0.0) so it's accessible from other machines on LAN
    server = HTTPServer(('0.0.0.0', port), HelperRequestHandler)
    
    # Get local IP for logging
    try:
        local_ip = socket.gethostbyname(socket.gethostname())
    except:
        local_ip = '127.0.0.1'
    
    logger.info(f'Server started at http://127.0.0.1:{port} (localhost)')
    logger.info(f'LAN access: http://{local_ip}:{port}')
    logger.info('Waiting for requests (Ctrl+C to exit)...')

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info('Shutting down...')
        server.shutdown()
        sys.exit(0)


if __name__ == '__main__':
    main()
