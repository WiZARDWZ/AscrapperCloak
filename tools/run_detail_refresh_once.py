import argparse
import config
import db_layer
from listing_detail_refresher import refresh_active_listings

TABLES = ["Listing", "ListingSnapshot", "ListingSnapshotAgent", "ListingEvent", "ScrapeRun"]

def get_counts(conn):
    out = {}
    cur = conn.cursor()
    for t in TABLES:
        cur.execute(f"SELECT COUNT(1) FROM dbo.{t}")
        out[t] = int(cur.fetchone()[0])
    return out

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--search-url", default=config.AREA_SEARCH_URL)
    ap.add_argument("--limit", type=int, default=config.DETAIL_REFRESH_DEFAULT_LIMIT)
    ap.add_argument("--stale-hours", type=int, default=config.DETAIL_REFRESH_STALE_HOURS)
    ap.add_argument("--listing-id", dest="listing_id")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--timeout", type=int, default=config.DETAIL_REFRESH_TIMEOUT)
    ap.add_argument("--sleep-between", type=float, default=config.DETAIL_REFRESH_SLEEP_BETWEEN)
    ap.add_argument("--print-db-delta", action="store_true")
    args = ap.parse_args()

    before = after = None
    if args.print_db_delta:
        c = db_layer.connect(config.DB_PATH); before = get_counts(c); c.close()

    result = refresh_active_listings(
        search_url=args.search_url,
        limit=args.limit,
        stale_hours=args.stale_hours,
        dry_run=args.dry_run,
        listing_external_id=args.listing_id,
        timeout=args.timeout,
        sleep_between=args.sleep_between,
    )

    print(f"search_url: {result['search_url']}")
    print(f"dry_run: {result['dry_run']}")
    print(f"candidates_count: {result['candidates_count']}")
    print(f"processed_count: {result['processed_count']}")
    print(f"events_created: {result['events_created']}")
    print(f"failed_count: {result['failed_count']}")
    failed_items = [item for item in result.get("items", []) if item.get("status") == "failed"]
    if failed_items:
        print("Failed items:")
        for item in failed_items:
            ident = item.get("external_id") or item.get("db_listing_id") or "unknown"
            reason = item.get("detail_refresh_error") or item.get("error") or "detail_refresh_failed"
            print(f"- {ident} | detail_refresh_failed | {reason}")
    elif args.listing_id and result.get("candidates_count") == 0 and result.get("errors"):
        print("Errors:")
        for err in result.get("errors", []):
            print(f"- {err}")

    notify = result.get("should_notify_events", [])
    if notify:
        print("Notify-ready events:")
        for item in result.get("items", []):
            for ev in item.get("should_notify_events", []):
                db_part = f" (db_listing_id={item.get('db_listing_id')})" if item.get("db_listing_id") else ""
                print(f"- {item.get('external_id')}{db_part} | {ev.get('event_type')} | {ev.get('old_value')} -> {ev.get('new_value')}")
    else:
        print("No changes detected.")

    if args.print_db_delta:
        c = db_layer.connect(config.DB_PATH); after = get_counts(c); c.close()
        print("DB delta:")
        for t in TABLES:
            print(f"- {t}: {after[t]-before[t]}")
