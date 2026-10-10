"""Transactional state for the web app. No network or printer side effects here."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import secrets
import sqlite3
import time
import uuid

DEFAULTS = dict(printer_host='192.168.1.100', printer_port=9100,
                webhook_url='http://192.168.1.103/code', font_size=12, cut=True,
                visitor_title='A little letter. A lot of Christmas magic.',
                visitor_intro='Tell Santa what is on your Christmas wish list. A personal letter will be waiting at the printer.',
                paused_submissions=False, paused_printing=False,
                browser_cooldown=60, global_per_minute=10, queue_limit=50,
                failed_codes_per_ip=10, retention_hours=24)
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
            CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY, at REAL NOT NULL,
                actor TEXT NOT NULL, action TEXT NOT NULL, detail TEXT NOT NULL);
            ''')
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

    def submit(self, owner, ip, idem, name, message, code):
        fingerprint = hashlib.sha256(json.dumps([name, message], ensure_ascii=False).encode()).hexdigest()
        rejection = None
        job = None
        with self.transaction() as db:
            now = self.clock()
            existing = db.execute('SELECT * FROM jobs WHERE owner=? AND idem=?', (owner, idem)).fetchone()
            if existing:
                if existing['fingerprint'] != fingerprint:
                    raise Rejected('This submission identifier was already used for a different letter.', 409)
                return dict(existing)
            cfg = self.settings_in(db)
            if cfg['paused_submissions']:
                raise Rejected('Santa is taking a short break. Please try again soon.', 503)
            db.execute('DELETE FROM bad_codes WHERE at<=?', (now - 300,))
            failures = db.execute('SELECT COUNT(*) FROM bad_codes WHERE ip=?', (ip,)).fetchone()[0]
            if failures >= cfg['failed_codes_per_ip']:
                raise Rejected('Too many code attempts. Please wait five minutes.', 429)
            slot = self.rotate_in(db, now)
            valid = slot is not None and any(secrets.compare_digest(code, row['code']) for row in
                    db.execute('SELECT code FROM codes WHERE slot IN (?,?)', (slot, slot - 1)))
            if not valid:
                # Commit the failure before returning an error, rather than rolling it back.
                db.execute('INSERT INTO bad_codes VALUES (?,?)', (ip, now))
                rejection = Rejected('That code is incorrect or expired. Check the code shown at the event.')
            else:
                last = db.execute('SELECT MAX(created) FROM jobs WHERE owner=? AND parent IS NULL', (owner,)).fetchone()[0]
                if last is not None and now - last < cfg['browser_cooldown']:
                    raise Rejected('Please wait a moment before sending another letter.', 429)
                total = db.execute('SELECT COUNT(*) FROM jobs WHERE created>? AND parent IS NULL', (now - 60,)).fetchone()[0]
                if total >= cfg['global_per_minute']:
                    raise Rejected('Santa has lots of letters right now. Please try again in a minute.', 429)
                self.capacity_in(db, cfg)
                job_id = uuid.uuid4().hex
                db.execute('INSERT INTO jobs(id,owner,idem,fingerprint,name,message,state,created,updated,config) '
                           'VALUES (?,?,?,?,?,?,\'waiting\',?,?,?)',
                           (job_id, owner, idem, fingerprint, name, message, now, now, json.dumps(cfg)))
                job = dict(db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone())
        if rejection:
            raise rejection
        return job

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
            db.execute('DELETE FROM deliveries WHERE at<?', (self.clock() - 7 * 86400,))
            db.execute('DELETE FROM audit WHERE at<?', (self.clock() - 7 * 86400,))
            db.execute('DELETE FROM bad_codes WHERE at<?', (self.clock() - 300,))
            db.execute('DELETE FROM codes WHERE slot<? AND slot NOT IN (SELECT slot FROM codes ORDER BY slot DESC LIMIT 2)',
                       (int(self.clock() // 300) - 3,))
