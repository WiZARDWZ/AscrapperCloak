from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path

import module1_list_scraper

DEFAULT_URL = "https://www.realestate.com.au/buy/in-noona,+nsw+2835/list-1?activeSort=list-date"


def _page_counts_from_page_stats(page_stats: list[dict] | None) -> dict[int, int]:
    counts: dict[int, int] = {}
    for item in page_stats or []:
        try:
            page = int(item.get("page") or 0)
            rows = int(item.get("rows") or 0)
        except Exception:
            continue
        if page > 0:
            counts[page] = counts.get(page, 0) + rows
    return counts


def _page_counts_from_extract_logs(logs: list[str]) -> dict[int, int]:
    counts: dict[int, int] = {}
    current_page: int | None = None
    for item in logs:
        page_match = re.search(r"\bPage\s+(\d+)\s*:", item or "")
        if page_match:
            current_page = int(page_match.group(1))
            continue
        rows_match = re.search(r"Extracted\s+(\d+)\s+rows\s+from\s+this\s+page", item or "", flags=re.I)
        if rows_match and current_page:
            counts[current_page] = counts.get(current_page, 0) + int(rows_match.group(1))
    return counts


def build_summary(url: str, rows: list[dict], logs: list[str]) -> dict:
    last_result = getattr(module1_list_scraper.scrape_search, "last_result", {}) or {}
    page_stats = last_result.get("page_stats") if isinstance(last_result, dict) else None
    page_counts = _page_counts_from_page_stats(page_stats)
    page_count_source = "last_result.page_stats"
    if not page_counts:
        page_counts = _page_counts_from_extract_logs(logs)
        page_count_source = "logs"
    if not page_counts:
        page_count_source = "unavailable"
    return {
        "url": url,
        "total_rows": len(rows),
        "page_counts": page_counts,
        "page_count_source": page_count_source,
        "last_result": last_result,
        "pagination_nav_mode": getattr(module1_list_scraper.config, "MODULE1_PAGINATION_NAV_MODE", ""),
        "fallback_paths": last_result.get("fallback_paths", []) if isinstance(last_result, dict) else [],
        "inter_page_delay_logged": any("inter-navigation delay" in item for item in logs),
        "chrome_error_logged": any("chrome-error" in item for item in logs),
        "recent_logs": logs[-60:],
    }


def validate_summary(summary: dict, *, expected_total: int = 28, expected_page1: int = 25, expected_page2: int = 3) -> tuple[bool, str]:
    page_counts = summary.get("page_counts") or {}
    page1_rows = int(page_counts.get(1) or page_counts.get("1") or 0)
    page2_rows = int(page_counts.get(2) or page_counts.get("2") or 0)
    terminal_chrome_error = str((summary.get("last_result") or {}).get("stop_reason", "")).lower() == "chrome_error"
    total_rows = int(summary.get("total_rows") or 0)
    passed = total_rows >= expected_total and page1_rows >= expected_page1 and page2_rows >= expected_page2 and not terminal_chrome_error
    message = (
        f"expected total_rows>={expected_total}, page1_rows>={expected_page1}, page2_rows>={expected_page2}, "
        f"no terminal chrome-error; got total_rows={total_rows} page1_rows={page1_rows} "
        f"page2_rows={page2_rows} terminal_chrome_error={terminal_chrome_error} "
        f"page_count_source={summary.get('page_count_source')}"
    )
    return passed, message


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
    summary = build_summary(args.url, rows, logs)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    expected_total = int(os.getenv("EXPECTED_TOTAL_ROWS", "28"))
    ok, message = validate_summary(summary, expected_total=expected_total)
    if not ok:
        print(f"FAILED: {message}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
