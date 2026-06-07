import argparse

from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from config import DB_PATH
from area_light_checker import light_check_area
from db_layer import connect


def _db_counts() -> dict:
    conn = connect(DB_PATH)
    try:
        cur = conn.cursor()
        out = {}
        for table in ["dbo.Listing", "dbo.ListingSnapshot", "dbo.ListingSearchState", "dbo.ListingEvent"]:
            cur.execute(f"SELECT COUNT(*) FROM {table}")
            out[table.split(".")[-1]] = int(cur.fetchone()[0])
        return out
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("search_url")
    parser.add_argument("--max-pages", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--timeout", type=int, default=None)
    parser.add_argument("--print-db-delta", action="store_true")
    args = parser.parse_args()
    before_counts = _db_counts() if args.print_db_delta else None

    result = light_check_area(
        db_path=DB_PATH,
        search_url=args.search_url,
        max_pages=args.max_pages,
        timeout=args.timeout,
        dry_run=args.dry_run,
    )

    print(f"search_url: {result['search_url']}")
    print(f"pages_checked: {result['pages_checked']}")
    print(f"rows_scraped: {result['rows_scraped']}")
    print(f"existing_count_before: {result['existing_count_before']}")
    print(f"new_count: {result['new_count']}")
    print(f"stopped_reason: {result['stopped_reason']}")
    print(f"dry_run: {result['dry_run']}")
    print(f"run_id: {result['run_id']}")
    if args.print_db_delta:
        after_counts = _db_counts()
        print("DB delta:")
        for key in ["Listing", "ListingSnapshot", "ListingSearchState", "ListingEvent"]:
            delta = after_counts.get(key, 0) - before_counts.get(key, 0)
            print(f"{key} {delta:+d}")

    if result["new_count"] > 0:
        print(f"New listings: {result['new_count']}")
        for row in result["new_listings"]:
            print(f"- {row.get('listing_id')} | {row.get('address')} | {row.get('price')} | {row.get('url')}")
    else:
        print("No new listings.")


if __name__ == "__main__":
    main()
