import asyncio
import base64
import io
import json
import socket
import threading
import urllib.error
import urllib.parse

import pytest

from santa_service import Service, probe_printer
from santa_store import DEFAULTS, Rejected, Store
import santa_sms
from test_web import Clock, AllowTestAdmin, headers
from santa_web import create_app
from fastapi.testclient import TestClient


@pytest.fixture
def db(tmp_path):
    store = Store(tmp_path, Clock())
    store.update_settings({'sms_enabled': True, 'sms_from': '+61400000001',
                           'sms_to': ['+61400000002', '+61400000003']}, 'admin')
    return store


def observe(db, value):
    db.observe_paper('192.168.1.100', 9100, value)


def accepted(db):
    alert = db.claim_sms()
    assert alert
    db.finish_sms(alert, {'state': 'accepted', 'sid': 'SM' + '1' * 32, 'detail': 'Accepted by Twilio'})
    return alert


def test_one_per_recipient_outage_and_refill_rearm(db):
    observe(db, True)
    assert len(db.sms_history()) == 2
    assert db.sms_history()[0]['message'] == DEFAULTS['sms_message']
    accepted(db)
    accepted(db)
    for state in (True, None, True, None):
        observe(db, state)
    assert db.claim_sms() is None
    again = Store(db.directory, db.clock)
    again.recover_sms()
    observe(again, True)
    assert again.claim_sms() is None
    observe(db, False)
    observe(db, True)
    assert len(db.sms_history()) == 4
    assert db.claim_sms()


def test_unknown_low_and_offline_do_not_alert(db):
    service = Service(db, probe=lambda *args: {'status': 'Paper low', 'paper_empty': False})
    service.refresh_printer()
    assert db.sms_history() == []
    service.probe = lambda *args: {'status': 'Unknown', 'paper_empty': None}
    service.refresh_printer()
    assert db.sms_history() == []
    service.probe = lambda *args: {'status': 'Paper empty', 'paper_empty': True}
    service.refresh_printer()
    assert len(db.sms_history()) == 2


def test_disabled_or_unconfigured_alert_settings(db):
    db.update_settings({'sms_enabled': False}, 'admin')
    observe(db, True)
    assert db.sms_history() == []
    db.update_settings({'sms_enabled': True}, 'admin')
    observe(db, True)
    assert len(db.sms_history()) == 2
    db.update_settings({'sms_enabled': False}, 'admin')
    assert db.claim_sms() is None
    assert all(row['state'] == 'cancelled' for row in db.sms_history())
    with pytest.raises(Rejected):
        db.update_settings({'sms_enabled': True, 'sms_to': []}, 'admin')


def test_refill_cancels_pending_alerts_and_removed_recipient(db):
    observe(db, True)
    db.update_settings({'sms_to': ['+61400000003']}, 'admin')
    assert accepted(db)['recipient'] == '+61400000003'
    assert db.claim_sms() is None
    assert any(row['state'] == 'cancelled' for row in db.sms_history())
    observe(db, False)
    with pytest.raises(Rejected, match='ended'):
        db.retry_sms(next(row['id'] for row in db.sms_history() if row['state'] == 'cancelled'), 'admin')


def test_sms_restart_marks_inflight_uncertain_without_resend(db):
    observe(db, True)
    alert = db.claim_sms()
    db.recover_sms()
    row = next(row for row in db.sms_history() if row['id'] == alert['id'])
    assert row['state'] == 'uncertain'
    observe(db, True)
    remaining = db.claim_sms()
    assert remaining['id'] != alert['id']
    db.finish_sms(remaining, {'state': 'accepted', 'detail': 'Accepted', 'sid': None})
    assert db.claim_sms() is None
    db.retry_sms(alert['id'], 'admin')
    assert db.claim_sms()['id'] == alert['id']


def test_only_definite_429_failures_retry_at_most_three_times(db):
    db.update_settings({'sms_to': ['+61400000002']}, 'admin')
    observe(db, True)
    for attempt, delay in ((1, 30), (2, 120), (3, 120)):
        alert = db.claim_sms()
        assert alert['attempts'] == attempt
        db.finish_sms(alert, {'state': 'retry', 'detail': 'Twilio HTTP 429'})
        assert db.claim_sms() is None
        db.clock.value += delay
    assert db.sms_history()[0]['state'] == 'failed'
    assert db.claim_sms() is None


def test_live_incident_is_not_duplicated_after_history_retention(db):
    observe(db, True)
    accepted(db)
    accepted(db)
    db.clock.value += 8 * 86400
    db.cleanup()
    observe(db, True)
    assert len(db.sms_history()) == 2
    assert db.claim_sms() is None


def test_status_for_old_printer_cannot_create_alert(db):
    db.update_settings({'printer_host': '192.168.1.200'}, 'admin')
    observe(db, True)
    assert db.sms_history() == []


@pytest.fixture
def auth(monkeypatch):
    monkeypatch.setenv('TWILIO_ACCOUNT_SID', 'AC' + 'a' * 32)
    monkeypatch.setenv('TWILIO_API_KEY_SID', 'SK' + 'b' * 32)
    monkeypatch.setenv('TWILIO_API_KEY_SECRET', 'test-secret-never-log')
    monkeypatch.delenv('TWILIO_AUTH_TOKEN', raising=False)


class Response:
    status = 201
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def read(self, limit):
        return json.dumps({'sid': 'SM' + 'c' * 32, 'status': 'queued'}).encode()


def test_twilio_transport_url_auth_payload_unicode(monkeypatch, auth):
    seen = []
    class Opener:
        def open(self, request, timeout):
            seen.append(request)
            assert timeout == 10
            return Response()
    monkeypatch.setattr('santa_sms.urllib.request.build_opener', lambda *args: Opener())
    result = santa_sms.send_sms('+61400000001', '+61400000002', DEFAULTS['sms_message'])
    assert result['state'] == 'accepted'
    request = seen[0]
    assert request.full_url == 'https://api.twilio.com/2010-04-01/Accounts/AC' + 'a' * 32 + '/Messages.json'
    assert urllib.parse.parse_qs(request.data.decode()) == {'From': ['+61400000001'], 'To': ['+61400000002'], 'Body': [DEFAULTS['sms_message']]}
    credentials = base64.b64decode(request.headers['Authorization'].split()[1]).decode()
    assert credentials == 'SK' + 'b' * 32 + ':test-secret-never-log'
    assert 'test-secret' not in json.dumps(result)


@pytest.mark.parametrize('status,expected', [(401, 'failed'), (429, 'retry'), (500, 'uncertain')])
def test_twilio_errors_sanitized(monkeypatch, auth, status, expected):
    class Opener:
        def open(self, request, timeout):
            raise urllib.error.HTTPError(request.full_url, status, 'SECRET', {}, io.BytesIO(b'{"code": 20003, "message": "SECRET"}'))
    monkeypatch.setattr('santa_sms.urllib.request.build_opener', lambda *args: Opener())
    result = santa_sms.send_sms('+61400000001', '+61400000002', 'Paper empty')
    assert result['state'] == expected
    assert 'SECRET' not in json.dumps(result)


def test_timeout_is_uncertain_and_auth_token_fallback(monkeypatch, auth):
    class Opener:
        def open(self, *args, **kwargs):
            raise TimeoutError()
    monkeypatch.setattr('santa_sms.urllib.request.build_opener', lambda *args: Opener())
    assert santa_sms.send_sms('+61400000001', '+61400000002', 'Paper empty')['state'] == 'uncertain'
    monkeypatch.delenv('TWILIO_API_KEY_SID')
    monkeypatch.delenv('TWILIO_API_KEY_SECRET')
    monkeypatch.setenv('TWILIO_AUTH_TOKEN', 'token')
    assert santa_sms.credentials() == ('AC' + 'a' * 32, 'AC' + 'a' * 32, 'token')
    monkeypatch.setenv('TWILIO_ACCOUNT_SID', 'invalid')
    assert not santa_sms.configured()


def test_admin_settings_and_history_secret_free(db, monkeypatch, auth):
    monkeypatch.delenv('PUBLIC_ORIGIN', raising=False)
    with TestClient(create_app(db, verifier=AllowTestAdmin(), start_background=False)) as client:
        assert client.get('/admin/api/sms-alerts').status_code == 401
        overview = client.get('/admin/api/overview', headers=headers(True))
        assert overview.json()['twilio_ready']
        assert 'test-secret-never-log' not in overview.text
        for change in ({'sms_from': '0400000000'}, {'sms_to': ['bad']}, {'sms_message': '  '}, {'sms_to': ['+61400000002'] * 11}):
            assert client.patch('/admin/api/settings', json=change, headers=headers(True)).status_code == 422
        response = client.patch('/admin/api/settings', json={'sms_to': ['+61400000002', '+61400000002'], 'sms_message': 'Custom 🚨 paper alert'}, headers=headers(True))
        assert response.status_code == 200
        assert response.json()['sms_to'] == ['+61400000002']
        observe(db, True)
        alert = db.claim_sms()
        assert alert['message'] == 'Custom 🚨 paper alert'
        db.finish_sms(alert, {'state': 'failed', 'detail': 'HTTP 401'})
        assert client.post('/admin/api/sms-alerts/' + alert['id'] + '/retry', json={}, headers=headers(True)).status_code == 200
        assert db.history('audit')[0]['action'] == 'retry SMS alert'


def test_dispatcher_does_not_block_queue_and_sends_one_per_recipient(db, monkeypatch, auth):
    calls = []
    service = Service(db, sms=lambda sender, to, message: (calls.append(to) or {'state': 'accepted', 'detail': 'Accepted', 'sid': None}))
    observe(db, True)
    async def run():
        task = asyncio.create_task(service.sms_loop())
        for _ in range(30):
            if len(calls) == 2:
                break
            await asyncio.sleep(.1)
        service.stopping = True
        await task
    asyncio.run(run())
    assert sorted(calls) == ['+61400000002', '+61400000003']


@pytest.mark.parametrize('values,empty', [
    ([0x16, 0x12, 0x12, 0x12], False),
    ([0x16, 0x12, 0x12, 0x1e], False),
    ([0x1e, 0x12, 0x12, 0x12], False),
    ([0x16, 0x32, 0x12, 0x12], True),
    ([0x16, 0x12, 0x12, 0x72], True),
    ([0x00], None),
])
def test_hardware_paper_status_flags(values, empty):
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        def respond():
            with listener.accept()[0] as peer:
                for value in values:
                    peer.recv(3)
                    peer.sendall(bytes([value]))
        thread = threading.Thread(target=respond)
        thread.start()
        result = probe_printer('127.0.0.1', port)
        thread.join(3)
        assert not thread.is_alive()
        assert result['paper_empty'] is empty


def test_refill_cancels_all_unsent_alerts(db):
    observe(db, True)
    observe(db, False)
    assert db.claim_sms() is None
    assert all(row['state'] == 'cancelled' for row in db.sms_history())


def test_paper_monitor_runs_while_printing_paused(db):
    db.update_settings({'paused_printing': True}, 'admin')
    service = Service(db, probe=lambda *args: {'status': 'Paper empty', 'paper_empty': True})
    async def run():
        task = asyncio.create_task(service.paper_loop())
        for _ in range(20):
            if db.sms_history():
                break
            await asyncio.sleep(.05)
        service.stopping = True
        await task
    asyncio.run(run())
    assert len(db.sms_history()) == 2
