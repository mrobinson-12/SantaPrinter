"""Single serial print worker and independent webhook scheduler."""
import asyncio
import json
import logging
import os
from pathlib import Path
import socket
import urllib.error
import urllib.request

import print_nt8360 as printer
import santa_reply
import santa_sms

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
                    return {'status': 'Unknown', 'checked': True, 'paper_empty': None}
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
        return {'status': ', '.join(problems) if problems else 'Online', 'checked': True,
                'paper_empty': bool(values[1] & 0x20 or values[3] & 0x60)}
    except OSError:
        return {'status': 'Unknown', 'checked': True, 'paper_empty': None}


class Service:
    def __init__(self, store, generate=None, send=None, webhook=None, probe=None, sms=None):
        self.store = store
        self.generate = generate or santa_reply.generate_reply
        self.send = send or printer.send_job
        self.webhook = webhook or deliver_code
        self.probe = probe or probe_printer
        self.sms = sms or santa_sms.send_sms
        self.stopping = False
        self.printer_status = {'status': 'Unknown', 'checked': False}
        self.last_probe = 0
        self.last_cleanup = 0
        self.tasks = []
        self.printer_lock = asyncio.Lock()

    def generate_job(self, job):
        try:
            cfg = json.loads(job['config'])
            # Preflight dependencies/layout before any paid model request.
            printer.render_letter('Santa', ROOT / 'santa-receipt-border.png', font_size=cfg['font_size'])
            reply = job['reply']
            if not reply:
                key = os.environ.get('OPENAI_API_KEY', '').strip()
                if not key:
                    raise ValueError('OPENAI_API_KEY is not configured.')
                message = (('My first name is ' + job['name'] + '.\n') if job['name'] else '') + job['message']
                reply = self.generate(message, key)
                if not reply or len(reply.split()) > 60 or len(reply) > 600:
                    raise ValueError('Generated letter exceeds the receipt limits or is empty.')
                # Save the reply immediately so rendering retries need no model call.
                if not self.store.transition(job['id'], 'generating', 'generating', reply=reply):
                    return
            image, _ = printer.render_letter(reply, ROOT / 'santa-receipt-border.png', font_size=cfg['font_size'])
            image.save(self.store.files / (job['id'] + '.png'), format='PNG')
            (self.store.files / (job['id'] + '.bin')).write_bytes(printer.build_raster_job(image, cut=cfg['cut']))
            self.store.transition(job['id'], 'generating', 'ready', reply=reply, error=None)
        except Exception as error:
            # Never persist API errors verbatim: upstream messages may contain credentials.
            detail = 'Letter generation or rendering failed ({}). Check server configuration and retry.'.format(type(error).__name__)
            if isinstance(error, ValueError) and str(error) == 'OPENAI_API_KEY is not configured.':
                detail = str(error)
            self.store.transition(job['id'], 'generating', 'failed', error=detail)

    def send_job(self, job):
        try:
            cfg = json.loads(job['config'])
            data = (self.store.files / (job['id'] + '.bin')).read_bytes()
            self.send(cfg['printer_host'], cfg['printer_port'], data, timeout=10)
            self.store.transition(job['id'], 'sending', 'sent', error=None)
        except Exception:
            self.store.uncertain(job['id'], 'Printer transmission failed. Some or all of the receipt may have printed. Check it before reprinting.')
        self.refresh_printer()

    def refresh_printer(self):
        cfg = self.store.settings()
        self.printer_status = {**self.probe(cfg['printer_host'], cfg['printer_port']), 'at': self.store.clock()}
        self.last_probe = self.store.clock()
        self.store.observe_paper(cfg['printer_host'], cfg['printer_port'], self.printer_status.get('paper_empty'))
        return self.printer_status

    async def check_printer(self):
        async with self.printer_lock:
            return await asyncio.to_thread(self.refresh_printer)

    async def codes_loop(self):
        while not self.stopping:
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

    async def worker_loop(self):
        while not self.stopping:
            try:
                job = self.store.claim()
                if job:
                    if job['state'] == 'generating':
                        await asyncio.to_thread(self.generate_job, job)
                    else:
                        async with self.printer_lock:
                            await asyncio.to_thread(self.send_job, job)

            except Exception:
                logger.error('Queue worker encountered an error; it will retry.')
            await asyncio.sleep(0.5)

    async def paper_loop(self):
        while not self.stopping:
            try:
                if self.store.clock() - self.last_probe >= 30:
                    await self.check_printer()
            except Exception:
                logger.error('Printer paper monitor encountered an error; it will retry.')
            await asyncio.sleep(1)

    async def sms_loop(self):
        while not self.stopping:
            try:
                if santa_sms.configured():
                    alert = self.store.claim_sms()
                    if alert:
                        try:
                            result = await asyncio.to_thread(self.sms, alert['sender'], alert['recipient'], alert['message'])
                        except Exception:
                            result = {'state': 'uncertain', 'detail': 'SMS send failed unexpectedly. Check Twilio logs before retrying.', 'sid': None}
                        self.store.finish_sms(alert, result)
            except Exception:
                logger.error('SMS dispatcher encountered an error; it will retry.')
            await asyncio.sleep(0.5)

    def start(self):
        self.store.recover()
        self.store.recover_sms()
        self.store.rotate()
        self.tasks = [asyncio.create_task(self.codes_loop()), asyncio.create_task(self.worker_loop()),
                      asyncio.create_task(self.paper_loop()), asyncio.create_task(self.sms_loop())]

    async def stop(self):
        self.stopping = True
        # Do not cancel a socket send running in a thread and mislabel its outcome.
        await asyncio.gather(*self.tasks)
