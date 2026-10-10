"""Real-browser acceptance checks; isolated temporary DB and no real hardware/API."""
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import HTTPException
from playwright.sync_api import sync_playwright, expect
import uvicorn
from santa_service import Service
from santa_store import Store
from santa_web import create_app

ROOT = Path(__file__).resolve().parents[1]


class TestAccess:
    def verify(self, token):
        if token != 'browser-test-only':
            raise HTTPException(401, 'Authentication required.')
        return 'event-team@example.com'


def main():
    for key in ('PUBLIC_ORIGIN', 'TRUST_CLOUDFLARE_HEADERS'):
        os.environ.pop(key, None)
    os.environ['OPENAI_API_KEY'] = 'mock-only-no-network'
    screenshots = ROOT / 'runtime' / 'qa'
    screenshots.mkdir(parents=True, exist_ok=True)
    sends, deliveries, errors = [], [], []
    probe_status = ['Online']
    with tempfile.TemporaryDirectory() as directory:
        db = Store(directory)
        db.update_settings({'browser_cooldown': 0, 'global_per_minute': 100}, 'test setup')
        fail_next = [False]
        def send(*args, **kwargs):
            sends.append(args)
            if fail_next[0]:
                fail_next[0] = False
                raise OSError('simulated partial send')
        service = Service(db,
            generate=lambda *args: 'Dear Alex,\n\nYour wish for a telescope has the elves looking up from their wrapping! May your Christmas be filled with starlight, happy discoveries, and a little North Pole magic.\n\nLove, Santa',
            send=send,
            webhook=lambda url, code: (deliveries.append({'code': code}) or True, 'HTTP 200'),
            probe=lambda *args: {'status': probe_status[0], 'checked': True})
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(create_app(db, service, TestAccess()), host='127.0.0.1', port=port, log_level='error', access_log=False))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        deadline = time.time() + 10
        while not server.started and time.time() < deadline:
            time.sleep(.05)
        assert server.started
        base = 'http://127.0.0.1:' + str(port)
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch()
                public = browser.new_context(viewport={'width': 375, 'height': 812}, reduced_motion='reduce')
                page = public.new_page()
                page.on('pageerror', lambda error: errors.append(str(error)))
                for width in (320, 375, 430, 1280):
                    page.set_viewport_size({'width': width, 'height': 900})
                    page.goto(base)
                    expect(page.locator('#title')).to_contain_text('Christmas magic')
                    expect(page.get_by_role('img', name='Lights on Elizabeth', exact=True)).to_be_visible()
                    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), width
                    for selector in ('#name', '#message', '#code', '#submit'):
                        assert page.locator(selector).bounding_box()['height'] >= 44
                    assert page.locator('#name').evaluate('el => parseFloat(getComputedStyle(el).fontSize)') >= 16
                    assert page.locator('#code').get_attribute('inputmode') == 'numeric'
                    page.screenshot(path=str(screenshots / ('visitor-' + str(width) + '.png')), full_page=True)
                page.set_viewport_size({'width': 375, 'height': 812})
                page.locator('#name').fill('Alex')
                page.locator('#message').fill('I wish for a telescope.')
                code = db.codes()['current']['code']
                page.locator('#code').fill('9999' if code != '9999' else '9998')
                page.locator('#submit').click()
                expect(page.locator('#code-error')).to_be_visible()
                assert page.locator('#message').input_value() == 'I wish for a telescope.'
                assert sends == []
                page.locator('#code').fill(code)
                page.route('**/api/letters', lambda route: route.abort())
                page.locator('#submit').click()
                expect(page.locator('#form-error')).to_contain_text('Connection interrupted')
                key = page.evaluate("JSON.parse(sessionStorage.getItem('santa-draft-v1')).pending")
                page.reload()
                expect(page.locator('#message')).to_have_value('I wish for a telescope.')
                assert page.locator('#code').input_value() == ''
                page.unroute('**/api/letters')
                page.locator('#code').fill(code)
                page.locator('#submit').click()
                expect(page.locator('#job-status')).to_have_text('Sent to printer', timeout=15000)
                expect(page.locator('#reply')).to_contain_text('telescope')
                expect(page.locator('#letter-reference')).to_contain_text('Letter reference:')
                expect(page.locator('#preview')).to_be_visible()
                assert len(sends) == 1
                job_id = page.evaluate("JSON.parse(sessionStorage.getItem('santa-draft-v1')).activeJob")
                assert db.get_job(job_id)['idem'] == key
                other = browser.new_context()
                assert other.request.get(base + '/api/letters/' + job_id).status == 404
                assert other.request.get(base + '/api/letters/' + job_id + '/preview').status == 404
                assert other.request.get(base + '/admin').status == 401
                assert other.request.get(base + '/admin/api/overview').status == 401
                other.close()
                page.screenshot(path=str(screenshots / 'visitor-receipt.png'), full_page=True)
                page.locator('#another').click()
                expect(page.locator('#name')).to_have_value('')
                expect(page.locator('#message')).to_have_value('')
                assert page.locator('#code').input_value() == ''
                team = browser.new_context(extra_http_headers={'Cf-Access-Jwt-Assertion': 'browser-test-only'}, viewport={'width': 1280, 'height': 900})
                admin = team.new_page()
                admin.on('pageerror', lambda error: errors.append(str(error)))
                admin.goto(base + '/admin')
                expect(admin.locator('#current-code')).to_have_text(code)
                expect(admin.locator('#printer-status')).to_have_text('Online')
                admin.screenshot(path=str(screenshots / 'admin-desktop.png'), full_page=True)
                for width in (320, 375, 430):
                    admin.set_viewport_size({'width': width, 'height': 900})
                    assert admin.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), ('admin', width)
                admin.screenshot(path=str(screenshots / 'admin-mobile.png'), full_page=True)
                admin.locator('#pause-submissions').click()
                expect(admin.locator('#pause-submissions')).to_have_text('Resume submissions')
                page.reload()
                expect(page.locator('#closed')).to_be_visible()
                expect(page.locator('#submit')).to_be_disabled()
                admin.locator('#pause-submissions').click()
                expect(admin.locator('#pause-submissions')).to_have_text('Pause submissions')
                admin.locator('#tab-queue').click()
                expect(admin.locator('#jobs button')).to_have_count(1)
                admin.locator('#queue-search').fill('no-such-visitor')
                expect(admin.locator('#jobs button')).to_have_count(0)
                admin.locator('#queue-search').fill(job_id[:8])
                expect(admin.locator('#jobs button')).to_have_count(1)
                admin.locator('#queue-search').fill('')
                admin.locator('#jobs button').click()
                expect(admin.locator('#job-dialog')).to_be_visible()
                admin.get_by_role('button', name='Reprint saved receipt', exact=True).click()
                expect(admin.locator('#confirm-preview')).to_be_visible()
                with admin.expect_response(lambda response: response.url.endswith('/reprint') and response.request.method == 'POST') as response_info:
                    admin.locator('#confirm-yes').click()
                reprint = response_info.value.json()
                deadline = time.time() + 15
                while len(sends) < 2 and time.time() < deadline:
                    time.sleep(.1)
                expect(admin.locator('#job-meta')).to_contain_text(reprint['id'][:8], timeout=15000)
                expect(admin.locator('#job-meta')).to_contain_text('sent', timeout=15000)
                assert len(sends) == 2
                assert sends[0][2] == sends[1][2]
                admin.locator('#close-dialog').click()
                admin.locator('#tab-settings').click()
                admin.locator('#setting-retention_hours').fill('48')
                admin.get_by_role('button', name='Save settings').click()
                expect(admin.locator('#admin-status')).to_have_text('Settings saved.')
                assert db.settings()['retention_hours'] == 48
                admin.locator('#tab-diagnostics').click()
                admin.locator('#checklist-run').click()
                expect(admin.locator('#checklist-results')).to_contain_text('Database')
                bundle = team.request.get(base + '/admin/api/diagnostics/bundle')
                assert bundle.status == 200 and bundle.json()['format'] == 1
                assert 'telescope' not in bundle.text() and 'Alex' not in bundle.text()
                admin.locator('#sms-add-window').click()
                expect(admin.locator('.sms-window')).to_have_count(1)
                expect(admin.locator('#sms-state')).to_contain_text('not configured')
                for width in (320, 375, 430, 1280):
                    admin.set_viewport_size({'width': width, 'height': 900})
                    assert admin.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), ('diagnostics', width)
                admin.set_viewport_size({'width': 375, 'height': 900})
                admin.screenshot(path=str(screenshots / 'admin-diagnostics-mobile.png'), full_page=True)
                admin.locator('#render-test').click()
                expect(admin.locator('#diagnostic-preview')).to_be_visible()
                assert len(sends) == 2
                fail_next[0] = True
                admin.locator('#print-test').click()
                admin.locator('#confirm-yes').click()
                expect(admin.locator('#job-meta')).to_contain_text('uncertain', timeout=15000)
                assert db.settings()['paused_printing']
                admin.get_by_role('button', name='Cancel after inspection').click()
                admin.locator('#confirm-yes').click()
                expect(admin.locator('#job-meta')).to_contain_text('cancelled')
                admin.locator('#close-dialog').click()
                admin.locator('#tab-overview').click()
                expect(admin.locator('#pause-printing')).to_have_text('Resume printing')
                admin.locator('#pause-printing').click()
                expect(admin.locator('#pause-printing')).to_have_text('Pause printing')
                assert not db.settings()['paused_printing']
                probe_status[0] = 'Cover open'
                admin.locator('#check-printer').click()
                expect(admin.locator('#printer-status')).to_have_text('Cover open')
                assert service.hardware_hold and not db.settings()['paused_printing']
                expect(admin.locator('#incidents')).to_contain_text('Printer unavailable')
                probe_status[0] = 'Online'
                admin.locator('#check-printer').click()
                expect(admin.locator('#printer-status')).to_have_text('Online')
                admin.locator('#check-printer').click()
                deadline = time.time() + 5
                while service.hardware_hold and time.time() < deadline:
                    time.sleep(.05)
                assert not service.hardware_hold
                admin.locator('#tab-ip').click()
                expect(admin.locator('#ip-default-minute')).to_have_value('5')
                expect(admin.locator('#ip-default-day')).to_have_value('10')
                admin.locator('#ip-default-minute').fill('4')
                admin.locator('#ip-defaults-form button').click()
                expect(admin.locator('#admin-status')).to_have_text('Default IP limits saved.')
                assert db.settings()['ip_per_minute'] == 4
                admin.locator('#ip-address').fill('127.0.0.1')
                admin.locator('#ip-minute').fill('2')
                admin.locator('#ip-day').fill('12')
                admin.locator('#ip-note').fill('Browser test')
                admin.locator('#ip-blocked').check()
                admin.locator('#ip-rule-form button[type=submit]').click()
                expect(admin.locator('#ip-rows')).to_contain_text('Blocked · Browser test')
                page.reload()
                page.locator('#message').fill('A new Christmas wish.')
                page.locator('#code').fill(db.codes()['current']['code'])
                page.locator('#submit').click()
                expect(page.locator('#form-error')).to_contain_text('blocked')
                admin.get_by_role('button', name='Unblock', exact=True).click()
                admin.locator('#confirm-yes').click()
                expect(admin.locator('#ip-rows')).to_contain_text('Custom rule · Browser test')
                admin.get_by_role('button', name='Use defaults', exact=True).click()
                admin.locator('#confirm-yes').click()
                expect(admin.locator('#ip-rows')).to_contain_text('Default')
                assert db.ip_controls()['addresses'][0]['effective_minute'] == 4
                admin.screenshot(path=str(screenshots / 'admin-ip-controls.png'), full_page=True)
                page.evaluate("sessionStorage.setItem('santa-draft-v1', JSON.stringify({activeJob: 'ffffffffffffffffffffffffffffffff'}))")
                page.reload()
                expect(page.locator('#job-status')).to_have_text('Letter no longer available')
                expect(page.locator('#job-error')).to_contain_text('may have expired')
                expect(page.locator('#another')).to_be_visible()
                assert deliveries and set(deliveries[0]) == {'code'}
                assert errors == [], errors
                print('Browser acceptance passed: mobile/desktop layouts, draft recovery, code gate, ownership, automatic printing, admin controls, exact reprint, diagnostics, guided uncertain-send recovery, queue search, checklist, hardware holds, and expired-letter recovery.')
                browser.close()
        finally:
            server.should_exit = True
            thread.join(15)
            assert not thread.is_alive(), 'Server did not shut down cleanly'


if __name__ == '__main__':
    main()
