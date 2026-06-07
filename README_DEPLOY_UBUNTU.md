# Ubuntu 22.04 Deployment Guide

This guide deploys AScrapper / OzHome Monitor on Ubuntu 22.04 Server with SQL Server, Telegram polling, CloakBrowser automation, Xvfb, and systemd.

Production uses SQL Server only. Do not configure SQLite for production.

## 1. Initial Server Packages

The deployment script installs:

- Python venv tooling
- build tools needed by Python packages
- CloakBrowser and its bundled Chromium runtime
- Google Chrome Stable and Chrome runtime libraries, retained only as a system/browser-library compatibility fallback
- Xvfb for headless server execution with `HEADLESS=0`
- `unixodbc` and `unixodbc-dev`
- Microsoft ODBC Driver 18 for SQL Server
- optional SQL Server tools package `mssql-tools18`

Run from the project checkout or with `REPO_URL` set:

```bash
chmod +x deploy.sh
APP_DIR="$HOME/apps/ascrapper" SERVICE_NAME=ascrapper ./deploy.sh
```

The script creates `.env` if missing and appends missing keys only. It never overwrites existing `.env` values silently.

## 2. Microsoft ODBC Driver 18

For Ubuntu 22.04, the script follows Microsoft's package-repository package flow:

```bash
curl -fsSLo /tmp/packages-microsoft-prod.deb https://packages.microsoft.com/config/ubuntu/22.04/packages-microsoft-prod.deb
sudo dpkg -i /tmp/packages-microsoft-prod.deb
sudo apt-get update
sudo ACCEPT_EULA=Y apt-get install -y msodbcsql18 mssql-tools18 unixodbc unixodbc-dev
```

If `packages-microsoft-prod` is already installed, the script reuses it to avoid duplicate repo/key files.

## 3. .env Template

Fill real values on the server only:

```dotenv
TELEGRAM_BOT_TOKEN=
DB_HOST=127.0.0.1
DB_PORT=1433
DB_NAME=
DB_USER=
DB_PASSWORD=
DB_DRIVER=ODBC Driver 18 for SQL Server
DB_ENCRYPT=yes
DB_TRUST_SERVER_CERTIFICATE=yes
DB_TIMEOUT=30
HEADLESS=0
PERF_PROFILE=normal
OUTPUT_DIR=output
LOG_DIR=logs
RUNTIME_DIR=runtime
PYTHONUNBUFFERED=1
APP_ENV=production
BROWSER_ENGINE=cloak
CLOAK_PROFILE_DIR=rea_profile
CLOAK_FINGERPRINT_SEED=42069
CLOAK_FINGERPRINT_PLATFORM=windows
CLOAK_FINGERPRINT_STORAGE_QUOTA=5000
CLOAK_VIEWPORT_WIDTH=1365
CLOAK_VIEWPORT_HEIGHT=768
CLOAK_LOCALE=en-AU
CLOAK_TIMEZONE=Australia/Sydney
CLOAK_DISABLE_HTTP2=1
CLOAK_USE_PERSISTENT_CONTEXT=1
CLOAK_HEADLESS=0
MODULE2_PROFILE_BASE_DIR=
LOW_BANDWIDTH_MODE=0
BLOCK_HEAVY_RESOURCES=0
BLOCK_TRACKERS=0
BLOCK_IMAGES=0
BLOCK_MEDIA=0
BLOCK_FONTS=0
BLOCK_MAPS=0
BLOCK_ADS=0
BLOCK_ANALYTICS=0
BROWSER_BLOCK_GRACE_SECONDS=30
BROWSER_BLOCK_POLL_SECONDS=1.0
BROWSER_NO_RESULTS_STABLE_SECONDS=1.0
BROWSER_KPSDK_SAME_SESSION_RECHECKS=2
BROWSER_KPSDK_SETTLE_SECONDS=10
BROWSER_PAGE_STATE_DEBUG=1
BROWSER_USE_RUNTIME_PROFILE_STATE=1
```

Optional compatibility names are still accepted by the app: `SQLSERVER_DRIVER`, `SQLSERVER_SERVER`, `SQLSERVER_DATABASE`, `SQLSERVER_USERNAME`, `SQLSERVER_PASSWORD`, `SQLSERVER_ENCRYPT`, and `SQLSERVER_TRUST_SERVER_CERTIFICATE`. Prefer the `DB_*` names for Ubuntu production.

For browser diagnosis, the `LOW_BANDWIDTH_MODE` and `BLOCK_*` values above disable resource blocking so Ubuntu matches the proven Windows browser behavior as closely as possible. Turn them back on only after the normal profile/page flow is confirmed.

CloakBrowser uses a persistent Playwright-style context. The profile path above resolves to `/opt/A-scrapper/rea_profile` when the service runs from `/opt/A-scrapper`.

Do not add a homepage warm-up by default. The verified Ubuntu CloakBrowser test loaded the target list page successfully, while the extra homepage request produced an unrelated 429.

Page-state classification is DOM-first. If listing cards or normal detail content are present, network-only `HTTP 429` or `x-kpsdk` entries do not trigger recovery. Keep `BROWSER_PAGE_STATE_DEBUG=1` while stabilizing Ubuntu runs so logs include `page_state`, `cards_found`, `network_reason`, `html_length`, and `body_text_length`.

KPSDK shells are allowed to settle in the same browser session before profile rotation. Module1, Module2, and Module3 wait `BROWSER_KPSDK_SETTLE_SECONDS`, reload the same URL with the same profile, and reclassify up to `BROWSER_KPSDK_SAME_SESSION_RECHECKS` times. Recovery/profile rotation runs only if the page remains blocked after those rechecks.

Treat scan trust explicitly: `listings` and DOM-confirmed `no_results` are trusted; `blocked_*`, `blank_render`, `render_timeout`, and `unknown` are untrusted. Untrusted scans must go retry-wait or technical retry and must not be interpreted as zero listings or used for lifecycle removal.

Smoke tools support explicit isolation flags. Use `--fresh-profile` to create a per-run browser profile, or `--profile-dir output/cloak_tests/my_profile` to force a specific profile and bypass runtime profile state. Module2 smoke tools default to fresh checkpoints; pass `--resume` only when intentionally validating checkpoint resume. `--max-windows` / `--module2-max-windows` are smoke-test limits only and produce `partial_test_limit` when they stop a sweep early.

Keep permissions restricted:

```bash
chmod 600 "$APP_DIR/.env"
```

## 4. Systemd Service Preview

`deploy.sh` writes `/etc/systemd/system/ascrapper.service` similar to:

```ini
[Unit]
Description=AScrapper / OzHome Monitor Telegram bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ascrapper
Group=ascrapper
WorkingDirectory=/home/ascrapper/apps/ascrapper
EnvironmentFile=/home/ascrapper/apps/ascrapper/.env
Environment=PYTHONUNBUFFERED=1
ExecStart=/usr/bin/env HEADLESS=0 /usr/bin/xvfb-run -a -s "-screen 0 1365x768x24" /home/ascrapper/apps/ascrapper/.venv/bin/python telegram_bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

The service must run as a normal app user, not root.

## 5. Manual Smoke Tests

Run these before relying on systemd:

```bash
cd "$APP_DIR"
. .venv/bin/activate

python -m tools.check_config
python -m tools.check_db_connection
python -m cloakbrowser info
HEADLESS=0 xvfb-run -a -s "-screen 0 1365x768x24" python -m tools.test_cloak_single_page --fresh-profile --wait 15
python -m compileall .
```

Debug realestate.com.au rendering under the same Xvfb/browser architecture used by the service:

```bash
HEADLESS=0 xvfb-run -a -s "-screen 0 1920x1080x24" python -m tools.debug_rea_page --wait 15
HEADLESS=0 xvfb-run -a -s "-screen 0 1920x1080x24" python -m tools.debug_rea_page --no-blocks --temp-profile --wait 15
HEADLESS=0 xvfb-run -a -s "-screen 0 1365x768x24" python -m tools.test_module1_cloak_one_page --timeout 25
python tools/test_browser_recovery.py
```

Manual module smoke test with one known area URL:

```bash
HEADLESS=0 xvfb-run -a -s "-screen 0 1365x768x24" python monitor.py --mode light --pages 1 --url "https://www.realestate.com.au/buy/in-petersham,+nsw+2049/list-1?activeSort=list-date"
```

Manual bot startup before systemd:

```bash
HEADLESS=0 xvfb-run -a -s "-screen 0 1365x768x24" python telegram_bot.py
```

Stop it with `Ctrl+C` after confirming startup and DB schema checks pass.

## 6. Browser Profile Recovery

Ubuntu should use the same persistent browser/profile lifecycle that worked on Windows, with Linux-native paths only. The browser engine is now CloakBrowser instead of the previous Chrome automation stack:

- persistent profile: `rea_profile` under the project directory, for example `/opt/A-scrapper/rea_profile`
- persistent context: `cloakbrowser.launch_persistent_context(...)`
- profile state: `output/browser_profile_state.json`
- invalid Windows paths such as `C:\Users\...\rea_profile` are ignored on Linux
- recovery must rebuild or record `/opt/A-scrapper/rea_profile`, never `/opt/A-scrapper/C:\Users\...`

Clean bad profile state safely:

```bash
cd /opt/A-scrapper
sudo systemctl stop ascrapper
sudo -u ascrapper bash -lc 'cd /opt/A-scrapper && mkdir -p runtime/profile_state_backup && if [ -f output/browser_profile_state.json ]; then mv output/browser_profile_state.json runtime/profile_state_backup/browser_profile_state.$(date +%Y%m%d_%H%M%S).json; fi'
sudo -u ascrapper bash -lc 'cd /opt/A-scrapper && find . -maxdepth 1 -type d -name "C:*" -print'
sudo -u ascrapper bash -lc 'cd /opt/A-scrapper && mkdir -p runtime/bad_profile_dirs && find . -maxdepth 1 -type d -name "C:*" -exec mv -t runtime/bad_profile_dirs -- {} +'
```

Recover or warm the Ubuntu profile:

```bash
sudo -u ascrapper bash -lc 'cd /opt/A-scrapper && .venv/bin/python -m tools.recover_browser_profile --reset-state-only'
sudo -u ascrapper bash -lc 'cd /opt/A-scrapper && HEADLESS=0 xvfb-run -a -s "-screen 0 1920x1080x24" .venv/bin/python -m tools.recover_browser_profile --wait 15'
sudo systemctl start ascrapper
```

If recovery prints `blocked_reason=realestate_rate_limited_or_blocked_http_429` or `blocked_reason=realestate_rate_limited_or_blocked_kpsdk`, the browser started but realestate.com.au returned a rate-limit/block shell instead of listing content. That state is retryable operationally and should not be treated as "zero listings".

Run the CloakBrowser migration smoke tests:

```bash
sudo -u ascrapper bash -lc 'cd /opt/A-scrapper && HEADLESS=0 xvfb-run -a -s "-screen 0 1365x768x24" .venv/bin/python -m tools.test_cloak_single_page --fresh-profile --wait 15'
sudo -u ascrapper bash -lc 'cd /opt/A-scrapper && HEADLESS=0 xvfb-run -a -s "-screen 0 1365x768x24" .venv/bin/python -m tools.test_module1_cloak_one_page --timeout 25'
sudo -u ascrapper bash -lc 'cd /opt/A-scrapper && HEADLESS=0 xvfb-run -a -s "-screen 0 1365x768x24" .venv/bin/python -m tools.test_module2_cloak_small --max-high 1500000 --max-pages-per-window 1 --max-windows 3'
sudo -u ascrapper bash -lc 'cd /opt/A-scrapper && HEADLESS=0 xvfb-run -a -s "-screen 0 1365x768x24" .venv/bin/python -m tools.test_module3_cloak_single_listing --wait 25'
```

## 7. Systemd Commands

```bash
sudo systemctl daemon-reload
sudo systemctl enable ascrapper
sudo systemctl restart ascrapper
sudo systemctl status ascrapper --no-pager
sudo journalctl -u ascrapper -f
```

Recent logs:

```bash
sudo journalctl -u ascrapper -n 100 --no-pager
```

## 8. Common Failures

- ODBC Driver 18 not found: run `odbcinst -q -d` and confirm `ODBC Driver 18 for SQL Server` is listed. Re-run `deploy.sh` if missing.
- SQL Server login timeout: verify SQL Server is listening on `DB_HOST:DB_PORT`, firewall rules allow it, and SQL Server accepts TCP connections.
- Encrypt/trust certificate error: for self-signed local SQL Server certificates, keep `DB_ENCRYPT=yes` and `DB_TRUST_SERVER_CERTIFICATE=yes`, or install a trusted certificate and set trust accordingly.
- Chrome not found: run `google-chrome --version`; re-run deploy with `INSTALL_CHROME=1`.
- CloakBrowser binary missing: run `.venv/bin/python -m cloakbrowser install` and `.venv/bin/python -m cloakbrowser info`.
- CloakBrowser launch crash: run `HEADLESS=0 xvfb-run -a -s "-screen 0 1365x768x24" python -m tools.test_cloak_single_page --fresh-profile --wait 15` and inspect the generated JSON/HTML/screenshot under `output/cloak_tests`.
- Module1 returns zero rows on Ubuntu: ensure the service uses Xvfb with `HEADLESS=0`, disable resource blocking with the `LOW_BANDWIDTH_MODE=0` and `BLOCK_*=0` settings above, then run `tools.debug_rea_page`.
- HTTP 429 / KPSDK shell: `tools.debug_rea_page` or `tools.recover_browser_profile` may show `window.KPSDK`, `ips.js`, `x-kpsdk` headers, small HTML length, no `__NEXT_DATA__`, and no listing cards. The app classifies this as `realestate_rate_limited_or_blocked_*`, moves jobs to retry wait, and does not report "no listings".
- Telegram token missing: fill `TELEGRAM_BOT_TOKEN` in `.env`; the app fails fast with a clear error.
- Permission denied on output/log directory: run `sudo chown -R <app-user>:<app-user> "$APP_DIR/output" "$APP_DIR/logs" "$APP_DIR/runtime"`.
- Service restarts repeatedly: inspect `sudo journalctl -u ascrapper -n 100 --no-pager`; startup diagnostics log Python, platform, DB host/name/driver, output dir, Chrome mode, and no secrets.

## 9. Validation Checklist

Expected production validation:

```bash
cd "$APP_DIR"
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m cloakbrowser install
.venv/bin/python -m compileall .
.venv/bin/python -m tools.check_config
.venv/bin/python -m tools.check_db_connection
HEADLESS=0 xvfb-run -a -s "-screen 0 1365x768x24" .venv/bin/python -m tools.test_cloak_single_page --fresh-profile --wait 15
sudo systemctl restart ascrapper
sudo systemctl status ascrapper --no-pager
sudo journalctl -u ascrapper -f
```

The database smoke test runs `SELECT 1` only and does not modify production data.
