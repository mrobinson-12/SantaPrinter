"""Same-origin mobile Santa app, with Cloudflare Access protected administration."""
import asyncio
from contextlib import asynccontextmanager
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import jwt
from jwt import PyJWKClient
from pydantic import BaseModel, ConfigDict, Field, field_validator

from santa_service import Service, ROOT
import santa_sms
from santa_store import Store, Rejected


class Letter(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    name: str = Field(default='', max_length=60)
    message: str = Field(min_length=1, max_length=1000)
    code: str = Field(pattern=r'^\d{4}$')
    idempotency_key: str = Field(pattern=r'^[A-Za-z0-9_-]{16,80}$')

    @field_validator('name', 'message')
    @classmethod
    def clean(cls, value, info):
        value = value.replace('\r\n', '\n').replace('\r', '\n').expandtabs(4).strip()
        if any((ord(c) < 32 and c != '\n') or 127 <= ord(c) < 160 for c in value):
            raise ValueError('Please remove control characters.')
        if len(value) > (60 if info.field_name == 'name' else 1000):
            raise ValueError('This field is too long after whitespace normalization.')
        if info.field_name == 'name' and '\n' in value:
            raise ValueError('Please use a single line for your name.')
        return value

    @field_validator('code')
    @classmethod
    def ascii_code(cls, value):
        if not re.fullmatch('[0-9]{4}', value):
            raise ValueError('Enter four digits.')
        return value


class Settings(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    printer_host: str | None = Field(default=None, min_length=1, max_length=253)
    printer_port: int | None = Field(default=None, ge=1, le=65535)
    webhook_url: str | None = Field(default=None, max_length=2048)
    font_size: int | None = Field(default=None, ge=8, le=18)
    cut: bool | None = None
    visitor_title: str | None = Field(default=None, min_length=1, max_length=120)
    visitor_intro: str | None = Field(default=None, min_length=1, max_length=500)
    paused_submissions: bool | None = None
    paused_printing: bool | None = None
    browser_cooldown: int | None = Field(default=None, ge=0, le=3600)
    global_per_minute: int | None = Field(default=None, ge=1, le=1000)
    queue_limit: int | None = Field(default=None, ge=1, le=500)
    failed_codes_per_ip: int | None = Field(default=None, ge=1, le=100)
    ip_per_minute: int | None = Field(default=None, ge=1, le=1000)
    ip_per_day: int | None = Field(default=None, ge=1, le=10000)
    sms_enabled: bool | None = None
    sms_from: str | None = Field(default=None, max_length=16)
    sms_to: list[str] | None = Field(default=None, max_length=10)
    sms_message: str | None = Field(default=None, min_length=1, max_length=1600)
    retention_hours: int | None = Field(default=None, ge=1, le=168)

    @field_validator('sms_from')
    @classmethod
    def sms_sender(cls, value):
        if value is None:
            return value
        value = value.strip()
        if value and not re.fullmatch(r'\+[1-9][0-9]{7,14}', value):
            raise ValueError('Use an international phone number such as +61412345678.')
        return value

    @field_validator('sms_to')
    @classmethod
    def sms_recipients(cls, values):
        if values is None:
            return values
        cleaned = []
        for value in values:
            value = value.strip()
            if not re.fullmatch(r'\+[1-9][0-9]{7,14}', value):
                raise ValueError('Use international recipient numbers such as +61412345678.')
            if value not in cleaned:
                cleaned.append(value)
        return cleaned

    @field_validator('sms_message')
    @classmethod
    def sms_body(cls, value):
        if value is not None and (not value.strip() or any(ord(c) < 32 and c not in '\n\r\t' for c in value)):
            raise ValueError('Enter an alert message without control characters.')
        return value

    @field_validator('printer_host')
    @classmethod
    def host(cls, value):
        if value is not None and not re.fullmatch(r'[A-Za-z0-9.:_-]+', value):
            raise ValueError('Use a hostname or IP address.')
        return value

    @field_validator('webhook_url')
    @classmethod
    def url(cls, value):
        if value is None:
            return value
        if any(c.isspace() or ord(c) < 32 for c in value):
            raise ValueError('The webhook URL cannot contain whitespace.')
        parts = urlsplit(value)
        if parts.port is not None and not 1 <= parts.port <= 65535:
            raise ValueError('Use a valid webhook port.')
        if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password or parts.fragment:
            raise ValueError('Use an HTTP(S) URL without credentials or a fragment.')
        return value


class IPRule(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    ip: str = Field(min_length=1, max_length=45)
    blocked: bool = False
    per_minute: int | None = Field(default=None, ge=1, le=1000)
    per_day: int | None = Field(default=None, ge=1, le=10000)
    note: str = Field(default='', max_length=200)

    @field_validator('ip')
    @classmethod
    def address(cls, value):
        try:
            return Store.canonical_ip(value)
        except Rejected as error:
            raise ValueError(error.message) from None


class IPDelete(BaseModel):
    model_config = ConfigDict(extra='forbid')
    ip: str = Field(min_length=1, max_length=45)


class Preview(BaseModel):
    model_config = ConfigDict(extra='forbid')
    text: str = Field(default='Dear friend,\n\nYour Christmas wish has brought a smile to the North Pole. May your holidays be filled with kindness, laughter, and a little magic.\n\nLove, Santa', min_length=1, max_length=600)


def public_job(job, files):
    state = job['state']
    labels = {'waiting': 'Received', 'generating': 'Santa is writing', 'ready': 'Waiting for the printer',
              'sending': 'Sending to printer', 'sent': 'Sent to printer', 'failed': 'Unable to complete your letter',
              'uncertain': 'Please check with the event team', 'cancelled': 'Letter cancelled'}
    return {'id': job['id'], 'state': state, 'label': labels[state], 'reply': job['reply'],
            'preview': '/api/letters/' + job['id'] + '/preview' if (files / (job['id'] + '.png')).exists() else None,
            'error': 'Please ask the event team for help with your letter.' if state in ('failed', 'uncertain') else None}


class AccessVerifier:
    def __init__(self):
        self.domain = os.getenv('CF_ACCESS_TEAM_DOMAIN', '').rstrip('/')
        self.audience = os.getenv('CF_ACCESS_AUD', '')
        self.emails = {value.strip().lower() for value in os.getenv('ADMIN_EMAILS', '').split(',') if value.strip()}
        parts = urlsplit(self.domain)
        self.configured = bool(parts.scheme == 'https' and parts.hostname and
                               parts.hostname.endswith('.cloudflareaccess.com') and parts.path == '' and
                               not parts.username and not parts.password and self.audience and self.emails)
        self.client = PyJWKClient(self.domain + '/cdn-cgi/access/certs', timeout=5) if self.configured else None

    def verify(self, token):
        if not self.configured:
            raise HTTPException(503, 'Cloudflare Access is not configured. Admin access is closed.')
        if not token or len(token) > 16000:
            raise HTTPException(401, 'Cloudflare Access authentication required.')
        try:
            header = jwt.get_unverified_header(token)
            if header.get('alg') != 'RS256' or not isinstance(header.get('kid'), str):
                raise jwt.InvalidTokenError('Unsupported token header')
            key = self.client.get_signing_key_from_jwt(token).key
            claims = jwt.decode(token, key, algorithms=['RS256'], audience=self.audience,
                                issuer=self.domain, options={'require': ['exp', 'iat', 'iss', 'aud', 'email', 'sub']})
            if not isinstance(claims['email'], str):
                raise jwt.InvalidTokenError('Invalid identity')
            email = claims['email'].lower()
            if email not in self.emails:
                raise HTTPException(403, 'This administrator is not allowed.')
            return email
        except jwt.PyJWTError:
            raise HTTPException(401, 'Invalid or expired Cloudflare Access token.') from None


def create_app(store=None, service=None, verifier=None, start_background=True):
    access = verifier or AccessVerifier()
    data_dir = Path(os.getenv('SANTA_DATA_DIR', str(ROOT / 'runtime'))).expanduser().resolve()

    @asynccontextmanager
    async def lifespan(app):
        # Advisory OS lock prevents another process from rotating codes or sending duplicate jobs.
        app.state.store = store or Store(data_dir)
        lock = None
        if start_background:
            lock = (app.state.store.directory / 'service.lock').open('a')
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                lock.close()
                raise RuntimeError('Another Santa service is running. Use exactly one process.') from None
        app.state.service = service or Service(app.state.store)
        try:
            if start_background:
                app.state.service.start()
            yield
        finally:
            if start_background:
                await app.state.service.stop()
            if lock:
                lock.close()

    app = FastAPI(title='Santa Printer', lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    def admin(request: Request):
        return access.verify(request.headers.get('Cf-Access-Jwt-Assertion', ''))

    def same_origin(request):
        configured = os.getenv('PUBLIC_ORIGIN', '').rstrip('/')
        if configured:
            expected = configured
        else:
            expected = str(request.base_url).rstrip('/')
        if request.headers.get('origin') != expected:
            raise HTTPException(403, 'This action must come from the app itself.')
        if request.headers.get('content-type', '').split(';')[0].lower() != 'application/json':
            raise HTTPException(415, 'Use application/json.')

    @app.middleware('http')
    async def security(request, call_next):
        is_admin = request.url.path == '/admin' or request.url.path.startswith('/admin/')
        try:
            if is_admin:
                request.state.actor = await asyncio.to_thread(admin, request)
            # Bound JSON parsing, including chunked requests with no Content-Length.
            if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
                same_origin(request)
                chunks, length = [], 0
                async for chunk in request.stream():
                    length += len(chunk)
                    if length > 16384:
                        raise HTTPException(413, 'Request is too large.')
                    chunks.append(chunk)
                request._body = b''.join(chunks)
        except HTTPException as error:
            return JSONResponse({'detail': error.detail}, status_code=error.status_code,
                                headers={'Cache-Control': 'no-store'})
        token = request.cookies.get('santa_session', '')
        new_session = not re.fullmatch(r'[a-f0-9]{64}', token)
        request.state.owner = secrets.token_hex(32) if new_session else token
        response = await call_next(request)
        if new_session and not is_admin:
            response.set_cookie('santa_session', request.state.owner, max_age=7 * 86400, httponly=True,
                                samesite='strict', secure=os.getenv('PUBLIC_ORIGIN', '').startswith('https://'), path='/')
        response.headers.update({'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
                                 'Referrer-Policy': 'same-origin', 'X-Frame-Options': 'DENY',
                                 'Content-Security-Policy': "default-src 'self'; img-src 'self' blob:; style-src 'self'; script-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"})
        return response

    @app.exception_handler(Rejected)
    async def rejected(request, error):
        return JSONResponse({'detail': error.message}, status_code=error.status)

    @app.exception_handler(RequestValidationError)
    async def validation(request, error):
        # Do not echo request bodies or submitted codes in validation errors.
        fields = {str(item['loc'][-1]): item['msg'] for item in error.errors()}
        return JSONResponse({'detail': 'Please check the highlighted fields.', 'fields': fields}, status_code=422)

    @app.get('/')
    def index():
        return FileResponse(ROOT / 'web' / 'index.html')

    @app.get('/admin')
    @app.get('/admin/')
    def admin_page():
        return FileResponse(ROOT / 'web' / 'admin.html')

    @app.get('/api/status')
    def status(request: Request):
        cfg = request.app.state.store.settings()
        return {key: cfg[key] for key in ('visitor_title', 'visitor_intro', 'paused_submissions')}

    def client_ip(request):
        peer = request.client.host if request.client else '127.0.0.1'
        if peer == 'testclient':
            peer = '127.0.0.1'
        if peer in ('127.0.0.1', '::1', 'testclient') and os.getenv('TRUST_CLOUDFLARE_HEADERS', '').lower() == 'true':
            try:
                return str(ipaddress.ip_address(request.headers.get('CF-Connecting-IP', '')))
            except ValueError:
                pass
        return peer

    @app.post('/api/letters', status_code=202)
    def submit(letter: Letter, request: Request):
        db = request.app.state.store
        job = db.submit(request.state.owner, client_ip(request), letter.idempotency_key,
                        letter.name, letter.message, letter.code)
        return public_job(job, db.files)

    @app.get('/api/letters/{job_id}')
    def job(job_id: str, request: Request):
        db = request.app.state.store
        return public_job(db.get_job(job_id, request.state.owner), db.files)

    def image_response(db, job_id):
        path = db.files / (job_id + '.png')
        if not path.is_file():
            raise HTTPException(404, 'Preview not available yet.')
        return FileResponse(path, media_type='image/png')

    @app.get('/api/letters/{job_id}/preview')
    def preview(job_id: str, request: Request):
        db = request.app.state.store
        db.get_job(job_id, request.state.owner)
        return image_response(db, job_id)

    @app.get('/admin/api/overview')
    def overview(request: Request):
        db = request.app.state.store
        cfg = db.settings()
        return dict(actor=request.state.actor, metrics=db.metrics(), settings=cfg, codes=db.codes(),
                    printer=request.app.state.service.printer_status, server_time=db.clock(),
                    model_ready=bool(os.getenv('OPENAI_API_KEY', '').strip()), twilio_ready=santa_sms.configured())

    @app.get('/admin/api/jobs')
    def jobs(request: Request, state: str | None = None):
        return request.app.state.store.jobs(state)

    @app.get('/admin/api/jobs/{job_id}')
    def admin_job(job_id: str, request: Request):
        return request.app.state.store.get_job(job_id)

    @app.get('/admin/api/jobs/{job_id}/preview')
    def admin_preview(job_id: str, request: Request):
        db = request.app.state.store
        db.get_job(job_id)
        return image_response(db, job_id)

    @app.post('/admin/api/jobs/{job_id}/{action}')
    def job_action(job_id: str, action: str, request: Request):
        if action not in ('cancel', 'retry', 'review', 'reprint'):
            raise HTTPException(404)
        db = request.app.state.store
        return db.get_job(db.action(job_id, action, request.state.actor))

    @app.patch('/admin/api/settings')
    def settings(changes: Settings, request: Request):
        return request.app.state.store.update_settings(changes.model_dump(exclude_none=True), request.state.actor)

    @app.post('/admin/api/codes/resend')
    def resend(request: Request):
        request.app.state.store.resend(request.state.actor)
        return {'ok': True}

    @app.get('/admin/api/ip-controls')
    def ip_controls(request: Request):
        return request.app.state.store.ip_controls()

    @app.put('/admin/api/ip-controls')
    def set_ip_rule(body: IPRule, request: Request):
        db = request.app.state.store
        db.set_ip_rule(body.ip, body.blocked, body.per_minute, body.per_day, body.note, request.state.actor)
        return db.ip_controls()

    @app.post('/admin/api/ip-controls/remove')
    def remove_ip_rule(body: IPDelete, request: Request):
        db = request.app.state.store
        db.delete_ip_rule(body.ip, request.state.actor)
        return db.ip_controls()

    @app.get('/admin/api/sms-alerts')
    def sms_alerts(request: Request):
        return request.app.state.store.sms_history()

    @app.post('/admin/api/sms-alerts/{alert_id}/retry')
    def retry_sms(alert_id: str, request: Request):
        request.app.state.store.retry_sms(alert_id, request.state.actor)
        return {'ok': True}

    @app.get('/admin/api/deliveries')
    def deliveries(request: Request):
        return request.app.state.store.history('deliveries')

    @app.get('/admin/api/audit')
    def audit(request: Request):
        return request.app.state.store.history('audit')

    @app.post('/admin/api/printer/status')
    async def printer_status(request: Request):
        # Serialize diagnostic socket access with the worker using a shared async lock.
        return await request.app.state.service.check_printer()

    @app.post('/admin/api/diagnostics/preview')
    async def diagnostics(body: Preview, request: Request):
        return await diagnostic_image(body, request, False)

    @app.post('/admin/api/diagnostics/test-print', status_code=202)
    async def test_print(body: Preview, request: Request):
        return await diagnostic_image(body, request, True)

    async def diagnostic_image(body, request, printing):
        import print_nt8360 as printer
        import uuid
        db = request.app.state.store
        cfg = db.settings()
        try:
            image, _ = await asyncio.to_thread(printer.render_letter, body.text, ROOT / 'santa-receipt-border.png',
                                               font_size=cfg['font_size'])
        except ValueError as error:
            raise HTTPException(400, str(error)) from None
        if not printing:
            import io
            from fastapi.responses import Response
            buffer = io.BytesIO()
            image.save(buffer, format='PNG')
            with db.transaction() as connection:
                db.audit_in(connection, request.state.actor, 'render diagnostic preview')
            return Response(buffer.getvalue(), media_type='image/png')
        job_id = uuid.uuid4().hex
        with db.transaction() as connection:
            db.capacity_in(connection, cfg)
            image.save(db.files / (job_id + '.png'), format='PNG')
            (db.files / (job_id + '.bin')).write_bytes(printer.build_raster_job(image, cut=cfg['cut']))
            connection.execute('INSERT INTO jobs(id,owner,idem,fingerprint,name,message,state,reply,created,updated,parent,config) '
                               "VALUES (?,?,?,?,?,?,'ready',?,?,?,?,?)",
                               (job_id, 'admin', job_id, job_id, '', 'Diagnostic test print', body.text,
                                db.clock(), db.clock(), 'diagnostic', json.dumps(cfg)))
            db.audit_in(connection, request.state.actor, 'test print', job_id)
        return {'id': job_id, 'state': 'ready'}

    app.mount('/assets', StaticFiles(directory=ROOT / 'web'), name='assets')
    return app


app = create_app()
