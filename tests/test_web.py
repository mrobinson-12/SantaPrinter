import concurrent.futures
from contextlib import contextmanager
import json
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from fastapi.testclient import TestClient

from santa_store import Store, Rejected
from santa_service import Service, deliver_code, probe_printer
from santa_web import AccessVerifier, create_app


class Clock:
    def __init__(self, value=30000):
        self.value = value
    def __call__(self):
        return self.value


@pytest.fixture
def db(tmp_path):
    store = Store(tmp_path, Clock())
    store.update_settings({'ip_per_minute': 1000, 'ip_per_day': 10000}, 'test setup')
    return store


def submit(db, owner='visitor', idem='abcdefghijklmnop', code=None, message='I wish for a telescope.'):
    if code is None:
        code = db.codes()['current']['code']
    return db.submit(owner, '1.2.3.4', idem, 'Alex', message, code)


def generate_to_ready(db, monkeypatch, reply='Dear Alex,\n\nMay your Christmas be full of starlight and smiles.\n\nLove, Santa'):
    monkeypatch.setenv('OPENAI_API_KEY', 'test-key')
    calls = []
    def generate(message, key):
        calls.append(message)
        return reply
    sends = []
    service = Service(db, generate=generate, send=lambda *args, **kwargs: sends.append(args), probe=lambda *args: {'status': 'Online'})
    job = submit(db)
    claimed = db.claim()
    service.generate_job(claimed)
    return service, job, calls, sends


def test_initial_code_and_leading_zero(db, monkeypatch):
    monkeypatch.setattr('santa_store.secrets.randbelow', lambda _: 7)
    code = db.codes()
    assert code['current']['code'] == '0007'
    assert code['previous'] is None
    assert submit(db, code='0007')['state'] == 'waiting'


def test_current_previous_expiry_exact_boundaries(db):
    first = db.codes()['current']['code']
    db.clock.value += 299
    assert submit(db, owner='a', code=first)
    db.clock.value += 1
    assert db.codes()['previous']['code'] == first
    assert submit(db, owner='b', code=first)
    db.clock.value += 299
    assert submit(db, owner='c', code=first)
    db.clock.value += 1
    with pytest.raises(Rejected, match='incorrect or expired'):
        submit(db, owner='d', code=first)


def test_avoid_last_two_collisions(db, monkeypatch):
    sequence = iter([1, 1, 2, 2, 1, 3])
    monkeypatch.setattr('santa_store.secrets.randbelow', lambda _: next(sequence))
    assert db.codes()['current']['code'] == '0001'
    db.clock.value += 300
    assert db.codes()['current']['code'] == '0002'
    db.clock.value += 300
    assert db.codes()['current']['code'] == '0003'


def test_restart_preserves_code_and_skips_gaps(db):
    first = db.codes()['current']['code']
    again = Store(db.directory, db.clock)
    assert again.codes()['current']['code'] == first
    db.clock.value += 900
    assert again.codes()['previous'] is None
    with pytest.raises(Rejected):
        submit(again, code=first)
    assert again.delivery_due()['slot'] == int(db.clock() // 300)


def test_backward_clock_cannot_resurrect_code(db):
    old = db.codes()['current']['code']
    db.clock.value += 300
    db.rotate()
    db.clock.value -= 300
    assert db.codes()['clock_warning']
    assert db.delivery_due() is None
    with pytest.raises(Rejected):
        submit(db, code=old)


def test_delivery_retry_and_resend(db):
    row = db.delivery_due()
    for delay in (5, 15, 30, 60):
        db.record_delivery(row['slot'], False, 'HTTP 500')
        assert db.delivery_due() is None
        db.clock.value += delay
        assert db.delivery_due() is not None
    db.record_delivery(row['slot'], True, 'HTTP 200')
    assert db.delivery_due() is None
    db.resend('admin@example.com')
    assert db.delivery_due()['code'] == row['code']
    assert db.history('audit')[0]['action'] == 'resend code'


def test_webhook_outage_does_not_extend_codes(db):
    old = db.codes()['current']['code']
    slot = db.delivery_due()['slot']
    db.record_delivery(slot, False, 'timed out')
    db.clock.value += 600
    due = db.delivery_due()
    assert due['slot'] == slot + 2
    with pytest.raises(Rejected):
        submit(db, code=old)


def test_idempotency_survives_code_expiry(db):
    job = submit(db)
    db.clock.value += 600
    assert submit(db, code='xxxx')['id'] == job['id']
    with pytest.raises(Rejected, match='different letter'):
        submit(db, message='Something else.')
    assert sum(db.metrics().values()) == 1


def test_shared_code_and_atomic_limits(db):
    code = db.codes()['current']['code']
    db.update_settings({'global_per_minute': 3}, 'admin')
    def attempt(number):
        try:
            return submit(db, owner=str(number), idem=str(number).zfill(16), code=code)['id']
        except Rejected:
            return None
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(12)))
    assert len([r for r in results if r]) == 3
    assert db.metrics() == {'waiting': 3}


def test_atomic_duplicate(db):
    code = db.codes()['current']['code']
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: submit(db, code=code)['id'], range(8)))
    assert len(set(results)) == 1


def test_invalid_attempt_limit_is_persisted(db):
    valid = db.codes()['current']['code']
    invalid = '9999' if valid != '9999' else '9998'
    for _ in range(10):
        with pytest.raises(Rejected) as error:
            submit(db, code=invalid)
        assert error.value.status == 400
    with pytest.raises(Rejected) as error:
        submit(db, code=valid)
    assert error.value.status == 429
    assert db.metrics() == {}
    db.clock.value += 300
    assert submit(db)


def test_browser_cooldown_queue_capacity_and_pause(db):
    submit(db)
    with pytest.raises(Rejected, match='wait a moment'):
        submit(db, idem='secondidentifier1')
    db.update_settings({'queue_limit': 1}, 'admin')
    with pytest.raises(Rejected, match='queue is full'):
        submit(db, owner='other')
    db.update_settings({'paused_submissions': True}, 'admin')
    with pytest.raises(Rejected, match='short break'):
        submit(db, owner='another')


def test_generation_render_transmission(db, monkeypatch):
    service, job, calls, sends = generate_to_ready(db, monkeypatch)
    ready = db.get_job(job['id'])
    assert ready['state'] == 'ready'
    assert len(calls) == 1
    assert (db.files / (job['id'] + '.png')).is_file()
    data = (db.files / (job['id'] + '.bin')).read_bytes()
    assert data.startswith(b'\x1b@')
    service.send_job(db.claim())
    assert db.get_job(job['id'])['state'] == 'sent'
    assert len(sends) == 1
    assert sends[0][2] == data


@pytest.mark.parametrize('reply', ['', 'word ' * 61, 'a' * 601])
def test_bad_model_reply_never_sends(db, monkeypatch, reply):
    _, job, _, sends = generate_to_ready(db, monkeypatch, reply)
    assert db.get_job(job['id'])['state'] == 'failed'
    assert sends == []
    assert not (db.files / (job['id'] + '.bin')).exists()


def test_missing_key_and_model_errors_never_send(db, monkeypatch):
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    sends = []
    service = Service(db, generate=lambda *a: (_ for _ in ()).throw(ValueError('SECRET')), send=lambda *a: sends.append(a))
    job = submit(db)
    service.generate_job(db.claim())
    assert db.get_job(job['id'])['error'] == 'OPENAI_API_KEY is not configured.'
    monkeypatch.setenv('OPENAI_API_KEY', 'SECRET')
    db.action(job['id'], 'retry', 'admin')
    service.generate_job(db.claim())
    assert 'SECRET' not in db.get_job(job['id'])['error']
    assert sends == []


def test_render_failure_preserves_reply_without_retrying_model(db, monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'key')
    job = submit(db)
    calls = []
    def generate(*args):
        calls.append(1)
        return 'Dear Alex,\n' + ' \n' * 100 + '\nLove, Santa'  # too many blank lines to fit
    service = Service(db, generate=generate)
    service.generate_job(db.claim())
    assert db.get_job(job['id'])['state'] == 'failed'
    assert db.get_job(job['id'])['reply'] is not None
    db.action(job['id'], 'retry', 'admin')
    service.generate_job(db.claim())
    assert len(calls) == 1


def test_partial_send_pauses_and_requires_review(db, monkeypatch):
    service, job, _, _ = generate_to_ready(db, monkeypatch)
    def broken_send(*args, **kwargs):
        raise OSError('partial socket send')
    service.send = broken_send
    service.send_job(db.claim())
    assert db.get_job(job['id'])['state'] == 'uncertain'
    assert db.settings()['paused_printing']
    assert db.claim() is None
    with pytest.raises(Rejected):
        db.action(job['id'], 'reprint', 'admin')
    with pytest.raises(Rejected):
        db.update_settings({'paused_printing': False}, 'admin')
    db.action(job['id'], 'review', 'admin')
    db.update_settings({'paused_printing': False}, 'admin')
    new_id = db.action(job['id'], 'reprint', 'admin')
    assert db.get_job(new_id)['parent'] == job['id']
    assert (db.files / (new_id + '.bin')).read_bytes() == (db.files / (job['id'] + '.bin')).read_bytes()


def test_cancel_during_generation_cannot_send(db, monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'key')
    job = submit(db)
    def generate(*args):
        db.action(job['id'], 'cancel', 'admin')
        return 'Dear Alex,\n\nMerry Christmas.\n\nLove, Santa'
    service = Service(db, generate=generate)
    service.generate_job(db.claim())
    assert db.get_job(job['id'])['state'] == 'cancelled'
    assert db.claim() is None
    assert not (db.files / (job['id'] + '.bin')).exists()


def test_sending_cannot_be_cancelled(db, monkeypatch):
    _, job, _, _ = generate_to_ready(db, monkeypatch)
    db.claim()
    with pytest.raises(Rejected):
        db.action(job['id'], 'cancel', 'admin')


def test_restart_recovery_keeps_untouched_queue(db):
    generating = submit(db, owner='a')
    db.claim()
    sending = submit(db, owner='b')
    db.transition(sending['id'], 'waiting', 'sending')
    waiting = submit(db, owner='c')
    db.recover()
    assert db.get_job(generating['id'])['state'] == 'failed'
    assert db.get_job(sending['id'])['state'] == 'uncertain'
    assert db.get_job(waiting['id'])['state'] == 'waiting'
    assert db.settings()['paused_printing']


def test_print_pause_still_allows_generation(db, monkeypatch):
    db.update_settings({'paused_printing': True}, 'admin')
    service, job, _, sends = generate_to_ready(db, monkeypatch)
    assert db.get_job(job['id'])['state'] == 'ready'
    assert db.claim() is None
    assert sends == []


def test_retention_preserves_unfinished_jobs(db, monkeypatch):
    _, job, _, _ = generate_to_ready(db, monkeypatch)
    db.transition(job['id'], 'ready', 'sent')
    pending = submit(db, owner='b')
    db.clock.value += 86401
    db.cleanup()
    with pytest.raises(Rejected):
        db.get_job(job['id'])
    assert not (db.files / (job['id'] + '.png')).exists()
    assert db.get_job(pending['id'])['state'] == 'waiting'


class AllowTestAdmin:
    def verify(self, token):
        if token != 'test-admin':
            raise HTTPException(401, 'Authentication required.')
        return 'admin@example.com'


@pytest.fixture
def client(db, monkeypatch):
    monkeypatch.delenv('PUBLIC_ORIGIN', raising=False)
    monkeypatch.delenv('TRUST_CLOUDFLARE_HEADERS', raising=False)
    with TestClient(create_app(db, verifier=AllowTestAdmin(), start_background=False)) as client:
        yield client


def headers(admin=False):
    return {'Origin': 'http://testserver', **({'Cf-Access-Jwt-Assertion': 'test-admin'} if admin else {})}


def post_letter(client, db, **changes):
    body = dict(name='Alex', message='I wish for a telescope.', code=db.codes()['current']['code'], idempotency_key='abcdefghijklmnop')
    body.update(changes)
    return client.post('/api/letters', json=body, headers=headers())


def test_public_status_no_secrets_and_cookie(client, db):
    response = client.get('/api/status')
    assert set(response.json()) == {'visitor_title', 'visitor_intro', 'paused_submissions'}
    assert 'HttpOnly' in response.headers['set-cookie']
    assert 'SameSite=strict' in response.headers['set-cookie']
    assert response.headers['cache-control'] == 'no-store'


def test_invalid_submission_has_no_side_effects(client, db):
    valid = db.codes()['current']['code']
    response = post_letter(client, db, code='9999' if valid != '9999' else '9998')
    assert response.status_code == 400
    assert db.metrics() == {}
    assert list(db.files.iterdir()) == []


@pytest.mark.parametrize('changes', [dict(name='x' * 61), dict(message='x' * 1001), dict(message='   '), dict(code='１２３４'), dict(code='123'), dict(message='\x1bX'), dict(name='a\nb')])
def test_field_validation(client, db, changes):
    response = post_letter(client, db, **changes)
    assert response.status_code == 422
    assert set(response.json()) == {'detail', 'fields'}
    assert db.metrics() == {}


def test_job_and_preview_ownership(client, db, monkeypatch):
    response = post_letter(client, db)
    assert response.status_code == 202
    job_id = response.json()['id']
    monkeypatch.setenv('OPENAI_API_KEY', 'key')
    Service(db, generate=lambda *a: 'Dear Alex,\n\nMerry Christmas.\n\nLove, Santa').generate_job(db.claim())
    assert client.get('/api/letters/' + job_id).status_code == 200
    assert client.get('/api/letters/' + job_id + '/preview').headers['content-type'] == 'image/png'
    client.cookies.clear()
    assert client.get('/api/letters/' + job_id).status_code == 404
    assert client.get('/api/letters/' + job_id + '/preview').status_code == 404
    assert client.get('/api/letters/../../santa.sqlite3/preview').status_code == 404


@pytest.mark.parametrize('path', ['/admin', '/admin/', '/admin/api/overview', '/admin/api/jobs', '/admin/api/deliveries', '/admin/api/audit'])
def test_admin_authentication_every_route(client, path):
    assert client.get(path).status_code == 401
    assert client.get(path, headers=headers(True)).status_code == 200


def test_mutation_requires_same_origin(client, db):
    assert client.patch('/admin/api/settings', json={'paused_printing': True}, headers={'Cf-Access-Jwt-Assertion': 'test-admin', 'Origin': 'https://evil.example'}).status_code == 403
    assert client.post('/api/letters', json={}).status_code == 403
    assert db.settings()['paused_printing'] is False


def test_request_size_limit(client):
    response = client.post('/api/letters', content='x' * 17000, headers={**headers(), 'Content-Type': 'application/json'})
    assert response.status_code == 413


def test_admin_actions_settings_and_diagnostic_preview(client, db):
    response = client.patch('/admin/api/settings', json={'paused_submissions': True, 'retention_hours': 48}, headers=headers(True))
    assert response.status_code == 200
    assert client.patch('/admin/api/settings', json={'retention_hours': 169}, headers=headers(True)).status_code == 422
    assert client.patch('/admin/api/settings', json={'font_size': 19}, headers=headers(True)).status_code == 422
    assert client.patch('/admin/api/settings', json={'webhook_url': 'ftp://x'}, headers=headers(True)).status_code == 422
    preview = client.post('/admin/api/diagnostics/preview', json={}, headers=headers(True))
    assert preview.status_code == 200
    assert preview.headers['content-type'] == 'image/png'
    assert db.metrics() == {}
    diagnostic = client.post('/admin/api/diagnostics/test-print', json={}, headers=headers(True))
    assert diagnostic.status_code == 202
    assert db.metrics() == {'ready': 1}
    assert db.history('audit')[0]['actor'] == 'admin@example.com'


def test_production_origin_and_secure_cookie(db, monkeypatch):
    monkeypatch.setenv('PUBLIC_ORIGIN', 'https://santa.example.com')
    with TestClient(create_app(db, start_background=False)) as client:
        response = client.get('/')
        assert 'Secure' in response.headers['set-cookie']
        assert client.post('/api/letters', json={}, headers=headers()).status_code == 403
        assert client.post('/api/letters', json={}, headers={'Origin': 'https://santa.example.com'}).status_code == 422


def test_access_unconfigured_fails_closed(monkeypatch):
    for key in ('CF_ACCESS_TEAM_DOMAIN', 'CF_ACCESS_AUD', 'ADMIN_EMAILS'):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(HTTPException) as error:
        AccessVerifier().verify('anything')
    assert error.value.status_code == 503


@pytest.fixture
def signed_access(monkeypatch):
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setenv('CF_ACCESS_TEAM_DOMAIN', 'https://test.cloudflareaccess.com')
    monkeypatch.setenv('CF_ACCESS_AUD', 'expected-audience')
    monkeypatch.setenv('ADMIN_EMAILS', 'admin@example.com')
    verifier = AccessVerifier()
    class Key:
        key = private.public_key()
    verifier.client.get_signing_key_from_jwt = lambda _: Key()
    def sign(**changes):
        claims = dict(iss=verifier.domain, aud=verifier.audience, sub='user-id', email='admin@example.com', iat=int(time.time()), exp=int(time.time()) + 60)
        claims.update(changes)
        return jwt.encode(claims, private, algorithm='RS256', headers={'kid': 'test'})
    return verifier, sign, private


def test_signed_admin_token(signed_access):
    verifier, sign, _ = signed_access
    assert verifier.verify(sign()) == 'admin@example.com'


@pytest.mark.parametrize('claims', [dict(exp=1), dict(aud='wrong'), dict(iss='https://evil.example'), dict(email='visitor@example.com'), dict(iat=int(time.time()) + 100)])
def test_invalid_access_claims(signed_access, claims):
    verifier, sign, _ = signed_access
    with pytest.raises(HTTPException):
        verifier.verify(sign(**claims))


def test_forged_jwt_and_missing_token(signed_access):
    verifier, sign, _ = signed_access
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = jwt.encode(jwt.decode(sign(), options={'verify_signature': False}), other, algorithm='RS256', headers={'kid': 'test'})
    for value in ('', 'nonsense', token):
        with pytest.raises(HTTPException):
            verifier.verify(value)


def test_webhook_exact_payload_and_no_redirect():
    seen = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append((self.path, self.headers['Content-Type'], self.rfile.read(int(self.headers['Content-Length']))))
            self.send_response(302 if self.path == '/redirect' else 200)
            if self.path == '/redirect':
                self.send_header('Location', '/code')
            self.end_headers()
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = 'http://127.0.0.1:' + str(server.server_port)
        assert deliver_code(base + '/code', '0007')[0]
        assert seen[0][0:2] == ('/code', 'application/json')
        assert json.loads(seen[0][2]) == {'code': '0007'}
        assert not deliver_code(base + '/redirect', '0012')[0]
        assert len(seen) == 2
    finally:
        server.shutdown()
        server.server_close()


def test_tcp_status_probe_reads_four_commands():
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(1)
    seen = []
    def printer():
        with listener.accept()[0] as peer:
            for value in [0x16, 0x12, 0x12, 0x12]:
                seen.append(peer.recv(3))
                peer.sendall(bytes([value]))
        listener.close()
    port = listener.getsockname()[1]
    thread = threading.Thread(target=printer)
    thread.start()
    assert probe_printer('127.0.0.1', port)['status'] == 'Online'
    thread.join(2)
    assert seen == [b'\x10\x04' + bytes([n]) for n in range(1, 5)]


def test_retention_preserves_last_two_generated_codes_across_gaps(db, monkeypatch):
    sequence = iter([1, 2, 2, 1, 3])
    monkeypatch.setattr('santa_store.secrets.randbelow', lambda _: next(sequence))
    assert db.codes()['current']['code'] == '0001'
    db.clock.value += 900
    assert db.codes()['current']['code'] == '0002'
    db.cleanup()
    db.clock.value += 300
    assert db.codes()['current']['code'] == '0003'


def test_second_worker_is_rejected_by_os_lock(db, monkeypatch):
    monkeypatch.delenv('PUBLIC_ORIGIN', raising=False)
    service = Service(db, webhook=lambda *args: (True, 'HTTP 200'),
                      probe=lambda *args: {'status': 'Online'})
    with TestClient(create_app(db, service=service)):
        with pytest.raises(RuntimeError, match='Another Santa service'):
            with TestClient(create_app(db)):
                pass


def test_live_signed_token_gate_at_http_layer(db, signed_access, monkeypatch):
    monkeypatch.delenv('PUBLIC_ORIGIN', raising=False)
    verifier, sign, _ = signed_access
    with TestClient(create_app(db, verifier=verifier, start_background=False)) as client:
        assert client.get('/admin/api/overview', headers={'Cf-Access-Jwt-Assertion': sign()}).status_code == 200
        assert client.get('/admin/api/overview', headers={'Cf-Access-Jwt-Assertion': sign(aud='wrong')}).status_code == 401
        assert client.get('/admin/api/overview', headers={'Cf-Access-Authenticated-User-Email': 'admin@example.com'}).status_code == 401


def test_normalized_whitespace_cannot_bypass_length_limit(client, db):
    assert post_letter(client, db, message='a' + '\t' * 500 + 'b').status_code == 422


def test_ip_defaults_and_minute_boundary(tmp_path):
    db = Store(tmp_path, Clock())
    assert db.settings()['ip_per_minute'] == 5
    assert db.settings()['ip_per_day'] == 10
    db.update_settings({'browser_cooldown': 0}, 'admin')
    for n in range(5):
        submit(db, owner=str(n))
    with pytest.raises(Rejected, match='per-minute') as error:
        submit(db, owner='sixth')
    assert error.value.status == 429
    assert db.metrics() == {'waiting': 5}
    db.clock.value += 60
    assert submit(db, owner='sixth')


def test_ip_daily_window_survives_purge_and_restart(db):
    db.update_settings({'ip_per_day': 2, 'retention_hours': 1}, 'admin')
    first = submit(db, owner='one')
    db.transition(first['id'], 'waiting', 'failed')
    db.clock.value += 61
    second = submit(db, owner='two')
    db.transition(second['id'], 'waiting', 'failed')
    db.clock.value += 3601
    db.cleanup()
    assert db.metrics() == {}
    again = Store(db.directory, db.clock)
    with pytest.raises(Rejected, match='daily limit'):
        submit(again, owner='three')
    db.clock.value = 30000 + 86400
    assert submit(again, owner='three')


def test_wrong_code_counts_but_idempotent_retry_does_not(db):
    db.update_settings({'ip_per_minute': 2}, 'admin')
    code = db.codes()['current']['code']
    bad = '9999' if code != '9999' else '9998'
    with pytest.raises(Rejected, match='incorrect'):
        submit(db, owner='bad', code=bad)
    job = submit(db)
    assert submit(db)['id'] == job['id']
    assert db.ip_controls()['addresses'][0]['minute_count'] == 2
    with pytest.raises(Rejected, match='per-minute'):
        submit(db, owner='third')


def test_ip_override_block_unblock_and_canonical_ipv6(db):
    db.update_settings({'ip_per_minute': 1}, 'admin')
    db.set_ip_rule('::ffff:1.2.3.4', False, 2, 20, 'Custom visitor', 'admin')
    submit(db, owner='one')
    job = submit(db, owner='two')
    assert job['ip'] == '1.2.3.4'
    with pytest.raises(Rejected, match='per-minute'):
        submit(db, owner='three')
    db.set_ip_rule('1.2.3.4', True, 2, 20, '', 'admin')
    with pytest.raises(Rejected) as error:
        submit(db, owner='two')  # blocks even accepted-request replays
    assert error.value.status == 403
    db.delete_ip_rule('1.2.3.4', 'admin')
    assert submit(db, owner='two')['id'] == job['id']
    assert db.ip_controls()['addresses'][0]['effective_minute'] == 1
    assert Store.canonical_ip('2001:0db8::1') == '2001:db8::1'


def test_ip_limit_atomic_across_browsers(db):
    db.update_settings({'ip_per_minute': 5, 'global_per_minute': 100}, 'admin')
    code = db.codes()['current']['code']
    def attempt(n):
        try:
            return submit(db, owner=str(n), code=code)['id']
        except Rejected:
            return None
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(15)))
    assert len([r for r in results if r]) == 5


def test_ip_rule_validation_auth_and_actions(client, db):
    assert client.get('/admin/api/ip-controls').status_code == 401
    assert client.put('/admin/api/ip-controls', json={'ip': '1.2.3.4', 'blocked': True}, headers=headers()).status_code == 401
    for body in ({'ip': 'bad'}, {'ip': '1.2.3.4', 'per_day': 0}, {'ip': '1.2.3.4', 'blocked': 'yes'}):
        assert client.put('/admin/api/ip-controls', json=body, headers=headers(True)).status_code == 422
    body = {'ip': '127.0.0.1', 'blocked': True, 'note': 'Test block'}
    assert client.put('/admin/api/ip-controls', json=body, headers=headers(True)).status_code == 200
    assert post_letter(client, db).status_code == 403
    assert db.metrics() == {}
    assert client.post('/admin/api/ip-controls/remove', json={'ip': '127.0.0.1'}, headers=headers(True)).status_code == 200
    assert post_letter(client, db).status_code == 202
    assert db.history('audit')[0]['action'] == 'remove IP rule'


def test_trusted_cloudflare_ip_and_spoofed_forwarding(db, monkeypatch):
    monkeypatch.delenv('PUBLIC_ORIGIN', raising=False)
    monkeypatch.setenv('TRUST_CLOUDFLARE_HEADERS', 'true')
    with TestClient(create_app(db, start_background=False)) as client:
        body = dict(name='', message='A wish', code=db.codes()['current']['code'], idempotency_key='abcdefghijklmnop')
        reply = client.post('/api/letters', json=body, headers={**headers(), 'CF-Connecting-IP': '203.0.113.9'})
        assert db.get_job(reply.json()['id'])['ip'] == '203.0.113.9'
    monkeypatch.setenv('TRUST_CLOUDFLARE_HEADERS', 'false')
    with TestClient(create_app(db, start_background=False)) as client:
        reply = client.post('/api/letters', json=body, headers={**headers(), 'CF-Connecting-IP': '203.0.113.99', 'X-Forwarded-For': '203.0.113.99'})
        assert db.get_job(reply.json()['id'])['ip'] == '127.0.0.1'


def test_legacy_database_upgrade(tmp_path):
    import sqlite3
    with sqlite3.connect(tmp_path / 'santa.sqlite3') as connection:
        connection.execute('CREATE TABLE jobs(id TEXT PRIMARY KEY,owner TEXT,idem TEXT,fingerprint TEXT,name TEXT,message TEXT,state TEXT,reply TEXT,error TEXT,created REAL,updated REAL,parent TEXT,config TEXT)')
        connection.execute('CREATE TABLE settings(id INTEGER PRIMARY KEY,value TEXT)')
        connection.execute('INSERT INTO settings VALUES(1,?)', (json.dumps({'visitor_intro': 'Custom greeting'}),))
    db = Store(tmp_path, Clock())
    assert db.settings()['visitor_intro'] == 'Custom greeting'
    assert submit(db)['ip'] == '1.2.3.4'
    assert db.settings()['ip_per_day'] == 10
