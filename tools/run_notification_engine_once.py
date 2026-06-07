import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import config
import db_layer

TABLES = ["NotificationOutbox", "ListingEvent", "ScrapeRun"]


def get_counts(conn):
    counts = {}
    cur = conn.cursor()
    for table in TABLES:
        cur.execute(f"SELECT COUNT(1) FROM dbo.{table}")
        counts[table] = int(cur.fetchone()[0])
    return counts


def print_summary(args, result):
    print(f"search_url: {args.search_url or ''}")
    print(f"dry_run: {result['dry_run']}")
    print(f"events_input: {result['events_input']}")
    print(f"notifyable_count: {result['notifyable_count']}")
    print(f"queued_count: {result['queued_count']}")
    print(f"skipped_count: {result['skipped_count']}")
    print(f"duplicates_count: {result['duplicates_count']}")
    print(f"subscriptions_considered: {result.get('subscriptions_considered', 0)}")
    if result.get("errors"):
        print("errors:")
        for err in result["errors"]:
            print(f"- event={err.get('event')}: {err.get('error')}")


def print_messages(result):
    for item in result.get("notifications", []):
        print("---")
        print(f"event_id: {item.get('event_id')}")
        print(f"event_type: {item.get('event_type')}")
        print(f"external_id: {item.get('external_id')}")
        print(f"status: {item.get('status')}")
        print("message:")
        print(item.get("message_text") or "")


def main():
    parser = argparse.ArgumentParser(description="Build notification outbox messages from ListingEvent rows once.")
    parser.add_argument("--search-url")
    parser.add_argument("--since-event-id", type=int)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--chat-id")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--include-already-queued", dest="include_already_queued", action="store_true", help="Include already queued rows so duplicate-key handling is visible (default).")
    parser.add_argument("--exclude-already-queued", dest="include_already_queued", action="store_false", help="Skip rows already queued for each subscription recipient.")
    parser.set_defaults(include_already_queued=True)
    parser.add_argument("--print-messages", action="store_true")
    parser.add_argument("--print-db-delta", action="store_true")
    args = parser.parse_args()

    conn = db_layer.connect(config.DB_PATH)
    try:
        db_layer.ensure_notification_tables(conn)
        conn.commit()
        before = get_counts(conn) if args.print_db_delta else None
        result = db_layer.queue_notifications_for_active_user_areas(
            conn,
            search_url=args.search_url,
            since_event_id=args.since_event_id,
            limit=args.limit,
            chat_id=args.chat_id,
            dry_run=args.dry_run,
            include_already_queued=args.include_already_queued,
        )
        if args.dry_run:
            conn.rollback()
        else:
            conn.commit()
        print_summary(args, result)
        if args.print_messages:
            print_messages(result)
        if args.print_db_delta:
            after = get_counts(conn)
            print("DB delta:")
            for table in TABLES:
                print(f"- {table}: {after[table] - before[table]}")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
