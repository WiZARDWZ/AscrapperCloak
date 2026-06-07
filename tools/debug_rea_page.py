from __future__ import annotations

import argparse
import json
import os
import re
import time
from datetime import datetime
from urllib.parse import urlparse

import config
from browser_recovery import get_realestate_blocked_reason
from chrome_options_helper import build_chrome_driver, cleanup_chrome_driver


DEFAULT_URL = "https://www.realestate.com.au/buy/in-noona,+nsw+2835/list-1?activeSort=list-date"
MARKERS = [
    ("ResidentialCard", "ResidentialCard"),
    ("property-card", "property-card"),
    ("listing-card-price", "listing-card-price"),
    ("We couldn't find anything", "We couldn't find anything"),
    ("captcha", "captcha"),
    ("robot", "robot"),
    ("Access Denied", "Access Denied"),
    ("Pardon Our Interruption", "Pardon Our Interruption"),
    ("__NEXT_DATA__", "__NEXT_DATA__"),
]


def _set_no_blocks() -> None:
    config.LOW_BANDWIDTH_MODE = False
    config.BLOCK_HEAVY_RESOURCES = False
    config.ULTRA_LOW_BANDWIDTH = False
    config.BLOCK_TRACKERS = False
    config.BLOCK_IMAGES = False
    config.BLOCK_MEDIA = False
    config.BLOCK_FONTS = False
    config.BLOCK_MAPS = False
    config.BLOCK_ADS = False
    config.BLOCK_ANALYTICS = False
    config.BLOCK_CSS = False
    config.BLOCK_JS = False


def _summarize_network(driver) -> list[dict]:
    rows_by_request_id: dict[str, dict] = {}
    try:
        raw_logs = driver.get_log("performance")
    except Exception as exc:
        return [{"error": f"could not read performance log: {exc}"}]
    for item in raw_logs:
        try:
            message = json.loads(item.get("message", "{}")).get("message", {})
        except Exception:
            continue
        method = message.get("method")
        params = message.get("params") or {}
        request_id = params.get("requestId")
        if not request_id:
            continue
        row = rows_by_request_id.setdefault(request_id, {})
        if method == "Network.responseReceived":
            response = params.get("response") or {}
            row.update(
                {
                    "url": response.get("url"),
                    "status": response.get("status"),
                    "mimeType": response.get("mimeType"),
                    "type": params.get("type"),
                    "encodedDataLength": row.get("encodedDataLength", 0),
                }
            )
        elif method == "Network.loadingFinished":
            row["encodedDataLength"] = params.get("encodedDataLength", row.get("encodedDataLength", 0))
        elif method == "Network.loadingFailed":
            row.update(
                {
                    "failed": True,
                    "errorText": params.get("errorText"),
                    "blockedReason": params.get("blockedReason"),
                    "type": params.get("type") or row.get("type"),
                }
            )
    rows = [row for row in rows_by_request_id.values() if row.get("url") or row.get("failed")]
    rows.sort(key=lambda row: int(row.get("encodedDataLength") or 0), reverse=True)
    return rows


def _print_network_summary(rows: list[dict], target_url: str, limit: int = 20) -> None:
    target_host = urlparse(target_url).netloc
    main_rows = [
        row for row in rows
        if row.get("url") == target_url or urlparse(str(row.get("url") or "")).netloc == target_host
    ]
    failed_rows = [row for row in rows if row.get("failed")]
    print("network_realestate_rows:")
    for row in main_rows[:limit]:
        print(json.dumps(row, ensure_ascii=False, sort_keys=True))
    print("network_failed_rows:")
    for row in failed_rows[:limit]:
        print(json.dumps(row, ensure_ascii=False, sort_keys=True))


def _safe_slug(url: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "_", urlparse(url).path.strip("/") or "rea_page")[:80]


def main() -> int:
    parser = argparse.ArgumentParser(description="Debug a realestate.com.au page under the configured CloakBrowser runtime.")
    parser.add_argument("--url", default=os.getenv("DEBUG_REA_URL", DEFAULT_URL))
    parser.add_argument("--wait", type=float, default=float(os.getenv("DEBUG_REA_WAIT", "8")))
    parser.add_argument("--no-blocks", action="store_true", help="Disable resource blocking for this debug run only.")
    parser.add_argument("--temp-profile", action="store_true", help="Use a temporary CloakBrowser profile for this debug run only.")
    args = parser.parse_args()

    if args.no_blocks:
        _set_no_blocks()
    if args.temp_profile:
        config.USE_TEMP_CHROME_PROFILE = True
        config.USE_PERSISTENT_CHROME_PROFILE = False
        config.CLOAK_USE_PERSISTENT_CONTEXT = False

    os.makedirs(config.OUTPUT_DIR, exist_ok=True)
    driver = None
    try:
        driver = build_chrome_driver()
        driver.get(args.url)
        time.sleep(max(0, args.wait))
        html = driver.page_source or ""
        text = ""
        try:
            text = driver.execute_script("return document.body ? document.body.innerText : ''") or ""
        except Exception:
            text = ""

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        slug = _safe_slug(args.url)
        html_path = os.path.join(config.OUTPUT_DIR, f"debug_rea_page_{slug}_{timestamp}.html")
        png_path = os.path.join(config.OUTPUT_DIR, f"debug_rea_page_{slug}_{timestamp}.png")
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html)
        try:
            driver.save_screenshot(png_path)
        except Exception as exc:
            png_path = f"(screenshot failed: {exc})"

        print(f"current_url: {driver.current_url}")
        print(f"title: {driver.title}")
        for label, marker in MARKERS:
            print(f"{label} => {marker in html or marker.lower() in text.lower()}")
        print(f"html_len: {len(html)}")
        print(f"text_len: {len(text)}")
        print(f"blocked_reason: {get_realestate_blocked_reason(driver) or ''}")
        print(f"html_path: {html_path}")
        print(f"screenshot_path: {png_path}")
        print("html_preview:")
        print(re.sub(r"\s+", " ", html[:1200]).strip())
        print("text_preview:")
        print(re.sub(r"\s+", " ", text[:1200]).strip())
        _print_network_summary(_summarize_network(driver), args.url)
        return 0
    finally:
        if driver is not None:
            try:
                driver.quit()
            finally:
                cleanup_chrome_driver(driver)


if __name__ == "__main__":
    raise SystemExit(main())
