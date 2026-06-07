import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import config
import db_layer
import job_queue
import monitoring_scheduler


class Conn:
    def close(self):
        pass
    def commit(self):
        pass



class MigrationConn:
    def __init__(self):
        self.commits = 0
        self.closed = False
    def commit(self):
        self.commits += 1
    def close(self):
        self.closed = True


def _capture_migration_sql():
    calls = []
    original = db_layer._execute_ddl_safely
    def fake_execute(conn, sql, description="", required=False):
        calls.append({"description": description, "sql": sql, "required": required})
        return True
    db_layer._execute_ddl_safely = fake_execute
    return calls, original


def _restore_migration_sql(original):
    db_layer._execute_ddl_safely = original


def test_ensure_job_tables_creates_job_from_scratch():
    calls, original = _capture_migration_sql()
    try:
        job_queue.ensure_job_tables(MigrationConn())
    finally:
        _restore_migration_sql(original)
    create_sql = next(call["sql"] for call in calls if call["description"] == "create dbo.Job")
    assert "CREATE TABLE dbo.Job" in create_sql
    assert "JobID INT IDENTITY(1,1)" in create_sql
    assert "Status NVARCHAR(50) NOT NULL CONSTRAINT DF_Job_Status DEFAULT ('queued')" in create_sql
    assert "RunAfter DATETIME2 NOT NULL CONSTRAINT DF_Job_RunAfter DEFAULT (SYSDATETIME())" in create_sql


def test_ensure_job_tables_upgrades_partial_job_table_with_full_column_specs():
    calls, original = _capture_migration_sql()
    try:
        job_queue.ensure_job_tables(MigrationConn())
    finally:
        _restore_migration_sql(original)
    add_calls = {call["description"].replace("add dbo.Job.", ""): call["sql"] for call in calls if call["description"].startswith("add dbo.Job.")}
    for column, definition in job_queue.JOB_REQUIRED_COLUMN_DEFINITIONS.items():
        sql = add_calls[column]
        assert f"ADD {column} {definition}" in sql
        assert f"ADD {column}\n" not in sql


def test_ensure_job_tables_required_columns_have_expected_types():
    assert job_queue.JOB_REQUIRED_COLUMN_DEFINITIONS["SearchID"] == "INT NULL"
    assert job_queue.JOB_REQUIRED_COLUMN_DEFINITIONS["UserAreaID"] == "INT NULL"
    assert job_queue.JOB_REQUIRED_COLUMN_DEFINITIONS["Priority"] == "INT NOT NULL CONSTRAINT DF_Job_Priority DEFAULT (30)"
    assert job_queue.JOB_REQUIRED_COLUMN_DEFINITIONS["RunAfter"] == "DATETIME2 NOT NULL CONSTRAINT DF_Job_RunAfter DEFAULT (SYSDATETIME())"
    assert job_queue.JOB_REQUIRED_COLUMN_DEFINITIONS["AttemptCount"] == "INT NOT NULL CONSTRAINT DF_Job_AttemptCount DEFAULT (0)"
    assert job_queue.JOB_REQUIRED_COLUMN_DEFINITIONS["MaxAttempts"] == "INT NOT NULL CONSTRAINT DF_Job_MaxAttempts DEFAULT (3)"
    assert job_queue.JOB_REQUIRED_COLUMN_DEFINITIONS["LockedBy"] == "NVARCHAR(200) NULL"
    assert job_queue.JOB_REQUIRED_COLUMN_DEFINITIONS["LockedAt"] == "DATETIME2 NULL"
    assert job_queue.JOB_REQUIRED_COLUMN_DEFINITIONS["StartedAt"] == "DATETIME2 NULL"
    assert job_queue.JOB_REQUIRED_COLUMN_DEFINITIONS["FinishedAt"] == "DATETIME2 NULL"
    assert job_queue.JOB_REQUIRED_COLUMN_DEFINITIONS["LastError"] == "NVARCHAR(MAX) NULL"
    assert job_queue.JOB_REQUIRED_COLUMN_DEFINITIONS["PayloadJson"] == "NVARCHAR(MAX) NULL"
    assert job_queue.JOB_REQUIRED_COLUMN_DEFINITIONS["DedupeKey"] == "NVARCHAR(300) NULL"
    assert job_queue.JOB_REQUIRED_COLUMN_DEFINITIONS["CreatedAt"] == "DATETIME2 NOT NULL CONSTRAINT DF_Job_CreatedAt DEFAULT (SYSDATETIME())"
    assert job_queue.JOB_REQUIRED_COLUMN_DEFINITIONS["UpdatedAt"] == "DATETIME2 NOT NULL CONSTRAINT DF_Job_UpdatedAt DEFAULT (SYSDATETIME())"


def test_job_indexes_are_guarded_by_required_column_existence():
    calls, original = _capture_migration_sql()
    try:
        job_queue.ensure_job_tables(MigrationConn())
    finally:
        _restore_migration_sql(original)
    dedupe_sql = next(call["sql"] for call in calls if call["description"] == "create UX_Job_Active_DedupeKey")
    due_sql = next(call["sql"] for call in calls if call["description"] == "create IX_Job_Due")
    assert "COL_LENGTH('dbo.Job', 'DedupeKey') IS NOT NULL" in dedupe_sql
    assert "COL_LENGTH('dbo.Job', 'Status') IS NOT NULL" in dedupe_sql
    for column in ("Status", "RunAfter", "Priority", "CreatedAt"):
        assert f"COL_LENGTH('dbo.Job', '{column}') IS NOT NULL" in due_sql


def test_print_queue_status_main_prints_json_for_upgraded_queue_helpers():
    import contextlib
    import io
    import json
    import tools.print_queue_status as print_queue_status

    originals = {
        "get_active_jobs": print_queue_status.job_queue.get_active_jobs,
        "get_queue_summary": print_queue_status.job_queue.get_queue_summary,
        "get_next_due_jobs": print_queue_status.job_queue.get_next_due_jobs,
    }
    print_queue_status.job_queue.get_active_jobs = lambda: [{"JobID": 1, "Status": "running"}, {"JobID": 2, "Status": "retry_wait"}]
    print_queue_status.job_queue.get_queue_summary = lambda: {"counts": [{"Priority": 0, "Status": "running", "Count": 1}]}
    print_queue_status.job_queue.get_next_due_jobs = lambda limit=20: [{"JobID": 3, "Status": "queued"}]
    try:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            print_queue_status.main()
        payload = json.loads(buffer.getvalue())
    finally:
        for name, value in originals.items():
            setattr(print_queue_status.job_queue, name, value)
    assert payload["running_jobs"] == [{"JobID": 1, "Status": "running"}]
    assert payload["retry_wait_jobs"] == [{"JobID": 2, "Status": "retry_wait"}]
    assert payload["next_due_jobs"] == [{"JobID": 3, "Status": "queued"}]

def _reset_queue():
    job_queue.enable_in_memory_store()


def _cleanup_queue():
    job_queue.disable_in_memory_store()



def test_status_check_constraint_upgrade_normalizes_legacy_statuses():
    calls, original = _capture_migration_sql()
    try:
        job_queue.ensure_job_tables(MigrationConn())
    finally:
        _restore_migration_sql(original)
    status_sql = next(call["sql"] for call in calls if call["description"] == "ensure dbo.Job.Status values and CK_Job_Status")
    assert "WHEN 'pending' THEN 'queued'" in status_sql
    assert "WHEN 'paused' THEN 'retry_wait'" in status_sql
    assert "WHEN 'done' THEN 'succeeded'" in status_sql
    assert "WHEN 'success' THEN 'succeeded'" in status_sql
    assert "WHEN 'error' THEN 'failed'" in status_sql
    assert "Unknown legacy Job.Status normalized to failed" in status_sql


def test_status_check_constraint_allows_phase2b_statuses_and_drops_stale_checks():
    calls, original = _capture_migration_sql()
    try:
        job_queue.ensure_job_tables(MigrationConn())
    finally:
        _restore_migration_sql(original)
    status_sql = next(call["sql"] for call in calls if call["description"] == "ensure dbo.Job.Status values and CK_Job_Status")
    for status in ("queued", "running", "succeeded", "failed", "retry_wait", "cancelled", "skipped"):
        assert f"'{status}'" in status_sql
        assert f"cc.definition NOT LIKE '%{status}%'" in status_sql
    assert "DROP CONSTRAINT" in status_sql
    assert "CONSTRAINT CK_Job_Status" in status_sql
    assert "Status IN ('queued', 'running', 'succeeded', 'failed', 'retry_wait', 'cancelled', 'skipped')" in status_sql


def test_status_default_is_updated_to_queued_when_stale():
    calls, original = _capture_migration_sql()
    try:
        job_queue.ensure_job_tables(MigrationConn())
    finally:
        _restore_migration_sql(original)
    default_sql = next(call["sql"] for call in calls if call["description"] == "ensure dbo.Job.Status default")
    assert "dc.definition NOT LIKE '%queued%'" in default_sql
    assert "DROP CONSTRAINT" in default_sql
    assert "DEFAULT ('queued') FOR Status" in default_sql


def test_phase2b_status_domain_rejects_unknown_statuses_after_migration():
    allowed = set(job_queue.PHASE2B_JOB_STATUSES)
    assert {"queued", "retry_wait", "succeeded"}.issubset(allowed)
    assert "pending" not in allowed
    assert "unknown_invalid_status" not in allowed
    assert job_queue.LEGACY_JOB_STATUS_NORMALIZATION["pending"] == "queued"
    assert job_queue.LEGACY_JOB_STATUS_NORMALIZATION["paused"] == "retry_wait"
    assert job_queue.LEGACY_JOB_STATUS_NORMALIZATION["done"] == "succeeded"


def test_status_constraint_migration_is_idempotent_sql():
    calls, original = _capture_migration_sql()
    try:
        job_queue.ensure_job_tables(MigrationConn())
    finally:
        _restore_migration_sql(original)
    status_sql = next(call["sql"] for call in calls if call["description"] == "ensure dbo.Job.Status values and CK_Job_Status")
    assert "IF NOT EXISTS (" in status_sql
    assert "cc.name='CK_Job_Status'" in status_sql
    assert "ALTER TABLE dbo.Job WITH CHECK ADD CONSTRAINT CK_Job_Status" in status_sql

def test_enqueue_job_creates_queued_job():
    _reset_queue()
    try:
        job = job_queue.enqueue_job(job_queue.JOB_TYPE_LIGHT_CHECK_NEW_LISTINGS, search_id=1, priority=20)
        assert job["Status"] == "queued"
        assert job["JobType"] == job_queue.JOB_TYPE_LIGHT_CHECK_NEW_LISTINGS
        assert job["SearchID"] == 1
    finally:
        _cleanup_queue()


def test_enqueue_job_once_dedupes_by_dedupe_key():
    _reset_queue()
    try:
        first = job_queue.enqueue_job_once(job_queue.JOB_TYPE_LIGHT_CHECK_NEW_LISTINGS, search_id=1)
        second = job_queue.enqueue_job_once(job_queue.JOB_TYPE_LIGHT_CHECK_NEW_LISTINGS, search_id=1)
        assert first["created"] is True
        assert second["created"] is False and second["JobID"] == first["JobID"]
    finally:
        _cleanup_queue()


def test_duplicate_active_jobs_for_same_dedupe_key_are_prevented():
    _reset_queue()
    try:
        key = "manual_check_now:search_id=1"
        first = job_queue.enqueue_job_once(job_queue.JOB_TYPE_MANUAL_CHECK_NOW, search_id=1, dedupe_key=key)
        claimed = job_queue.claim_next_job("worker-a")
        assert claimed["JobID"] == first["JobID"]
        duplicate = job_queue.enqueue_job_once(job_queue.JOB_TYPE_MANUAL_CHECK_NOW, search_id=1, dedupe_key=key)
        assert duplicate["created"] is False and duplicate["Status"] == "running"
    finally:
        _cleanup_queue()


def test_succeeded_old_job_does_not_block_new_same_dedupe_key():
    _reset_queue()
    try:
        first = job_queue.enqueue_job_once(job_queue.JOB_TYPE_LIGHT_CHECK_NEW_LISTINGS, search_id=1)
        job_queue.mark_job_succeeded(first["JobID"], {"ok": True})
        second = job_queue.enqueue_job_once(job_queue.JOB_TYPE_LIGHT_CHECK_NEW_LISTINGS, search_id=1)
        assert second["created"] is True
        assert second["JobID"] != first["JobID"]
    finally:
        _cleanup_queue()


def test_claim_next_job_picks_lowest_priority_first():
    _reset_queue()
    try:
        job_queue.enqueue_job(job_queue.JOB_TYPE_DETAIL_REFRESH_EXISTING, search_id=1, priority=30)
        setup = job_queue.enqueue_job(job_queue.JOB_TYPE_SETUP_FULL_BASELINE, search_id=2, priority=0)
        claimed = job_queue.claim_next_job("worker-a")
        assert claimed["JobID"] == setup["JobID"]
    finally:
        _cleanup_queue()


def test_claim_next_job_respects_run_after():
    _reset_queue()
    try:
        future = datetime.now() + timedelta(hours=1)
        job_queue.enqueue_job(job_queue.JOB_TYPE_SETUP_FULL_BASELINE, search_id=1, priority=0, run_after=future)
        assert job_queue.claim_next_job("worker-a") is None
    finally:
        _cleanup_queue()


def test_claim_next_job_marks_running_with_lock_fields():
    _reset_queue()
    try:
        job_queue.enqueue_job(job_queue.JOB_TYPE_LIGHT_CHECK_NEW_LISTINGS, search_id=1)
        claimed = job_queue.claim_next_job("worker-a")
        assert claimed["Status"] == "running"
        assert claimed["LockedBy"] == "worker-a"
        assert claimed["LockedAt"] is not None
        assert claimed["StartedAt"] is not None
    finally:
        _cleanup_queue()


def test_mark_job_succeeded_sets_succeeded_and_finished_at():
    _reset_queue()
    try:
        job = job_queue.enqueue_job(job_queue.JOB_TYPE_LIGHT_CHECK_NEW_LISTINGS, search_id=1)
        out = job_queue.mark_job_succeeded(job["JobID"], {"ok": True})
        assert out["Status"] == "succeeded" and out["FinishedAt"] is not None
    finally:
        _cleanup_queue()


def test_mark_job_failed_retryable_sets_retry_wait():
    _reset_queue()
    try:
        job = job_queue.enqueue_job(job_queue.JOB_TYPE_LIGHT_CHECK_NEW_LISTINGS, search_id=1, max_attempts=3)
        out = job_queue.mark_job_failed(job["JobID"], "temporary", retryable=True, retry_after_seconds=60)
        assert out["AttemptCount"] == 1
        assert out["Status"] == "retry_wait"
        assert out["RunAfter"] > datetime.now()
    finally:
        _cleanup_queue()


def test_mark_job_failed_terminal_sets_failed():
    _reset_queue()
    try:
        job = job_queue.enqueue_job(job_queue.JOB_TYPE_LIGHT_CHECK_NEW_LISTINGS, search_id=1, max_attempts=1)
        out = job_queue.mark_job_failed(job["JobID"], "fatal", retryable=False)
        assert out["Status"] == "failed"
    finally:
        _cleanup_queue()


def _patch_scheduler_subscriptions(subscriptions):
    originals = {
        "connect": db_layer.connect,
        "ensure_telegram_bot_tables": db_layer.ensure_telegram_bot_tables,
        "get_active_user_area_subscriptions": db_layer.get_active_user_area_subscriptions,
    }
    db_layer.connect = lambda path=None: Conn()
    db_layer.ensure_telegram_bot_tables = lambda conn: None
    db_layer.get_active_user_area_subscriptions = lambda conn: list(subscriptions)
    return originals


def _restore_scheduler_patch(originals):
    for name, value in originals.items():
        setattr(db_layer, name, value)


def test_enqueue_due_monitoring_jobs_creates_setup_light_and_detail_jobs():
    _reset_queue()
    now = datetime(2026, 6, 3, 12, 0, 0)
    subs = [
        {"UserAreaID": 1, "SearchID": 1, "BaselineStatus": "pending", "DetailBaselineStatus": "pending", "PriceBaselineStatus": "pending", "NotificationReadyAt": None, "LastLightCheckAt": None, "LastDetailRefreshAt": None},
        {"UserAreaID": 2, "SearchID": 2, "BaselineStatus": "completed", "DetailBaselineStatus": "completed", "PriceBaselineStatus": "completed", "NotificationReadyAt": now - timedelta(days=1), "LastLightCheckAt": None, "LastDetailRefreshAt": None},
    ]
    originals = _patch_scheduler_subscriptions(subs)
    try:
        out = monitoring_scheduler.enqueue_due_monitoring_jobs(now=now)
        types = sorted(job["JobType"] for job in out["created"])
        assert types == sorted([job_queue.JOB_TYPE_SETUP_FULL_BASELINE, job_queue.JOB_TYPE_LIGHT_CHECK_NEW_LISTINGS, job_queue.JOB_TYPE_DETAIL_REFRESH_EXISTING, job_queue.JOB_TYPE_PRICE_REFRESH_EXISTING, job_queue.JOB_TYPE_DAILY_FULL_LISTING_SWEEP])
    finally:
        _restore_scheduler_patch(originals)
        _cleanup_queue()


def test_enqueue_due_monitoring_jobs_does_not_duplicate_existing_active_jobs():
    _reset_queue()
    now = datetime(2026, 6, 3, 12, 0, 0)
    subs = [{"UserAreaID": 2, "SearchID": 2, "BaselineStatus": "completed", "DetailBaselineStatus": "completed", "PriceBaselineStatus": "completed", "NotificationReadyAt": now, "LastLightCheckAt": None, "LastDetailRefreshAt": None}]
    originals = _patch_scheduler_subscriptions(subs)
    try:
        first = monitoring_scheduler.enqueue_due_monitoring_jobs(now=now)
        second = monitoring_scheduler.enqueue_due_monitoring_jobs(now=now)
        assert len(first["created"]) == 4
        assert len(second["created"]) == 0
        assert len(second["skipped_duplicates"]) == 4
    finally:
        _restore_scheduler_patch(originals)
        _cleanup_queue()


def test_p0_setup_claimed_before_p2_p3():
    _reset_queue()
    try:
        job_queue.enqueue_job(job_queue.JOB_TYPE_LIGHT_CHECK_NEW_LISTINGS, search_id=1, priority=20)
        job_queue.enqueue_job(job_queue.JOB_TYPE_DETAIL_REFRESH_EXISTING, search_id=1, priority=30)
        setup = job_queue.enqueue_job(job_queue.JOB_TYPE_SETUP_FULL_BASELINE, search_id=2, priority=0)
        assert job_queue.claim_next_job("worker-a")["JobID"] == setup["JobID"]
    finally:
        _cleanup_queue()


def test_shared_search_multiple_subscriptions_create_one_light_and_one_detail_job():
    _reset_queue()
    now = datetime(2026, 6, 3, 12, 0, 0)
    subs = [
        {"UserAreaID": 1, "SearchID": 42, "BaselineStatus": "completed", "DetailBaselineStatus": "completed", "PriceBaselineStatus": "completed", "NotificationReadyAt": now, "LastLightCheckAt": None, "LastDetailRefreshAt": None},
        {"UserAreaID": 2, "SearchID": 42, "BaselineStatus": "completed", "DetailBaselineStatus": "completed", "PriceBaselineStatus": "completed", "NotificationReadyAt": now, "LastLightCheckAt": None, "LastDetailRefreshAt": None},
    ]
    originals = _patch_scheduler_subscriptions(subs)
    try:
        out = monitoring_scheduler.enqueue_due_monitoring_jobs(now=now)
        assert [job["JobType"] for job in out["created"]].count(job_queue.JOB_TYPE_LIGHT_CHECK_NEW_LISTINGS) == 1
        assert [job["JobType"] for job in out["created"]].count(job_queue.JOB_TYPE_DETAIL_REFRESH_EXISTING) == 1
        assert [job["JobType"] for job in out["created"]].count(job_queue.JOB_TYPE_PRICE_REFRESH_EXISTING) == 1
        assert [job["JobType"] for job in out["created"]].count(job_queue.JOB_TYPE_DAILY_FULL_LISTING_SWEEP) == 1
    finally:
        _restore_scheduler_patch(originals)
        _cleanup_queue()



def test_due_calculation_uses_supplied_local_now_and_three_hour_old_timestamps_are_due():
    _reset_queue()
    now = datetime(2026, 6, 3, 16, 20, 0)
    old = datetime(2026, 6, 3, 13, 20, 37)
    subs = [{"UserAreaID": 1, "SearchID": 1, "BaselineStatus": "completed", "DetailBaselineStatus": "completed", "PriceBaselineStatus": "completed", "NotificationReadyAt": now, "LastLightCheckAt": old, "LastDetailRefreshAt": old}]
    originals = _patch_scheduler_subscriptions(subs)
    try:
        out = monitoring_scheduler.enqueue_due_monitoring_jobs(now=now)
        created_types = sorted(job["JobType"] for job in out["created"])
        assert created_types == sorted([job_queue.JOB_TYPE_LIGHT_CHECK_NEW_LISTINGS, job_queue.JOB_TYPE_DETAIL_REFRESH_EXISTING, job_queue.JOB_TYPE_PRICE_REFRESH_EXISTING, job_queue.JOB_TYPE_DAILY_FULL_LISTING_SWEEP])
        light_check = next(check for check in out["due_checks"] if check["job_type"] == job_queue.JOB_TYPE_LIGHT_CHECK_NEW_LISTINGS)
        assert light_check["current_time_used"] == now
        assert light_check["due_before"] == datetime(2026, 6, 3, 15, 50, 0)
        assert light_check["seconds_since_last"] == 10763
        assert light_check["reason"] == "due"
    finally:
        _restore_scheduler_patch(originals)
        _cleanup_queue()


def test_due_calculation_marks_recent_timestamps_not_due_with_debug_fields():
    _reset_queue()
    now = datetime(2026, 6, 3, 16, 20, 0)
    recent = now - timedelta(minutes=10)
    subs = [{"UserAreaID": 1, "SearchID": 1, "BaselineStatus": "completed", "DetailBaselineStatus": "completed", "PriceBaselineStatus": "completed", "NotificationReadyAt": now, "LastLightCheckAt": recent, "LastDetailRefreshAt": recent, "LastPriceRefreshAt": recent, "LastFullListingSweepAt": recent}]
    originals = _patch_scheduler_subscriptions(subs)
    try:
        out = monitoring_scheduler.enqueue_due_monitoring_jobs(now=now)
        assert out["created"] == []
        assert len(out["not_due"]) == 4
        for item in out["not_due"]:
            assert item["current_time_used"] == now
            assert item["last_at"] == recent
            if "seconds_since_last" in item:
                assert item["seconds_since_last"] == 600
            assert item["reason"] == "not_due"
            assert item["is_due"] is False
    finally:
        _restore_scheduler_patch(originals)
        _cleanup_queue()


def test_null_light_timestamp_on_one_shared_subscription_makes_search_due():
    _reset_queue()
    now = datetime(2026, 6, 3, 16, 20, 0)
    recent = now - timedelta(minutes=10)
    subs = [
        {"UserAreaID": 1, "SearchID": 42, "BaselineStatus": "completed", "DetailBaselineStatus": "completed", "PriceBaselineStatus": "completed", "NotificationReadyAt": now, "LastLightCheckAt": None, "LastDetailRefreshAt": recent, "LastPriceRefreshAt": recent, "LastFullListingSweepAt": recent},
        {"UserAreaID": 2, "SearchID": 42, "BaselineStatus": "completed", "DetailBaselineStatus": "completed", "PriceBaselineStatus": "completed", "NotificationReadyAt": now, "LastLightCheckAt": recent, "LastDetailRefreshAt": recent, "LastPriceRefreshAt": recent, "LastFullListingSweepAt": recent},
    ]
    originals = _patch_scheduler_subscriptions(subs)
    try:
        out = monitoring_scheduler.enqueue_due_monitoring_jobs(now=now)
        assert [job["JobType"] for job in out["created"]] == [job_queue.JOB_TYPE_LIGHT_CHECK_NEW_LISTINGS]
        check = next(check for check in out["due_checks"] if check["job_type"] == job_queue.JOB_TYPE_LIGHT_CHECK_NEW_LISTINGS)
        assert check["last_at"] is None
        assert check["reason"] == "due_null_last_at"
    finally:
        _restore_scheduler_patch(originals)
        _cleanup_queue()


def test_scheduler_due_path_does_not_use_utcnow_for_db_timestamp_comparisons():
    import inspect
    source = inspect.getsource(monitoring_scheduler.enqueue_due_monitoring_jobs)
    assert "utcnow" not in source
    assert "_fetch_sql_server_local_time" in source


def test_detail_refresh_existing_candidate_selection_returns_limit_and_oldest_first_sql():
    original_upsert = db_layer._upsert_search
    original_ddl = db_layer._execute_ddl_safely

    class Cursor:
        def __init__(self):
            self.sql = ""
            self.params = None
            self.description = [(name,) for name in (
                "db_listing_id", "external_id", "listing_id", "url", "address", "property_type", "price", "price_display",
                "bedrooms", "bathrooms", "parking", "current_status", "last_detail_refresh_at", "stale_at"
            )]
        def execute(self, sql, *params):
            self.sql = sql
            self.params = params
        def fetchall(self):
            return [(i, str(i), str(i), f"http://x/{i}", "addr", "house", None, "$1", 2, 1, 1, "active", None if i == 1 else datetime(2026, 6, 3, 12, 0, 0), None) for i in range(1, 36)]

    class Conn:
        def __init__(self, cursor): self._cursor = cursor
        def cursor(self): return self._cursor
        def rollback(self): pass

    cursor = Cursor()
    try:
        db_layer._upsert_search = lambda conn, url: 1
        db_layer._execute_ddl_safely = lambda *args, **kwargs: True
        rows = db_layer.get_active_listings_for_detail_refresh(Conn(cursor), "url", limit=35, stale_hours=0)
    finally:
        db_layer._upsert_search = original_upsert
        db_layer._execute_ddl_safely = original_ddl
    assert len(rows) == 35
    assert cursor.params[0] == 35
    assert cursor.params[-2] is None and cursor.params[-1] is None
    assert "LOWER(COALESCE(lss.Status, '')) = 'active'" in cursor.sql
    assert "lss.LastDetailRefreshAt AS last_detail_refresh_at" in cursor.sql
    assert "ORDER BY CASE WHEN lss.LastDetailRefreshAt IS NULL THEN 0 ELSE 1 END ASC" in cursor.sql
    assert "lss.LastDetailRefreshAt ASC" in cursor.sql


def test_detail_refresh_existing_worker_path_uses_search_id_limit_and_disables_stale_filter():
    calls = {}
    originals = {
        "load": monitoring_scheduler._load_search_subscription,
        "refresh": monitoring_scheduler.refresh_active_listings,
        "connect": monitoring_scheduler.db_layer.connect,
        "mark": monitoring_scheduler.db_layer.mark_search_detail_refreshed,
        "debug": monitoring_scheduler.db_layer.get_detail_refresh_candidate_debug_counts,
        "queue": monitoring_scheduler._queue_notifications_for_search,
    }
    class Conn:
        def close(self): pass
    def fake_refresh(search_url, **kwargs):
        calls["search_url"] = search_url
        calls.update(kwargs)
        return {
            "search_url": search_url,
            "limit": kwargs["limit"],
            "stale_hours": kwargs["stale_hours"],
            "candidates_count": 35,
            "processed_count": 35,
            "refreshed_count": 35,
            "failed_count": 0,
            "events_created": 0,
            "first_candidate_listing_ids": ["1"],
            "first_candidate_last_detail_refresh_at": [None],
        }
    try:
        monitoring_scheduler._load_search_subscription = lambda search_id, preferred_user_area_id=None: {"SearchID": search_id, "SearchURL": "url", "UserAreaID": 1}
        monitoring_scheduler.refresh_active_listings = fake_refresh
        monitoring_scheduler.db_layer.connect = lambda path=None: Conn()
        monitoring_scheduler.db_layer.mark_search_detail_refreshed = lambda conn, search_id: calls.setdefault("marked", search_id)
        monitoring_scheduler.db_layer.get_detail_refresh_candidate_debug_counts = lambda conn, search_id: {"total_state_rows": 164, "active_state_rows": 164, "valid_url_rows": 164}
        monitoring_scheduler._queue_notifications_for_search = lambda search_id, dry_run=False: []
        out = monitoring_scheduler.run_detail_refresh_existing_for_search(1, limit=35, dry_run=False, send_telegram=False)
    finally:
        monitoring_scheduler._load_search_subscription = originals["load"]
        monitoring_scheduler.refresh_active_listings = originals["refresh"]
        monitoring_scheduler.db_layer.connect = originals["connect"]
        monitoring_scheduler.db_layer.mark_search_detail_refreshed = originals["mark"]
        monitoring_scheduler.db_layer.get_detail_refresh_candidate_debug_counts = originals["debug"]
        monitoring_scheduler._queue_notifications_for_search = originals["queue"]
    detail = out["detail_refresh"]
    assert out["status"] == "completed"
    assert calls["stale_hours"] == 0
    assert calls["limit"] == 35
    assert calls["subscription"]["SearchID"] == 1
    assert calls["marked"] == 1
    assert detail["search_id"] == 1
    assert detail["selection_strategy"] == "oldest_first"
    assert detail["stale_filter_enabled"] is False
    assert detail["total_state_rows"] == 164
    assert detail["active_state_rows"] == 164
    assert detail["valid_url_rows"] == 164
    assert detail["candidates_count"] == 35


def test_detail_refresh_existing_does_not_mark_subscription_when_zero_processed_with_active_rows():
    calls = {}
    originals = {
        "load": monitoring_scheduler._load_search_subscription,
        "refresh": monitoring_scheduler.refresh_active_listings,
        "connect": monitoring_scheduler.db_layer.connect,
        "mark": monitoring_scheduler.db_layer.mark_search_detail_refreshed,
        "debug": monitoring_scheduler.db_layer.get_detail_refresh_candidate_debug_counts,
        "queue": monitoring_scheduler._queue_notifications_for_search,
    }
    class Conn:
        def close(self): pass
    try:
        monitoring_scheduler._load_search_subscription = lambda search_id, preferred_user_area_id=None: {"SearchID": search_id, "SearchURL": "url", "UserAreaID": 1}
        monitoring_scheduler.refresh_active_listings = lambda *args, **kwargs: {"candidates_count": 0, "processed_count": 0, "first_candidate_listing_ids": []}
        monitoring_scheduler.db_layer.connect = lambda path=None: Conn()
        monitoring_scheduler.db_layer.mark_search_detail_refreshed = lambda conn, search_id: calls.setdefault("marked", search_id)
        monitoring_scheduler.db_layer.get_detail_refresh_candidate_debug_counts = lambda conn, search_id: {"total_state_rows": 164, "active_state_rows": 164, "valid_url_rows": 164}
        monitoring_scheduler._queue_notifications_for_search = lambda search_id, dry_run=False: []
        out = monitoring_scheduler.run_detail_refresh_existing_for_search(1, limit=35, dry_run=False, send_telegram=False)
    finally:
        monitoring_scheduler._load_search_subscription = originals["load"]
        monitoring_scheduler.refresh_active_listings = originals["refresh"]
        monitoring_scheduler.db_layer.connect = originals["connect"]
        monitoring_scheduler.db_layer.mark_search_detail_refreshed = originals["mark"]
        monitoring_scheduler.db_layer.get_detail_refresh_candidate_debug_counts = originals["debug"]
        monitoring_scheduler._queue_notifications_for_search = originals["queue"]
    assert out["detail_refresh"]["active_state_rows"] == 164
    assert "marked" not in calls


def test_ingest_detail_refresh_updates_per_listing_timestamp_for_processed_rows():
    import inspect
    source = inspect.getsource(db_layer.ingest_detail_refresh_rows_conn)
    assert "mark_listing_search_state_detail_refreshed(conn, sid, lid)" in source
    mark_source = inspect.getsource(db_layer.mark_listing_search_state_detail_refreshed)
    assert "LastDetailRefreshAt=SYSDATETIME()" in mark_source
    assert "WHERE SearchID=? AND ListingID=?" in mark_source

def test_direct_detail_refresh_existing_tool_prints_json_result():
    import contextlib
    import io
    import json
    import tools.run_detail_refresh_existing_job_once as tool

    original = tool.monitoring_scheduler.run_detail_refresh_existing_for_search
    original_argv = sys.argv[:]
    try:
        tool.monitoring_scheduler.run_detail_refresh_existing_for_search = lambda **kwargs: {"status": "completed", "detail_refresh": {"candidates_count": 35, "search_id": kwargs["search_id"]}}
        sys.argv = ["run_detail_refresh_existing_job_once.py", "--search-id", "1", "--limit", "35"]
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            tool.main()
        payload = json.loads(buffer.getvalue())
    finally:
        tool.monitoring_scheduler.run_detail_refresh_existing_for_search = original
        sys.argv = original_argv
    assert payload["detail_refresh"]["candidates_count"] == 35
    assert payload["detail_refresh"]["search_id"] == 1



def test_admin_telegram_ids_default_parser():
    import types
    import telegram_bot

    assert config.parse_admin_telegram_ids("111694049") == {"111694049"}
    assert config.parse_admin_telegram_ids("111694049, 222,333 ") == {"111694049", "222", "333"}
    user_update = types.SimpleNamespace(effective_user=types.SimpleNamespace(id=111694049), effective_chat=types.SimpleNamespace(id=-100123))
    chat_update = types.SimpleNamespace(effective_user=types.SimpleNamespace(id=222), effective_chat=types.SimpleNamespace(id=111694049))
    assert telegram_bot.parse_admin_telegram_ids("111694049") == {"111694049"}
    assert telegram_bot._admin_identity_matches(user_update)
    assert telegram_bot._admin_identity_matches(chat_update)


def test_light_check_interval_is_1800_seconds():
    assert config.NEW_LISTING_CHECK_INTERVAL_SECONDS == 1800


def test_price_display_priority_displayed_then_inferred_fallback():
    import notification_formatter

    both = notification_formatter.effective_price_text({"price_display": "$950,000", "inferred_price_low": 900000, "inferred_price_high": 1000000})
    fallback = notification_formatter.effective_price_text({"price_display": "Auction", "inferred_price_low": 900000, "inferred_price_high": 1000000})
    assert both.startswith("$950,000")
    assert "Estimated range: $900,000 - $1,000,000" in both
    assert fallback == "Estimated range: $900,000 - $1,000,000"


def test_light_check_new_listing_enqueues_enrichment_without_notifications():
    _reset_queue()
    calls = {"queued": 0}
    originals = {
        "load": monitoring_scheduler._load_search_subscription,
        "light": monitoring_scheduler.light_check_area,
        "connect": monitoring_scheduler.db_layer.connect,
        "mark": monitoring_scheduler.db_layer.mark_search_light_checked,
        "queue": monitoring_scheduler._queue_notifications_for_search,
    }
    class Conn:
        def close(self): pass
    try:
        monitoring_scheduler._load_search_subscription = lambda search_id, preferred_user_area_id=None: {"SearchID": search_id, "SearchURL": "url", "UserAreaID": 9}
        monitoring_scheduler.light_check_area = lambda *args, **kwargs: {"new_count": 1, "new_listings": [{"listing_id": "abc123"}]}
        monitoring_scheduler.db_layer.connect = lambda path=None: Conn()
        monitoring_scheduler.db_layer.mark_search_light_checked = lambda conn, search_id: None
        def queue(*args, **kwargs):
            calls["queued"] += 1
            return []
        monitoring_scheduler._queue_notifications_for_search = queue
        out = monitoring_scheduler.execute_job({"JobType": job_queue.JOB_TYPE_LIGHT_CHECK_NEW_LISTINGS, "SearchID": 7, "UserAreaID": 9}, send_telegram=True)
        assert out["notifications"] == []
        assert calls["queued"] == 0
        assert out["new_listing_jobs"][0]["JobType"] == job_queue.JOB_TYPE_PROCESS_NEW_LISTING
    finally:
        monitoring_scheduler._load_search_subscription = originals["load"]
        monitoring_scheduler.light_check_area = originals["light"]
        monitoring_scheduler.db_layer.connect = originals["connect"]
        monitoring_scheduler.db_layer.mark_search_light_checked = originals["mark"]
        monitoring_scheduler._queue_notifications_for_search = originals["queue"]
        _cleanup_queue()


def test_price_refresh_schedules_by_configured_times_and_runs_all_batches():
    _reset_queue()
    now = datetime(2026, 6, 3, 14, 5, 0)  # 2026-06-04 00:05 Australia/Sydney
    old = datetime(2026, 6, 2, 12, 0, 0)
    subs = [{"UserAreaID": 1, "SearchID": 1, "BaselineStatus": "completed", "DetailBaselineStatus": "completed", "PriceBaselineStatus": "completed", "NotificationReadyAt": now, "LastLightCheckAt": now, "LastDetailRefreshAt": now, "LastPriceRefreshAt": old, "LastFullListingSweepAt": now}]
    originals = _patch_scheduler_subscriptions(subs)
    try:
        out = monitoring_scheduler.enqueue_due_monitoring_jobs(now=now)
        assert any(job["JobType"] == job_queue.JOB_TYPE_PRICE_REFRESH_EXISTING for job in out["created"])
    finally:
        _restore_scheduler_patch(originals)
        _cleanup_queue()

    _reset_queue()
    calls = {"marked": 0, "remaining_calls": 0}
    originals = {
        "run": monitoring_scheduler.run_price_baseline_for_search,
        "connect": monitoring_scheduler.db_layer.connect,
        "remaining": monitoring_scheduler.db_layer.get_active_listings_for_price_inference,
        "mark": monitoring_scheduler.db_layer.mark_search_price_refreshed,
    }
    class Conn:
        def close(self): pass
    try:
        monitoring_scheduler.run_price_baseline_for_search = lambda *args, **kwargs: {"status": "completed", "processed_count": 10}
        monitoring_scheduler.db_layer.connect = lambda path=None: Conn()
        def remaining(conn, search_id, limit=1, before_time=None, **kwargs):
            calls["remaining_calls"] += 1
            return [{"listing_id": "still-due"}] if calls["remaining_calls"] == 1 else []
        monitoring_scheduler.db_layer.get_active_listings_for_price_inference = remaining
        monitoring_scheduler.db_layer.mark_search_price_refreshed = lambda conn, search_id: calls.__setitem__("marked", calls["marked"] + 1)
        first = monitoring_scheduler.run_price_refresh_existing_for_search(1, payload={"run_id": "r1", "run_started_at": now.isoformat()})
        second = monitoring_scheduler.run_price_refresh_existing_for_search(1, payload={"run_id": "r1", "run_started_at": now.isoformat()})
        assert first["status"] == "running" and first["enqueued_next_batch"]
        assert second["status"] == "completed"
        assert calls["marked"] == 1
    finally:
        monitoring_scheduler.run_price_baseline_for_search = originals["run"]
        monitoring_scheduler.db_layer.connect = originals["connect"]
        monitoring_scheduler.db_layer.get_active_listings_for_price_inference = originals["remaining"]
        monitoring_scheduler.db_layer.mark_search_price_refreshed = originals["mark"]
        _cleanup_queue()


def test_daily_full_listing_safety_sweep_schedules_at_0400_sydney():
    _reset_queue()
    now = datetime(2026, 6, 3, 18, 5, 0)  # 2026-06-04 04:05 Australia/Sydney
    old = datetime(2026, 6, 2, 12, 0, 0)
    subs = [{"UserAreaID": 1, "SearchID": 1, "BaselineStatus": "completed", "DetailBaselineStatus": "completed", "PriceBaselineStatus": "completed", "NotificationReadyAt": now, "LastLightCheckAt": now, "LastDetailRefreshAt": now, "LastPriceRefreshAt": now, "LastFullListingSweepAt": old}]
    originals = _patch_scheduler_subscriptions(subs)
    try:
        out = monitoring_scheduler.enqueue_due_monitoring_jobs(now=now)
        assert any(job["JobType"] == job_queue.JOB_TYPE_DAILY_FULL_LISTING_SWEEP for job in out["created"])
    finally:
        _restore_scheduler_patch(originals)
        _cleanup_queue()

def run_tests():
    tests = [name for name in globals() if name.startswith("test_")]
    for name in tests:
        globals()[name]()
    print("job_queue tests passed")


if __name__ == "__main__":
    run_tests()

