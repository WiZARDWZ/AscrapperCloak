from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import config


def _check_writable(path_value: str) -> bool:
    path = Path(path_value)
    path.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.NamedTemporaryFile(prefix="ascrapper-write-check-", dir=path, delete=True):
            pass
    except OSError as exc:
        print(f"writable: FAIL path={path} error={exc}")
        return False
    print(f"writable: OK path={path}")
    return True


def _check_database() -> bool:
    missing = config.validate_runtime_config(require_db=True)
    if missing:
        print(f"database: FAIL missing={','.join(missing)}")
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
        print(f"database: {'OK' if ok else 'FAIL'}")
        return ok
    except Exception as exc:
        print(f"database: FAIL error={config.mask_sensitive_text(exc)}")
        return False


def _check_browser() -> bool:
    try:
        from cloak_browser_helper import build_cloak_driver, cleanup_cloak_driver

        driver = build_cloak_driver()
        try:
            print("cloakbrowser: OK")
        finally:
            cleanup_cloak_driver(driver)
        return True
    except Exception as exc:
        print(f"cloakbrowser: FAIL error={config.mask_sensitive_text(exc)}")
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify the Windows development runtime.")
    parser.add_argument("--url", help="Optional realestate.com.au search URL for read-only Module1 verification.")
    parser.add_argument("--skip-db", action="store_true")
    parser.add_argument("--skip-browser", action="store_true")
    args = parser.parse_args()

    checks = []
    profile_ok = config.RUNTIME_PROFILE == "windows_dev"
    print(f"runtime_profile: {'OK' if profile_ok else 'FAIL'} ({config.RUNTIME_PROFILE})")
    checks.append(profile_ok)
    print(f"xvfb_required: false (platform={os.name}, headless={config.HEADLESS})")
    checks.extend(
        [
            _check_writable(config.RUNTIME_DIR),
            _check_writable(config.OUTPUT_DIR),
            _check_writable(config.LOG_DIR),
            _check_writable(config.CLOAK_PROFILE_DIR),
        ]
    )
    if not args.skip_db:
        checks.append(_check_database())
    if not args.skip_browser:
        checks.append(_check_browser())
    if args.url:
        from tools.verify_trusted_baseline_scan import verify_trusted_baseline_scan

        try:
            result = verify_trusted_baseline_scan(search_url=args.url)
            scan_ok = bool(result.get("trusted_scan"))
            print(f"module1_trusted_scan: {'OK' if scan_ok else 'FAIL'} ({result.get('stop_reason')})")
            checks.append(scan_ok)
        except Exception as exc:
            print(f"module1_trusted_scan: FAIL error={config.mask_sensitive_text(exc)}")
            checks.append(False)
    return 0 if all(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
