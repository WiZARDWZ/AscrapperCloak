"""Safely clear operational AScrapper data while preserving reference tables.

This tool is intentionally conservative: it dry-runs by default, refuses to run
without an explicit confirmation token, and refuses while active jobs exist unless
--maintenance-override is supplied.
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Iterable

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import config
import db_layer

CONFIRM_TOKEN = "RESET_OPERATIONAL_DATA"
REFERENCE_TABLES = {"State", "Suburb", "PropertyType", "NSWSuburb", "SuburbAlias"}
OPERATIONAL_TABLES = [
    "NotificationOutbox",
    "NotificationLog",
    "ListingSnapshotAgent",
    "ListingEvent",
    "ListingPriceHistory",
    "ListingStatusHistory",
    "ListingMedia",
    "ListingAgentAssignment",
    "ListingSnapshot",
    "ListingSearchState",
    "listing_price_inference_state",
    "user_area_subscription_state",
    "area_monitoring_state",
    "Job",
    "UserAreaSubscription",
    "TelegramUserSession",
    "SuburbSearch",
    "Listing",
    "Property",
    "Agency",
    "Agent",
]
USER_TABLES = ["TelegramUser"]


def _table_exists(conn, table: str) -> bool:
    row = db_layer._one(conn.cursor(), "SELECT OBJECT_ID(?)", f"dbo.{table}")
    return bool(row and row[0] is not None)


def _count(conn, table: str) -> int:
    if not _table_exists(conn, table):
        return 0
    row = db_layer._one(conn.cursor(), f"SELECT COUNT(1) FROM dbo.{table}")
    return int(row[0] or 0) if row else 0


def _active_jobs(conn) -> int:
    if not _table_exists(conn, "Job"):
        return 0
    row = db_layer._one(conn.cursor(), "SELECT COUNT(1) FROM dbo.Job WHERE Status IN ('queued','running','retry_wait','pending','paused','scheduled')")
    return int(row[0] or 0) if row else 0


def _delete_table(conn, table: str) -> None:
    if _table_exists(conn, table):
        conn.cursor().execute(f"DELETE FROM dbo.{table}")


def _reseed(conn, table: str) -> None:
    if _table_exists(conn, table):
        try:
            conn.cursor().execute(f"DBCC CHECKIDENT ('dbo.{table}', RESEED, 0) WITH NO_INFOMSGS")
        except Exception:
            pass


def reset_operational_data(*, dry_run: bool, confirm: str | None, preserve_users: bool, maintenance_override: bool = False) -> dict:
    conn = db_layer.connect(config.DB_PATH)
    before_tables = OPERATIONAL_TABLES + ([] if preserve_users else USER_TABLES)
    try:
        before = {table: _count(conn, table) for table in before_tables}
        active_jobs = _active_jobs(conn)
        if active_jobs and not maintenance_override:
            raise SystemExit(f"Refusing reset while {active_jobs} active jobs exist. Stop service/workers or pass --maintenance-override.")
        if not dry_run and confirm != CONFIRM_TOKEN:
            raise SystemExit(f"Refusing destructive reset without --confirm {CONFIRM_TOKEN}")
        if dry_run:
            return {"dry_run": True, "before": before, "after": before, "active_jobs": active_jobs}
        try:
            for table in before_tables:
                _delete_table(conn, table)
            for table in before_tables:
                if table not in REFERENCE_TABLES:
                    _reseed(conn, table)
            after = {table: _count(conn, table) for table in before_tables}
            conn.commit()
            return {"dry_run": False, "before": before, "after": after, "active_jobs": _active_jobs(conn)}
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reset operational AScrapper data safely.")
    parser.add_argument("--dry-run", action="store_true", help="Print counts without deleting anything.")
    parser.add_argument("--confirm", default=None, help=f"Required token for destructive mode: {CONFIRM_TOKEN}")
    parser.add_argument("--preserve-users", action="store_true", help="Keep Telegram users/access rows.")
    parser.add_argument("--maintenance-override", action="store_true", help="Allow reset despite active jobs after operator stopped service externally.")
    args = parser.parse_args(list(argv) if argv is not None else None)
    result = reset_operational_data(dry_run=args.dry_run, confirm=args.confirm, preserve_users=args.preserve_users, maintenance_override=args.maintenance_override)
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
