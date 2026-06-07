"""Inject one controlled post-ready ListingEvent for dev/admin acceptance testing."""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import config
import db_layer

TEST_MARKER = "dev_post_ready_acceptance"
CREATED_BY = "tools/dev_inject_post_ready_event.py"
SUPPORTED_EVENT_TYPES = (
    "price_changed",
    "new_listing",
    "status_changed",
    "agent_changed",
    "inspection_changed",
    "auction_changed",
)


def validate_subscription(subscription: dict[str, Any] | None) -> dict[str, Any]:
    if not subscription:
        raise ValueError("User area subscription was not found")
    if not subscription.get("IsActive"):
        raise ValueError("Refusing to inject: user area subscription is not active")
    if not subscription.get("NotificationReadyAt"):
        raise ValueError("Refusing to inject: NotificationReadyAt is NULL")
    if str(subscription.get("DetailBaselineStatus") or "").lower() != "completed":
        raise ValueError("Refusing to inject: DetailBaselineStatus is not completed")
    if subscription.get("SearchID") is None:
        raise ValueError("Refusing to inject: user area subscription has no SearchID")
    return subscription


def _row_to_listing(row) -> dict[str, Any]:
    if not row:
        raise ValueError("No matching active listing was found for the subscription area")
    return {"listing_id": int(row[0]), "external_id": str(row[1])}


def select_listing(conn, subscription: dict[str, Any], listing_id: int | None = None, external_id: str | None = None) -> dict[str, Any]:
    if listing_id is not None and external_id is not None:
        raise ValueError("Provide only one of --listing-id or --external-id")
    search_id = int(subscription["SearchID"])
    cur = conn.cursor()
    active_filter = "(l.CurrentStatus IS NULL OR LOWER(l.CurrentStatus) IN ('active','unknown','under_offer')) AND (lss.Status IS NULL OR LOWER(lss.Status) NOT IN ('sold','withdrawn','removed','unavailable','expired'))"
    if listing_id is not None:
        cur.execute(
            f"""
            SELECT TOP 1 l.listingID, l.ExternalID
            FROM dbo.Listing l
            JOIN dbo.ListingSearchState lss ON lss.ListingID=l.listingID
            WHERE l.listingID=? AND lss.SearchID=? AND {active_filter}
            """,
            int(listing_id),
            search_id,
        )
        row = cur.fetchone()
        if not row:
            raise ValueError("Refusing to inject: --listing-id is inactive or does not belong to the subscription SearchID/area")
        return _row_to_listing(row)
    if external_id is not None:
        cur.execute(
            f"""
            SELECT TOP 1 l.listingID, l.ExternalID
            FROM dbo.Listing l
            JOIN dbo.ListingSearchState lss ON lss.ListingID=l.listingID
            WHERE CAST(l.ExternalID AS NVARCHAR(255))=? AND lss.SearchID=? AND {active_filter}
            ORDER BY l.listingID ASC
            """,
            str(external_id),
            search_id,
        )
        row = cur.fetchone()
        if not row:
            raise ValueError("Refusing to inject: --external-id is inactive or does not belong to the subscription SearchID/area")
        return _row_to_listing(row)
    cur.execute(
        f"""
        SELECT TOP 1 l.listingID, l.ExternalID
        FROM dbo.Listing l
        JOIN dbo.ListingSearchState lss ON lss.ListingID=l.listingID
        WHERE lss.SearchID=? AND {active_filter}
        ORDER BY l.listingID ASC
        """,
        search_id,
    )
    return _row_to_listing(cur.fetchone())


def build_event_json(user_area_id: int, event_type: str, old_price: str, new_price: str) -> tuple[str | None, str, str]:
    common = {
        "test_marker": TEST_MARKER,
        "user_area_id": int(user_area_id),
        "event_type": event_type,
        "created_by": CREATED_BY,
    }
    context = {
        **common,
        "area_label": "Acceptance test area",
        "address": "10 Acceptance Test Street",
        "listing_url": "https://example.test/listing/dev-acceptance",
        "external_id": "dev-acceptance",
        "property_type": "House",
        "bedrooms": 3,
        "bathrooms": 2,
        "car_spaces": 1,
        "price_display": new_price,
        "estimated_price_low": 1200000,
        "estimated_price_high": 1300000,
        "agency_name": "Current Acceptance Agency",
        "agent_names": ["Current Acceptance Agent"],
        "inspection_summary": "Sunday 11:00 am",
        "auction_summary": "Sunday 1:00 pm",
        "should_notify": True,
        "severity": "normal",
        "reason": TEST_MARKER,
    }
    values = {
        "price_changed": ({"price_display": old_price, "estimated_price_low": 1250000, "estimated_price_high": 1350000, "price_method": "acceptance"}, {"price_display": new_price, "estimated_price_low": 1200000, "estimated_price_high": 1300000, "price_method": "acceptance"}),
        "agent_changed": ({"agent_names": ["Previous Acceptance Agent"], "agency_name": "Current Acceptance Agency"}, {"agent_names": ["Current Acceptance Agent"], "agency_name": "Current Acceptance Agency"}),
        "inspection_changed": ({"inspection_summary": "Saturday 10:00 am", "inspection_times": ["Saturday 10:00 am"]}, {"inspection_summary": "Sunday 11:00 am", "inspection_times": ["Sunday 11:00 am"]}),
        "auction_changed": ({"auction_label": "Auction Saturday", "auction_time": "Saturday 12:00 pm"}, {"auction_label": "Auction Sunday", "auction_time": "Sunday 1:00 pm"}),
        "status_changed": ({"status": "active"}, {"status": "under_offer"}),
        "new_listing": (None, context),
    }
    old_value, new_value = values[event_type]
    payload = {**context, "field": event_type.removesuffix("_changed"), "old_value": old_value, "new_value": new_value}
    return tuple(json.dumps(value, ensure_ascii=False, sort_keys=True) if value is not None else None for value in (old_value, new_value, payload))


def inject_post_ready_event(
    conn,
    user_area_id: int,
    event_type: str = "price_changed",
    listing_id: int | None = None,
    external_id: str | None = None,
    old_price: str = "Guide $1,300,000",
    new_price: str = "Guide $1,250,000",
    dry_run: bool = False,
) -> dict[str, Any]:
    if event_type not in SUPPORTED_EVENT_TYPES:
        raise ValueError(f"Unsupported event type: {event_type}")
    subscription = validate_subscription(db_layer.get_user_area_subscription(conn, int(user_area_id)))
    listing = select_listing(conn, subscription, listing_id=listing_id, external_id=external_id)
    cur = conn.cursor()
    cur.execute("SELECT SYSDATETIME()")
    database_now = cur.fetchone()[0]
    if database_now <= subscription["NotificationReadyAt"]:
        raise ValueError("Refusing to inject: database time is not after NotificationReadyAt")
    old_json, new_json, payload_json = build_event_json(int(user_area_id), event_type, old_price, new_price)
    event_hash = f"{TEST_MARKER}:{uuid.uuid4()}"
    result = {
        "dry_run": bool(dry_run),
        "event_id": None,
        "run_id": None,
        "listing_id": listing["listing_id"],
        "external_id": listing["external_id"],
        "search_id": int(subscription["SearchID"]),
        "notification_ready_at": subscription["NotificationReadyAt"],
        "event_type": event_type,
        "event_hash": event_hash,
        "reason": TEST_MARKER,
        "database_now": database_now,
        "old_value_json": old_json,
        "new_value_json": new_json,
        "event_payload_json": payload_json,
    }
    if dry_run:
        return result
    db_layer.ensure_listing_event_metadata_columns(conn)
    run_id = db_layer.create_lightweight_scrape_run(conn, int(subscription["SearchID"]), source="dev_post_ready_acceptance", run_type="light")
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO dbo.ListingEvent(
            RunID, SearchID, ListingID, EventType, CreatedAt, ShouldNotify, Severity,
            Reason, EventHash, OldValueJson, NewValueJson, EventPayloadJson
        )
        OUTPUT INSERTED.EventID, INSERTED.CreatedAt
        VALUES (?, ?, ?, ?, SYSDATETIME(), 1, 'normal', ?, ?, ?, ?, ?)
        """,
        run_id,
        int(subscription["SearchID"]),
        listing["listing_id"],
        event_type,
        TEST_MARKER,
        event_hash,
        old_json,
        new_json,
        payload_json,
    )
    inserted = cur.fetchone()
    result.update({"event_id": int(inserted[0]), "run_id": int(run_id), "created_at": inserted[1]})
    return result


def _print_result(result: dict[str, Any]) -> None:
    action = "would_insert" if result["dry_run"] else "inserted"
    print(f"action: {action}")
    print(f"EventID: {result.get('event_id') or ''}")
    print(f"ListingID: {result['listing_id']}")
    print(f"ExternalID: {result['external_id']}")
    print(f"SearchID: {result['search_id']}")
    print(f"NotificationReadyAt: {result['notification_ready_at']}")
    print(f"EventType: {result['event_type']}")
    if result.get("created_at"):
        print(f"CreatedAt: {result['created_at']}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-area-id", type=int, required=True)
    parser.add_argument("--event-type", choices=SUPPORTED_EVENT_TYPES, default="price_changed")
    parser.add_argument("--listing-id", type=int)
    parser.add_argument("--external-id")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--old-price", default="Guide $1,300,000")
    parser.add_argument("--new-price", default="Guide $1,250,000")
    args = parser.parse_args()
    conn = None
    try:
        conn = db_layer.connect(config.DB_PATH)
        result = inject_post_ready_event(conn, **vars(args))
        if args.dry_run:
            conn.rollback()
        else:
            conn.commit()
        _print_result(result)
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
