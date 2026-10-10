"""Transactional state for the web app. No network or printer side effects here."""
from contextlib import contextmanager
import hashlib
import ipaddress
import json
from pathlib import Path
import secrets
import sqlite3
import time
import uuid

DEFAULTS = dict(printer_host='192.168.1.100', printer_port=9100,
                webhook_url='http://192.168.1.103/code', font_size=12, cut=True,
                visitor_title='A little letter. A lot of Christmas magic.',
                visitor_intro='Make a Christmas wish at Lights on Elizabeth, then collect your personal Santa letter at the display.',
                paused_submissions=False, paused_printing=False,
                browser_cooldown=60, global_per_minute=10, queue_limit=50,
                failed_codes_per_ip=10, retention_hours=24,
                ip_per_minute=5, ip_per_day=10, sms_enabled=False, sms_from='', sms_to=[],
                sms_message='ALERT 🚨 Receipt Printer out of Paper - please refill the paper on the receipt printer')
ACTIVE = ('waiting', 'generating', 'ready', 'sending', 'uncertain')


class Rejected(Exception):
    def __init__(self, message, status=400):
        self.message, self.status = message, status
        super().__init__(message)


class Store:
    def __init__(self, directory, clock=time.time):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.files = self.directory / 'receipts'
        self.files.mkdir(exist_ok=True, mode=0o700)
        self.path = self.directory / 'santa.sqlite3'
        self.clock = clock
        with self.connect() as db:
            db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS codes (slot INTEGER PRIMARY KEY, code TEXT NOT NULL,
                delivered INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt REAL NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS deliveries (id INTEGER PRIMARY KEY, slot INTEGER,
                at REAL NOT NULL, ok INTEGER NOT NULL, detail TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, owner TEXT NOT NULL,
                idem TEXT NOT NULL, fingerprint TEXT NOT NULL, name TEXT NOT NULL,
                message TEXT NOT NULL, state TEXT NOT NULL, reply TEXT,
                error TEXT, created REAL NOT NULL, updated REAL NOT NULL,
                parent TEXT, config TEXT NOT NULL, UNIQUE(owner, idem));
            CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(state, created);
            CREATE INDEX IF NOT EXISTS jobs_owner ON jobs(owner, created);
            CREATE TABLE IF NOT EXISTS bad_codes (ip TEXT NOT NULL, at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS ip_attempts (ip TEXT NOT NULL, at REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS ip_attempts_window ON ip_attempts(ip,at);
            CREATE TABLE IF NOT EXISTS ip_rules (ip TEXT PRIMARY KEY, blocked INTEGER NOT NULL,
                per_minute INTEGER, per_day INTEGER, note TEXT NOT NULL, updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS paper_state (printer TEXT PRIMARY KEY, empty INTEGER NOT NULL, incident TEXT);
            CREATE TABLE IF NOT EXISTS sms_alerts (id TEXT PRIMARY KEY, incident TEXT NOT NULL,
                printer TEXT NOT NULL, recipient TEXT NOT NULL, sender TEXT NOT NULL, message TEXT NOT NULL,
                state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL,
                created REAL NOT NULL, updated REAL NOT NULL, detail TEXT, sid TEXT,
                UNIQUE(incident,recipient));
            CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY, at REAL NOT NULL,
                actor TEXT NOT NULL, action TEXT NOT NULL, detail TEXT NOT NULL);
            ''')
            if 'ip' not in {row['name'] for row in db.execute('PRAGMA table_info(jobs)')}:
                db.execute('ALTER TABLE jobs ADD COLUMN ip TEXT')
            # Upgrade the old default copy while preserving personalised event text.
            existing = db.execute('SELECT value FROM settings WHERE id=1').fetchone()
            if existing:
                cfg = json.loads(existing[0])
                if cfg.get('visitor_intro') == 'Tell Santa what is on your Christmas wish list. A personal letter will be waiting at the printer.':
                    cfg['visitor_intro'] = DEFAULTS['visitor_intro']
                    db.execute('UPDATE settings SET value=? WHERE id=1', (json.dumps(cfg),))
            db.execute('INSERT OR IGNORE INTO settings VALUES (1, ?)', (json.dumps(DEFAULTS),))

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @contextmanager
    def transaction(self):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            yield db

    @staticmethod
    def settings_in(db):
        return {**DEFAULTS, **json.loads(db.execute('SELECT value FROM settings WHERE id=1').fetchone()[0])}

    def settings(self):
        with self.connect() as db:
            return self.settings_in(db)

    def audit_in(self, db, actor, action, detail=''):
        db.execute('INSERT INTO audit(at,actor,action,detail) VALUES (?,?,?,?)',
                   (self.clock(), actor, action, detail))

    def update_settings(self, changes, actor):
        with self.transaction() as db:
            settings = self.settings_in(db)
            # An uncertain send always requires an explicit review before resuming.
            if changes.get('paused_printing') is False and db.execute(
                    "SELECT 1 FROM jobs WHERE state='uncertain' LIMIT 1").fetchone():
                raise Rejected('Review uncertain print jobs before resuming printing.', 409)
            settings.update(changes)
            if settings['sms_enabled'] and (not settings['sms_from'] or not settings['sms_to']):
                raise Rejected('Set the Twilio sending number and at least one recipient before enabling SMS alerts.')
            db.execute('UPDATE settings SET value=? WHERE id=1', (json.dumps(settings),))
            self.audit_in(db, actor, 'settings', json.dumps(changes))
        return settings

    def rotate_in(self, db, now):
        slot = int(now // 300)
        latest = db.execute('SELECT slot FROM codes ORDER BY slot DESC LIMIT 1').fetchone()
        # A backwards wall-clock change must never resurrect older codes.
        if latest and slot < latest['slot']:
            return None
        if not db.execute('SELECT 1 FROM codes WHERE slot=?', (slot,)).fetchone():
            recent = {r[0] for r in db.execute('SELECT code FROM codes ORDER BY slot DESC LIMIT 2')}
            code = '{:04d}'.format(secrets.randbelow(10000))
            while code in recent:
                code = '{:04d}'.format(secrets.randbelow(10000))
            db.execute('INSERT INTO codes(slot,code,next_attempt) VALUES (?,?,?)', (slot, code, now))
        return slot

    def rotate(self):
        with self.transaction() as db:
            return self.rotate_in(db, self.clock())

    def codes(self):
        with self.transaction() as db:
            slot = self.rotate_in(db, self.clock())
            if slot is None:
                return dict(current=None, previous=None, clock_warning=True)
            result = {}
            for label, number in [('current', slot), ('previous', slot - 1)]:
                row = db.execute('SELECT * FROM codes WHERE slot=?', (number,)).fetchone()
                result[label] = {**dict(row), 'expires': (number + 2) * 300,
                                 'rotates': (number + 1) * 300} if row else None
            result['clock_warning'] = False
            return result

    def delivery_due(self):
        with self.transaction() as db:
            now = self.clock()
            slot = self.rotate_in(db, now)
            row = db.execute('SELECT * FROM codes WHERE slot=? AND delivered=0 AND next_attempt<=?',
                             (slot, now)).fetchone()
            return dict(row) if row else None

    def record_delivery(self, slot, ok, detail):
        with self.transaction() as db:
            now = self.clock()
            row = db.execute('SELECT attempts FROM codes WHERE slot=?', (slot,)).fetchone()
            attempts = row['attempts'] + 1
            delay = [5, 15, 30][attempts - 1] if attempts <= 3 else 60
            db.execute('UPDATE codes SET delivered=?, attempts=?, next_attempt=? WHERE slot=?',
                       (int(ok), attempts, now + delay, slot))
            db.execute('INSERT INTO deliveries(slot,at,ok,detail) VALUES (?,?,?,?)',
                       (slot, now, int(ok), detail[:500]))

    def resend(self, actor):
        with self.transaction() as db:
            slot = self.rotate_in(db, self.clock())
            if slot is None:
                raise Rejected('Server clock moved backwards; correct it before resending.', 409)
            db.execute('UPDATE codes SET delivered=0, attempts=0, next_attempt=0 WHERE slot=?', (slot,))
            self.audit_in(db, actor, 'resend code', str(slot))

    @staticmethod
    def canonical_ip(value):
        try:
            if '%' in value:
                raise ValueError('Scoped IP addresses are not supported.')
            address = ipaddress.ip_address(value.strip())
            if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
                address = address.ipv4_mapped
            return str(address)
        except ValueError:
            raise Rejected('Enter a valid IPv4 or IPv6 address.') from None

    def set_ip_rule(self, ip, blocked, per_minute, per_day, note, actor):
        ip = self.canonical_ip(ip)
        with self.transaction() as db:
            db.execute('INSERT INTO ip_rules VALUES (?,?,?,?,?,?) ON CONFLICT(ip) DO UPDATE SET '
                       'blocked=excluded.blocked,per_minute=excluded.per_minute,per_day=excluded.per_day,'
                       'note=excluded.note,updated=excluded.updated',
                       (ip, int(blocked), per_minute, per_day, note, self.clock()))
            self.audit_in(db, actor, 'IP rule', json.dumps(dict(ip=ip, blocked=blocked,
                          per_minute=per_minute, per_day=per_day, note=note)))
        return ip

    def delete_ip_rule(self, ip, actor):
        ip = self.canonical_ip(ip)
        with self.transaction() as db:
            db.execute('DELETE FROM ip_rules WHERE ip=?', (ip,))
            self.audit_in(db, actor, 'remove IP rule', ip)

    def ip_controls(self):
        with self.connect() as db:
            now, cfg = self.clock(), self.settings_in(db)
            rules = {r['ip']: dict(r) for r in db.execute('SELECT * FROM ip_rules')}
            activity = {r['ip']: dict(r) for r in db.execute(
                'SELECT ip,MAX(at) last_seen,SUM(at>?) minute_count,COUNT(*) day_count '
                'FROM ip_attempts WHERE at>? GROUP BY ip', (now - 60, now - 86400))}
            rows = []
            for ip in rules.keys() | activity.keys():
                rule = rules.get(ip, {})
                rows.append(dict(ip=ip, blocked=bool(rule.get('blocked')), has_rule=ip in rules,
                                 per_minute=rule.get('per_minute'), per_day=rule.get('per_day'),
                                 effective_minute=rule.get('per_minute') if rule.get('per_minute') is not None else cfg['ip_per_minute'],
                                 effective_day=rule.get('per_day') if rule.get('per_day') is not None else cfg['ip_per_day'],
                                 note=rule.get('note', ''), **{k: v for k, v in activity.get(ip, {}).items() if k != 'ip'}))
            rows.sort(key=lambda r: (not r['blocked'], not r['has_rule'], -(r.get('last_seen') or 0), r['ip']))
            return dict(defaults=dict(per_minute=cfg['ip_per_minute'], per_day=cfg['ip_per_day']), addresses=rows)

    def submit(self, owner, ip, idem, name, message, code):
        ip = self.canonical_ip(ip)
        fingerprint = hashlib.sha256(json.dumps([name, message], ensure_ascii=False).encode()).hexdigest()
        rejection, job = None, None
        with self.transaction() as db:
            now, cfg = self.clock(), self.settings_in(db)
            rule = db.execute('SELECT * FROM ip_rules WHERE ip=?', (ip,)).fetchone()
            if rule and rule['blocked']:
                raise Rejected('Submissions from this IP address are blocked. Please ask the event team.', 403)
            existing = db.execute('SELECT * FROM jobs WHERE owner=? AND idem=?', (owner, idem)).fetchone()
            if existing:
                if existing['fingerprint'] != fingerprint:
                    raise Rejected('This submission identifier was already used for a different letter.', 409)
                return dict(existing)
            if cfg['paused_submissions']:
                raise Rejected('Santa is taking a short break. Please try again soon.', 503)
            minute_limit = rule['per_minute'] if rule and rule['per_minute'] is not None else cfg['ip_per_minute']
            day_limit = rule['per_day'] if rule and rule['per_day'] is not None else cfg['ip_per_day']
            counts = db.execute('SELECT SUM(at>?) minute_count,COUNT(*) day_count FROM ip_attempts '
                                'WHERE ip=? AND at>?', (now - 60, ip, now - 86400)).fetchone()
            if (counts['minute_count'] or 0) >= minute_limit:
                raise Rejected('This IP has reached its per-minute limit. Please wait a minute.', 429)
            if counts['day_count'] >= day_limit:
                raise Rejected('This IP has reached its daily limit. Please try again later or ask the event team.', 429)
            db.execute('INSERT INTO ip_attempts VALUES (?,?)', (ip, now))
            try:
                job = self.enqueue_in(db, cfg, now, owner, ip, idem, fingerprint, name, message, code)
            except Rejected as error:
                # Failed codes/cooldowns also consume an admitted attempt. Commit before returning an error.
                rejection = error
        if rejection:
            raise rejection
        return job

    def enqueue_in(self, db, cfg, now, owner, ip, idem, fingerprint, name, message, code):
        db.execute('DELETE FROM bad_codes WHERE at<=?', (now - 300,))
        failures = db.execute('SELECT COUNT(*) FROM bad_codes WHERE ip=?', (ip,)).fetchone()[0]
        if failures >= cfg['failed_codes_per_ip']:
            raise Rejected('Too many code attempts. Please wait five minutes.', 429)
        slot = self.rotate_in(db, now)
        valid = slot is not None and any(secrets.compare_digest(code, row['code']) for row in
                db.execute('SELECT code FROM codes WHERE slot IN (?,?)', (slot, slot - 1)))
        if not valid:
            db.execute('INSERT INTO bad_codes VALUES (?,?)', (ip, now))
            raise Rejected('That code is incorrect or expired. Check the code shown at the event.')
        last = db.execute('SELECT MAX(created) FROM jobs WHERE owner=? AND parent IS NULL', (owner,)).fetchone()[0]
        if last is not None and now - last < cfg['browser_cooldown']:
            raise Rejected('Please wait a moment before sending another letter.', 429)
        total = db.execute('SELECT COUNT(*) FROM jobs WHERE created>? AND parent IS NULL', (now - 60,)).fetchone()[0]
        if total >= cfg['global_per_minute']:
            raise Rejected('Santa has lots of letters right now. Please try again in a minute.', 429)
        self.capacity_in(db, cfg)
        job_id = uuid.uuid4().hex
        db.execute('INSERT INTO jobs(id,owner,idem,fingerprint,name,message,state,created,updated,config,ip) '
                   "VALUES (?,?,?,?,?,?,'waiting',?,?,?,?)",
                   (job_id, owner, idem, fingerprint, name, message, now, now, json.dumps(cfg), ip))
        return dict(db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone())

    @staticmethod
    def capacity_in(db, cfg):
        count = db.execute("SELECT COUNT(*) FROM jobs WHERE state IN ('waiting','generating','ready','sending','uncertain')").fetchone()[0]
        if count >= cfg['queue_limit']:
            raise Rejected('The letter queue is full. Please try again shortly.', 429)

    def get_job(self, job_id, owner=None):
        with self.connect() as db:
            row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
            if row is None or (owner is not None and row['owner'] != owner):
                raise Rejected('Letter not found.', 404)
            return dict(row)

    def jobs(self, state=None):
        with self.connect() as db:
            rows = db.execute('SELECT * FROM jobs WHERE (? IS NULL OR state=?) ORDER BY created DESC LIMIT 200',
                              (state, state)).fetchall()
            return [dict(row) for row in rows]

    def transition(self, job_id, old, new, **fields):
        allowed = {'reply', 'error'}
        assert fields.keys() <= allowed
        with self.transaction() as db:
            values = [new, self.clock(), *fields.values(), job_id, old]
            updates = ''.join(', {}=?'.format(key) for key in fields)
            return db.execute('UPDATE jobs SET state=?,updated=?' + updates + ' WHERE id=? AND state=?', values).rowcount == 1

    def claim(self):
        with self.transaction() as db:
            cfg = self.settings_in(db)
            row = None
            if not cfg['paused_printing']:
                row = db.execute("SELECT * FROM jobs WHERE state='ready' ORDER BY created LIMIT 1").fetchone()
            if row:
                db.execute("UPDATE jobs SET state='sending',updated=? WHERE id=?", (self.clock(), row['id']))
                return {**dict(row), 'state': 'sending'}
            row = db.execute("SELECT * FROM jobs WHERE state='waiting' ORDER BY created LIMIT 1").fetchone()
            if row:
                db.execute("UPDATE jobs SET state='generating',updated=? WHERE id=?", (self.clock(), row['id']))
                return {**dict(row), 'state': 'generating'}
            return None

    def uncertain(self, job_id, error):
        with self.transaction() as db:
            db.execute("UPDATE jobs SET state='uncertain',error=?,updated=? WHERE id=? AND state='sending'",
                       (error[:500], self.clock(), job_id))
            cfg = self.settings_in(db)
            cfg['paused_printing'] = True
            db.execute('UPDATE settings SET value=? WHERE id=1', (json.dumps(cfg),))

    def recover(self):
        with self.transaction() as db:
            db.execute("UPDATE jobs SET state='failed',error='Generation interrupted by restart. Retry explicitly.', updated=? WHERE state='generating'", (self.clock(),))
            interrupted = db.execute("SELECT 1 FROM jobs WHERE state IN ('sending','uncertain') LIMIT 1").fetchone()
            db.execute("UPDATE jobs SET state='uncertain',error='Transmission interrupted by restart. Check the physical receipt.',updated=? WHERE state='sending'", (self.clock(),))
            if interrupted:
                cfg = self.settings_in(db)
                cfg['paused_printing'] = True
                db.execute('UPDATE settings SET value=? WHERE id=1', (json.dumps(cfg),))

    def action(self, job_id, action, actor):
        with self.transaction() as db:
            row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
            if not row:
                raise Rejected('Letter not found.', 404)
            state = row['state']
            if action == 'cancel':
                if state not in ('waiting', 'generating', 'ready'):
                    raise Rejected('This letter can no longer be cancelled.', 409)
                db.execute("UPDATE jobs SET state='cancelled',updated=? WHERE id=?", (self.clock(), job_id))
            elif action == 'retry':
                if state != 'failed':
                    raise Rejected('Only failed jobs can be retried.', 409)
                self.capacity_in(db, self.settings_in(db))
                # Existing reply means render again without another model call.
                db.execute("UPDATE jobs SET state='waiting',error=NULL,updated=? WHERE id=?", (self.clock(), job_id))
            elif action == 'review':
                if state != 'uncertain':
                    raise Rejected('Only uncertain jobs need review.', 409)
                db.execute("UPDATE jobs SET state='failed',error='Transmission reviewed by admin. Reprint saved receipt if needed.',updated=? WHERE id=?", (self.clock(), job_id))
            elif action == 'reprint':
                if state in ('waiting', 'generating', 'ready', 'sending', 'uncertain'):
                    raise Rejected('Finish or review this job before reprinting.', 409)
                if not row['reply'] or not (self.files / (job_id + '.bin')).exists():
                    raise Rejected('No saved receipt is available.', 409)
                cfg = self.settings_in(db)
                self.capacity_in(db, cfg)
                new_id = uuid.uuid4().hex
                # Copy durable artifacts before making the new job visible to the worker.
                for extension in ('.bin', '.png'):
                    (self.files / (new_id + extension)).write_bytes((self.files / (job_id + extension)).read_bytes())
                db.execute('INSERT INTO jobs(id,owner,idem,fingerprint,name,message,state,reply,created,updated,parent,config) '
                           "VALUES (?,?,?,?,?,?,'ready',?,?,?,?,?)",
                           (new_id, row['owner'], new_id, row['fingerprint'], row['name'], row['message'],
                            row['reply'], self.clock(), self.clock(), job_id, json.dumps(cfg)))
                self.audit_in(db, actor, action, job_id + ' -> ' + new_id)
                return new_id
            else:
                raise Rejected('Unknown action.', 404)
            self.audit_in(db, actor, action, job_id)
        return job_id

    def history(self, table):
        assert table in ('deliveries', 'audit')
        with self.connect() as db:
            return [dict(row) for row in db.execute('SELECT * FROM ' + table + ' ORDER BY id DESC LIMIT 100')]

    def metrics(self):
        with self.connect() as db:
            return {r['state']: r['n'] for r in db.execute('SELECT state,COUNT(*) n FROM jobs GROUP BY state')}

    def cleanup(self):
        with self.transaction() as db:
            cutoff = self.clock() - self.settings_in(db)['retention_hours'] * 3600
            rows = db.execute("SELECT id FROM jobs WHERE updated<? AND state IN ('sent','failed','cancelled')", (cutoff,)).fetchall()
            for row in rows:
                for extension in ('.bin', '.png'):
                    (self.files / (row['id'] + extension)).unlink(missing_ok=True)
                db.execute('DELETE FROM jobs WHERE id=?', (row['id'],))
            # Diagnostics contain no letter content, but still have a finite retention.
            db.execute("DELETE FROM sms_alerts WHERE updated<? AND state NOT IN ('pending','sending','uncertain') AND incident NOT IN (SELECT incident FROM paper_state WHERE empty=1 AND incident IS NOT NULL)", (self.clock() - 7 * 86400,))
            db.execute('DELETE FROM deliveries WHERE at<?', (self.clock() - 7 * 86400,))
            db.execute('DELETE FROM audit WHERE at<?', (self.clock() - 7 * 86400,))
            db.execute('DELETE FROM ip_attempts WHERE at<=?', (self.clock() - 86400,))
            db.execute('DELETE FROM bad_codes WHERE at<?', (self.clock() - 300,))
            db.execute('DELETE FROM codes WHERE slot<? AND slot NOT IN (SELECT slot FROM codes ORDER BY slot DESC LIMIT 2)',
                       (int(self.clock() // 300) - 3,))


    def observe_paper(self, host, port, empty):
        if empty is None:
            return  # Unknown/offline status never rearms a paper-out incident.
        printer = host + ':' + str(port)
        with self.transaction() as db:
            cfg, now = self.settings_in(db), self.clock()
            if host != cfg['printer_host'] or port != cfg['printer_port']:
                return  # Ignore a status query for a printer whose settings just changed.
            previous = db.execute('SELECT * FROM paper_state WHERE printer=?', (printer,)).fetchone()
            if not empty:
                db.execute("UPDATE sms_alerts SET state='cancelled',updated=?,detail='Paper refilled before this alert was sent.' WHERE printer=? AND state='pending'", (now, printer))
                db.execute('INSERT INTO paper_state VALUES (?,0,NULL) ON CONFLICT(printer) DO UPDATE SET empty=0,incident=NULL', (printer,))
                return
            incident = previous['incident'] if previous and previous['empty'] else uuid.uuid4().hex
            db.execute('INSERT INTO paper_state VALUES (?,1,?) ON CONFLICT(printer) DO UPDATE SET empty=1,incident=excluded.incident', (printer, incident))
            if cfg['sms_enabled']:
                for recipient in cfg['sms_to']:
                    db.execute('INSERT INTO sms_alerts(id,incident,printer,recipient,sender,message,state,next_attempt,created,updated) '
                               "VALUES (?,?,?,?,?,?,'pending',?,?,?) ON CONFLICT(incident,recipient) DO NOTHING",
                               (uuid.uuid4().hex, incident, printer, recipient, cfg['sms_from'], cfg['sms_message'], now, now, now))

    def claim_sms(self):
        with self.transaction() as db:
            cfg, now = self.settings_in(db), self.clock()
            for row in db.execute("SELECT * FROM sms_alerts WHERE state='pending' AND next_attempt<=? ORDER BY created", (now,)).fetchall():
                paper = db.execute('SELECT * FROM paper_state WHERE printer=?', (row['printer'],)).fetchone()
                current_printer = cfg['printer_host'] + ':' + str(cfg['printer_port'])
                if not cfg['sms_enabled'] or row['recipient'] not in cfg['sms_to'] or row['printer'] != current_printer or not paper or not paper['empty'] or paper['incident'] != row['incident']:
                    db.execute("UPDATE sms_alerts SET state='cancelled',updated=?,detail='Alert no longer enabled, configured, or relevant.' WHERE id=?", (now, row['id']))
                    continue
                # Apply current sender/message to messages that have not left the server.
                db.execute("UPDATE sms_alerts SET state='sending',attempts=attempts+1,updated=?,sender=?,message=? WHERE id=?", (now, cfg['sms_from'], cfg['sms_message'], row['id']))
                return {**dict(row), 'sender': cfg['sms_from'], 'message': cfg['sms_message'], 'state': 'sending', 'attempts': row['attempts'] + 1}
            return None

    def finish_sms(self, alert, result):
        state = result['state']
        if state == 'retry':
            state = 'pending' if alert['attempts'] < 3 else 'failed'
        assert state in ('accepted', 'pending', 'failed', 'uncertain')
        with self.transaction() as db:
            db.execute("UPDATE sms_alerts SET state=?,updated=?,next_attempt=?,detail=?,sid=? WHERE id=? AND state='sending'",
                       (state, self.clock(), self.clock() + (30 if alert['attempts'] == 1 else 120), result['detail'], result.get('sid'), alert['id']))

    def recover_sms(self):
        with self.transaction() as db:
            db.execute("UPDATE sms_alerts SET state='uncertain',updated=?,detail='SMS send interrupted by restart. Check Twilio logs; no automatic resend.' WHERE state='sending'", (self.clock(),))

    def sms_history(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute('SELECT * FROM sms_alerts ORDER BY created DESC,id LIMIT 100')]

    def retry_sms(self, alert_id, actor):
        with self.transaction() as db:
            row = db.execute('SELECT * FROM sms_alerts WHERE id=?', (alert_id,)).fetchone()
            if not row or row['state'] not in ('failed', 'uncertain', 'cancelled'):
                raise Rejected('Only failed, uncertain, or cancelled alerts can be retried.', 409)
            paper = db.execute('SELECT * FROM paper_state WHERE printer=?', (row['printer'],)).fetchone()
            if not paper or not paper['empty'] or paper['incident'] != row['incident']:
                raise Rejected('This paper-out incident has ended.', 409)
            db.execute("UPDATE sms_alerts SET state='pending',attempts=0,next_attempt=0,updated=?,detail='Retry requested by admin.' WHERE id=?", (self.clock(), alert_id))
            self.audit_in(db, actor, 'retry SMS alert', alert_id)
