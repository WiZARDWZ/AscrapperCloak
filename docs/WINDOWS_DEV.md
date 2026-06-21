# Windows + PyCharm Development

AScrapper defaults to the `windows_dev` runtime profile so the browser is visible, Xvfb is not required, and runtime files stay under the project directory.

## Setup

1. Clone the repository and open its root folder in PyCharm.
2. In `config.py`, keep:

   ```python
   RUNTIME_PROFILE = "windows_dev"
   ```

3. Edit the `WINDOWS_DEV_CONFIG` database and Telegram values. This is intentionally convenient for local debugging. Never commit real passwords or tokens to a public repository.
4. Create and select the project virtual environment:

   ```powershell
   python -m venv .venv
   .venv\Scripts\activate
   python -m pip install -r requirements.txt
   python -m pip install -r requirements-windows.txt
   ```

5. Run the non-destructive bootstrap and checks:

   ```powershell
   python tools/setup_windows_dev.py
   python tools/check_config.py
   python tools/run_windows_dev_check.py
   ```

`tools/setup_windows_dev.py` creates `runtime`, `runtime/rea-profile`, `output`, and `logs`; reports Python/package/ODBC status; and runs a read-only SQL Server `SELECT 1`. Add `--browser-smoke` to launch and close CloakBrowser.

## PyCharm run configurations

Use the project root as the working directory and the `.venv` interpreter.

```powershell
python tools/check_config.py
python tools/verify_trusted_baseline_scan.py --url "REAL_URL"
python tools/run_monitoring_scheduler_loop.py
python telegram_bot.py
```

The trusted-baseline command is read-only. Windows headed CloakBrowser does not need `DISPLAY` or Xvfb.

## Moving stable code to Ubuntu

Commit source, tests, requirements, and documentation only. Do not copy:

- `.venv`
- `runtime` or browser profiles
- `output`
- `logs`
- `__pycache__` or `.pytest_cache`
- local `.env` files

Use `RUNTIME_PROFILE=ubuntu_prod` (or change the one Python setting) on Ubuntu and follow `docs/UBUNTU_RELEASE.md`.
