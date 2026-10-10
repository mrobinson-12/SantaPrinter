"""Single serial print worker and independent webhook scheduler."""
import asyncio
import json
import logging
import hashlib
import shutil
import time
import os
from pathlib import Path
import socket
import urllib.error
import urllib.request

import print_nt8360 as printer
import santa_reply

logger = logging.getLogger('santa')
ROOT = Path(__file__).resolve().parent


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def deliver_code(url, code):
    request = urllib.request.Request(url, data=json.dumps({'code': code}).encode(),
                                     headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=5) as response:
            return 200 <= response.status < 300, 'HTTP {}'.format(response.status)
    except urllib.error.HTTPError as error:
        return False, 'HTTP {}'.format(error.code)
    except (OSError, urllib.error.URLError, ValueError):
        return False, 'Webhook connection failed or timed out.'


def probe_printer(host, port):
    try:
        with socket.create_connection((host, port), timeout=2) as connection:
            connection.settimeout(2)
            values = []
            for number in range(1, 5):
                connection.sendall(bytes([0x10, 0x04, number]))
                value = connection.recv(1)
                if len(value) != 1 or value[0] & 0x93 != 0x12:
                    return {'status': 'Unknown', 'checked': True}
                values.append(value[0])
        problems = []
        if values[0] & 0x08:
            problems.append('Offline')
        if values[1] & 0x04:
            problems.append('Cover open')
        if values[1] & 0x20 or values[3] & 0x60:
            problems.append('Paper empty')
        if values[2] & 0x6c:
            problems.append('Printer error')
        if values[3] & 0x0c:
            problems.append('Paper low')
        return {'status': ', '.join(problems) if problems else 'Online', 'checked': True}
    except OSError:
        return {'status': 'Unknown', 'checked': True}


class Service:
    def __init__(self, store, generate=None, send=None, webhook=None, probe=None):
        self.store = store
        self.generate = generate or santa_reply.generate_reply
        self.send = send or printer.send_job
        self.webhook = webhook or deliver_code
        self.probe = probe or probe_printer
        self.stopping = False
        self.printer_status = {'status': 'Unknown', 'checked': False}
        self.last_probe = 0
        self.last_cleanup = 0
        self.tasks = []
        self.printer_lock = asyncio.Lock()
        self.hardware_hold = True
        self.healthy_checks = 0
        saved = store.saved_health()
        self.last_ai_success = saved.get('last_ai_success') or 0
        self.last_ai_error = None
        self.last_fallback = saved.get('last_fallback') or 0
        self.last_progress = store.clock()
        self.heartbeats = {}

    def fallback_reply(self, job):
        return (ROOT / 'santa-fallback.txt').read_text().format(name=job['name'] or 'friend').strip()

    def generate_job(self, job):
        cfg = json.loads(job['config'])
        try:
            printer.render_letter('Santa', ROOT / 'santa-receipt-border.png', font_size=cfg['font_size'])
            reply = job['reply']
            if not reply:
                key = os.environ.get('OPENAI_API_KEY', '').strip()
                fallback = False
                try:
                    if not key:
                        raise santa_reply.GenerationError('missing_key')
                    if job['generation_attempts'] > 3:
                        raise santa_reply.GenerationError('retry_exhausted')
                    message = (('My first name is ' + job['name'] + '.\n') if job['name'] else '') + job['message']
                    reply = self.generate(message, key)
                    if not reply or len(reply.split()) > 60 or len(reply) > 600:
                        raise santa_reply.GenerationError('invalid_response')
                    self.last_ai_success = self.store.clock()
                except (santa_reply.GenerationError, OSError, ValueError) as error:
                    transient = isinstance(error, (OSError, TimeoutError)) or getattr(error, 'retryable', False)
                    code = getattr(error, 'code', 'invalid_response')
                    self.last_ai_error = code
                    if transient and job['generation_attempts'] < 3:
                        delay = (5, 15)[job['generation_attempts'] - 1]
                        self.store.transition(job['id'], 'generating', 'waiting', error='AI unavailable; retry scheduled.',
                                              error_code='ai_' + code, next_attempt=self.store.clock() + delay)
                        return
                    if not self.store.settings()['fallback_enabled']:
                        self.store.transition(job['id'], 'generating', 'failed', error='AI unavailable. Retry or enable prepared letters.', error_code='ai_' + code)
                        return
                    reply, fallback = self.fallback_reply(job), True
                    self.last_fallback = self.store.clock()
                if not self.store.transition(job['id'], 'generating', 'generating', reply=reply, fallback=int(fallback), error_code=('fallback_' + code) if fallback else None):
                    return
            image, _ = printer.render_letter(reply, ROOT / 'santa-receipt-border.png', font_size=cfg['font_size'])
            hashes = self.store.save_artifacts(job['id'], image, printer.build_raster_job(image, cut=cfg['cut']))
            self.store.transition(job['id'], 'generating', 'ready', reply=reply, error=None, error_code=None,
                                  **hashes, ready_at=self.store.clock(), next_attempt=0)
        except Exception:
            self.store.transition(job['id'], 'generating', 'failed', error='Receipt rendering or storage failed. Check artwork, fonts and disk space; retry the saved reply.', error_code='artifact')

    def send_job(self, job):
        # Artifacts are checked before opening the print connection.
        try:
            data = self.store.receipt_bytes(job)
        except (OSError, ValueError):
            self.store.transition(job['id'], 'sending', 'failed', error='Saved receipt is missing or damaged. Retry rendering the saved reply.', error_code='artifact')
            return
        cfg = self.store.settings()
        destination = '{}:{}'.format(cfg['printer_host'], cfg['printer_port'])
        self.store.transition(job['id'], 'sending', 'sending', destination=destination)
        try:
            self.send(cfg['printer_host'], cfg['printer_port'], data, timeout=10)
            self.store.transition(job['id'], 'sending', 'sent', error=None, error_code=None, sent_at=self.store.clock())
            self.last_progress = self.store.clock()
        except printer.ConnectionFailure:
            delay = (5, 15, 30)[min(job['send_attempts'] - 1, 2)]
            self.store.transition(job['id'], 'sending', 'ready', error='Connection failed before any receipt bytes were sent. Waiting for printer.',
                                  error_code='printer_connection', next_attempt=self.store.clock() + delay)
            self.hardware_hold = True
            self.healthy_checks = 0
            self.store.incident('printer', 'Printer unavailable or reporting a fault.', True)
        except Exception:
            self.store.uncertain(job['id'], 'Transmission may have printed some or all of this receipt. Inspect it before choosing an outcome.')
        self.refresh_printer()

    def refresh_printer(self):
        cfg = self.store.settings()
        result = self.probe(cfg['printer_host'], cfg['printer_port'])
        status = result.get('status', 'Unknown')
        healthy = status in ('Online', 'Paper low')
        self.healthy_checks = self.healthy_checks + 1 if healthy else 0
        if not healthy:
            self.hardware_hold = True
        elif self.healthy_checks >= 2:
            self.hardware_hold = False
        self.printer_status = {**result, 'at': self.store.clock(), 'held': self.hardware_hold}
        self.last_probe = self.store.clock()
        self.store.incident('printer', 'Printer unavailable or reporting a fault.', self.hardware_hold)
        self.store.incident('paper_low', 'Printer paper is low; refill soon.', status == 'Paper low')
        return self.printer_status

    async def check_printer(self):
        async with self.printer_lock:
            return await asyncio.to_thread(self.refresh_printer)

    def health(self):
        now = self.store.clock()
        with self.store.connect() as db:
            oldest = db.execute("SELECT MIN(created) FROM jobs WHERE state IN ('waiting','generating','ready','sending','uncertain')").fetchone()[0]
            last_sent = db.execute('SELECT MAX(sent_at) FROM jobs').fetchone()[0]
            recent = [dict(r) for r in db.execute('SELECT generation_attempts,fallback,error_code,ready_at FROM jobs ORDER BY updated DESC LIMIT 10')]
        workers = {name: {'checked': at, 'healthy': now - at < 60} for name, at in self.heartbeats.items()}
        metrics = self.store.metrics()
        errors = metrics.get('failed', 0) + metrics.get('uncertain', 0)
        cfg = self.store.settings()
        incidents = self.store.incidents()
        attention = errors or any(i['kind'] in ('uncertain', 'jobs', 'printer', 'worker', 'app', 'clock', 'disk', 'stalled') for i in incidents)
        degraded = cfg['paused_printing'] or cfg['paused_submissions'] or bool(incidents) or self.hardware_hold or self.last_fallback > self.last_ai_success or any(not w['healthy'] for w in workers.values())
        return dict(status='Needs attention' if attention else 'Degraded' if degraded else 'Ready', workers=workers,
                    oldest_waiting_age=max(0, now - oldest) if oldest is not None else None,
                    last_sent=last_sent, disk_free=shutil.disk_usage(self.store.directory).free,
                    ai_configured=bool(os.environ.get('OPENAI_API_KEY', '').strip()),
                    last_ai_success=self.last_ai_success or None, last_ai_error=self.last_ai_error,
                    last_fallback=self.last_fallback or None, recent_generation=recent, hardware_hold=self.hardware_hold,
                    paused_printing=cfg['paused_printing'], paused_submissions=cfg['paused_submissions'])

    async def codes_loop(self):
        while not self.stopping:
            self.heartbeats['codes'] = self.store.clock()
            try:
                due = self.store.delivery_due()
                if due:
                    ok, detail = await asyncio.to_thread(self.webhook, self.store.settings()['webhook_url'], due['code'])
                    self.store.record_delivery(due['slot'], ok, detail)
                if self.store.clock() - self.last_cleanup > 60:
                    # File deletion can be slow; keep it off the request event loop.
                    await asyncio.to_thread(self.store.cleanup)
                    self.last_cleanup = self.store.clock()
            except Exception:
                logger.error('Code scheduler encountered an error; it will retry.')
            await asyncio.sleep(0.5)

    async def generation_loop(self, number):
        while not self.stopping:
            self.heartbeats['generation_' + str(number)] = self.store.clock()
            try:
                job = self.store.claim('generation')
                if job:
                    await asyncio.to_thread(self.generate_job, job)
                    self.last_progress = self.store.clock()
            except Exception:
                logger.error('Generation worker encountered an error; it will retry.')
            await asyncio.sleep(0.5)

    async def worker_loop(self):
        while not self.stopping:
            self.heartbeats['printing'] = self.store.clock()
            try:
                async with self.printer_lock:
                    # Check every five seconds under load/hold, every thirty seconds idle.
                    interval = 5 if self.hardware_hold or any(self.store.metrics().get(k) for k in ('ready', 'sending')) else 30
                    if self.store.clock() - self.last_probe >= interval:
                        await asyncio.to_thread(self.refresh_printer)
                    if not self.hardware_hold:
                        job = self.store.claim('printing')
                        if job:
                            # Fresh preflight before every transmission, under the same lock.
                            await asyncio.to_thread(self.refresh_printer)
                            if self.hardware_hold:
                                self.store.transition(job['id'], 'sending', 'ready', error_code='printer_hold')
                            else:
                                await asyncio.to_thread(self.send_job, job)
            except Exception:
                logger.error('Printer worker encountered an error; it will retry.')
            await asyncio.sleep(0.5)

    async def monitoring_loop(self):
        from santa_notifications import NotificationService
        notifications = NotificationService(self.store)
        while not self.stopping:
            try:
                health = self.health()
                metrics = self.store.metrics()
                count = sum(metrics.get(k, 0) for k in ('waiting', 'generating', 'ready', 'sending', 'uncertain'))
                self.store.incident('uncertain', 'A receipt needs physical inspection.', bool(metrics.get('uncertain')), metrics.get('uncertain', 0))
                self.store.incident('jobs', 'Letters failed; retry the saved reply or inspect diagnostics.', bool(metrics.get('failed')), metrics.get('failed', 0))
                codes = self.store.codes()
                self.store.incident('codes', 'Current event code has not been delivered.', not bool(codes.get('current') and codes['current']['delivered']))
                self.store.incident('clock', 'Server clock moved backwards; correct host time.', codes['clock_warning'])
                self.store.incident('worker', 'A worker has stopped reporting health.', any(not w['healthy'] for w in health['workers'].values()))
                self.store.incident('stalled', 'Letters are waiting without processing progress.', bool(count) and self.store.clock() - self.last_progress >= 120, count)
                self.store.incident('fallback', 'AI unavailable; prepared Santa letters are being used.', self.last_fallback > self.last_ai_success)
                self.store.incident('disk', 'Storage is nearly full; free space before continuing.', health['disk_free'] < 100 * 1024 * 1024)
                if self.store.incidents() and health['status'] == 'Ready':
                    health['status'] = 'Degraded'
                self.store.save_health(health)
                await asyncio.to_thread(notifications.tick)
            except Exception:
                logger.error('Monitoring failed; it will retry without stopping letters.')
            await asyncio.sleep(2)

    def start(self):
        self.store.recover()
        self.store.rotate()
        self.heartbeats = {name: self.store.clock() for name in ('codes', 'printing', 'generation_1', 'generation_2')}
        self.tasks = [asyncio.create_task(self.codes_loop()), asyncio.create_task(self.worker_loop()),
                      asyncio.create_task(self.generation_loop(1)), asyncio.create_task(self.generation_loop(2)),
                      asyncio.create_task(self.monitoring_loop())]

    async def stop(self):
        self.stopping = True
        await asyncio.gather(*self.tasks)
