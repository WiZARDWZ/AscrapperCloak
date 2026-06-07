from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime

import config
from browser_recovery import get_realestate_blocked_reason
from chrome_options_helper import build_chrome_driver, cleanup_chrome_driver


DEFAULT_URL = "https://www.realestate.com.au/buy/in-noona,+nsw+2835/list-1?activeSort=list-date"


def _linux_safe_profile_dir() -> str:
    profile_dir = os.path.abspath(getattr(config, "CLOAK_PROFILE_DIR", None) or config.CHROME_PROFILE_DIR or config.BROWSER_PROFILE_BASE_DIR)
    if os.name != "nt" and (":\\" in profile_dir or "\\Users\\" in profile_dir):
        profile_dir = os.path.abspath(config.BROWSER_PROFILE_BASE_DIR)
    return profile_dir


def _write_profile_state(profile_dir: str, reason: str) -> None:
    os.makedirs(os.path.dirname(config.BROWSER_PROFILE_STATE_PATH) or ".", exist_ok=True)
    with open(config.BROWSER_PROFILE_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(
            {
                "current_profile_dir": profile_dir,
                "updated_at": datetime.now().isoformat(),
                "reason": reason,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Recover/warm the Linux-safe realestate CloakBrowser profile.")
    parser.add_argument("--url", default=os.getenv("DEBUG_REA_URL", DEFAULT_URL))
    parser.add_argument("--wait", type=float, default=float(os.getenv("DEBUG_REA_WAIT", "15")))
    parser.add_argument("--reset-state-only", action="store_true", help="Only rewrite browser_profile_state.json.")
    args = parser.parse_args()

    profile_dir = _linux_safe_profile_dir()
    os.makedirs(profile_dir, exist_ok=True)
    _write_profile_state(profile_dir, "manual_recover_browser_profile")
    print(f"profile_dir: {profile_dir}")
    print(f"profile_state: {config.BROWSER_PROFILE_STATE_PATH}")

    if args.reset_state_only:
        print("state reset only; browser was not started")
        return 0

    driver = None
    try:
        driver = build_chrome_driver(profile_dir_override=profile_dir)
        driver.get(args.url)
        time.sleep(max(0, args.wait))
        html = driver.page_source or ""
        reason = get_realestate_blocked_reason(driver)
        print(f"current_url: {driver.current_url}")
        print(f"title: {driver.title}")
        print(f"html_len: {len(html)}")
        print(f"has_next_data: {'__NEXT_DATA__' in html}")
        print(f"has_residential_card: {'ResidentialCard' in html or 'property-card' in html}")
        print(f"blocked_reason: {reason or ''}")
        return 2 if reason else 0
    finally:
        if driver is not None:
            try:
                driver.quit()
            finally:
                cleanup_chrome_driver(driver)


if __name__ == "__main__":
    raise SystemExit(main())
