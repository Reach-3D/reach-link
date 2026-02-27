"""
LAN Discovery: Discovers Moonraker printers on local network.
Supports mDNS and direct HTTP probing.
"""

import json
import socket
import threading
from typing import Any, Callable, Dict, List, Optional
import urllib.request
import urllib.error


class LANDiscovery:
    """Discovers and validates Moonraker instances on LAN"""
    
    def __init__(self, log_callback: Optional[Callable[[str, str, str], None]] = None):
        self.log_callback = log_callback or (lambda level, source, msg: None)

    def _log(self, level: str, message: str, source: str = 'discover'):
        """Log a message"""
        self.log_callback(level, source, message)

    def scan(self) -> List[Dict[str, Any]]:
        """Scan LAN for Moonraker printers"""
        
        self._log('info', 'Starting LAN scan for Moonraker instances...')
        discovered = []

        # Try common printer hostnames
        common_hostnames = [
            'mainsail.local',
            'fluidd.local',
            'klipper.local',
            'moonraker.local',
            'printer.local',
            'octoprint.local',
        ]

        # Also try numeric subnets
        discovered.extend(self._probe_numeric_range())
        discovered.extend(self._probe_hostnames(common_hostnames))

        # Deduplicate by hostname
        unique = {}
        for printer in discovered:
            key = printer.get('hostname')
            if key and key not in unique:
                unique[key] = printer

        discovered = list(unique.values())
        self._log('info', f'Scan complete: discovered {len(discovered)} printer(s)')

        return discovered

    def _probe_numeric_range(self) -> List[Dict[str, Any]]:
        """Probe common private IP ranges for Moonraker"""
        
        discovered = []
        ranges = [
            ('192.168.0.', range(1, 255)),
            ('192.168.1.', range(1, 255)),
            ('10.0.0.', range(1, 255)),
        ]

        for subnet, host_range in ranges:
            self._log('debug', f'Probing subnet {subnet}*...')
            
            threads = []
            for i in host_range:
                host = f'{subnet}{i}'
                thread = threading.Thread(target=self._probe_single_host, args=(host, discovered))
                thread.daemon = True
                thread.start()
                threads.append(thread)

            # Wait for threads with timeout
            for thread in threads:
                thread.join(timeout=1)

        return discovered

    def _probe_hostnames(self, hostnames: List[str]) -> List[Dict[str, Any]]:
        """Probe common hostnames for Moonraker"""
        
        discovered = []
        for hostname in hostnames:
            self._probe_single_host(hostname, discovered)

        return discovered

    def _probe_single_host(self, host: str, results: List[Dict[str, Any]]):
        """Probe single host for Moonraker"""
        
        try:
            # Try HTTP first (most common)
            result = self.validate_connection(host, 7125, False, '')
            if result['success']:
                printer = result['data']
                printer['hostname'] = host
                printer['host'] = host
                printer['port'] = 7125
                printer['ssl'] = False
                results.append(printer)
                self._log('info', f'Found Moonraker at {host}:7125')
        except:
            pass

        try:
            # Try HTTPS
            result = self.validate_connection(host, 7125, True, '')
            if result['success']:
                printer = result['data']
                printer['hostname'] = host
                printer['host'] = host
                printer['port'] = 7125
                printer['ssl'] = True
                results.append(printer)
                self._log('info', f'Found Moonraker at {host}:7125 (HTTPS)')
        except:
            pass

    def validate_connection(self, host: str, port: int = 7125, ssl: bool = False, api_key: str = '') -> Dict[str, Any]:
        """Validate Moonraker connection via HTTP probe"""
        
        protocol = 'https' if ssl else 'http'
        url = f'{protocol}://{host}:{port}/api/server/info'

        try:
            self._log('debug', f'Probing {url}...')

            # Create request with timeout
            req = urllib.request.Request(url)
            if api_key:
                req.add_header('X-API-Key', api_key)

            # Disable SSL verification for self-signed certs
            if ssl:
                import ssl
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                response = urllib.request.urlopen(req, timeout=3, context=ctx)
            else:
                response = urllib.request.urlopen(req, timeout=3)

            data = json.loads(response.read().decode('utf-8'))
            result_data = data.get('result', {})

            # Extract server info
            server_info = result_data.get('server_info', {})

            return {
                'success': True,
                'data': {
                    'hostname': host,
                    'moonrakerVersion': server_info.get('moonraker_version', 'unknown'),
                    'klipperState': result_data.get('klippy_state', 'unknown'),
                    'klippyConnected': result_data.get('klippy_connected', False),
                    'websocketHealthy': True,  # If we got here via HTTP, WebSocket is likely ok
                    'apiKey': '',  # Don't return keys
                },
            }

        except urllib.error.HTTPError as e:
            if e.code == 401:
                self._log('debug', f'{host}: API Key required')
                return {
                    'success': False,
                    'error': 'API key required',
                }
            else:
                return {
                    'success': False,
                    'error': f'HTTP error: {e.code}',
                }
        except (urllib.error.URLError, socket.timeout) as e:
            return {
                'success': False,
                'error': 'Connection timeout or refused',
            }
        except Exception as e:
            return {
                'success': False,
                'error': str(e),
            }
