from __future__ import annotations

import argparse
import json
from typing import Any

import config
import db_layer
from tools.print_setup_progress import setup_progress

SETUP_JOB_TYPES_SQL = "'baseline_setup_area','setup_detail_baseline','setup_price_baseline'"


def _cancel_setup_jobs(conn, search_id: int, reason: str) -> int:
    cur = conn.cursor()
    cur.execute(
        f"""
        IF OBJECT_ID('dbo.Job') IS NOT NULL
        UPDATE dbo.Job
        SET Status='cancelled',
            FinishedAt=COALESCE(FinishedAt, SYSDATETIME()),
            LockedAt=NULL,
            LockedBy=NULL,
            UpdatedAt=SYSDATETIME(),
            LastError=?
        WHERE SearchID=?
          AND JobType IN ({SETUP_JOB_TYPES_SQL})
          AND Status IN ('queued','running','retry_wait')
        """,
        config.mask_sensitive_text(reason or "manual setup reset"),
        int(search_id),
    )
    try:
        return int(cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else 0)
    except Exception:
        return 0


def _reset_listing_setup_detail_markers(conn, search_id: int) -> int:
    cur = conn.cursor()
    cur.execute(
        """
        IF OBJECT_ID('dbo.ListingSearchState') IS NOT NULL
        AND COL_LENGTH('dbo.ListingSearchState', 'SetupDetailStatus') IS NOT NULL
        UPDATE dbo.ListingSearchState
        SET SetupDetailStatus=NULL,
            SetupDetailAttemptCount=0,
            SetupDetailLastAttemptAt=NULL,
            SetupDetailNextRetryAt=NULL,
            SetupDetailLastError=NULL,
            SetupDetailCompletedAt=NULL,
            LastDetailRefreshAt=NULL
        WHERE SearchID=?
        """,
        int(search_id),
    )
    try:
        return int(cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else 0)
    except Exception:
        return 0


def reset_setup_state(search_id: int, *, reason: str, force: bool = False) -> dict[str, Any]:
    conn = db_layer.connect(config.DB_PATH)
    try:
        db_layer.ensure_runtime_monitoring_schema(conn)
        before = setup_progress(search_id)
        active_jobs = db_layer.get_active_setup_pipeline_jobs(conn, search_id)
        running_jobs = [job for job in active_jobs if str(job.get("Status") or "").lower() == "running"]
        if running_jobs and not force:
            return {
                "ok": False,
                "reason": "service_or_worker_appears_active; rerun with --force after stopping service",
                "search_id": search_id,
                "running_setup_jobs": running_jobs,
                "before": before,
            }
        cancelled_jobs = _cancel_setup_jobs(conn, search_id, reason)
        reset_detail_rows = _reset_listing_setup_detail_markers(conn, search_id)
        db_layer.upsert_area_monitoring_state(
            conn,
            search_id,
            setup_status="failed",
            module1_status="pending",
            module3_status="pending",
            module2_status="pending",
            inferred_price_count=0,
            unknown_price_count=0,
            last_error=config.mask_sensitive_text(reason or "manual setup reset"),
        )
        cur = conn.cursor()
        cur.execute(
            """
            IF OBJECT_ID('dbo.UserAreaSubscription') IS NOT NULL
            UPDATE dbo.UserAreaSubscription
            SET SubscriptionStatus='preparing',
                NotifyEnabled=0,
                BaselineStatus='pending',
                BaselineStartedAt=NULL,
                BaselineCompletedAt=NULL,
                BaselineLastError=NULL,
                DetailBaselineStatus='pending',
                DetailBaselineStartedAt=NULL,
                DetailBaselineCompletedAt=NULL,
                DetailBaselineAttemptCount=0,
                DetailBaselineLastAttemptAt=NULL,
                DetailBaselineNextRetryAt=NULL,
                DetailBaselineLastError=NULL,
                PriceBaselineStatus='pending',
                PriceBaselineStartedAt=NULL,
                PriceBaselineCompletedAt=NULL,
                PriceBaselineLastError=NULL,
                NotificationReadyAt=NULL,
                UpdatedAt=SYSDATETIME()
            WHERE SearchID=? AND IsActive=1
            """,
            int(search_id),
        )
        conn.commit()
        after = setup_progress(search_id)
        return {
            "ok": True,
            "search_id": search_id,
            "cancelled_setup_jobs": cancelled_jobs,
            "reset_detail_rows": reset_detail_rows,
            "before": before,
            "after": after,
        }
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Safely reset failed setup state for one SearchID")
    parser.add_argument("--search-id", type=int, required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--force", action="store_true", help="Cancel running setup jobs too; only use after stopping the service")
    args = parser.parse_args()
    result = reset_setup_state(args.search_id, reason=args.reason, force=args.force)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if not result.get("ok"):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
