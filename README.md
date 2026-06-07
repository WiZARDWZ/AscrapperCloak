# ascrapper

## Pipeline / Monitor
- اجرای کامل: `python monitor.py --mode full --url "<AREA_SEARCH_URL>"`
- چک سبک (فقط module1 + full_scan=False): `python monitor.py --mode light --pages 1 --url "<AREA_SEARCH_URL>"`
- اگر `module1` صفر ردیف بدهد، پایپ‌لاین fail-fast می‌شود و پیام راهنمای Xvfb می‌دهد.

## Telegram Bot
1. وابستگی‌ها را نصب کنید: `pip install -r requirements.txt`.
2. تنظیمات حساس، مخصوصاً `TELEGRAM_BOT_TOKEN`، را فقط از طریق environment یا فایل محلی `.env` وارد کنید؛ token را داخل `config.py` ننویسید.
3. اجرا: `python telegram_bot.py`

### UX دکمه‌ای
- `➕ افزودن لینک محله`
- `📌 لینک‌های من` (با دکمه‌های `▶️ اجرای الان` و `🗑 حذف`)
- `▶️ اجرای استخراج الان`
- `📤 دریافت اکسل`
- `⏰ تنظیم زمان پایش`
- `- `ℹ️ راهنما`

Commands فقط fallback هستند: `/add`, `/list`, `/run`, `/set_interval`, `/export`.

## Ubuntu 22 (بدون GUI) — حالت قابل اتکا

> **الزامی:** روی Ubuntu 22 برای این پروژه از Xvfb استفاده کنید (`HEADLESS=0 + xvfb-run`).

نصب:

```bash
sudo apt-get update && sudo apt-get install -y xvfb
```

اجرای بات:

```bash
HEADLESS=0 xvfb-run -a -s "-screen 0 1365x768x24" python telegram_bot.py
```

تست module1:

```bash
HEADLESS=0 xvfb-run -a -s "-screen 0 1365x768x24" python module1_list_scraper.py
```


## SQL Server configuration
Production uses SQL Server through ODBC. Prefer these env vars: `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_DRIVER`, `DB_ENCRYPT`, `DB_TRUST_SERVER_CERTIFICATE`, `DB_TIMEOUT`.

Default driver: `ODBC Driver 18 for SQL Server`.

## Runtime files
`.env`, browser profiles, checkpoints, logs, local databases, `output/`, `runtime/`, virtualenvs, and Python cache files are runtime-only and are ignored by Git. Keep shareable defaults in `.env.example`; never commit real secrets or live browser profiles.

## Local debug (low bandwidth + persistent profile)
- برای کاهش مصرف اینترنت در دیباگ لوکال، پروژه به‌صورت پیش‌فرض از **persistent Chrome profile** استفاده می‌کند (`rea_profile`).
- اجرای اول ممکن است کمی مصرف بالاتر داشته باشد تا cache/service worker/static assets ساخته شود.
- در اجراهای بعدی باید مصرف شبکه پایین‌تر شود چون profile/cache حفظ می‌شود.
- برای warm کردن profile:
  - `python -m tools.warm_chrome_profile`
- برای reset دستی profile:
  - `python -m tools.reset_chrome_profile`
- profile به‌صورت خودکار پاک نمی‌شود.
- برای جلوگیری از دانلود خودکار ChromeDriver:
  - نسخه Chrome را چک کنید (major version).
  - ChromeDriver با همان major version را دستی در `drivers/chromedriver.exe` قرار دهید.
  - در `config.py` مقدار `ALLOW_UC_DRIVER_DOWNLOAD = False` را نگه دارید.
  - سپس warm profile را اجرا کنید.

## مدیریت امن Telegram Bot Token

توکن تلگرام را داخل `config.py`، command line، history شل، لاگ، چت یا commit قرار ندهید. فایل محلی `.env` توسط Git نادیده گرفته می‌شود و روش پیشنهادی برای توسعه است:

```dotenv
TELEGRAM_BOT_TOKEN=replace_with_token_from_botfather
# Initial suburb setup scans up to 50 list pages by default. Use 0 or none for unlimited.
INITIAL_BASELINE_MAX_PAGES=50
```

سپس Bot یا scheduler را بدون قراردادن توکن در command history اجرا کنید:

```bash
python telegram_bot.py
python tools/run_monitoring_tick_once.py --send-telegram
```

برنامه `TELEGRAM_BOT_TOKEN` را از environment یا فایل محلی `.env` می‌خواند و نباید مقدار آن را چاپ کند. اگر توکن در لاگ، چت، history یا commit ظاهر شد، آن را از طریق BotFather فوراً revoke/regenerate کنید و مقدار جدید را فقط در `.env` امن قرار دهید.
