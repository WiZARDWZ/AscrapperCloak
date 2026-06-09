from __future__ import annotations

import json
import os
from pathlib import Path

import config
from chrome_options_helper import build_chrome_driver, cleanup_chrome_driver
import cloak_browser_helper
from cloak_browser_helper import debug_page_snapshot
from realestate_page_state import classify_search_page

REA_URL = os.getenv(
    "TEST_REA_URL",
    "https://www.realestate.com.au/buy/in-noona,+nsw+2835/list-1?activeSort=list-date",
)


def _body_text(driver) -> str:
    try:
        return driver.execute_script("return document.body ? document.body.innerText : ''") or ""
    except Exception:
        return ""


def main() -> int:
    profile = os.getenv("TEST_PROFILE") or config.get_effective_browser_profile_dir("module1")
    Path(profile).mkdir(parents=True, exist_ok=True)
    print(
        "effective_config=",
        json.dumps(
            {
                "browser_engine": config.BROWSER_ENGINE,
                "headless": config.CLOAK_HEADLESS,
                "humanize": config.CLOAK_HUMANIZE,
                "geoip": config.CLOAK_GEOIP,
                "proxy_configured": bool(config.CLOAK_PROXY),
                "viewport": {"width": config.CLOAK_VIEWPORT_WIDTH, "height": config.CLOAK_VIEWPORT_HEIGHT},
                "locale": config.CLOAK_LOCALE,
                "timezone": config.CLOAK_TIMEZONE,
                "http2_mode": config.CLOAK_HTTP2_MODE,
            },
            sort_keys=True,
        ),
    )
    driver = build_chrome_driver(profile_dir_override=profile)
    try:
        print("cloak_launch_config=", json.dumps(cloak_browser_helper.LAST_CLOAK_LAUNCH_CONFIG, sort_keys=True))
        try:
            print("fingerprint=", json.dumps(driver.fingerprint(), sort_keys=True))
        except Exception as exc:
            print("fingerprint_error=", str(exc))
        try:
            driver.get(REA_URL)
            goto_status = "ok"
        except Exception as exc:
            goto_status = f"error:{exc}"
        state = classify_search_page(driver, timeout=1, min_cards=1)
        snap = debug_page_snapshot(driver)
        body = _body_text(driver)
        print("goto_status=", goto_status)
        print("current_url=", driver.current_url)
        print("title=", driver.title)
        print("page_state=", state.state)
        print("cards_found=", state.cards_count)
        print("html_length=", state.html_length or snap.get("html_length"))
        print("body_text_length=", state.body_text_length or snap.get("body_text_length"))
        print("http_errors=", json.dumps(driver.http_errors[:20], sort_keys=True))
        print("sample_body_text=", " ".join(body.split())[:500])
        if str(driver.current_url).startswith("chrome-error://chromewebdata/"):
            return 2
        return 0 if state.state == "listings" else 1
    finally:
        try:
            driver.quit()
        finally:
            cleanup_chrome_driver(driver)


if __name__ == "__main__":
    raise SystemExit(main())
