"""
Firebase Realtime Database REST Client for reach-link agent
Uses Firebase REST API (stdlib-only, no external dependencies)
"""

import json
import logging
import time
from typing import Any, Dict, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)


class FirebaseRealtimeDatabaseClient:
    """
    Lightweight Firebase Realtime Database client using REST API
    Writes printer status and reads queued commands
    """

    def __init__(self, database_url: str, token: str, printer_id: str):
        """
        Initialize Firebase RTDB client
        
        Args:
            database_url: Firebase RTDB URL (e.g., https://project.firebaseio.com)
            token: Firebase authentication token or printer secret token
            printer_id: Unique printer ID
        """
        self.database_url = database_url.rstrip("/")
        self.token = token
        self.printer_id = printer_id
        self.last_status = {}

    def _make_request(
        self,
        path: str,
        method: str = "GET",
        data: Optional[Dict[str, Any]] = None,
        timeout: int = 10,
    ) -> Optional[Dict[str, Any]]:
        """
        Make HTTP request to Firebase REST API
        
        Args:
            path: RTDB path (e.g., /printers/{printerId}/status)
            method: HTTP method (GET, PUT, PATCH, DELETE)
            data: Data to send (for PUT/PATCH)
            timeout: Request timeout in seconds
            
        Returns:
            JSON response or None on error
        """
        # Firebase REST API endpoint
        url = f"{self.database_url}{path}.json?auth={self.token}"

        try:
            headers = {"Content-Type": "application/json"}
            body = None

            if data is not None:
                body = json.dumps(data).encode("utf-8")

            req = Request(url, data=body, headers=headers, method=method)
            with urlopen(req, timeout=timeout) as response:
                response_body = response.read().decode("utf-8")
                if response_body:
                    return json.loads(response_body)
                return None
        except HTTPError as e:
            if e.code == 401:
                logger.error("Firebase auth failed (401): Invalid token")
            elif e.code == 404:
                logger.debug(f"Firebase path not found (404): {path}")
            else:
                logger.error(f"Firebase HTTP error {e.code}: {e.reason}")
            return None
        except (URLError, OSError, json.JSONDecodeError) as e:
            logger.debug(f"Firebase request error: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error in Firebase request: {e}")
            return None

    def update_printer_status(
        self,
        state: str,
        temperatures: Dict[str, Any],
        job: Optional[Dict[str, Any]],
        system_health: Optional[Dict[str, Any]],
    ) -> bool:
        """
        Write printer status to RTDB at /printers/{printerId}/status
        Only writes if status has changed (optimization)
        
        Args:
            state: 'idle', 'printing', 'paused', 'error'
            temperatures: Temperature sensor data
            job: Current job info
            system_health: System health metrics
            
        Returns:
            True if write successful
        """
        current_status = {
            "lastHeartbeat": int(time.time() * 1000),
            "state": state,
            "temperatureSensors": temperatures or {},
            "currentJob": job,
            "systemHealth": system_health or {"errors": [], "warnings": []},
        }

        # Skip write if nothing changed (optimization)
        if current_status == self.last_status:
            return True

        path = f"/printers/{self.printer_id}/status"
        result = self._make_request(path, method="PATCH", data=current_status)

        if result is not None:
            self.last_status = current_status
            logger.debug(f"RTDB status updated: {state}")
            return True

        return False

    def get_queued_commands(self) -> Optional[Dict[str, Dict[str, Any]]]:
        """
        Read all queued commands from /printers/{printerId}/queue
        
        Returns:
            Dict of {commandId: commandData} or None on error
        """
        path = f"/printers/{self.printer_id}/queue"
        result = self._make_request(path, method="GET")

        if result is None:
            return None

        # Firebase returns null if path doesn't exist
        if result is None:
            return {}

        # Ensure result is a dict
        if isinstance(result, dict):
            return result

        return {}

    def dequeue_command(self, command_id: str) -> bool:
        """
        Delete a command from the queue after processing
        
        Args:
            command_id: ID of command to remove
            
        Returns:
            True if deletion successful
        """
        path = f"/printers/{self.printer_id}/queue/{command_id}"
        result = self._make_request(path, method="DELETE", data=None)

        if result is not None:
            logger.debug(f"RTDB command dequeued: {command_id}")
            return True

        return False

    def write_command_result(
        self,
        command_id: str,
        status: str,
        result: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
    ) -> bool:
        """
        Write command execution result to RTDB at /printers/{printerId}/commandResults/{commandId}
        
        Args:
            command_id: ID of the command
            status: 'pending', 'executing', 'completed', 'failed'
            result: Result data (if successful)
            error: Error message (if failed)
            
        Returns:
            True if write successful
        """
        path = f"/printers/{self.printer_id}/commandResults/{command_id}"
        data = {
            "status": status,
            "timestamp": int(time.time() * 1000),
            "result": result,
            "error": error,
        }

        result_response = self._make_request(path, method="PUT", data=data)

        if result_response is not None:
            logger.debug(f"RTDB command result written: {command_id} -> {status}")
            return True

        return False
