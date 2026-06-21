from __future__ import annotations

import argparse
import importlib.util
import platform
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import config

REQUIRED_IMPORTS = (
    "telegram",
    "openpyxl",
    "cloakbrowser",
    "playwright",
    "apscheduler",
    "pyodbc",
    "dotenv",
    "bs4",
    "lxml",
    "pytest",
)


def _import_status(module_name: str) -> bool:
    available = importlib.util.find_spec(module_name) is not None
    print(f"{module_name}: {'available' if available else 'missing'}")
    return available


def _create_runtime_directories() -> None:
    for configured_path in (
        config.RUNTIME_DIR,
        config.OUTPUT_DIR,
        config.LOG_DIR,
        config.CLOAK_PROFILE_DIR,
    ):
        path = Path(configured_path)
        path.mkdir(parents=True, exist_ok=True)
        print(f"directory: {path} (ready)")


def _check_odbc() -> bool:
    if not _import_status("pyodbc"):
        print("ODBC Driver 18: unknown (install pyodbc first)")
        return False
    import pyodbc

    drivers = list(pyodbc.drivers())
    driver_ok = config.DB_DRIVER in drivers
    print(f"configured ODBC driver: {config.DB_DRIVER}")
    print(f"installed ODBC drivers: {drivers or ['none detected']}")
    print(f"ODBC Driver installed: {driver_ok}")
    return driver_ok


def _check_database() -> bool:
    missing = config.validate_runtime_config(require_db=True)
    if missing:
        print(f"database connection: skipped; configure {', '.join(missing)}")
        return False
    try:
        import db_layer

        conn = db_layer.connect()
        try:
            cur = conn.cursor()
            cur.execute("SELECT 1")
            row = cur.fetchone()
        finally:
            conn.close()
        ok = bool(row and row[0] == 1)
        print(f"database connection: {'OK' if ok else 'unexpected SELECT 1 result'}")
        return ok
    except Exception as exc:
        print(f"database connection: failed ({config.mask_sensitive_text(exc)})")
        return False


def _browser_smoke() -> bool:
    try:
        from cloak_browser_helper import build_cloak_driver, cleanup_cloak_driver

        driver = build_cloak_driver()
        try:
            print("browser launch: OK")
        finally:
            cleanup_cloak_driver(driver)
        return True
    except Exception as exc:
        print(f"browser launch: failed ({config.mask_sensitive_text(exc)})")
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare and inspect Windows/PyCharm development prerequisites.")
    parser.add_argument("--skip-db", action="store_true", help="Do not run the read-only SQL Server SELECT 1 check.")
    parser.add_argument("--browser-smoke", action="store_true", help="Launch and close CloakBrowser.")
    args = parser.parse_args()

    print(f"Python: {platform.python_version()} ({sys.executable})")
    print(f"Platform: {platform.platform()}")
    print(f"Runtime profile: {config.RUNTIME_PROFILE}")
    print("Recommended venv: python -m venv .venv")
    print("Install: python -m pip install -r requirements.txt")
    print("Install Windows extras: python -m pip install -r requirements-windows.txt")
    _create_runtime_directories()
    print("Required package imports:")
    for module_name in REQUIRED_IMPORTS:
        _import_status(module_name)
    _check_odbc()
    if not args.skip_db:
        _check_database()
    else:
        print("database connection: skipped by --skip-db")
    if args.browser_smoke:
        _browser_smoke()
    else:
        print("browser launch: dry-run only; pass --browser-smoke to launch")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
