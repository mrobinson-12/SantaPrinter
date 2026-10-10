#!/usr/bin/env python3
"""Render deployment units for review; installing requires --install as root."""
import argparse
import os
from pathlib import Path
import re
import pwd
import grp
import subprocess

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--user', required=True)
parser.add_argument('--backup-root', required=True)
parser.add_argument('--output', default=str(root / 'runtime' / 'systemd'))
parser.add_argument('--install', action='store_true')
args = parser.parse_args()
if not re.fullmatch('[a-z_][a-z0-9_-]*', args.user) or any(not re.fullmatch(r'/[A-Za-z0-9_./-]+', p) for p in (str(root), args.backup_root)):
    parser.error('Use a Linux username and absolute paths without whitespace or shell metacharacters.')
if args.install and os.geteuid() != 0:
    parser.error('--install requires root')
account = pwd.getpwnam(args.user) if args.install else None
if args.install:
    runtime = root / 'runtime'
    runtime.mkdir(exist_ok=True, mode=0o700)
    os.chown(runtime, account.pw_uid, account.pw_gid)
output = Path('/etc/systemd/system') if args.install else Path(args.output)
output.mkdir(parents=True, exist_ok=True)
units = list((root / 'deploy').glob('santa-printer*.service')) + list((root / 'deploy').glob('santa-printer*.timer'))
for source in units:
    text = source.read_text().replace('/opt/santa-backups', args.backup_root).replace('/opt/SantaPrinter', str(root)).replace('User=santa', 'User=' + args.user).replace(str(root) + ' santa ', str(root) + ' ' + args.user + ' ')
    (output / source.name).write_text(text)
service_group = grp.getgrgid(account.pw_gid).gr_name if account else args.user
rotation = (root / 'deploy' / 'santa-printer.logrotate').read_text().replace('/opt/SantaPrinter', str(root)).replace('su santa santa', 'su ' + args.user + ' ' + service_group)
rotation_target = Path('/etc/logrotate.d/santa-printer') if args.install else output / 'santa-printer.logrotate'
rotation_target.write_text(rotation)
print('Units written to ' + str(output))
if args.install:
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    subprocess.run(['systemctl', 'enable', '--now', 'santa-printer.service', 'santa-printer-watchdog.timer', 'santa-printer-backup.timer'], check=True)
