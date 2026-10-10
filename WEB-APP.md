# Santa Printer web app

The server runs on the same LAN as the webhook and receipt printer. Cloudflare Tunnel exposes the visitor app through HTTPS. All AI calls, code delivery, rendering, and printer connections happen on this computer.

## Start locally

Python 3.10+ is required for the web app. The original CLI scripts are unchanged.

```sh
cd /Users/michael/SantaPrinter
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
chmod 600 .env
```

Edit `.env` and provide the OpenAI key, public HTTPS origin (no path), Access team domain, Access application audience, and a comma-separated list of approved administrator emails. Environment variables take precedence over `.env`. Never put credentials in a browser bundle or source control.

For local visitor UI testing, leave `PUBLIC_ORIGIN` blank. For the tunnel, set it to the exact public HTTPS origin. In production, secure browser cookies and same-origin checks use this setting. A local browser cannot administer the app without a valid Cloudflare Access token.

```sh
.venv/bin/python run_web.py
```

Open http://127.0.0.1:8000. Startup immediately generates and POSTs a four-digit code to `http://192.168.1.103/code`; it also queries the printer's read-only status. Keep the computer awake during the event. Do not run with reload or multiple workers; an OS lock prevents a second worker process.

## Cloudflare configuration

1. Use your existing tunnel, pointing the public hostname at `http://127.0.0.1:8000`. The sample in `deploy/cloudflared.example.yml` shows the local-service route. No inbound router ports are needed.
2. Create a self-hosted Cloudflare Access application for **both** `santa.example.com/admin` and `santa.example.com/admin/*`. This includes `/admin/api/*`; do not protect only the HTML page. Leave visitor routes public.
3. Enable email one-time PIN and an Allow policy for your exact approved emails. Do not allow every email address. SMS requires an identity provider that supports it.
4. Copy the Access application AUD into `CF_ACCESS_AUD`, its `https://TEAM.cloudflareaccess.com` issuer into `CF_ACCESS_TEAM_DOMAIN`, and the same email allowlist into `ADMIN_EMAILS`.
5. Set `PUBLIC_ORIGIN=https://santa.example.com`. Restart the server after changing environment configuration.
6. Disable Cloudflare caching for `/api/*` and `/admin*`. The server also returns `Cache-Control: no-store`.
7. Run cloudflared as a service using your existing tunnel's supported service setup. Keep the origin bound to loopback. Trust Cloudflare IP headers only when the origin is reachable exclusively through cloudflared/local trusted processes.

The backend validates Access JWT signatures, issuer, audience, expiry, and email on every admin page/API request. Unconfigured Access returns 503; missing or invalid authentication returns 401; unapproved emails return 403. There is no password fallback or debug authentication bypass.

## Run automatically

On this Mac, after filling in `.env` and stopping any foreground server:

```sh
.venv/bin/python deploy/install-macos-service.py
```

This installs a per-user launchd agent that starts at login and restarts on failure. Logs are in `runtime/`; request access logs are disabled. Restart after changing `.env`:

```sh
launchctl kickstart -k gui/$(id -u)/local.santa-printer
```

Remove the service if needed:

```sh
launchctl bootout gui/$(id -u)/local.santa-printer
rm ~/Library/LaunchAgents/local.santa-printer.plist
```

On Linux, adapt `deploy/santa-printer.service` to the deployment user and checkout paths, install it in systemd, then enable it. Cloudflared needs its own service.

## Operation

The mobile visitor form accepts an optional first name (60 characters), message (1,000 characters), and four ASCII digits. Drafts remain in the tab's session storage during connection failures; access codes are never persisted there. Each letter is assigned an idempotency key before transmission and belongs to a cryptographically random HTTP-only browser session. Other visitors cannot retrieve it.

Codes rotate at five-minute UTC boundaries. Both the current and immediately preceding interval's generated code are valid, independently of webhook delivery. Rotation state survives restarts. Skipped intervals are not delivered retrospectively, and backwards clock changes block acceptance until the clock catches up or is corrected. First-ever startup creates one code; no previous code exists yet. Delivery requires a 2xx response; redirects are rejected. Failures retry after 5, 15, 30, then 60 seconds between attempts, until the code is superseded. The admin panel can resend the current code without changing it.

Generation uses two bounded workers; physical transmissions remain serial. Jobs snapshot receipt layout settings when accepted and use the current printer destination at transmission. Generation is saved before rendering, so a rendering retry can reuse the reply. PNG previews and exact ESC/POS bytes are stored outside the public asset directory. New settings apply to new jobs; reprints use exact saved bytes and the current printer destination.

The admin overview shows codes, delivery results, printer status, retained counts, and separate controls for pausing submissions and printing. Paused printing still allows queued letters to generate. The queue supports filtering, viewing, cancellation before transmission, explicit retries, and confirmed saved-receipt reprints. Diagnostics include a render-only preview, a confirmed physical test print, delivery history, and an audit trail attributed to the verified administrator email.

A failure before connecting retries safely; a socket failure or restart during transmission marks the job **uncertain** and pauses printing. Check the physical receipt, choose the physical outcome (appeared, missing/partial and reprint, or cancel), then resume printing. The server never automatically repeats a potentially partial send. “Sent to printer” means successful transmission, not confirmed physical completion.

Letters and previews are purged 24 hours after their last update by default (configurable 1–168 hours). Unfinished/uncertain jobs are preserved until resolved. Diagnostic and audit records expire after seven days. Counts describe retained jobs, not all-time totals. Browser/session and global cooldowns limit abuse; shared event codes are not single-use credentials.

## Tests and launch checks

```sh
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
.venv/bin/python -m playwright install chromium
.venv/bin/python tests/browser_check.py
```

Unit/integration tests use temporary SQLite storage, a fake clock, signed test JWTs, mocked AI/printer functions, a local HTTP webhook, and a local TCP status responder. Browser checks start an isolated server with test-only authentication in the test harness and mocked hardware; production contains no auth bypass. Screenshots go into ignored `runtime/qa/`.

Before opening the event, confirm the current code is delivered in `/admin`, run render-only diagnostics, explicitly send one test receipt, and inspect the physical result. Verify the approved email PIN flow through the actual tunnel and try `/admin/api/overview` in a signed-out browser. Without credentials/tunnel configuration, live API generation and Cloudflare authentication cannot be verified.

## Data and backup

`SANTA_DATA_DIR` defaults to `runtime/` and contains SQLite, WAL files, receipt artifacts, and the singleton process lock. Restrict access to this directory. To take a consistent simple backup, stop the server and copy the whole directory; restart afterwards. Never expose this directory through your web server. Keep the original border file and a supported font available on the server.


## Lights on Elizabeth branding and IP controls

The supplied Lights on Elizabeth logo appears on the visitor and event-team pages. Existing installations automatically get the new display introduction only if they still use the original default text; custom introductions are preserved.

In `/admin`, open **IP controls** to configure default per-IP submission limits (5 per minute and 10 per rolling 24 hours initially), inspect recent IP usage, or create an exact IPv4/IPv6 rule. Each rule can block submissions or override either limit independently; blank overrides inherit defaults. The rule can be edited, unblocked, or removed to restore defaults. Removing a rule does not clear recent usage. Saved rules persist until removed. All rule changes are recorded in the admin audit.

Limits apply to new visitor letter submission attempts with valid form fields, including incorrect codes and rejected cooldown/queue attempts. Replays of an accepted idempotent submission do not consume quota; blocked IPs cannot replay submission requests. Pages, status polls, existing letter retrieval, diagnostics, and admin actions do not consume visitor quota. Rate-limited and blocked requests enqueue no work and make no AI/printer calls. Usage survives restarts and letter purging, expires after 24 hours, and is kept separately from receipt retention. Historical jobs created before this upgrade have no recorded IP.

`TRUST_CLOUDFLARE_HEADERS=true` must be set when the origin is bound to loopback and cloudflared is the trusted proxy. Otherwise visitors share the proxy's IP and quota. Only loopback connections can supply `CF-Connecting-IP`; `X-Forwarded-For` is ignored. IPv4-mapped IPv6 addresses are normalised to IPv4. Visitors sharing Wi-Fi/mobile carrier NAT may share an IP, so adjust limits for your event. Existing browser, global, queue, and failed-code limits still apply in addition to the new controls.

The database upgrade is automatic and preserves jobs/settings. To deploy the committed update in the VM/LXC:

```sh
cd ~/SantaPrinter
git pull --ff-only
.venv/bin/python -m pip install -r requirements.txt
sudo systemctl restart santa-printer
```

## Reliability toolkit upgrade

See [OPERATIONS.md](OPERATIONS.md) for the current worker/recovery behavior, Linux upgrade and rollback, LAN owner administration, scheduled Twilio alerts, backups, and event rehearsal. The older single-worker/manual-generation-recovery descriptions above are superseded by this toolkit. Queued jobs retain receipt layout and bytes but use the current printer destination. Interrupted generation resumes automatically, and prepared Santa letters are enabled as the AI-outage fallback. Queue API results now contain `items`, `total`, `page`, and `page_size`.
