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
        "inter_page_delay_logged": any("inter-navigation delay" in item for item in logs),
        "chrome_error_logged": any("chrome-error" in item for item in logs),
        "recent_logs": logs[-60:],
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    expected_total = int(os.getenv("EXPECTED_TOTAL_ROWS", "28"))
    return 0 if len(rows) == expected_total else 1


if __name__ == "__main__":
    raise SystemExit(main())
