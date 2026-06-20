from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import config
import db_layer
import module1_list_scraper
from monitor import evaluate_module1_baseline_scan


def _resolve_search_url(search_url: str | None, search_id: int | None) -> str:
    if search_url:
        return db_layer.ensure_sort_list_date(search_url)
    if search_id is None:
        raise ValueError("--url or --search-id is required")
    conn = db_layer.connect(config.DB_PATH)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT COALESCE(NULLIF(NormalizedSearchURL, ''), SearchURL)
            FROM dbo.SuburbSearch
            WHERE SearchID=?
            """,
            int(search_id),
        )
        row = cur.fetchone()
        if not row or not row[0]:
            raise ValueError(f"SearchID {search_id} was not found")
        return db_layer.ensure_sort_list_date(str(row[0]))
    finally:
        conn.close()


def verify_trusted_baseline_scan(
    *,
    search_url: str | None = None,
    search_id: int | None = None,
    max_pages: int | None = None,
    timeout: int | None = None,
    on_log=None,
) -> dict:
    """Run real Module1 and the production trust guard without any write path."""
    resolved_url = _resolve_search_url(search_url, search_id)
    scan_result = module1_list_scraper.scrape_search_with_result(
        resolved_url,
        max_pages=config.INITIAL_BASELINE_MAX_PAGES if max_pages is None else max_pages,
        timeout=config.PIPELINE_TIMEOUT if timeout is None else timeout,
        on_log=on_log,
    )
    evaluated = evaluate_module1_baseline_scan(scan_result, resolved_url)
    return {
        "search_url": resolved_url,
        "search_id": search_id,
        "scan_status": evaluated.get("scan_status"),
        "trusted_scan": bool(evaluated.get("trusted_scan")),
        "stop_reason": evaluated.get("stop_reason"),
        "rows_scraped": int(evaluated.get("rows_scraped") or 0),
        "rows_accepted": int(evaluated.get("rows_accepted") or 0),
        "rows_rejected": int(evaluated.get("rows_rejected") or 0),
        "current_url": evaluated.get("current_url"),
        "current_url_matches_target": bool(evaluated.get("current_url_matches_target")),
        "ingest_allowed": bool(evaluated.get("trusted_scan") and evaluated.get("rows_accepted")),
        "detail_job_enqueued": False,
        "database_mutated": False,
        "blocked_reason": evaluated.get("blocked_reason"),
        "retry_after_seconds": evaluated.get("retry_after_seconds"),
    }


def _print_structured(result: dict) -> None:
    ordered_keys = (
        "scan_status",
        "trusted_scan",
        "stop_reason",
        "rows_scraped",
        "rows_accepted",
        "rows_rejected",
        "current_url",
        "current_url_matches_target",
        "ingest_allowed",
        "detail_job_enqueued",
        "database_mutated",
        "blocked_reason",
        "retry_after_seconds",
    )
    for key in ordered_keys:
        value = result.get(key)
        if isinstance(value, bool):
            value = str(value).lower()
        elif value is None:
            value = ""
        print(f"{key}={value}")
    print("json=" + json.dumps(result, ensure_ascii=False, sort_keys=True, default=str))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only real Module1 verification for trusted baseline enforcement."
    )
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--url", dest="search_url")
    scope.add_argument("--search-id", type=int)
    parser.add_argument("--max-pages", type=int, default=None)
    parser.add_argument("--timeout", type=int, default=None)
    args = parser.parse_args()

    try:
        result = verify_trusted_baseline_scan(
            search_url=args.search_url,
            search_id=args.search_id,
            max_pages=args.max_pages,
            timeout=args.timeout,
        )
    except Exception as exc:
        result = {
            "search_url": args.search_url,
            "search_id": args.search_id,
            "scan_status": "technical_failure",
            "trusted_scan": False,
            "stop_reason": "verification_error",
            "rows_scraped": 0,
            "rows_accepted": 0,
            "rows_rejected": 0,
            "current_url": None,
            "current_url_matches_target": False,
            "ingest_allowed": False,
            "detail_job_enqueued": False,
            "database_mutated": False,
            "blocked_reason": None,
            "retry_after_seconds": None,
            "error": config.mask_sensitive_text(exc),
        }
    _print_structured(result)
    return 0 if result["trusted_scan"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
