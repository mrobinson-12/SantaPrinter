"""Scheduled, durable SMS alerts. Transport failures never interrupt print workers."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import base64
import hashlib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

TIMEZONE = ZoneInfo('Australia/Brisbane')


def in_schedule(timestamp, schedule):
    local = datetime.fromtimestamp(timestamp, TIMEZONE)
    minute = local.hour * 60 + local.minute
    yesterday = (local - timedelta(days=1)).weekday()
    for window in schedule:
        start = sum(int(v) * factor for v, factor in zip(window['start'].split(':'), (60, 1)))
        end = sum(int(v) * factor for v, factor in zip(window['end'].split(':'), (60, 1)))
        if start < end and local.weekday() in window['days'] and start <= minute < end:
            return True
        if start > end and ((local.weekday() in window['days'] and minute >= start) or (yesterday in window['days'] and minute < end)):
            return True
    return False


def credentials_ready():
    return bool(re.fullmatch(r'AC[0-9a-fA-F]{32}', os.getenv('TWILIO_ACCOUNT_SID', '')) and
                os.getenv('TWILIO_AUTH_TOKEN') and re.fullmatch(r'\+[1-9][0-9]{7,14}', os.getenv('TWILIO_FROM', '')))


def credential_fingerprint():
    return hashlib.sha256('|'.join(os.getenv(k, '') for k in ('TWILIO_ACCOUNT_SID', 'TWILIO_AUTH_TOKEN', 'TWILIO_FROM')).encode()).hexdigest()


def notification_fingerprint(cfg):
    return hashlib.sha256((credential_fingerprint() + json.dumps([cfg['sms_recipients'], cfg['sms_schedule']], sort_keys=True)).encode()).hexdigest()


class NotificationError(Exception):
    def __init__(self, retryable=False, uncertain=False):
        self.retryable, self.uncertain = retryable, uncertain
        super().__init__('SMS provider request failed.')


class Twilio:
    def request(self, sid=None, body=None):
        account = os.getenv('TWILIO_ACCOUNT_SID', '')
        if not credentials_ready() or (sid and not re.fullmatch(r'SM[0-9a-fA-F]{32}', sid)):
            raise NotificationError()
        url = 'https://api.twilio.com/2010-04-01/Accounts/' + account + '/Messages' + ('/' + sid if sid else '') + '.json'
        token = base64.b64encode((account + ':' + os.environ['TWILIO_AUTH_TOKEN']).encode()).decode()
        data = urllib.parse.urlencode(body).encode() if body else None
        request = urllib.request.Request(url, data=data, headers={'Authorization': 'Basic ' + token})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            raise NotificationError(retryable=error.code == 429 or error.code >= 500) from None
        except (OSError, ValueError):
            # A lost POST response may still have created a billable message.
            raise NotificationError(retryable=bool(sid), uncertain=not bool(sid)) from None

    def send(self, recipient, text):
        return self.request(body={'To': recipient, 'From': os.environ['TWILIO_FROM'], 'Body': text})

    def status(self, sid):
        return self.request(sid=sid)


class NotificationService:
    def __init__(self, store, transport=None):
        self.store, self.transport = store, transport or Twilio()

    def test(self, actor):
        cfg = self.store.settings()
        if not credentials_ready() or not cfg['sms_recipients'] or not cfg['sms_schedule']:
            from santa_store import Rejected
            raise Rejected('Configure Twilio credentials, recipients and weekly hours first.', 409)
        now = self.store.clock()
        fingerprint = notification_fingerprint(cfg)
        with self.store.transaction() as db:
            if db.execute("SELECT 1 FROM outbox WHERE event LIKE 'test:%' AND status IN ('pending','submitting','queued','accepted','sending','sent')").fetchone():
                from santa_store import Rejected
                raise Rejected('A test is already awaiting delivery.', 409)
            for number in cfg['sms_recipients']:
                db.execute('INSERT INTO outbox(recipient,event,bucket,body,created,next_attempt) VALUES (?,?,?,?,?,?)',
                           (number, 'test:' + fingerprint, int(now), 'Santa Printer: notification test. Your display fault alerts will arrive here.', now, now))
            self.store.audit_in(db, actor, 'test notifications')
        self.tick()

    def enqueue_incidents(self):
        cfg, now = self.store.settings(), self.store.clock()
        if not (cfg['sms_enabled'] and cfg['sms_tested'] and credentials_ready() and
                cfg.get('sms_credentials') == notification_fingerprint(cfg) and in_schedule(now, cfg['sms_schedule'])):
            return
        url = os.getenv('PUBLIC_ORIGIN', '').rstrip('/') + '/admin'
        with self.store.transaction() as db:
            # Do not send obsolete fault messages after a fault has recovered.
            db.execute("UPDATE outbox SET status='cancelled' WHERE status='pending' AND event IN ('opening','reminder') AND incident IN (SELECT id FROM incidents WHERE resolved IS NOT NULL)")
            for incident in db.execute('SELECT * FROM incidents WHERE resolved IS NULL OR resolved>?', (now - 7 * 86400,)).fetchall():
                previous = db.execute("SELECT MAX(created) FROM outbox WHERE incident=? AND event IN ('opening','reminder')", (incident['id'],)).fetchone()[0]
                if incident['resolved']:
                    if previous is None:
                        continue
                    event, bucket = 'recovery', 0
                else:
                    threshold = 0 if incident['kind'] == 'uncertain' else 60
                    if now - incident['opened'] < threshold:
                        continue
                    event, bucket = ('opening', 0) if previous is None else ('reminder', int(now // 900))
                    if previous is not None and (incident['acknowledged'] or now - previous < 900):
                        continue
                text = 'Santa Printer: ' + (incident['kind'].capitalize() + ' issue resolved.' if event == 'recovery' else incident['summary']) + ' Queue impact: ' + str(incident['affected']) + '. ' + url
                for number in cfg['sms_recipients']:
                    db.execute('INSERT OR IGNORE INTO outbox(incident,recipient,event,bucket,body,created,next_attempt) VALUES (?,?,?,?,?,?,?)',
                               (incident['id'], number, event, bucket, text, now, now))

    def tick(self):
        self.enqueue_incidents()
        now, cfg = self.store.clock(), self.store.settings()
        allowed = (cfg['sms_enabled'] and cfg['sms_tested'] and cfg.get('sms_credentials') == notification_fingerprint(cfg) and in_schedule(now, cfg['sms_schedule']))
        # Crash during a POST leaves an ambiguous SMS outcome, never blindly repost it.
        with self.store.transaction() as db:
            db.execute("UPDATE outbox SET status='unknown' WHERE status='submitting' AND checked<?", (now - 60,))
            db.execute("UPDATE outbox SET status='cancelled' WHERE status='pending' AND event IN ('opening','reminder') AND incident IN (SELECT id FROM incidents WHERE resolved IS NOT NULL)")
        for _ in range(4):
            with self.store.transaction() as db:
                row = db.execute("SELECT * FROM outbox WHERE status='pending' AND next_attempt<=? AND (? OR event LIKE 'test:%') ORDER BY id LIMIT 1", (now, int(allowed))).fetchone()
                if not row:
                    break
                if row['recipient'] not in cfg['sms_recipients'] or (row['event'].startswith('test:') and row['event'] != 'test:' + notification_fingerprint(cfg)):
                    db.execute("UPDATE outbox SET status='cancelled' WHERE id=?", (row['id'],))
                    continue
                db.execute("UPDATE outbox SET status='submitting',checked=?,attempts=attempts+1 WHERE id=?", (now, row['id']))
            try:
                result = self.transport.send(row['recipient'], row['body'])
                sid, status = result.get('sid'), result.get('status', 'queued')
                if not re.fullmatch(r'SM[0-9a-fA-F]{32}', sid or '') or status not in ('queued','accepted','sending','sent','delivered','undelivered','failed'):
                    raise NotificationError(uncertain=True)
                with self.store.transaction() as db:
                    db.execute('UPDATE outbox SET status=?,sid=?,checked=? WHERE id=?', (status, sid, now, row['id']))
            except NotificationError as error:
                retry = error.retryable and row['attempts'] < 3
                with self.store.transaction() as db:
                    db.execute('UPDATE outbox SET status=?,next_attempt=? WHERE id=?',
                               ('unknown' if error.uncertain else 'pending' if retry else 'failed', now + (5, 15, 60)[min(row['attempts'], 2)], row['id']))
            except Exception:
                with self.store.transaction() as db:
                    db.execute("UPDATE outbox SET status='unknown' WHERE id=?", (row['id'],))
        with self.store.connect() as db:
            rows = db.execute("SELECT * FROM outbox WHERE sid IS NOT NULL AND status IN ('queued','accepted','sending','sent') AND checked<=? ORDER BY checked LIMIT 4", (now - 30,)).fetchall()
        for row in rows:
            try:
                status = self.transport.status(row['sid']).get('status')
                if status not in ('queued','accepted','sending','sent','delivered','undelivered','failed'):
                    continue
                with self.store.transaction() as db:
                    db.execute('UPDATE outbox SET status=?,checked=? WHERE id=?', (status, now, row['id']))
            except Exception:
                with self.store.transaction() as db:
                    db.execute('UPDATE outbox SET checked=? WHERE id=?', (now, row['id']))
        with self.store.transaction() as db:
            tests = db.execute("SELECT * FROM outbox WHERE event=? ORDER BY created DESC LIMIT ?", ('test:' + notification_fingerprint(cfg), len(cfg['sms_recipients']))).fetchall()
            if (cfg['sms_recipients'] and len(tests) == len(cfg['sms_recipients']) and
                    {r['recipient'] for r in tests} == set(cfg['sms_recipients']) and all(r['status'] == 'delivered' for r in tests)):
                settings = self.store.settings_in(db)
                if notification_fingerprint(settings) == notification_fingerprint(cfg):
                    settings['sms_tested'] = True
                    settings['sms_credentials'] = notification_fingerprint(settings)
                    db.execute('UPDATE settings SET value=? WHERE id=1', (json.dumps(settings),))

    def history(self):
        with self.store.connect() as db:
            return [dict(r) for r in db.execute('SELECT id,recipient,event,created,status,attempts,sid FROM outbox ORDER BY id DESC LIMIT 50')]
