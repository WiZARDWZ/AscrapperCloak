from __future__ import annotations

import argparse
import json
from typing import Any

import config
import db_layer


def _rows(cur) -> list[dict[str, Any]]:
    cols = [c[0] for c in cur.description]
    return [{cols[i]: row[i] for i in range(len(cols))} for row in cur.fetchall()]


def _schema_status(conn, *, migrate: bool = True) -> dict[str, Any]:
    migration_error = None
    if migrate:
        try:
            db_layer.ensure_runtime_monitoring_schema(conn)
        except Exception as exc:
            migration_error = config.mask_sensitive_text(exc)
    setup_detail = db_layer.get_setup_detail_schema_status(conn)
    area_monitoring = db_layer.get_area_monitoring_schema_status(conn)
    missing = sorted(set(setup_detail.get("missing_columns") or []) | set(area_monitoring.get("missing_columns") or []))
    schema_ok = bool(setup_detail.get("setup_detail_schema_ok")) and bool(area_monitoring.get("area_monitoring_schema_ok")) and not migration_error
    return {
        "schema_ok": schema_ok,
        "setup_detail_schema_ok": bool(setup_detail.get("setup_detail_schema_ok")),
        "area_monitoring_schema_ok": bool(area_monitoring.get("area_monitoring_schema_ok")),
        "missing_columns": missing,
        "setup_detail_missing_columns": setup_detail.get("missing_columns") or [],
        "area_monitoring_missing_columns": area_monitoring.get("missing_columns") or [],
        "migration_error": migration_error,
        "suggested_action": None if schema_ok else "Run service startup or db_layer.ensure_runtime_monitoring_schema(conn); verify SQL Server permissions for ALTER TABLE/CREATE INDEX.",
    }


def setup_progress(search_id: int, *, migrate_schema: bool = True) -> dict[str, Any]:
    conn = db_layer.connect(config.DB_PATH)
    try:
        schema = _schema_status(conn, migrate=migrate_schema)
        if not schema["schema_ok"]:
            return {"search_id": search_id, **schema}
        state = db_layer.get_area_monitoring_state(conn, search_id) or {}
        subs = db_layer.get_active_user_area_subscriptions_for_search(conn, search_id)
        sub = subs[0] if subs else {"SearchID": search_id, "BaselineListingsCollected": state.get("active_listing_count")}
        detail = db_layer.get_detail_baseline_progress(conn, sub)
        active_jobs = db_layer.get_active_setup_pipeline_jobs(conn, search_id)
        current_setup_run_id = None
        current_run_started_at = sub.get("DetailBaselineStartedAt")
        if hasattr(current_run_started_at, "isoformat"):
            current_setup_run_id = current_run_started_at.isoformat()
        elif current_run_started_at:
            current_setup_run_id = str(current_run_started_at)
        cur = conn.cursor()
        cur.execute(
            """
            SELECT TOP (1) JobID, JobType, Status, CreatedAt, StartedAt, FinishedAt, LastError
            FROM dbo.Job
            WHERE SearchID=? AND JobType='baseline_setup_area'
            ORDER BY JobID DESC
            """,
            search_id,
        )
        latest_baseline = _rows(cur)
        cur.execute(
            """
            SELECT TOP (1) JobID, JobType, Status, CreatedAt, StartedAt, FinishedAt, LastError
            FROM dbo.Job
            WHERE SearchID=? AND JobType='setup_detail_baseline'
            ORDER BY JobID DESC
            """,
            search_id,
        )
        latest_detail = _rows(cur)
        latest_current_detail = db_layer.get_latest_setup_detail_job(conn, search_id, started_at=current_run_started_at)
        current_run_detail_jobs_count = db_layer.count_succeeded_setup_detail_jobs(conn, search_id, started_at=current_run_started_at)
        cur.execute(
            """
            SELECT COUNT(1)
            FROM dbo.Job
            WHERE SearchID=? AND JobType='setup_detail_baseline'
            """,
            search_id,
        )
        historical_detail_jobs_count = int((cur.fetchone() or [0])[0] or 0)
        cur.execute(
            """
            SELECT TOP (10) JobID, JobType, Status, RunAfter, CreatedAt, StartedAt, FinishedAt, LastError
            FROM dbo.Job
            WHERE SearchID=? AND JobType IN ('baseline_setup_area','setup_detail_baseline','setup_price_baseline')
            ORDER BY JobID DESC
            """,
            search_id,
        )
        last_jobs = _rows(cur)
        total = int(detail.get("detail_baseline_total_count") or state.get("active_listing_count") or 0)
        remaining = int(detail.get("detail_baseline_remaining_count") or 0)
        done = int(detail.get("detail_baseline_completed_count") or max(0, total - remaining))
        active_types = {str(job.get("JobType")) for job in active_jobs}
        active_detail_jobs = [job for job in active_jobs if str(job.get("JobType")) == "setup_detail_baseline"]
        active_baseline_jobs = [job for job in active_jobs if str(job.get("JobType")) == "baseline_setup_area"]
        active_price_jobs = [job for job in active_jobs if str(job.get("JobType")) == "setup_price_baseline"]
        module1_completed = str(state.get("module1_status") or "").lower() == "completed"
        module3_status = str(state.get("module3_status") or "").lower()
        latest_detail_status = str((latest_detail[0] if latest_detail else {}).get("Status") or "").lower()
        latest_detail_error = str((latest_detail[0] if latest_detail else {}).get("LastError") or "").strip()
        expected_next_detail_job = module1_completed and module3_status not in {"completed", "skipped"} and remaining > 0
        missing_next_detail_job = expected_next_detail_job and not active_detail_jobs and not active_baseline_jobs
        failed_but_recoverable = (
            str(state.get("setup_status") or "").lower() == "failed"
            and done > 0
            and remaining > 0
            and latest_detail_status == "succeeded"
            and not latest_detail_error
        )
        return {
            "search_id": search_id,
            **schema,
            "area_display_name": (subs[0].get("AreaLabel") if subs else None),
            "current_setup_run_id": current_setup_run_id,
            "current_baseline_job_id": (latest_baseline[0].get("JobID") if latest_baseline else None),
            "setup_status": state.get("setup_status"),
            "module1_status": state.get("module1_status"),
            "module3_status": state.get("module3_status"),
            "module2_status": state.get("module2_status"),
            "area_monitoring_last_error": state.get("last_error"),
            "module3_last_error": sub.get("DetailBaselineLastError"),
            "baseline_target_total": total,
            "detail_done": done,
            "detail_remaining": remaining,
            "detail_percent": round(done / total * 100.0, 2) if total else 0,
            "latest_baseline_job": latest_baseline[0] if latest_baseline else None,
            "latest_detail_job": latest_detail[0] if latest_detail else None,
            "latest_current_run_detail_job": latest_current_detail,
            "latest_detail_job_result_status": (latest_detail[0].get("Status") if latest_detail else None),
            "active_setup_jobs": active_jobs,
            "active_price_jobs": active_price_jobs,
            "last_10_setup_jobs": last_jobs,
            "expected_next_detail_job": expected_next_detail_job,
            "missing_next_detail_job": missing_next_detail_job,
            "repair_recommended": missing_next_detail_job and (str(state.get("setup_status") or "").lower() != "failed" or failed_but_recoverable),
            "current_run_batch_number": current_run_detail_jobs_count + 1,
            "historical_detail_jobs_count": historical_detail_jobs_count,
            "current_run_detail_jobs_count": current_run_detail_jobs_count,
            "safety_checks": {
                "baseline_rerun_active": "baseline_setup_area" in active_types and "setup_detail_baseline" in active_types,
                "price_premature": "setup_price_baseline" in active_types and remaining > 0,
                "progress_stalled": total > 0 and remaining > total * 0.9 and current_run_detail_jobs_count > total,
            },
        }
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Print setup pipeline progress for a SearchID")
    parser.add_argument("--search-id", type=int, required=True)
    parser.add_argument("--no-migrate", action="store_true", help="Only report schema health; do not run idempotent schema ensure first")
    args = parser.parse_args()
    print(json.dumps(setup_progress(args.search_id, migrate_schema=not args.no_migrate), ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
