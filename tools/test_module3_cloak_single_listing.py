import argparse
import json
import os
import re
from datetime import datetime

import module1_list_scraper as module1
import module3_enrich_details as module3
from tools.cloak_smoke_common import add_profile_args, apply_profile_dir, resolve_profile_dir


DEFAULT_URL = "https://www.realestate.com.au/buy/in-petersham,+nsw+2049/list-1?activeSort=list-date"
DETAIL_KEYS = [
    "description",
    "detail_price_display",
    "agents",
    "agent_1_name",
    "agent_1_id",
    "agent_1_profile_url",
    "agent_1_phone",
    "agent_1_phone_masked",
    "agent_1_rating",
    "agent_1_reviews",
    "agent_name",
    "agent_id",
    "agent_profile_url",
    "agency_name",
    "agency_profile_url",
    "agency_code",
    "agency_address",
]


def _load_rows(path: str) -> list[dict]:
    with open(path, "r", encoding="utf-8-sig") as f:
        if path.lower().endswith(".json"):
            return json.load(f)
        import csv

        return list(csv.DictReader(f))


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Module3 CloakBrowser enrichment for a single listing.")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--input-file", default=None)
    parser.add_argument("--out-dir", default=os.path.join("output", "cloak_tests"))
    parser.add_argument("--wait", type=int, default=25)
    add_profile_args(parser)
    args = parser.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    effective_profile_dir = apply_profile_dir(resolve_profile_dir(args, args.out_dir, "module3_profile"))
    input_file = args.input_file
    if not input_file:
        rows = module1.scrape_search(args.url, max_pages=1, timeout=25)
        rows = [row for row in rows if str(row.get("url") or "").strip().upper() != "N/A"][:1]
        input_file = os.path.join(args.out_dir, f"module3_single_input_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
        with open(input_file, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=2)
    else:
        rows = _load_rows(input_file)
        rows = rows[:1]
        input_file = os.path.join(args.out_dir, f"module3_single_input_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
        with open(input_file, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=2)

    logs = []
    csv_path, json_path = module3.module3_run(
        area_search_url=args.url,
        input_file=input_file,
        out_dir=args.out_dir,
        only_if_missing=False,
        wait_timeout=args.wait,
        sleep_between=0,
        empty_retry=1,
        on_log=logs.append,
    )
    rows_out = _load_rows(json_path) if json_path else []
    present_detail_keys = sorted({key for key in DETAIL_KEYS if any(row.get(key) not in (None, "", [], {}) for row in rows_out)})
    page_state = None
    for item in reversed(logs):
        match = re.search(r"page_state=([a-z0-9_]+)", str(item))
        if match:
            page_state = match.group(1)
            break
    summary = {
        "input_file": input_file,
        "csv_path": csv_path,
        "json_path": json_path,
        "rows": len(rows_out),
        "page_state": page_state or next((row.get("StatusReason") or row.get("page_state") for row in rows_out if row), None),
        "detail_status": next((row.get("ListingLifecycleStatus") or row.get("current_status") or row.get("detail_extraction_quality") for row in rows_out if row), None),
        "effective_profile_dir": effective_profile_dir,
        "present_detail_keys": present_detail_keys,
        "logs_tail": logs[-30:],
    }
    summary_path = os.path.join(args.out_dir, "module3_cloak_single_listing_summary.json")
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if rows_out and json_path and csv_path else 2


if __name__ == "__main__":
    raise SystemExit(main())
