#!/usr/bin/env python3
"""Offline maintenance and read-only diagnostics for Santa Printer."""
import argparse
from contextlib import contextmanager, closing
import fcntl
import getpass
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import time

ROOT = Path(__file__).resolve().parent


def load_environment():
    env_file = ROOT / '.env'
    if env_file.exists():
        for raw in env_file.read_text().splitlines():
            line = raw.strip()
            if line and not line.startswith('#'):
                key, separator, value = line.partition('=')
                if not separator or not key.strip().replace('_', '').isalnum():
                    raise ValueError('Invalid .env line; use KEY=value.')
                os.environ.setdefault(key.strip(), value.strip().strip('\"\''))

    for name in ('SANTA_DATA_DIR', 'SANTA_OWNER_PASSWORD_FILE', 'SANTA_LAN_CERT', 'SANTA_LAN_KEY'):
        value = os.getenv(name)
        if value:
            path = Path(value).expanduser()
            os.environ[name] = str(path if path.is_absolute() else (ROOT / path).resolve())


@contextmanager
def offline_lock(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / 'service.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ValueError('Stop santa-printer before maintenance; the service is running.') from None
        yield


class ReadOnlyStore:
    """Diagnostic commands never initialise or migrate a runtime."""
    def __init__(self, directory):
        self.directory = Path(directory)
        self.path = self.directory / 'santa.sqlite3'
        self.clock = time.time
        if not self.path.is_file():
            raise ValueError('No runtime database exists yet. Start the app before diagnostics.')

    @contextmanager
    def connect(self):
        with closing(sqlite3.connect('file:' + str(self.path) + '?mode=ro', uri=True)) as db:
            db.row_factory = sqlite3.Row
            yield db

    def settings(self):
        from santa_store import Store
        with self.connect() as db:
            return Store.settings_in(db)

    def metrics(self):
        with self.connect() as db:
            return {row['state']: row['n'] for row in db.execute('SELECT state,COUNT(*) n FROM jobs GROUP BY state')}

    def saved_health(self):
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='runtime_health'").fetchone():
                return {}
            row = db.execute('SELECT * FROM runtime_health WHERE id=1').fetchone()
            return dict(at=row['at'], **json.loads(row['value'])) if row else {}


def doctor(store, service=None, probe=True):
    import print_nt8360 as printer
    from santa_notifications import credentials_ready
    from santa_service import probe_printer
    checks = []
    def add(name, ok, detail):
        checks.append(dict(name=name, ok=bool(ok), detail=detail))
    cfg = store.settings()
    with store.connect() as db:
        add('Database', db.execute('PRAGMA quick_check').fetchone()[0] == 'ok', 'SQLite integrity check')
        code = db.execute('SELECT delivered FROM codes ORDER BY slot DESC LIMIT 1').fetchone()
    add('Storage', shutil.disk_usage(store.directory).free >= 100*1024*1024 and os.access(store.directory, os.W_OK), 'At least 100 MB free and data directory writable')
    try:
        printer.render_letter('Santa', ROOT / 'santa-receipt-border.png', font_size=cfg['font_size'])
        printer.render_letter((ROOT / 'santa-fallback.txt').read_text().format(name='friend'), ROOT / 'santa-receipt-border.png', font_size=cfg['font_size'])
        add('Receipt artwork, font and fallback', True, 'Rendering passed')
    except Exception:
        add('Receipt artwork, font and fallback', False, 'Check artwork, font availability and prepared-letter template')
    status = probe_printer(cfg['printer_host'], cfg['printer_port']) if probe else service.printer_status if service else {'status': 'Not checked'}
    add('Printer', status['status'] in ('Online', 'Paper low'), status['status'])
    add('Code delivery', bool(code and code['delivered']), 'Latest code delivered' if code and code['delivered'] else 'Check webhook connectivity')
    add('AI credentials', bool(os.getenv('OPENAI_API_KEY')), 'Configured; use the paid test to verify connectivity' if os.getenv('OPENAI_API_KEY') else 'Missing; prepared-letter fallback available' if cfg['fallback_enabled'] else 'Missing')
    add('Public administration', bool(os.getenv('CF_ACCESS_AUD') and os.getenv('ADMIN_EMAILS') and os.getenv('CF_ACCESS_TEAM_DOMAIN')), 'Cloudflare settings present; verify login through the public site')
    add('Notifications', credentials_ready() and bool(cfg['sms_recipients'] and cfg['sms_schedule'] and cfg['sms_tested']), 'Requires credentials, recipients, weekly hours and delivered test messages')
    add('LAN administration', bool(os.getenv('SANTA_LAN_HOST') and os.getenv('SANTA_LAN_ORIGIN') and Path(os.getenv('SANTA_OWNER_PASSWORD_FILE') or str(store.directory / 'owner-password.hash')).is_file()), 'Configure LAN HTTPS listener and owner password')
    return dict(checks=checks, physical_test_required=True)


def diagnostic_bundle(store, service=None):
    versions = {}
    for package in ('fastapi', 'uvicorn', 'Pillow', 'PyJWT'):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = 'missing'
    # Explicit allowlist: audit detail can contain names, IPs and webhook URLs.
    with store.connect() as db:
        events = [dict(r) for r in db.execute('SELECT at,stage,code FROM job_events ORDER BY id DESC LIMIT 200')]
        incidents = [dict(r) for r in db.execute('SELECT kind,opened,checked,resolved,acknowledged,affected FROM incidents ORDER BY id DESC LIMIT 100')]
        notifications = [dict(r) for r in db.execute('SELECT created,event,status,attempts FROM outbox ORDER BY id DESC LIMIT 50')]
        for item in notifications:
            if item['event'].startswith('test:'):
                item['event'] = 'test'
    health = service.health() if service else store.saved_health()
    safe_health = {k: health.get(k) for k in ('status', 'workers', 'oldest_waiting_age', 'last_sent', 'disk_free', 'ai_configured', 'last_ai_success', 'last_fallback', 'hardware_hold')}
    return json.dumps(dict(format=1, versions=versions, generated=store.clock(), health=safe_health,
                           metrics=store.metrics(), events=events, incidents=incidents, notifications=notifications), indent=2).encode()


def backup(directory, destination):
    """Caller holds offline lock. SQLite and artifacts form one stopped snapshot."""
    directory, destination = Path(directory).resolve(), Path(destination).resolve()
    if not (directory / 'santa.sqlite3').is_file():
        raise ValueError('No database exists to back up.')
    if destination == directory or directory in destination.parents or destination.exists():
        raise ValueError('Use a new backup directory outside the data directory.')
    destination.mkdir(parents=True, mode=0o700)
    os.chmod(destination, 0o700)
    try:
        with closing(sqlite3.connect(str(directory / 'santa.sqlite3'))) as source, closing(sqlite3.connect(str(destination / 'santa.sqlite3'))) as target:
            source.backup(target)
            target.execute('PRAGMA journal_mode=DELETE')
            if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('Backup database failed integrity check.')
            cfg = json.loads(target.execute('SELECT value FROM settings WHERE id=1').fetchone()[0])
        receipts = destination / 'receipts'
        receipts.mkdir(mode=0o700)
        with closing(sqlite3.connect(str(destination / 'santa.sqlite3'))) as db:
            retained = {row[0] for row in db.execute('SELECT id FROM jobs')}
        for path in (directory / 'receipts').glob('*'):
            if re.fullmatch(r'[0-9a-f]{32}\.(?:bin|png)', path.name) and path.stem in retained and path.is_file() and not path.is_symlink():
                shutil.copyfile(path, receipts / path.name)
        password = directory / 'owner-password.hash'
        if password.is_file() and not password.is_symlink():
            shutil.copyfile(password, destination / password.name)
        for name in ('cert.pem', 'key.pem'):
            tls = directory / 'lan-tls' / name
            if tls.is_file() and not tls.is_symlink():
                (destination / 'lan-tls').mkdir(exist_ok=True, mode=0o700)
                shutil.copyfile(tls, destination / 'lan-tls' / name)
        manifest = {}
        for path in destination.rglob('*'):
            if path.is_file():
                os.chmod(path, 0o600)
                manifest[str(path.relative_to(destination))] = hashlib.sha256(path.read_bytes()).hexdigest()
        (destination / 'manifest.json').write_text(json.dumps(dict(format=1, created=time.time(),
                         retention_hours=cfg.get('retention_hours', 24), files=manifest), indent=2))
        os.chmod(destination / 'manifest.json', 0o600)
        return destination
    except BaseException:
        shutil.rmtree(destination)
        raise


def restore(directory, snapshot):
    directory, snapshot = Path(directory).resolve(), Path(snapshot).resolve()
    manifest = json.loads((snapshot / 'manifest.json').read_text())
    if manifest.get('format') != 1 or 'santa.sqlite3' not in manifest.get('files', {}):
        raise ValueError('Unsupported or incomplete snapshot.')
    for name, digest in manifest['files'].items():
        if name not in ('santa.sqlite3', 'owner-password.hash', 'lan-tls/cert.pem', 'lan-tls/key.pem') and not re.fullmatch(r'receipts/[0-9a-f]{32}\.(?:bin|png)', name):
            raise ValueError('Invalid snapshot path.')
        source = snapshot / name
        if source.is_symlink() or snapshot not in source.resolve().parents or hashlib.sha256(source.read_bytes()).hexdigest() != digest:
            raise ValueError('Snapshot integrity check failed.')
    with closing(sqlite3.connect('file:' + str(snapshot / 'santa.sqlite3') + '?mode=ro', uri=True)) as db:
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('Snapshot database failed integrity check.')
    # Stage all files before changing the stopped runtime. Keep service.lock inode intact.
    with tempfile.TemporaryDirectory(dir=directory, prefix='restore-') as staging:
        staging = Path(staging)
        (staging / 'receipts').mkdir(mode=0o700)
        for name in manifest['files']:
            target = staging / name
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copyfile(snapshot / name, target)
            os.chmod(target, 0o600)
        old = directory / ('receipts-old-' + str(time.time_ns()))
        if (directory / 'receipts').exists():
            os.replace(directory / 'receipts', old)
        os.replace(staging / 'receipts', directory / 'receipts')
        for suffix in ('-wal', '-shm'):
            (directory / ('santa.sqlite3' + suffix)).unlink(missing_ok=True)
        os.replace(staging / 'santa.sqlite3', directory / 'santa.sqlite3')
        if (staging / 'owner-password.hash').exists():
            os.replace(staging / 'owner-password.hash', directory / 'owner-password.hash')
        if (staging / 'lan-tls').exists():
            (directory / 'lan-tls').mkdir(exist_ok=True, mode=0o700)
            for path in (staging / 'lan-tls').iterdir():
                os.replace(path, directory / 'lan-tls' / path.name)
        if old.exists():
            shutil.rmtree(old)


def prune_backups(root):
    for path in Path(root).iterdir():
        if not path.is_dir() or path.is_symlink():
            continue
        try:
            manifest = json.loads((path / 'manifest.json').read_text())
            if time.time() - manifest['created'] > manifest['retention_hours'] * 3600:
                shutil.rmtree(path)
        except (OSError, ValueError, KeyError):
            continue


def main():
    load_environment()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', default=os.getenv('SANTA_DATA_DIR', str(ROOT / 'runtime')))
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('doctor')
    commands.add_parser('bundle')
    commands.add_parser('reset-owner-password')
    commands.add_parser('backup').add_argument('destination')
    commands.add_parser('restore').add_argument('snapshot')
    commands.add_parser('prune-backups').add_argument('root')
    args = parser.parse_args()
    from santa_store import Store
    directory = Path(args.data_dir).expanduser().resolve()
    try:
        if args.command in ('doctor', 'bundle'):
            store = ReadOnlyStore(directory)
            if args.command == 'doctor':
                print(json.dumps(doctor(store), indent=2))
            else:
                print(diagnostic_bundle(store).decode())
        elif args.command == 'prune-backups':
            prune_backups(args.root)
        elif args.command == 'reset-owner-password':
            from santa_local_auth import hash_password
            password = getpass.getpass('New owner password: ')
            if password != getpass.getpass('Repeat password: '):
                raise ValueError('Passwords did not match.')
            target = Path(os.getenv('SANTA_OWNER_PASSWORD_FILE') or str(directory / 'owner-password.hash'))
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            Store.atomic_write(target, hash_password(password).encode())
            print('Owner password updated. Existing LAN sessions are invalidated.')
        else:
            with offline_lock(directory):
                if args.command == 'backup':
                    print(backup(directory, args.destination))
                else:
                    # Preserve a rollback snapshot before replacing anything.
                    safety = directory.parent / ('santa-before-restore-' + str(time.time_ns()))
                    if (directory / 'santa.sqlite3').exists():
                        backup(directory, safety)
                    restore(directory, args.snapshot)
                    print('Restored. Pre-restore snapshot: ' + str(safety))
    except (ValueError, OSError, sqlite3.Error) as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    main()
