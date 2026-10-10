#!/usr/bin/env python3
"""Independent systemd timer: detect an unhealthy/unavailable app, then drain SMS alerts."""
import fcntl
import json
import os
from pathlib import Path
import urllib.request

from santa_tools import ROOT, load_environment
from santa_store import Store
from santa_notifications import NotificationService


def check(url='http://127.0.0.1:8000/healthz'):
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status == 200 and json.load(response).get('ok') is True
    except Exception:
        return False


def main():
    load_environment()
    directory = Path(os.getenv('SANTA_DATA_DIR', str(ROOT / 'runtime'))).expanduser().resolve()
    # The main app must initialise/migrate its DB before the independent watchdog starts.
    if not (directory / 'santa.sqlite3').is_file():
        return
    with (directory / 'watchdog.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return
        store = Store(directory)
        metrics = store.metrics()
        affected = sum(metrics.get(state, 0) for state in ('waiting', 'generating', 'ready', 'sending', 'uncertain'))
        store.incident('app', 'Santa Printer app is unavailable or unhealthy. Check the Linux service.', not check(), affected)
        NotificationService(store).tick()


if __name__ == '__main__':
    main()
