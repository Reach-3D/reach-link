"""
SSH Executor: Handles SSH connections and reach-link installation.
"""

import os
import tempfile
from typing import Any, Callable, Dict, Optional
from paramiko import AutoAddPolicy, SSHClient
from paramiko.ssh_exception import SSHException


class SSHResult:
    """Result of SSH operation"""
    def __init__(self, success: bool, message: str = '', data: Optional[Dict[str, Any]] = None, error: str = ''):
        self.success = success
        self.message = message
        self.data = data or {}
        self.error = error

    def to_dict(self) -> Dict[str, Any]:
        result = {
            'success': self.success,
            'message': self.message,
            'data': self.data,
        }
        if self.error:
            result['error'] = self.error
        return result


class SSHExecutor:
    """Executes SSH commands for reach-link installation"""
    
    def __init__(self, log_callback: Optional[Callable[[str, str, str], None]] = None):
        self.log_callback = log_callback or (lambda level, source, msg: None)

    def _log(self, level: str, message: str, source: str = 'install'):
        """Log a message"""
        self.log_callback(level, source, message)

    def install_reach_link(
        self,
        printer_id: str,
        relay_url: str,
        reach_link_token: str,
        ssh_host: str,
        ssh_port: int,
        ssh_username: str,
        ssh_auth_method: str,
        ssh_password: Optional[str] = None,
        ssh_private_key: Optional[str] = None,
        ssh_sudo_password: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Install reach-link on printer via SSH"""
        
        self._log('info', f'Starting SSH connection to {ssh_username}@{ssh_host}:{ssh_port}')

        try:
            # Create SSH client
            ssh = SSHClient()
            ssh.set_missing_host_key_policy(AutoAddPolicy())

            # Connect
            try:
                if ssh_auth_method == 'password':
                    self._log('info', 'Authenticating with password...')
                    ssh.connect(
                        hostname=ssh_host,
                        port=ssh_port,
                        username=ssh_username,
                        password=ssh_password,
                        timeout=10,
                        allow_agent=False,
                        look_for_keys=False,
                    )
                else:  # privateKey
                    self._log('info', 'Authenticating with private key...')
                    
                    # Save private key to temp file
                    with tempfile.NamedTemporaryFile(mode='w', suffix='.pem', delete=False) as f:
                        f.write(ssh_private_key)
                        key_path = f.name

                    try:
                        ssh.connect(
                            hostname=ssh_host,
                            port=ssh_port,
                            username=ssh_username,
                            key_filename=key_path,
                            timeout=10,
                            allow_agent=False,
                            look_for_keys=False,
                        )
                    finally:
                        if os.path.exists(key_path):
                            os.remove(key_path)

                self._log('info', f'Successfully connected to {ssh_host}')
            except SSHException as e:
                self._log('error', f'SSH connection failed: {str(e)}')
                return {
                    'success': False,
                    'error': f'SSH connection failed: {str(e)}',
                }

            # Generate and execute installation script
            try:
                # Build environment variables
                env_vars = {
                    'REACH_LINK_PRINTER_ID': printer_id,
                    'REACH_LINK_RELAY': relay_url,
                    'REACH_LINK_TOKEN': reach_link_token,
                }

                # Create install script
                script = self._generate_install_script(env_vars)
                self._log('debug', 'Generated installation script (500+ lines)')

                # Upload script to printer
                self._log('info', 'Uploading installation script to printer...')
                sftp = ssh.open_sftp()
                remote_script = '/tmp/reach-link-install.sh'
                
                with tempfile.NamedTemporaryFile(mode='w', suffix='.sh', delete=False) as f:
                    f.write(script)
                    local_script = f.name

                try:
                    sftp.put(local_script, remote_script)
                    self._log('info', f'Script uploaded to {remote_script}')
                finally:
                    if os.path.exists(local_script):
                        os.remove(local_script)

                sftp.close()

                # Execute installation script
                self._log('info', 'Executing installation script on printer...')
                
                # Make script executable
                ssh.exec_command(f'chmod +x {remote_script}')

                # Execute with optional sudo
                if ssh_sudo_password:
                    cmd = f'echo "{ssh_sudo_password}" | sudo -S bash {remote_script}'
                else:
                    cmd = f'bash {remote_script}'

                stdin, stdout, stderr = ssh.exec_command(cmd)
                exit_status = stdout.channel.recv_exit_status()

                # Capture output
                output_lines = []
                for line in stdout:
                    line = line.strip()
                    if line:
                        self._log('info', line)
                        output_lines.append(line)

                for line in stderr:
                    line = line.strip()
                    if line:
                        self._log('error', line)
                        output_lines.append(line)

                # Cleanup remote script
                try:
                    ssh.exec_command(f'rm {remote_script}')
                except:
                    pass

                if exit_status == 0:
                    self._log('info', 'Installation completed successfully!')
                    return {
                        'success': True,
                        'message': 'reach-link installed successfully',
                        'data': {
                            'exitCode': exit_status,
                            'output': '\n'.join(output_lines),
                        },
                    }
                else:
                    self._log('error', f'Installation failed with exit code {exit_status}')
                    return {
                        'success': False,
                        'error': f'Installation script failed with exit code {exit_status}',
                        'data': {
                            'exitCode': exit_status,
                            'output': '\n'.join(output_lines),
                        },
                    }

            except Exception as e:
                self._log('error', f'Script execution failed: {str(e)}')
                return {
                    'success': False,
                    'error': f'Script execution failed: {str(e)}',
                }
            finally:
                try:
                    ssh.close()
                except:
                    pass

        except Exception as e:
            self._log('error', f'SSH operation failed: {str(e)}')
            return {
                'success': False,
                'error': f'SSH operation failed: {str(e)}',
            }

    def _generate_install_script(self, env_vars: Dict[str, str]) -> str:
        """Generate shell script for reach-link installation"""
        
        # Build environment variable exports
        env_exports = '\n'.join([
            f'export {key}="{value}"'
            for key, value in env_vars.items()
        ])

        script = f"""#!/bin/bash
set -e

# Reach-Link Installation Script
# Auto-generated for printer setup

{env_exports}

echo "Reach-Link Installation Starting..."
echo "Printer ID: $REACH_LINK_PRINTER_ID"
echo "Relay URL: $REACH_LINK_RELAY"

# Detect OS
if [ -f /etc/os-release ]; then
    . /etc/os-release
    OS=$ID
else
    OS=$(uname -s)
fi

echo "Detected OS: $OS"

# Create installation directory
INSTALL_DIR="/opt/reach-link"
mkdir -p $INSTALL_DIR
echo "Created installation directory: $INSTALL_DIR"

# Download reach-link agent from GitHub
AGENT_URL="https://github.com/Reach-3D/reach-link/releases/download/v1.0.5/reach-link.py"
echo "Downloading reach-link agent from $AGENT_URL..."
curl -f -L -o $INSTALL_DIR/reach-link.py $AGENT_URL
chmod +x $INSTALL_DIR/reach-link.py
echo "Downloaded reach-link agent"

# Create systemd service file if on Linux
if [ "$OS" = "linux" ] || [ "$OS" = "debian" ]; then
    echo "Creating systemd service..."
    cat > /etc/systemd/system/reach-link.service <<EOF
[Unit]
Description=Reach-Link Remote Access Agent
After=network.target

[Service]
Type=simple
User=pi
WorkingDirectory=$INSTALL_DIR
Environment="REACH_LINK_PRINTER_ID=$REACH_LINK_PRINTER_ID"
Environment="REACH_LINK_RELAY=$REACH_LINK_RELAY"
Environment="REACH_LINK_TOKEN=$REACH_LINK_TOKEN"
Environment="REACH_LINK_HEARTBEAT_INTERVAL=30"
Environment="REACH_LINK_TELEMETRY_INTERVAL=10"
Environment="REACH_LINK_COMMAND_POLL_INTERVAL=4"
ExecStart=/usr/bin/python3 $INSTALL_DIR/reach-link.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
    
    chmod 644 /etc/systemd/system/reach-link.service
    systemctl daemon-reload
    systemctl enable reach-link.service
    systemctl start reach-link.service
    echo "Service created and started"
    systemctl status reach-link.service || true
else
    echo "Supervisor installation not yet implemented for this OS"
fi

echo "Reach-Link Installation Complete!"
echo "Service should be running and connecting to $REACH_LINK_RELAY"
"""
        return script
