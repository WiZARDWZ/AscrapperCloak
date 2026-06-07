from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from monitoring_scheduler import run_price_baseline_for_search
from json_safe import json_safe


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one SearchID-scoped Module 2 price baseline/refresh batch.")
    parser.add_argument("--search-id", type=int, required=True, help="SuburbSearch.SearchID to process")
    parser.add_argument("--limit", type=int, default=None, help="Maximum active listings to process in this batch")
    parser.add_argument("--refresh", action="store_true", help="Use recurring refresh due filter instead of setup baseline mode")
    parser.add_argument("--sweep-mode", choices=["setup_full_sweep", "smart_refresh"], default=None, help="Override sweep mode; baseline defaults to setup_full_sweep")
    parser.add_argument("--dry-run", action="store_true", help="Select candidates without running Module 2 or updating DB")
    args = parser.parse_args()

    try:
        result = run_price_baseline_for_search(
            args.search_id,
            limit=args.limit,
            dry_run=args.dry_run,
            setup=not args.refresh,
            sweep_mode=args.sweep_mode or ("smart_refresh" if args.refresh else "setup_full_sweep"),
        )
    except Exception as exc:
        result = {"status": "failed", "search_id": args.search_id, "error": str(exc)}
    print(json.dumps(json_safe(result), ensure_ascii=False, indent=2))
    return 0 if result.get("status") != "failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
