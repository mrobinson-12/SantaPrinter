"""Owner authentication used exclusively by the dedicated LAN HTTPS app."""
import hashlib
import hmac
import secrets
from pathlib import Path

from fastapi import HTTPException


def hash_password(password):
    if not 12 <= len(password) <= 256:
        raise ValueError('Choose an owner password of 12–256 characters.')
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, maxmem=64*1024*1024)
    return salt.hex() + ':' + digest.hex()


def verify_password(password, encoded):
    try:
        salt, expected = encoded.split(':')
        if len(password) > 256 or len(salt) != 32 or len(expected) != 128:
            return False
        actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1, maxmem=64*1024*1024)
        return hmac.compare_digest(actual.hex(), expected)
    except (ValueError, TypeError):
        return False


class LocalAuth:
    def __init__(self, store, password_file):
        self.store, self.password_file = store, Path(password_file)

    def encoded(self):
        try:
            return self.password_file.read_text().strip()
        except OSError:
            raise HTTPException(503, 'LAN owner password is not configured.') from None

    def version(self):
        return hashlib.sha256(self.encoded().encode()).hexdigest()

    def login(self, peer, password):
        now = self.store.clock()
        with self.store.transaction() as db:
            db.execute('DELETE FROM login_attempts WHERE at<=?', (now - 300,))
            if db.execute('SELECT COUNT(*) FROM login_attempts WHERE peer=?', (peer,)).fetchone()[0] >= 5 or db.execute('SELECT COUNT(*) FROM login_attempts').fetchone()[0] >= 30:
                raise HTTPException(429, 'Too many login attempts. Wait five minutes.')
            db.execute('INSERT INTO login_attempts VALUES (?,?)', (peer, now))
        encoded = self.encoded()
        if not verify_password(password, encoded):
            raise HTTPException(401, 'Incorrect owner password.')
        token = secrets.token_hex(32)
        with self.store.transaction() as db:
            db.execute('DELETE FROM local_sessions WHERE expires<=?', (now,))
            db.execute('INSERT INTO local_sessions(token,expires,password_version) VALUES (?,?,?)',
                       (hashlib.sha256(token.encode()).hexdigest(), now + 8 * 3600, hashlib.sha256(encoded.encode()).hexdigest()))
            self.store.audit_in(db, 'local-owner', 'LAN login')
        return token

    def verify(self, token):
        with self.store.connect() as db:
            row = db.execute('SELECT expires,password_version FROM local_sessions WHERE token=?', (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
        if not row or row['expires'] <= self.store.clock() or not hmac.compare_digest(row['password_version'], self.version()):
            raise HTTPException(401, 'LAN owner login required.')
        return 'local-owner'

    def logout(self, token):
        with self.store.transaction() as db:
            db.execute('DELETE FROM local_sessions WHERE token=?', (hashlib.sha256(token.encode()).hexdigest(),))
            self.store.audit_in(db, 'local-owner', 'LAN logout')
