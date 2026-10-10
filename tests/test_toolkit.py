"""Failure-path tests with no real printer, AI calls, credentials or SMS."""
import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
import socket
import threading

from fastapi.testclient import TestClient
import pytest

import print_nt8360 as printer
import santa_reply
from santa_local_auth import LocalAuth, hash_password
from santa_notifications import (NotificationError, NotificationService, TIMEZONE,
                                 in_schedule, notification_fingerprint)
from santa_service import Service
from santa_store import Store, Rejected
from santa_tools import backup, restore, offline_lock, diagnostic_bundle, doctor
from santa_web import create_app


class Clock:
    def __init__(self):
        self.value = datetime(2026, 12, 18, 19, 0, tzinfo=TIMEZONE).timestamp()
    def __call__(self):
        return self.value


@pytest.fixture
def db(tmp_path):
    store = Store(tmp_path / 'runtime', Clock())
    store.update_settings({'browser_cooldown': 0, 'ip_per_minute': 1000, 'ip_per_day': 10000}, 'tests')
    return store


def submit(db, number=0, name='Alex'):
    return db.submit('owner' + str(number), '1.2.3.4', 'letteridentifier' + str(number), name,
                     'PRIVATE wish about a telescope.', db.codes()['current']['code'])


def prepared(db, monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'test-key')
    calls, sends = [], []
    service = Service(db, generate=lambda *a: calls.append(1) or 'Dear Alex,\n\nMerry Christmas! May your holidays sparkle with kindness and happy surprises.\n\nLove, Santa',
                      send=lambda *a, **k: sends.append(a), probe=lambda *a: {'status': 'Online'})
    job = submit(db)
    service.generate_job(db.claim('generation'))
    return service, db.get_job(job['id']), calls, sends


def test_fallback_no_key_and_no_visitor_content(db, monkeypatch):
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    service = Service(db, generate=lambda *a: pytest.fail('must not call AI'))
    job = submit(db)
    service.generate_job(db.claim('generation'))
    result = db.get_job(job['id'])
    assert result['state'] == 'ready' and result['fallback']
    assert 'Alex' in result['reply'] and 'PRIVATE' not in result['reply'] and 'telescope' not in result['reply']
    assert db.receipt_bytes(result).startswith(b'\x1b@')


def test_transient_generation_retries_then_fallback(db, monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'test-key')
    attempts = []
    def fail(*args):
        attempts.append(1)
        raise santa_reply.GenerationError('connection', True)
    service = Service(db, generate=fail)
    job = submit(db)
    for delay in (5, 15):
        service.generate_job(db.claim('generation'))
        assert db.get_job(job['id'])['state'] == 'waiting'
        assert db.claim('generation') is None
        db.clock.value += delay
    service.generate_job(db.claim('generation'))
    assert len(attempts) == 3
    assert db.get_job(job['id'])['fallback']
    assert [e['code'] for e in db.job_detail(job['id'])['events']].count('ai_connection') == 2


def test_auth_error_uses_immediate_fallback(db, monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'test-key')
    def fail(*args):
        raise santa_reply.GenerationError('authentication')
    service = Service(db, generate=fail)
    job = submit(db)
    service.generate_job(db.claim('generation'))
    assert db.get_job(job['id'])['fallback'] and db.get_job(job['id'])['generation_attempts'] == 1


def test_restart_reuses_saved_reply_without_call(db, monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'test-key')
    job = submit(db)
    db.claim('generation')
    db.transition(job['id'], 'generating', 'generating', reply='Dear Alex,\n\nHave a merry Christmas!\n\nLove, Santa')
    db.recover()
    service = Service(db, generate=lambda *a: pytest.fail('saved reply should be reused'))
    service.generate_job(db.claim('generation'))
    assert db.get_job(job['id'])['state'] == 'ready'


def test_restart_exhausted_attempts_falls_back_without_extra_call(db, monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'test-key')
    job = submit(db)
    with db.transaction() as conn:
        conn.execute("UPDATE jobs SET state='generating',generation_attempts=3 WHERE id=?", (job['id'],))
    db.recover()
    service = Service(db, generate=lambda *a: pytest.fail('retry allowance exhausted'))
    service.generate_job(db.claim('generation'))
    assert db.get_job(job['id'])['fallback']


@pytest.mark.parametrize('status', ['Unknown', 'Cover open', 'Paper empty', 'Offline', 'Printer error'])
def test_hardware_hold_needs_two_healthy_checks(db, status):
    statuses = iter([status, 'Online', 'Online'])
    service = Service(db, probe=lambda *a: {'status': next(statuses)})
    assert service.refresh_printer()['held']
    assert service.refresh_printer()['held']
    assert not service.refresh_printer()['held']
    assert not db.incidents()


def test_paper_low_does_not_stop_manual_pause(db):
    service = Service(db, probe=lambda *a: {'status': 'Paper low'})
    db.update_settings({'paused_printing': True}, 'owner')
    service.refresh_printer(); service.refresh_printer()
    assert not service.hardware_hold and db.settings()['paused_printing']


def test_known_pre_send_failure_retries_without_uncertainty(db, monkeypatch):
    service, job, _, _ = prepared(db, monkeypatch)
    def broken(*a, **k):
        raise printer.ConnectionFailure()
    service.send = broken
    service.send_job(db.claim('printing'))
    result = db.get_job(job['id'])
    assert result['state'] == 'ready' and result['next_attempt'] == db.clock() + 5
    assert not db.settings()['paused_printing']
    assert service.hardware_hold


def test_socket_connect_error_has_no_sent_bytes(monkeypatch):
    def broken(*a, **k):
        raise ConnectionRefusedError()
    monkeypatch.setattr(socket, 'create_connection', broken)
    with pytest.raises(printer.ConnectionFailure):
        printer.send_job('example', 9100, b'receipt', 1)


@pytest.mark.parametrize('damage', ['missing', 'corrupt', 'preview-missing', 'preview-corrupt'])
def test_artifact_damage_never_connects_and_can_render_saved_reply(db, monkeypatch, damage):
    service, job, calls, sends = prepared(db, monkeypatch)
    file = db.files / (job['id'] + ('.png' if damage.startswith('preview') else '.bin'))
    file.unlink() if damage.endswith('missing') else file.write_bytes(b'\x1b@tampered')
    service.send_job(db.claim('printing'))
    assert not sends and db.get_job(job['id'])['error_code'] == 'artifact'
    assert not db.settings()['paused_printing']
    db.action(job['id'], 'retry', 'owner')
    service.generate_job(db.claim('generation'))
    assert len(calls) == 1 and db.get_job(job['id'])['state'] == 'ready'


def test_destination_change_uses_saved_bytes(db, monkeypatch):
    service, job, _, sends = prepared(db, monkeypatch)
    data = db.receipt_bytes(job)
    db.update_settings({'printer_host': '192.168.1.200', 'printer_port': 9200}, 'owner')
    service.send_job(db.claim('printing'))
    assert sends[0][:2] == ('192.168.1.200', 9200) and sends[0][2] == data
    assert db.get_job(job['id'])['destination'] == '192.168.1.200:9200'


@pytest.mark.parametrize('outcome,state', [('appeared', 'sent'), ('review-cancel', 'cancelled'), ('missing', 'failed')])
def test_uncertain_outcomes_and_exact_replacement(db, monkeypatch, outcome, state):
    service, job, _, _ = prepared(db, monkeypatch)
    original = db.receipt_bytes(job)
    db.claim('printing'); db.uncertain(job['id'], 'uncertain')
    with pytest.raises(Rejected):
        db.action(job['id'], 'reprint', 'owner')
    result = db.action(job['id'], outcome, 'owner')
    assert db.get_job(job['id'])['state'] == state and db.settings()['paused_printing']
    if outcome == 'missing':
        assert result != job['id'] and db.receipt_bytes(db.get_job(result)) == original
        assert db.job_detail(job['id'])['reprints'] == [result]
    with pytest.raises(Rejected):
        db.action(job['id'], outcome, 'owner')


def test_workers_print_while_two_generators_are_blocked(tmp_path, monkeypatch):
    import time
    monkeypatch.setenv('OPENAI_API_KEY', 'test-key')
    db = Store(tmp_path)
    db.update_settings({'browser_cooldown': 0}, 'test')
    initial = submit(db)
    initial_service = Service(db, generate=lambda *a: 'Dear friend, Merry Christmas! Love, Santa')
    initial_service.generate_job(db.claim('generation'))
    submit(db, 1); submit(db, 2)
    release = threading.Event()
    generations, sends = [], []
    def generate(*a):
        generations.append(1)
        release.wait(5)
        return 'Dear friend, Merry Christmas! Love, Santa'
    service = Service(db, generate=generate, send=lambda *a, **k: sends.append(a),
                      probe=lambda *a: {'status': 'Online'}, webhook=lambda *a: (True, 'ok'))
    async def scenario():
        service.refresh_printer(); service.refresh_printer()
        service.start()
        try:
            deadline = time.monotonic() + 3
            while (len(generations) < 2 or not sends) and time.monotonic() < deadline:
                await asyncio.sleep(.05)
            assert len(generations) == 2 and len(sends) == 1
            assert db.get_job(initial['id'])['state'] == 'sent'
        finally:
            release.set()
            await service.stop()
    asyncio.run(scenario())


def test_queue_search_pagination_and_position(db):
    a = submit(db, 0, 'Alex'); b = submit(db, 1, 'Bob'); submit(db, 2, 'Alex')
    results = db.jobs(search='Alex', page=2, page_size=1)
    assert results['total'] == 2 and len(results['items']) == 1
    assert db.jobs(search=a['id'][:8])['total'] == 1
    assert db.jobs(search='%')['total'] == 0
    assert db.queue_position(db.get_job(b['id'])) in (1, 2, 3)


def test_bundle_has_no_secrets_or_personal_data(db, monkeypatch):
    service, job, _, _ = prepared(db, monkeypatch)
    db.update_settings({'printer_host': '192.168.44.99'}, 'SECRET_ACTOR')
    bundle = diagnostic_bundle(db, service).decode()
    for text in ('PRIVATE', 'telescope', 'Alex', '1.2.3.4', '192.168.44.99', 'SECRET_ACTOR', 'test-key', db.codes()['current']['code']):
        assert text not in bundle
    assert json.loads(bundle)['events']
    assert doctor(db, service, False)['physical_test_required']


def test_backup_restore_and_lock_protection(db, monkeypatch, tmp_path):
    _, job, _, _ = prepared(db, monkeypatch)
    saved = db.receipt_bytes(job)
    target = tmp_path / 'snapshots' / 'one'
    with offline_lock(db.directory):
        backup(db.directory, target)
        with pytest.raises(ValueError, match='service is running'):
            with offline_lock(db.directory):
                pass
    db.action(job['id'], 'cancel', 'owner')
    (db.files / (job['id'] + '.bin')).write_bytes(b'damage')
    with offline_lock(db.directory):
        restore(db.directory, target)
    restored = Store(db.directory, db.clock)
    assert restored.get_job(job['id'])['state'] == 'ready'
    assert restored.receipt_bytes(restored.get_job(job['id'])) == saved
    manifest = json.loads((target / 'manifest.json').read_text())
    manifest['files']['../../escape'] = 'a' * 64
    (target / 'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='Invalid snapshot path'):
        restore(db.directory, target)


def test_local_auth_expiry_throttle_and_password_reset(db, tmp_path):
    password = tmp_path / 'owner.hash'
    password.write_text(hash_password('a strong owner password'))
    auth = LocalAuth(db, password)
    token = auth.login('peer', 'a strong owner password')
    assert auth.verify(token) == 'local-owner'
    db.clock.value += 8*3600
    with pytest.raises(Exception) as error:
        auth.verify(token)
    assert error.value.status_code == 401
    token = auth.login('peer', 'a strong owner password')
    password.write_text(hash_password('a different owner password'))
    with pytest.raises(Exception):
        auth.verify(token)
    for _ in range(5):
        try:
            auth.login('bad-peer', 'wrong')
        except Exception:
            pass
    with pytest.raises(Exception) as error:
        auth.login('bad-peer', 'a different owner password')
    assert error.value.status_code == 429


class PublicVerifier:
    def verify(self, token):
        from fastapi import HTTPException
        if token != 'verified-public-token':
            raise HTTPException(401, 'Cloudflare required')
        return 'owner@example.com'


def test_lan_session_never_authenticates_public_listener(db, monkeypatch, tmp_path):
    file = tmp_path / 'owner.hash'; file.write_text(hash_password('a strong owner password'))
    monkeypatch.setenv('SANTA_OWNER_PASSWORD_FILE', str(file))
    monkeypatch.setenv('SANTA_LAN_ORIGIN', 'https://lan.test:8443')
    monkeypatch.setenv('PUBLIC_ORIGIN', 'https://public.test')
    service = Service(db, probe=lambda *a: {'status': 'Online'})
    with TestClient(create_app(db, service, start_background=False, lan=True), base_url='https://lan.test:8443') as local:
        assert local.get('/admin', follow_redirects=False).status_code == 303
        response = local.post('/login/session', json={'password': 'a strong owner password'}, headers={'Origin': 'https://lan.test:8443'})
        assert response.status_code == 200 and 'Secure' in response.headers['set-cookie']
        assert local.get('/admin/api/overview').status_code == 200
        token = local.cookies.get('santa_owner')
        assert local.get('/api/status').status_code == 404
        with TestClient(create_app(db, service, PublicVerifier(), start_background=False), base_url='https://public.test') as public:
            public.cookies.set('santa_owner', token)
            assert public.get('/admin/api/overview').status_code == 401
            assert public.get('/login').status_code == 404
            assert public.get('/admin', headers={'Cf-Access-Jwt-Assertion': 'verified-public-token'}).status_code == 200
        assert local.post('/admin/api/logout', json={}, headers={'Origin': 'https://public.test'}).status_code == 403
        assert local.post('/admin/api/logout', json={}, headers={'Origin': 'https://lan.test:8443'}).status_code == 200
        assert local.get('/admin/api/overview').status_code == 401


class SMS:
    def __init__(self):
        self.sent = []
        self.fail = None
    def send(self, recipient, body):
        if self.fail:
            raise self.fail
        self.sent.append((recipient, body))
        return dict(sid='SM' + format(len(self.sent), '032x'), status='queued')
    def status(self, sid):
        return dict(status='delivered')


@pytest.fixture
def notifications(db, monkeypatch):
    monkeypatch.setenv('TWILIO_ACCOUNT_SID', 'AC' + 'a'*32)
    monkeypatch.setenv('TWILIO_AUTH_TOKEN', 'test-only-token')
    monkeypatch.setenv('TWILIO_FROM', '+61412345678')
    db.update_settings({'sms_recipients': ['+61411111111', '+61422222222'],
                        'sms_schedule': [{'days': [4], 'start': '18:00', 'end': '23:00'}]}, 'owner')
    transport = SMS()
    return NotificationService(db, transport), transport


def activate(db, service):
    service.test('owner')
    assert not db.settings()['sms_tested']
    db.clock.value += 30
    service.tick()
    assert db.settings()['sms_tested']
    db.update_settings({'sms_enabled': True}, 'owner')


def test_sms_test_delivery_and_incident_dedup_reminder_recovery(db, notifications):
    service, transport = notifications
    activate(db, service)
    assert len(transport.sent) == 2
    db.incident('uncertain', 'A receipt needs physical inspection.', True, 1)
    service.tick(); service.tick()
    assert len(transport.sent) == 4
    db.clock.value += 899; service.tick()
    assert len(transport.sent) == 4
    db.clock.value += 1; service.tick()
    assert len(transport.sent) == 6
    db.acknowledge(db.incidents()[0]['id'], 'owner')
    db.clock.value += 900; service.tick()
    assert len(transport.sent) == 6
    db.incident('uncertain', 'A receipt needs physical inspection.', False)
    service.tick(); service.tick()
    assert len(transport.sent) == 8 and 'resolved' in transport.sent[-1][1]


def test_sms_threshold_and_hours(db, notifications):
    service, transport = notifications
    activate(db, service)
    db.incident('printer', 'Printer unavailable.', True)
    service.tick(); assert len(transport.sent) == 2
    db.clock.value += 60; service.tick(); assert len(transport.sent) == 4
    db.clock.value += 5*3600; service.tick(); assert len(transport.sent) == 4


def test_changed_schedule_requires_fresh_delivery_test(db, notifications):
    service, _ = notifications
    activate(db, service)
    db.update_settings({'sms_schedule': [{'days': [4], 'start': '18:00', 'end': '22:00'}]}, 'owner')
    service.tick()
    assert not db.settings()['sms_tested'] and not db.settings()['sms_enabled']
    with pytest.raises(Rejected):
        db.update_settings({'sms_enabled': True}, 'owner')


def test_sms_retry_and_ambiguous_submission_not_reposted(db, notifications):
    service, transport = notifications
    transport.fail = NotificationError(retryable=True)
    service.test('owner')
    assert all(r['status'] == 'pending' for r in service.history())
    db.clock.value += 5
    transport.fail = NotificationError(uncertain=True)
    service.tick()
    assert all(r['status'] == 'unknown' for r in service.history())
    db.clock.value += 1000
    transport.fail = None; service.tick()
    assert not transport.sent


def test_schedule_crosses_midnight_and_week_boundary():
    schedule = [{'days': [6], 'start': '20:00', 'end': '01:00'}]
    def at(day, hour, minute=0):
        return datetime(2026, 12, day, hour, minute, tzinfo=TIMEZONE).timestamp()
    assert in_schedule(at(20, 20), schedule)
    assert in_schedule(at(21, 0, 59), schedule)
    assert not in_schedule(at(21, 1), schedule)
    assert not in_schedule(at(19, 21), schedule)


def test_notifications_validation_and_private_apis(db):
    service = Service(db)
    with TestClient(create_app(db, service, PublicVerifier(), start_background=False)) as client:
        headers = {'Cf-Access-Jwt-Assertion': 'verified-public-token', 'Origin': 'http://testserver'}
        for changes in (
            {'sms_recipients': ['bad-number']},
            {'sms_schedule': [{'days': [7], 'start': '18:00', 'end': '22:00'}]},
            {'sms_schedule': [{'days': [1], 'start': '25:00', 'end': '22:00'}]},
            {'sms_schedule': [{'days': [1], 'start': '18:00', 'end': '18:00'}]},
            {'sms_tested': True},
        ):
            assert client.patch('/admin/api/settings', json=changes, headers=headers).status_code == 422
        for endpoint in ('diagnostics/checklist', 'diagnostics/bundle'):
            assert client.get('/admin/api/' + endpoint).status_code == 401
        assert client.get('/admin/api/jobs?page=0', headers=headers).status_code == 422


def test_recipients_change_cancels_queued_sms(db, notifications):
    service, transport = notifications
    activate(db, service)
    transport.fail = NotificationError(retryable=True)
    db.incident('uncertain', 'A receipt needs physical inspection.', True)
    service.tick()
    db.update_settings({'sms_recipients': ['+61433333333']}, 'owner')
    transport.fail = None
    db.clock.value += 30
    service.tick()
    assert len(transport.sent) == 2
    assert not db.settings()['sms_tested']
    assert any(row['status'] == 'cancelled' for row in service.history())


def test_readonly_doctor_does_not_upgrade_legacy_database(tmp_path):
    import sqlite3
    from santa_tools import ReadOnlyStore
    directory = tmp_path / 'legacy'; directory.mkdir()
    path = directory / 'santa.sqlite3'
    with sqlite3.connect(str(path)) as conn:
        conn.executescript('CREATE TABLE settings(id INTEGER PRIMARY KEY,value TEXT); CREATE TABLE codes(delivered INTEGER,slot INTEGER); CREATE TABLE jobs(state TEXT);')
        conn.execute('INSERT INTO settings VALUES (1,?)', (json.dumps({}),))
    store = ReadOnlyStore(directory)
    service = type('Status', (), {'printer_status': {'status': 'Online'}})()
    doctor(store, service, False)
    with sqlite3.connect(str(path)) as conn:
        assert [row[1] for row in conn.execute('PRAGMA table_info(jobs)')] == ['state']
    assert not (tmp_path / 'santa-backups').exists()


def test_watchdog_health_endpoint_rejects_stale_workers(db):
    service = Service(db)
    service.heartbeats = {'printing': db.clock()}
    with TestClient(create_app(db, service, PublicVerifier(), start_background=False)) as client:
        assert client.get('/healthz').json() == {'ok': True}
        db.clock.value += 61
        assert client.get('/healthz').status_code == 503


def test_preupgrade_snapshot_and_saved_receipt_hashes(tmp_path):
    import sqlite3
    directory = tmp_path / 'runtime'; directory.mkdir(); (directory / 'receipts').mkdir()
    identifier = 'a' * 32
    bin_data, png_data = b'\x1b@legacy receipt', b'legacy preview'
    (directory / 'receipts' / (identifier + '.bin')).write_bytes(bin_data)
    (directory / 'receipts' / (identifier + '.png')).write_bytes(png_data)
    with sqlite3.connect(str(directory / 'santa.sqlite3')) as conn:
        conn.executescript('CREATE TABLE settings(id INTEGER PRIMARY KEY,value TEXT); CREATE TABLE jobs(id TEXT PRIMARY KEY,owner TEXT,idem TEXT,fingerprint TEXT,name TEXT,message TEXT,state TEXT,reply TEXT,error TEXT,created REAL,updated REAL,parent TEXT,config TEXT);')
        conn.execute('INSERT INTO settings VALUES (1,?)', (json.dumps({}),))
        conn.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?,'sent',?,NULL,1,1,NULL,?)", (identifier, 'o', 'i', 'f', 'Alex', 'private', 'saved reply', '{}'))
    store = Store(directory)
    assert store.receipt_bytes(store.get_job(identifier)) == bin_data
    snapshots = list((tmp_path / 'santa-backups').iterdir())
    assert len(snapshots) == 1
    with sqlite3.connect(str(snapshots[0] / 'santa.sqlite3')) as conn:
        assert 'preview_hash' not in {row[1] for row in conn.execute('PRAGMA table_info(jobs)')}


def test_dual_listener_supervisor_https_and_singleton(tmp_path):
    """Exercise the real supervisor on two ephemeral loopback ports, without hardware."""
    import ssl
    import subprocess
    import sys
    import time
    import urllib.error
    import urllib.request
    root = Path(__file__).resolve().parents[1]
    def free_port():
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            return listener.getsockname()[1]
    public_port, lan_port = free_port(), free_port()
    directory = tmp_path / 'runtime'
    store = Store(directory)
    store.update_settings({'printer_host': '127.0.0.1', 'printer_port': free_port(), 'webhook_url': 'http://127.0.0.1:' + str(free_port()) + '/code'}, 'test')
    password = directory / 'owner-password.hash'; password.write_text(hash_password('a strong owner password'))
    tls = directory / 'lan-tls'
    subprocess.run([sys.executable, str(root / 'deploy/configure-lan.py'), '--host', '127.0.0.1', '--origin', 'https://127.0.0.1:' + str(lan_port), '--output', str(tls)], check=True, capture_output=True)
    env = dict(os.environ, SANTA_DATA_DIR=str(directory), SANTA_PUBLIC_PORT=str(public_port),
               SANTA_LAN_HOST='127.0.0.1', SANTA_LAN_PORT=str(lan_port), SANTA_LAN_ORIGIN='https://127.0.0.1:' + str(lan_port),
               SANTA_LAN_CERT=str(tls / 'cert.pem'), SANTA_LAN_KEY=str(tls / 'key.pem'), SANTA_OWNER_PASSWORD_FILE=str(password),
               PUBLIC_ORIGIN='', OPENAI_API_KEY='', CF_ACCESS_AUD='', TWILIO_ACCOUNT_SID='', TWILIO_AUTH_TOKEN='', TWILIO_FROM='')
    process = subprocess.Popen([sys.executable, str(root / 'run_web.py')], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 10
        while True:
            try:
                with urllib.request.urlopen('http://127.0.0.1:' + str(public_port) + '/healthz', timeout=1) as response:
                    assert json.load(response)['ok']
                break
            except (OSError, urllib.error.URLError):
                if process.poll() is not None or time.monotonic() > deadline:
                    raise AssertionError('Supervisor failed: ' + process.stderr.read().decode() if process.poll() is not None else 'Supervisor startup timed out')
                time.sleep(.1)
        context = ssl.create_default_context(cafile=str(tls / 'cert.pem'))
        origin = env['SANTA_LAN_ORIGIN']
        request = urllib.request.Request(origin + '/login/session', data=json.dumps({'password': 'a strong owner password'}).encode(), headers={'Origin': origin, 'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, context=context, timeout=5) as response:
            cookie = response.headers['Set-Cookie'].split(';')[0]
        with urllib.request.urlopen(urllib.request.Request(origin + '/admin/api/overview', headers={'Cookie': cookie}), context=context, timeout=5) as response:
            assert json.load(response)['actor'] == 'local-owner'
        with pytest.raises(ValueError, match='service is running'):
            with offline_lock(directory):
                pass
    finally:
        process.terminate()
        process.communicate(timeout=15)
        assert process.returncode == 0
