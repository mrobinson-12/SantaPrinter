# Santa Printer event operations

The app runs one shared runtime with two AI workers, one print worker, and independent code/monitor loops. The public listener stays at `127.0.0.1:8000`. AI requests no longer delay already prepared receipts.

## Update your existing Linux server

Your existing installation is already configured. Keep its `.env` file and printer settings. These instructions assume the checkout is `~/SantaPrinter` and the systemd service is called `santa-printer`, as in the existing setup guide.

SSH into the server using the account that owns the checkout. Paste the following block. It stops the app, pulls the update, backs up your actual configured data directory, installs dependencies, and starts the app with the new watchdog and daily backup timer. The parentheses make the block stop on an error without closing your SSH session.

```sh
(
set -e
cd ~/SantaPrinter
if systemctl cat santa-printer-watchdog.timer >/dev/null 2>&1; then
    sudo systemctl stop santa-printer-watchdog.timer santa-printer-watchdog.service
fi
sudo systemctl stop santa-printer
git pull --ff-only
.venv/bin/python santa_tools.py backup "$PWD/santa-backups/update-$(date -u +%Y%m%dT%H%M%SZ)"
.venv/bin/python -m pip install -r requirements.txt
service_user=$(systemctl show santa-printer -p User --value)
sudo .venv/bin/python deploy/install-linux-services.py --user "${service_user:-root}" --backup-root "$PWD/santa-backups" --install
sudo systemctl status santa-printer --no-pager
)
```

The last command should show **active (running)**. Open your normal website's `/admin` page, run the readiness checks under Diagnostics, and print one test letter to inspect it. If the block reports an error, stop and copy that error before trying additional commands.

The core upgrade works with your existing configuration. **Twilio SMS is an optional setup step below**, not requirements for starting the updated app. Do not copy `.env.example` over your real `.env`.

Use Python 3.10 or later. Linux needs the timezone database (`tzdata`) and `logrotate` for schedules and rotated file logs. On Debian/Ubuntu, install missing packages with `sudo apt-get install tzdata logrotate`. Dependency pins reflect the verified development environment; pip also resolves interpreter-specific conditional dependencies.

The new daily backup runs at **05:00 Australia/Brisbane**, briefly stopping the app to make a consistent copy. Backups live in `~/SantaPrinter/santa-backups/`. App logs now live in `runtime/service.log`, rotated daily or above 10 MB, with seven compressed rotations. Cloudflared keeps its existing configuration and service.

## If the app does not start after updating

Read the error with:

```sh
sudo systemctl status santa-printer --no-pager -l
tail -n 60 ~/SantaPrinter/runtime/service.log
```

After fixing the reported problem, restart with:

```sh
sudo systemctl reset-failed santa-printer
sudo systemctl restart santa-printer
```

## Fresh installations and rollback

For a fresh installation, configure `.env` and the virtual environment first. Render units for review with `.venv/bin/python deploy/install-linux-services.py --user YOUR_SERVICE_USER --backup-root /YOUR/BACKUP/DIRECTORY`, then add `--install` when invoking the same command with sudo. The service user must own the checkout/runtime and be able to read `.env` and TLS files.

Startup creates an additional pre-migration snapshot in `santa-backups/` when upgrading an existing job schema. For rollback, stop the app and watchdog, restore the snapshot using the new `santa_tools.py`, check out the previous Git commit shown by `git reflog`, reinstall that commit's requirements, then start the old app service. Disable the new watchdog and backup timers until upgrading again. Restore also keeps a pre-restore safety snapshot outside runtime.

Acknowledging an incident hides its card and suppresses reminders until the fault clears and occurs again. Acknowledgement does not release printer holds or resolve uncertain transmissions.

## Configure SMS

Put `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, and the SMS-capable international `TWILIO_FROM` number in `.env`, then restart. These secrets are never returned to browsers or exported in diagnostics. Configure international recipient numbers and weekly hours under **Diagnostics → Fault notifications**. Monday through Sunday are selectable; overnight windows finish the next day in Australia/Brisbane.

Save, send a test, and wait for **delivered** for every recipient before enabling SMS. Twilio acceptance or `sent` is not delivery confirmation. Changing recipients, hours or credentials requires another test. Ensure the Twilio account/sender can send to the intended destinations; test from the actual deployment.

Notifications are immediate for uncertain transmissions, after 60 seconds for persistent printer/code/worker faults, and after two minutes without progress for a nonempty queue. Prepared-letter fallback produces a degraded incident after 60 seconds. Reminders occur every 15 minutes during scheduled hours. Acknowledgement suppresses reminders; recovery still sends one message. Faults remain visible until resolved. Requests with lost submission responses are marked `unknown` and are not blindly repeated. Retryable provider rejections receive bounded retries. The notification history shows provider identifiers and delivery states.

The independent watchdog checks `/healthz` every 30 seconds and drains the same outbox. It can notify when the app crashes or workers become unhealthy, while the VM and its internet are still working. Host power loss or a complete internet outage requires a separate external monitor.

## Before opening

Run `.venv/bin/python santa_tools.py doctor` and the dashboard readiness checks. Resolve faults, verify the current code on the display, and inspect one physical test receipt. Use the explicit paid AI test if connectivity is unverified. Test the public login on your owner device. Confirm SMS test delivery, active schedule, free storage and paper supply.

The admission target is ten letters per minute. Measure the real printer using a timed batch of diagnostic receipts, inspect all output, and reduce the global limit if it cannot keep up. Rendering, TCP delivery, and live sensors cannot prove physical receipt completion.

## Fixing faults

- **Paper empty / cover open / unreachable:** fix the physical fault, then check again. The hardware hold clears after two good checks. Paper low warns without holding. Manual pauses stay paused.
- **Possible partial receipt:** inspect the printer, then choose *Receipt appeared*, *Missing or partial — reprint*, or *Cancel after inspection*. A replacement uses the saved bytes. Printing stays paused until you resume; unresolved uncertain jobs prevent resuming.
- **AI outage:** transient requests retry after 5 and 15 seconds. Prepared letters use only the validated name and fixed local template, never the visitor's wish. Disable fallback in Settings if you prefer manual retries. Saved replies are reused after rendering retries or restarts.
- **Damaged receipt artifact:** retry rendering the saved reply. No print connection is made for an invalid file.
- **Wrong printer address:** save the correct destination. All queued receipts use it; layout/bytes stay unchanged. No uncertain job is automatically resent.
- **Public site/login unavailable:** use SSH: `sudo systemctl status santa-printer`, inspect `runtime/service.log`, run `doctor`, then `sudo systemctl restart santa-printer` after correcting configuration. The restart limit prevents endless crash loops; use `sudo systemctl reset-failed santa-printer` after fixing a startup problem.

Download the diagnostic bundle for sharing. It uses a strict allowlist and excludes credentials, codes, visitor messages/names, IPs and audit detail. Full runtime backups contain private data and must not be shared as diagnostics.

## Backup, restore and retention

Manual backup/restore refuses while the app holds its process lock. Stop the watchdog as well before maintenance. Snapshot permissions are restricted; the database is a standalone consistent SQLite copy and receipt files are integrity checked against a manifest during restore. Preserve `.env` separately.

```sh
.venv/bin/python santa_tools.py backup /secure/backups/event-snapshot
.venv/bin/python santa_tools.py restore /secure/backups/event-snapshot
.venv/bin/python santa_tools.py prune-backups /secure/backups
```

The daily job prunes snapshots after the configured letter-retention duration recorded in each snapshot. A letter copied shortly before app deletion can remain in a snapshot for another full retention period (normally another 24 hours). Automatic pre-upgrade and pre-restore snapshots also need pruning in their parent directory. Restrict backup access and include them in your deletion policy. Unfinished/uncertain jobs remain preserved in the live app until resolved; incident, notification and diagnostic history expires after seven days.

## Rehearsal and acceptance

```sh
.venv/bin/python -m pytest -q
.venv/bin/python tests/browser_check.py
.venv/bin/python tests/rehearse.py
```

These checks use temporary databases and fake AI/printer/SMS services. The rehearsal accelerates three minutes at ten letters per minute; it does not measure real hardware speed. Before opening, rehearse paper-out, cover-open, disconnected printer, interrupted transmission, app restart, blocked AI connectivity, and Cloudflare failure with the deployed equipment. Verify no automatic duplicate receipt, preserved pending jobs, working fallback, correct Cloudflare login, and fault/recovery SMS within scheduled hours.
