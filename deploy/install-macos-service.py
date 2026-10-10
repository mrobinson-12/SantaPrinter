#!/usr/bin/env python3
"""Install/update the Santa server as a per-user launchd service on macOS."""
import os
from pathlib import Path
import plistlib
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
python = root / '.venv' / 'bin' / 'python'
if sys.platform != 'darwin':
    raise SystemExit('This installer is for macOS. See deploy/santa-printer.service for Linux.')
if not python.exists() or not (root / '.env').exists():
    raise SystemExit('Install requirements and fill in .env before installing the service.')
label = 'local.santa-printer'
directory = root / 'runtime'
directory.mkdir(mode=0o700, exist_ok=True)
agent = Path.home() / 'Library' / 'LaunchAgents' / (label + '.plist')
agent.parent.mkdir(parents=True, exist_ok=True)
config = {'Label': label, 'ProgramArguments': [str(python), str(root / 'run_web.py')],
          'WorkingDirectory': str(root), 'RunAtLoad': True, 'KeepAlive': True,
          'ThrottleInterval': 10, 'ProcessType': 'Background', 'Umask': 0o077,
          'StandardOutPath': str(directory / 'service.log'),
          'StandardErrorPath': str(directory / 'service-error.log')}
agent.write_bytes(plistlib.dumps(config))
agent.chmod(0o600)
domain = 'gui/' + str(os.getuid())
subprocess.run(['launchctl', 'bootout', domain + '/' + label], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
subprocess.run(['launchctl', 'bootstrap', domain, str(agent)], check=True)
print('Installed ' + str(agent))
print('Server: http://127.0.0.1:8000. Prevent system sleep during the event.')
