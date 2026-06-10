import sys
import types
from datetime import datetime, timedelta
from decimal import Decimal

sys.modules.setdefault("pyodbc", types.SimpleNamespace(connect=lambda *args, **kwargs: None))

import db_layer
import job_queue
import monitoring_scheduler
from realestate_errors import RealEstateBlockedError


def setup_function():
    job_queue.enable_in_memory_store()


def teardown_function():
    job_queue.disable_in_memory_store()


def test_mark_job_retry_wait_sets_run_after_and_clears_lock():
    job = job_queue.enqueue_job(job_queue.JOB_TYPE_LIGHT_CHECK_NEW_LISTINGS, search_id=1)
    claimed = job_queue.claim_next_job("worker-a")
    out = job_queue.mark_job_retry_wait(claimed["JobID"], "temporary browser failure", retry_after_seconds=60)
    assert out["Status"] == "retry_wait"
    assert out["RunAfter"] > datetime.now()
    assert out["LockedAt"] is None
    assert out["LockedBy"] is None


def test_due_retry_wait_is_claimed_without_stale_lock():
    past = datetime.now() - timedelta(seconds=1)
    job_queue.enable_in_memory_store([
        {
            "JobID": 1,
            "JobType": job_queue.JOB_TYPE_LIGHT_CHECK_NEW_LISTINGS,
            "SearchID": 1,
            "UserAreaID": 1,
            "Priority": 20,
            "Status": "retry_wait",
            "RunAfter": past,
            "AttemptCount": 1,
            "MaxAttempts": 3,
            "LockedBy": "old-worker",
            "LockedAt": past,
            "StartedAt": past,
            "FinishedAt": past,
            "LastError": "temporary",
            "PayloadJson": None,
            "DedupeKey": None,
            "CreatedAt": past,
            "UpdatedAt": past,
        }
    ])
    claimed = job_queue.claim_next_job("worker-b")
    assert claimed["Status"] == "running"
    assert claimed["LockedBy"] == "worker-b"
    assert claimed["JobID"] == 1


def test_json_dumps_safe_serializes_decimal_payloads():
    payload = {"price_low": Decimal("123.45"), "nested": {"land": Decimal("1000000000.25")}}
    dumped = db_layer.json_dumps_safe(payload, sort_keys=True)
    assert '"123.45"' in dumped
    assert '"1000000000.25"' in dumped


class MigrationCursor:
    def __init__(self, conn):
        self.conn = conn
        self.description = None
    def execute(self, sql, *params):
        self.conn.sql.append(str(sql))
        return self
    def fetchall(self):
        return []
    def fetchone(self):
        return None


class MigrationConn:
    def __init__(self):
        self.sql = []
    def cursor(self):
        return MigrationCursor(self)
    def commit(self):
        pass
    def rollback(self):
        pass


def test_area_numeric_capacity_migration_widens_property_and_snapshot_columns():
    conn = MigrationConn()
    db_layer.ensure_area_numeric_capacity(conn)
    sql = "\n".join(conn.sql)
    assert "ALTER TABLE dbo.Property ALTER COLUMN LandAreaSqm DECIMAL(18,2) NULL" in sql
    assert "ALTER TABLE dbo.Property ALTER COLUMN BuildingAreaSqm DECIMAL(18,2) NULL" in sql
    assert "ALTER TABLE dbo.ListingSnapshot ALTER COLUMN LandSizeSqm DECIMAL(18,2) NULL" in sql
    assert "COL_LENGTH('dbo.ListingSnapshot', 'FloorAreaSqm') IS NOT NULL" in sql


def test_baseline_scheduler_skips_terminal_failed_area_without_manual_retry(monkeypatch):
    now = datetime(2026, 6, 8, 12, 0, 0)
    sub = {"UserAreaID": 10, "SearchID": 42, "SearchURL": "url", "BaselineStatus": "pending", "DetailBaselineStatus": "pending", "PriceBaselineStatus": "pending"}
    class Conn:
        def cursor(self): return self
        def close(self): pass
        def execute(self, *a, **k): return self
        def fetchone(self): return [now]
    monkeypatch.setattr(monitoring_scheduler.db_layer, "connect", lambda path=None: Conn())
    monkeypatch.setattr(monitoring_scheduler.db_layer, "ensure_telegram_bot_tables", lambda conn: None)
    monkeypatch.setattr(monitoring_scheduler.job_queue if hasattr(monitoring_scheduler, 'job_queue') else job_queue, "ensure_job_tables", lambda conn=None: None, raising=False)
    monkeypatch.setattr(monitoring_scheduler.db_layer, "get_active_user_area_subscriptions", lambda conn: [sub])
    monkeypatch.setattr(monitoring_scheduler.db_layer, "get_area_monitoring_state", lambda conn, area_id: {"setup_status": "failed_ingest"})
    out = monitoring_scheduler.enqueue_due_monitoring_jobs(now=now)
    assert out["created"] == []
    assert out["not_due"][0]["reason"] == "terminal_failed_area_requires_manual_retry"


def test_transient_page_goto_failure_marks_retry_wait(monkeypatch):
    job = job_queue.enqueue_job(job_queue.JOB_TYPE_LIGHT_CHECK_NEW_LISTINGS, search_id=1, max_attempts=3)
    def raise_goto(_job, send_telegram=True):
        raise RuntimeError("Page.goto: net::ERR_HTTP_RESPONSE_CODE_FAILURE")
    monkeypatch.setattr(monitoring_scheduler, "execute_job", raise_goto)
    out = monitoring_scheduler.run_next_job_once(worker_id="worker-a", send_telegram=False)
    assert out["status"] == "retry_wait"
    stored = job_queue._TEST_STORE[0]
    assert stored["Status"] == "retry_wait"
    assert stored["RunAfter"] > datetime.now()
    assert stored["LockedAt"] is None


def _ready_price_subscription(now, last_price_refresh_at=None):
    return {
        "UserAreaID": 10,
        "SearchID": 42,
        "SearchURL": "https://example.test/search",
        "AreaSetupStatus": "ready",
        "AreaReadyAt": now - timedelta(days=1),
        "SubscriptionStatus": "active",
        "SubscriptionNotifyEnabled": 1,
        "BaselineStatus": "completed",
        "DetailBaselineStatus": "completed",
        "PriceBaselineStatus": "completed",
        "NotificationReadyAt": now - timedelta(days=1),
        "LastLightCheckAt": now,
        "LastDetailRefreshAt": now,
        "LastPriceRefreshAt": last_price_refresh_at,
        "LastFullListingSweepAt": now,
    }


class SchedulerConn:
    def cursor(self): return self
    def close(self): pass
    def execute(self, *a, **k): return self
    def fetchone(self): return None


def _patch_ready_scheduler(monkeypatch, now, subscriptions):
    monkeypatch.setattr(monitoring_scheduler.config, "SCHEDULE_TIMEZONE", "UTC")
    monkeypatch.setattr(monitoring_scheduler.config, "PRICE_REFRESH_TIMES", "12:00")
    monkeypatch.setattr(monitoring_scheduler.config, "PRICE_INFERENCE_ENABLED", True)
    monkeypatch.setattr(monitoring_scheduler.db_layer, "connect", lambda path=None: SchedulerConn())
    monkeypatch.setattr(monitoring_scheduler.db_layer, "ensure_telegram_bot_tables", lambda conn: None)
    monkeypatch.setattr(monitoring_scheduler.db_layer, "get_active_user_area_subscriptions", lambda conn: subscriptions)
    monkeypatch.setattr(monitoring_scheduler.db_layer, "get_due_price_retry_listing_ids", lambda *a, **k: [])
    monkeypatch.setattr(monitoring_scheduler.db_layer, "get_area_monitoring_state", lambda conn, area_id: {"setup_status": "ready"})


def test_price_refresh_scheduled_occurrence_creates_one_job_only(monkeypatch):
    now = datetime(2026, 6, 8, 12, 30, 0)
    _patch_ready_scheduler(monkeypatch, now, [_ready_price_subscription(now)])
    first = monitoring_scheduler.enqueue_due_monitoring_jobs(now=now)
    price_jobs = [job for job in first["created"] if job.get("JobType") == job_queue.JOB_TYPE_MODULE2_PRICE_REFRESH_AREA]
    assert len(price_jobs) == 1
    assert "scheduled_at=2026-06-08T12:00:00+00:00" in price_jobs[0]["DedupeKey"]
    job_queue.mark_job_succeeded(price_jobs[0]["JobID"], {"status": "completed"})
    second = monitoring_scheduler.enqueue_due_monitoring_jobs(now=now + timedelta(minutes=1))
    assert not [job for job in second["created"] if job.get("JobType") == job_queue.JOB_TYPE_MODULE2_PRICE_REFRESH_AREA]
    assert any(job.get("reason") == "existing_scheduled_price_job" for job in second["skipped_duplicates"])


def test_price_refresh_not_due_after_last_price_refresh_updated(monkeypatch):
    now = datetime(2026, 6, 8, 12, 30, 0)
    _patch_ready_scheduler(monkeypatch, now, [_ready_price_subscription(now, last_price_refresh_at=now)])
    out = monitoring_scheduler.enqueue_due_monitoring_jobs(now=now)
    assert not [job for job in out["created"] if job.get("JobType") == job_queue.JOB_TYPE_MODULE2_PRICE_REFRESH_AREA]
    assert any(check.get("job_type") == job_queue.JOB_TYPE_MODULE2_PRICE_REFRESH_AREA and check.get("is_due") is False for check in out["due_checks"])


def test_active_module2_price_refresh_area_blocks_new_scheduled_job(monkeypatch):
    now = datetime(2026, 6, 8, 12, 30, 0)
    _patch_ready_scheduler(monkeypatch, now, [_ready_price_subscription(now)])
    active = job_queue.enqueue_job(job_queue.JOB_TYPE_MODULE2_PRICE_REFRESH_AREA, search_id=42, priority=job_queue.PRIORITY_PRICE_REFRESH)
    job_queue.claim_next_job("worker")
    out = monitoring_scheduler.enqueue_due_monitoring_jobs(now=now)
    assert not [job for job in out["created"] if job.get("JobType") == job_queue.JOB_TYPE_MODULE2_PRICE_REFRESH_AREA]
    assert any(job.get("reason") == "active_price_refresh_exists" for job in out["skipped_duplicates"])


def test_zero_candidate_price_refresh_marks_search_refreshed(monkeypatch):
    marked = []
    class Conn:
        def close(self): pass
    monkeypatch.setattr(monitoring_scheduler, "_search_is_active_for_monitoring", lambda search_id: True)
    monkeypatch.setattr(monitoring_scheduler, "run_price_baseline_for_search", lambda *a, **k: {"status": "completed", "processed_count": 0})
    monkeypatch.setattr(monitoring_scheduler.db_layer, "connect", lambda path=None: Conn())
    monkeypatch.setattr(monitoring_scheduler.db_layer, "get_active_listings_for_price_inference", lambda *a, **k: [])
    monkeypatch.setattr(monitoring_scheduler.db_layer, "mark_search_price_refreshed", lambda conn, search_id: marked.append(search_id))
    out = monitoring_scheduler.run_price_refresh_existing_for_search(42, payload={"run_started_at": datetime(2026, 6, 8, 12, 30).isoformat()}, dry_run=False)
    assert out["status"] == "completed"
    assert marked == [42]


def test_run_price_baseline_zero_candidates_can_advance_price_refresh(monkeypatch):
    marked = []
    class Conn:
        def close(self): pass
    monkeypatch.setattr(monitoring_scheduler, "_load_search_subscription", lambda search_id, preferred_user_area_id=None: {"SearchURL": "url"})
    monkeypatch.setattr(monitoring_scheduler, "_price_sweep_history", lambda conn, search_id: {})
    monkeypatch.setattr(monitoring_scheduler, "_default_price_sweep_mode", lambda setup, sweep_mode, history: "smart_refresh")
    monkeypatch.setattr(monitoring_scheduler.db_layer, "connect", lambda path=None: Conn())
    monkeypatch.setattr(monitoring_scheduler.db_layer, "get_active_listings_for_price_inference", lambda *a, **k: [])
    monkeypatch.setattr(monitoring_scheduler.db_layer, "mark_search_price_refreshed", lambda conn, search_id: marked.append(search_id))
    out = monitoring_scheduler.run_price_baseline_for_search(42, dry_run=False, setup=False, mark_search_complete=True)
    assert out["status"] == "completed"
    assert out["candidates_count"] == 0
    assert marked == [42]


def test_price_retry_unknowns_dedupes_same_retry_window(monkeypatch):
    now = datetime(2026, 6, 8, 12, 30, 0)
    monkeypatch.setattr(monitoring_scheduler.config, "PRICE_UNKNOWN_RETRY_INTERVAL_SECONDS", 3600)
    first = monitoring_scheduler._enqueue_price_retry_unknowns(42, ["a", "b"], run_after=now)
    job_queue.mark_job_succeeded(first["JobID"], {"status": "completed_with_unknowns"})
    second = monitoring_scheduler._enqueue_price_retry_unknowns(42, ["b", "a"], run_after=now + timedelta(minutes=10))
    assert second["created"] is False
    assert second["reason"] == "existing_price_retry_for_window"


def test_unknown_price_inference_sets_future_next_retry_at():
    import inspect
    source = inspect.getsource(db_layer.update_listing_price_inference)
    assert 'PRICE_UNKNOWN_RETRY_INTERVAL_SECONDS' in source
    assert 'status in {"unknown_pending_retry", "technical_failed"}' in source
    assert 'next_retry_at=next_retry_at' in source


def test_browser_recovery_logs_requested_and_completed(monkeypatch):
    import browser_recovery

    logs = []
    monkeypatch.setattr(
        browser_recovery,
        "recover_browser_after_429",
        lambda **kwargs: ("driver2", kwargs["rotations_used"] + 1, "/tmp/new_profile", "recovered"),
    )
    driver, rotations, profile, status = browser_recovery.recover_browser_for_untrusted_state(
        driver="driver1",
        current_profile_dir="/tmp/old_profile",
        build_driver_func=lambda profile_dir_override=None: "driver2",
        rotations_used=0,
        max_rotations=2,
        reason="chrome_error:unknown",
        job_id=123,
        search_id=42,
        log_func=logs.append,
    )
    assert status == "recovered"
    assert rotations == 1
    assert profile == "/tmp/new_profile"
    assert any("[recovery] requested" in item and "old_profile=/tmp/old_profile" in item and "job_id=123" in item and "search_id=42" in item for item in logs)
    assert any("[recovery] completed" in item and "new_profile=/tmp/new_profile" in item and "reason=chrome_error:unknown" in item for item in logs)


def _running_job(job_type=job_queue.JOB_TYPE_BASELINE_SETUP_AREA, minutes_old=600, attempts=0, max_attempts=3, search_id=2):
    old = datetime.now() - timedelta(minutes=minutes_old)
    return {
        "JobID": 1296,
        "JobType": job_type,
        "SearchID": search_id,
        "UserAreaID": 20,
        "Priority": 0,
        "Status": "running",
        "RunAfter": old,
        "AttemptCount": attempts,
        "MaxAttempts": max_attempts,
        "LockedBy": "test-stale-worker",
        "LockedAt": old,
        "StartedAt": old,
        "FinishedAt": None,
        "LastError": None,
        "PayloadJson": '{"area_id": 2, "search_url": "url"}',
        "DedupeKey": f"{job_queue.JOB_TYPE_BASELINE_SETUP_AREA}:area_id={search_id}",
        "CreatedAt": old,
        "UpdatedAt": old,
    }


def test_baseline_setup_area_stale_running_job_recovers_to_queued_and_clears_lock():
    job_queue.enable_in_memory_store([_running_job(minutes_old=10 * 60, attempts=0, max_attempts=3)])
    out = job_queue.recover_stale_running_jobs(now=datetime.now())
    stored = job_queue._TEST_STORE[0]
    assert out["recovered_count"] == 1
    assert out["failed_count"] == 0
    assert out["stale_job_ids"] == [1296]
    assert stored["Status"] == "queued"
    assert stored["LockedAt"] is None
    assert stored["LockedBy"] is None
    assert stored["StartedAt"] is None
    assert stored["FinishedAt"] is None
    assert "recovered stale running job after worker timeout" in stored["LastError"]


def test_baseline_setup_area_running_job_not_stale_remains_running():
    job_queue.enable_in_memory_store([_running_job(minutes_old=60, attempts=0, max_attempts=3)])
    out = job_queue.recover_stale_running_jobs(now=datetime.now())
    stored = job_queue._TEST_STORE[0]
    assert out["recovered_count"] == 0
    assert stored["Status"] == "running"
    assert stored["LockedBy"] == "test-stale-worker"


def test_stale_running_job_at_max_attempts_is_marked_failed():
    job_queue.enable_in_memory_store([_running_job(minutes_old=10 * 60, attempts=3, max_attempts=3)])
    out = job_queue.recover_stale_running_jobs(now=datetime.now())
    stored = job_queue._TEST_STORE[0]
    assert out["failed_count"] == 1
    assert stored["Status"] == "failed"
    assert stored["LockedAt"] is None
    assert stored["LockedBy"] is None
    assert "failed after stale running timeout and max attempts reached" in stored["LastError"]


def test_scheduler_not_ready_area_with_stale_baseline_requeues_without_duplicate(monkeypatch):
    now = datetime.now()
    stale = _running_job(minutes_old=10 * 60, attempts=0, max_attempts=3, search_id=2)
    job_queue.enable_in_memory_store([stale])
    sub = {"UserAreaID": 20, "SearchID": 2, "SearchURL": "url", "BaselineStatus": "pending", "DetailBaselineStatus": "pending", "PriceBaselineStatus": "pending"}

    class Conn:
        def cursor(self): return self
        def close(self): pass
        def execute(self, *a, **k): return self
        def fetchone(self): return [now]

    monkeypatch.setattr(monitoring_scheduler.db_layer, "connect", lambda path=None: Conn())
    monkeypatch.setattr(monitoring_scheduler.db_layer, "ensure_telegram_bot_tables", lambda conn: None)
    monkeypatch.setattr(monitoring_scheduler.db_layer, "get_active_user_area_subscriptions", lambda conn: [sub])
    monkeypatch.setattr(monitoring_scheduler.db_layer, "get_area_monitoring_state", lambda conn, area_id: {"setup_status": "preparing"})
    out = monitoring_scheduler.enqueue_due_monitoring_jobs(now=now)
    assert out["stale_recovery"]["recovered_count"] == 1
    assert out["created"] == []
    assert len(out["skipped_duplicates"]) == 1
    assert len(job_queue._TEST_STORE) == 1
    claimed = job_queue.claim_next_job("worker-recovery")
    assert claimed["JobID"] == 1296
    assert claimed["Status"] == "running"
    assert claimed["LockedBy"] == "worker-recovery"


def test_scheduler_not_ready_area_with_no_baseline_job_creates_baseline(monkeypatch):
    now = datetime.now()
    sub = {"UserAreaID": 20, "SearchID": 2, "SearchURL": "url", "BaselineStatus": "pending", "DetailBaselineStatus": "pending", "PriceBaselineStatus": "pending"}

    class Conn:
        def cursor(self): return self
        def close(self): pass
        def execute(self, *a, **k): return self
        def fetchone(self): return [now]

    monkeypatch.setattr(monitoring_scheduler.db_layer, "connect", lambda path=None: Conn())
    monkeypatch.setattr(monitoring_scheduler.db_layer, "ensure_telegram_bot_tables", lambda conn: None)
    monkeypatch.setattr(monitoring_scheduler.db_layer, "get_active_user_area_subscriptions", lambda conn: [sub])
    monkeypatch.setattr(monitoring_scheduler.db_layer, "get_area_monitoring_state", lambda conn, area_id: {"setup_status": "preparing"})
    out = monitoring_scheduler.enqueue_due_monitoring_jobs(now=now)
    assert len(out["created"]) == 1
    assert out["created"][0]["JobType"] == job_queue.JOB_TYPE_BASELINE_SETUP_AREA
    assert out["created"][0]["DedupeKey"] == "baseline_setup_area:area_id=2"


def test_startup_schema_recovers_stale_jobs(monkeypatch):
    import telegram_bot

    called = []
    class Conn:
        def close(self): pass

    monkeypatch.setattr(telegram_bot, "_connect", lambda: Conn())
    monkeypatch.setattr(telegram_bot.db_layer, "ensure_telegram_bot_tables", lambda conn: None)
    monkeypatch.setattr(telegram_bot.job_queue, "ensure_job_tables", lambda conn=None: None)
    monkeypatch.setattr(telegram_bot.job_queue, "recover_stale_running_jobs", lambda conn=None: called.append(conn) or {"recovered_count": 1, "failed_count": 0, "stale_job_ids": [1296], "recovered_job_types": [job_queue.JOB_TYPE_BASELINE_SETUP_AREA]})
    telegram_bot.ensure_runtime_schema()
    assert len(called) == 1


def test_scheduler_summary_includes_stale_recovery_diagnostics():
    import telegram_bot

    summary = telegram_bot._summarize_scheduler_result({
        "created": [],
        "skipped_duplicates": [{}],
        "ready_search_ids_considered": [],
        "not_ready_search_ids_considered": [2],
        "not_due": [],
        "errors": [],
        "stale_recovery": {"recovered_count": 1, "failed_count": 0, "stale_job_ids": [1296], "recovered_job_types": [job_queue.JOB_TYPE_BASELINE_SETUP_AREA]},
    })
    assert summary["stale_running_recovered"] == 1
    assert summary["stale_running_failed"] == 0
    assert summary["stale_job_ids"] == [1296]
    assert summary["recovered_job_types"] == [job_queue.JOB_TYPE_BASELINE_SETUP_AREA]
    assert summary["not_ready_searches"] == 1
    assert summary["blocked_by_active_duplicate"] == 1


def test_run_next_job_once_does_not_leave_job_running_on_handled_exception(monkeypatch):
    job_queue.enable_in_memory_store()
    job_queue.enqueue_job(job_queue.JOB_TYPE_LIGHT_CHECK_NEW_LISTINGS, search_id=1, max_attempts=1)

    def fail(_job, send_telegram=True):
        raise RuntimeError("deterministic parser failure")

    monkeypatch.setattr(monitoring_scheduler, "execute_job", fail)
    monkeypatch.setattr(monitoring_scheduler, "is_retryable_navigation_error", lambda exc: False)
    out = monitoring_scheduler.run_next_job_once(worker_id="worker-a", send_telegram=False)
    stored = job_queue._TEST_STORE[0]
    assert out["status"] == "failed"
    assert stored["Status"] == "failed"
    assert stored["LockedAt"] is None
    assert stored["LockedBy"] is None
    assert stored["Status"] != "running"
