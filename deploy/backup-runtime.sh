#!/bin/sh
# Called as root by the generated backup unit. Stops writers for a consistent snapshot.
set -eu
app_root=$1
app_user=$2
backup_root=$3
systemctl stop santa-printer-watchdog.timer santa-printer-watchdog.service
trap 'systemctl start santa-printer.service santa-printer-watchdog.timer' EXIT HUP INT TERM
systemctl stop santa-printer.service
install -d -m 700 -o "$app_user" "$backup_root"
cd "$app_root"
snapshot="$backup_root/$(date -u +%Y%m%dT%H%M%SZ)"
runuser -u "$app_user" -- "$app_root/.venv/bin/python" "$app_root/santa_tools.py" backup "$snapshot"
runuser -u "$app_user" -- "$app_root/.venv/bin/python" "$app_root/santa_tools.py" prune-backups "$backup_root"
