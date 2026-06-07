import argparse
import json
import os

import module1_list_scraper as module1


DEFAULT_URL = "https://www.realestate.com.au/buy/in-petersham,+nsw+2049/list-1?activeSort=list-date"
REQUIRED_KEYS = [
    "listing_id",
    "price",
    "address",
    "bedrooms",
    "bathrooms",
    "parking",
    "property_type",
    "agency",
    "inspection_short_label",
    "inspection_long_label",
    "inspection",
    "auction_label",
    "auction_time",
    "auction",
    "url",
    "scraped_at",
    "search_url",
]


def _count_present(rows, key):
    return sum(1 for row in rows if str(row.get(key) or "").strip() and str(row.get(key)).upper() != "N/A")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Module1 through CloakBrowser for one page.")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--out-dir", default=os.path.join("output", "cloak_tests"))
    parser.add_argument("--timeout", type=int, default=25)
    args = parser.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    logs = []
    rows = module1.scrape_search(args.url, max_pages=1, timeout=args.timeout, on_log=logs.append)
    csv_path, json_path = module1.save_results(rows, out_dir=args.out_dir)
    missing_keys = sorted({key for row in rows for key in REQUIRED_KEYS if key not in row})
    summary = {
        "url": args.url,
        "rows": len(rows),
        "listing_id_present": _count_present(rows, "listing_id"),
        "url_present": _count_present(rows, "url"),
        "address_present": _count_present(rows, "address"),
        "missing_required_keys": missing_keys,
        "csv_path": csv_path,
        "json_path": json_path,
        "module1_last_result": getattr(module1.scrape_search, "last_result", {}),
        "logs_tail": logs[-20:],
    }
    summary_path = os.path.join(args.out_dir, "module1_cloak_one_page_summary.json")
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if rows and not missing_keys and summary["url_present"] > 0 and summary["address_present"] > 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
