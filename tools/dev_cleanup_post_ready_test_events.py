"""Inspect or conservatively clean dev post-ready acceptance events."""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import config
import db_layer
from tools.dev_inject_post_ready_event import TEST_MARKER


def _rows_to_dicts(cur) -> list[dict[str, Any]]:
    columns = [column[0] for column in cur.description]
    return [{columns[index]: row[index] for index in range(len(columns))} for row in cur.fetchall()]


def list_test_events(conn, user_area_id: int) -> list[dict[str, Any]]:
    subscription = db_layer.get_user_area_subscription(conn, int(user_area_id))
    if not subscription:
        raise ValueError("User area subscription was not found")
    cur = conn.cursor()
    cur.execute(
        """
        SELECT e.EventID, e.SearchID, e.ListingID, l.ExternalID, e.EventType, e.Reason,
               e.CreatedAt, no.NotificationID, no.ChatID, no.Status AS NotificationStatus,
               no.QueuedAt, no.SentAt
        FROM dbo.ListingEvent e
        LEFT JOIN dbo.Listing l ON l.listingID=e.ListingID
        LEFT JOIN dbo.NotificationOutbox no ON no.EventID=e.EventID
        WHERE e.SearchID=?
          AND (e.Reason=? OR e.EventPayloadJson LIKE ?)
          AND e.EventPayloadJson LIKE ?
        ORDER BY e.EventID, no.NotificationID
        """,
        int(subscription["SearchID"]),
        TEST_MARKER,
        f'%"test_marker": "{TEST_MARKER}"%',
        f'%"user_area_id": {int(user_area_id)}%',
    )
    return _rows_to_dicts(cur)


def mark_queued_skipped(conn, user_area_id: int) -> int:
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE no
        SET Status='skipped', SkippedAt=SYSDATETIME(), LastError=?
        FROM dbo.NotificationOutbox no
        JOIN dbo.ListingEvent e ON e.EventID=no.EventID
        WHERE no.Status='queued'
          AND e.Reason=?
          AND e.EventPayloadJson LIKE ?
        """,
        "dev cleanup: marked skipped",
        TEST_MARKER,
        f'%"user_area_id": {int(user_area_id)}%',
    )
    return max(0, int(cur.rowcount or 0))


def delete_test_events(conn, user_area_id: int, force_sent: bool = False) -> dict[str, int]:
    subscription = db_layer.get_user_area_subscription(conn, int(user_area_id))
    if not subscription:
        raise ValueError("User area subscription was not found")
    cur = conn.cursor()
    protected = "" if force_sent else "AND NOT EXISTS (SELECT 1 FROM dbo.NotificationOutbox sent WHERE sent.EventID=e.EventID AND sent.Status IN ('queued','sending','sent'))"
    cur.execute(
        f"""
        SELECT e.EventID
        INTO #DevPostReadyAcceptanceDelete
        FROM dbo.ListingEvent e
        WHERE e.SearchID=? AND e.Reason=? AND e.EventPayloadJson LIKE ?
          {protected}
        """,
        int(subscription["SearchID"]),
        TEST_MARKER,
        f'%"user_area_id": {int(user_area_id)}%',
    )
    cur.execute("DELETE no FROM dbo.NotificationOutbox no JOIN #DevPostReadyAcceptanceDelete d ON d.EventID=no.EventID")
    notifications_deleted = max(0, int(cur.rowcount or 0))
    cur.execute("DELETE e FROM dbo.ListingEvent e JOIN #DevPostReadyAcceptanceDelete d ON d.EventID=e.EventID")
    events_deleted = max(0, int(cur.rowcount or 0))
    return {"events_deleted": events_deleted, "notifications_deleted": notifications_deleted}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-area-id", type=int, required=True)
    parser.add_argument("--mark-skipped", action="store_true")
    parser.add_argument("--delete", action="store_true")
    parser.add_argument("--force-sent", action="store_true", help="Allow --delete to remove sent test history. Use only when explicitly intended.")
    args = parser.parse_args()
    if args.force_sent and not args.delete:
        parser.error("--force-sent requires --delete")
    conn = None
    try:
        conn = db_layer.connect(config.DB_PATH)
        db_layer.ensure_notification_tables(conn)
        rows = list_test_events(conn, args.user_area_id)
        print(json.dumps(rows, ensure_ascii=False, default=str, indent=2))
        print(f"matching_rows: {len(rows)}")
        if args.mark_skipped:
            print(f"queued_notifications_marked_skipped: {mark_queued_skipped(conn, args.user_area_id)}")
        if args.delete:
            print(json.dumps(delete_test_events(conn, args.user_area_id, force_sent=args.force_sent), indent=2))
        if args.mark_skipped or args.delete:
            conn.commit()
        else:
            conn.rollback()
        return 0
    except Exception as exc:
        if conn is not None:
            conn.rollback()
        print(f"ERROR: {config.mask_sensitive_text(exc)}", file=sys.stderr)
        return 1
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
