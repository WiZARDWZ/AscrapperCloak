import argparse
import json
import os
from datetime import datetime

import config
from chrome_options_helper import build_chrome_driver, cleanup_chrome_driver
from cloak_browser_helper import By, WebDriverWait, TimeoutException, debug_page_snapshot
from realestate_page_state import classify_search_page


DEFAULT_URL = "https://www.realestate.com.au/buy/in-petersham,+nsw+2049/list-1?activeSort=list-date"


def _wait_for_cards(driver, timeout: int) -> str:
    selectors = [
        'article[data-testid="ResidentialCard"]',
        "article.residential-card",
        "article[data-testid]",
    ]

    def _cond(d):
        for selector in selectors:
            if d.find_elements(By.CSS_SELECTOR, selector):
                return "cards_found"
        return False

    try:
        return WebDriverWait(driver, timeout, poll_frequency=0.5).until(_cond)
    except TimeoutException:
        return "timeout"


def main() -> int:
    parser = argparse.ArgumentParser(description="Open one realestate.com.au page with CloakBrowser and write diagnostics.")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--out-dir", default=os.path.join("output", "cloak_tests"))
    parser.add_argument("--profile-dir", default=None)
    parser.add_argument("--fresh-profile", action="store_true")
    parser.add_argument("--disable-http2", action="store_true")
    parser.add_argument("--wait", type=int, default=15)
    args = parser.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    if args.disable_http2:
        config.CLOAK_DISABLE_HTTP2 = True

    profile_dir = args.profile_dir
    if args.fresh_profile:
        profile_dir = os.path.join(args.out_dir, f"cloak_profile_{datetime.now().strftime('%Y%m%d_%H%M%S')}")

    driver = None
    try:
        driver = build_chrome_driver(profile_dir_override=profile_dir)
        driver.get(args.url)
        wait_reason = _wait_for_cards(driver, args.wait)
        snapshot = debug_page_snapshot(driver)
        page_state = classify_search_page(driver, timeout=args.wait)
        snapshot["wait_reason"] = wait_reason
        snapshot["page_state"] = page_state.state
        snapshot["page_state_reason"] = page_state.reason
        snapshot["page_state_is_blocked"] = page_state.is_blocked
        snapshot["page_state_is_usable"] = page_state.is_usable
        snapshot["network_reason"] = page_state.network_reason
        snapshot["requested_url"] = args.url
        snapshot["profile_dir"] = getattr(driver, "profile_dir", profile_dir)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        html_path = os.path.join(args.out_dir, f"cloak_single_page_{ts}.html")
        png_path = os.path.join(args.out_dir, f"cloak_single_page_{ts}.png")
        json_path = os.path.join(args.out_dir, f"cloak_single_page_{ts}.json")
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(driver.page_source or "")
        try:
            driver.screenshot(png_path)
        except Exception as exc:
            snapshot["screenshot_error"] = str(exc)
        snapshot["html_path"] = html_path
        snapshot["screenshot_path"] = png_path
        snapshot["json_path"] = json_path
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)
        print(json.dumps(snapshot, ensure_ascii=False, indent=2))
        return 0 if snapshot.get("cards_found", 0) > 0 and not snapshot.get("blank_render_detected") else 2
    finally:
        if driver:
            try:
                driver.quit()
            finally:
                cleanup_chrome_driver(driver)


if __name__ == "__main__":
    raise SystemExit(main())
