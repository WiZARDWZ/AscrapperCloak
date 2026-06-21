# Ubuntu Release

Ubuntu production keeps the existing systemd + Xvfb architecture. The deployment environment should set `RUNTIME_PROFILE=ubuntu_prod`; `.env` overrides remain supported for SQL Server, Telegram, paths, and browser settings.

```bash
systemctl stop ascrapper
git pull
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m pytest tests/test_trusted_baseline_enforcement.py -q
HEADLESS=0 xvfb-run -a -s "-screen 0 1365x768x24" python tools/verify_trusted_baseline_scan.py --url "REAL_URL"
systemctl start ascrapper
```

The systemd service may continue using:

```bash
xvfb-run -a -s "-screen 0 1365x768x24" python telegram_bot.py
```

Behavior remains explicit:

- Linux headed mode requires `DISPLAY`, normally supplied by `xvfb-run`.
- Linux headless mode does not require Xvfb.
- SQL Server defaults to ODBC Driver 18 and production credentials may come from `.env` or service environment variables.
- Runtime/output paths remain under the checkout unless overridden, including checkouts below `/opt`.
