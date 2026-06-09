from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import module1_list_scraper

DEFAULT_URL = "https://www.realestate.com.au/buy/in-noona,+nsw+2835/list-1?activeSort=list-date"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Module1 CloakBrowser sequential two-page REA smoke test.")
    parser.add_argument("--url", default=os.getenv("TEST_REA_URL", DEFAULT_URL))
    parser.add_argument("--timeout", type=int, default=int(os.getenv("TEST_TIMEOUT", "60")))
    parser.add_argument("--out", default=os.getenv("TEST_OUTPUT", "output/module1_cloak_two_pages_summary.json"))
    args = parser.parse_args()

    if "MODULE1_PAGINATION_NAV_MODE" not in os.environ:
        os.environ["MODULE1_PAGINATION_NAV_MODE"] = "click_next"
        module1_list_scraper.config.MODULE1_PAGINATION_NAV_MODE = "click_next"
    nav_mode = getattr(module1_list_scraper.config, "MODULE1_PAGINATION_NAV_MODE", "")
    print(f"Selected Module1 pagination nav mode: {nav_mode}")
    logs: list[str] = []
    rows = module1_list_scraper.scrape_search(args.url, max_pages=2, timeout=args.timeout, on_log=logs.append)
    page_counts: dict[int, int] = {}
    for row in rows:
        try:
            page = int(row.get("page") or 0)
        except Exception:
            page = 0
        page_counts[page] = page_counts.get(page, 0) + 1
    summary = {
        "url": args.url,
        "total_rows": len(rows),
        "page_counts": page_counts,
        "last_result": getattr(module1_list_scraper.scrape_search, "last_result", {}),
        "pagination_nav_mode": nav_mode,
        "fallback_paths": getattr(module1_list_scraper.scrape_search, "last_result", {}).get("fallback_paths", []),
        "inter_page_delay_logged": any("inter-navigation delay" in item for item in logs),
        "chrome_error_logged": any("chrome-error" in item for item in logs),
        "recent_logs": logs[-60:],
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    expected_total = int(os.getenv("EXPECTED_TOTAL_ROWS", "28"))
    page2_rows = page_counts.get(2, 0)
    terminal_chrome_error = str(summary.get("last_result", {}).get("stop_reason", "")).lower() == "chrome_error"
    failed = len(rows) < expected_total or page2_rows < 3 or terminal_chrome_error
    if failed:
        print(f"FAILED: expected total_rows>={expected_total}, page2_rows>=3, no terminal chrome-error; got total_rows={len(rows)} page2_rows={page2_rows} terminal_chrome_error={terminal_chrome_error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
