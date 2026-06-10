import asyncio
import sys
import types
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

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
        def commit(self): pass
        def close(self): pass

    monkeypatch.setattr(telegram_bot, "_connect", lambda: Conn())
    monkeypatch.setattr(telegram_bot.db_layer, "ensure_telegram_bot_tables", lambda conn: None)
    monkeypatch.setattr(telegram_bot.db_layer, "sanitize_notification_outbox", lambda conn: {"notifications_skipped_by_revalidation": 0})
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



def test_production_area_setup_status_writes_use_supported_contract():
    import re

    unsupported = {"completed", "retry_wait", "failed_ingest"}
    offenders = []
    for path in ["monitor.py", "monitoring_scheduler.py", "db_layer.py", "telegram_bot.py"]:
        text = Path(path).read_text(encoding="utf-8")
        for match in re.finditer(r"setup_status\s*=\s*['\"]([^'\"]+)['\"]", text):
            if match.group(1) in unsupported:
                offenders.append(f"{path}:{match.start()}:{match.group(1)}")
    assert offenders == []


def test_baseline_true_no_results_marks_area_ready_and_activates(monkeypatch):
    import monitor

    calls = []
    class Conn:
        def commit(self): calls.append(("commit",))
        def close(self): calls.append(("close",))

    monkeypatch.setattr(monitor, "init_db", lambda path: None)
    monkeypatch.setattr(monitor, "connect", lambda path: Conn())
    monkeypatch.setattr(monitor, "get_or_create_area", lambda conn, url: 42)
    monkeypatch.setattr(monitor, "upsert_area_monitoring_state", lambda conn, area_id, **kwargs: calls.append(("state", area_id, kwargs)))
    monkeypatch.setattr(monitor, "activate_area_subscriptions", lambda conn, area_id: calls.append(("activate", area_id)))
    monkeypatch.setattr(monitor.module1_list_scraper, "scrape_search", lambda *a, **k: [])
    monitor.module1_list_scraper.scrape_search.last_result = {"status": "no_results", "stop_reason": "no_results"}

    out = monitor.baseline_setup_area("https://example.test/buy/in-empty,+nsw+2999/list-1")

    assert out["status"] == "ready"
    assert out["active_listing_count"] == 0
    state_calls = [item for item in calls if item[0] == "state"]
    assert state_calls[-1][2]["setup_status"] == "ready"
    assert state_calls[-1][2]["active_listing_count"] == 0
    assert state_calls[-1][2]["inferred_price_count"] == 0
    assert state_calls[-1][2]["unknown_price_count"] == 0
    assert ("activate", 42) in calls


def test_baseline_untrusted_zero_rows_fails_without_ready(monkeypatch):
    import monitor

    calls = []
    class Conn:
        def commit(self): pass
        def close(self): pass

    monkeypatch.setattr(monitor, "init_db", lambda path: None)
    monkeypatch.setattr(monitor, "connect", lambda path: Conn())
    monkeypatch.setattr(monitor, "get_or_create_area", lambda conn, url: 42)
    monkeypatch.setattr(monitor, "upsert_area_monitoring_state", lambda conn, area_id, **kwargs: calls.append(kwargs))
    monkeypatch.setattr(monitor, "activate_area_subscriptions", lambda conn, area_id: (_ for _ in ()).throw(AssertionError("should not activate")))
    monkeypatch.setattr(monitor.module1_list_scraper, "scrape_search", lambda *a, **k: [])
    monitor.module1_list_scraper.scrape_search.last_result = {"status": "render_timeout", "page_state": "render_timeout", "stop_reason": "render_timeout"}

    try:
        monitor.baseline_setup_area("https://example.test/buy/in-empty,+nsw+2999/list-1")
    except RuntimeError as exc:
        assert "Module1 returned 0 rows" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")

    assert calls[-1]["setup_status"] == "failed"
    assert calls[-1]["module1_status"] == "failed"


def test_retryable_baseline_error_uses_supported_preparing_area_state(monkeypatch):
    import monitoring_scheduler

    written = []
    class Conn:
        def commit(self): pass
        def close(self): pass

    job = {"JobID": 10, "JobType": job_queue.JOB_TYPE_BASELINE_SETUP_AREA, "SearchID": 42, "UserAreaID": 7, "PayloadJson": '{"search_url": "url"}'}
    monkeypatch.setattr(monitoring_scheduler, "_search_is_active_for_monitoring", lambda search_id: True)
    monkeypatch.setattr(monitoring_scheduler.job_queue if hasattr(monitoring_scheduler, "job_queue") else job_queue, "touch_job_heartbeat", lambda job_id: {})
    monkeypatch.setattr(monitoring_scheduler, "baseline_setup_area", lambda *a, **k: (_ for _ in ()).throw(RealEstateBlockedError("blocked", retry_after_seconds=60)))
    monkeypatch.setattr(monitoring_scheduler.db_layer, "connect", lambda path=None: Conn())
    monkeypatch.setattr(monitoring_scheduler.db_layer, "upsert_area_monitoring_state", lambda conn, area_id, **kwargs: written.append(kwargs))

    out = monitoring_scheduler.execute_job(job, send_telegram=False)

    assert out["status"] == "retry_wait"
    assert written[-1]["setup_status"] == "preparing"
    assert written[-1]["module1_status"] == "retry_wait"


def test_ingest_failure_uses_failed_setup_status_and_preserves_reason(monkeypatch, tmp_path):
    import json
    import monitor

    calls = []
    class Conn:
        def commit(self): pass
        def close(self): pass

    rows1 = [{"listing_id": "1", "url": "https://example.test/property-1", "price": "$1", "address": "A"}]
    rows3 = [{"listing_id": "1", "url": "https://example.test/property-1", "price": "$1", "address": "A", "detail_scraped_at": "now"}]
    json1 = tmp_path / "m1.json"
    json3 = tmp_path / "m3.json"
    json1.write_text(json.dumps(rows1), encoding="utf-8")
    json3.write_text(json.dumps(rows3), encoding="utf-8")

    monkeypatch.setattr(monitor, "init_db", lambda path: None)
    monkeypatch.setattr(monitor, "connect", lambda path: Conn())
    monkeypatch.setattr(monitor, "get_or_create_area", lambda conn, url: 42)
    monkeypatch.setattr(monitor, "upsert_area_monitoring_state", lambda conn, area_id, **kwargs: calls.append(kwargs))
    monkeypatch.setattr(monitor.module1_list_scraper, "scrape_search", lambda *a, **k: rows1)
    monkeypatch.setattr(monitor.module1_list_scraper, "save_results", lambda rows, out_dir=None: (str(tmp_path / "m1.csv"), str(json1)))
    monkeypatch.setattr(monitor.module3_enrich_details, "module3_run", lambda *a, **k: (str(tmp_path / "m3.csv"), str(json3)))
    monitor.module3_enrich_details.module3_run.last_result = {"status": "completed", "success_count": 1}
    monkeypatch.setattr(monitor.module2_infer_prices, "module2_run", lambda *a, **k: (str(tmp_path / "m2.csv"), str(json3)))
    monitor.module2_infer_prices.module2_run.last_result = {"status": "completed"}
    monkeypatch.setattr(monitor, "ingest_full_rows", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("ingest boom")))

    try:
        monitor.baseline_setup_area("https://example.test/buy/in-area,+nsw+2999/list-1")
    except RuntimeError as exc:
        assert "ingest boom" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")

    assert calls[-1]["setup_status"] == "failed"
    assert calls[-1]["last_error"].startswith("failed_ingest:")


def test_retry_setup_area_resets_state_and_dedupes_baseline_job(monkeypatch):
    job_queue.enable_in_memory_store()
    executed = []

    class Cursor:
        def __init__(self): self.rowcount = 1
        def execute(self, sql, *params):
            executed.append((str(sql), params))
            return self
        def fetchone(self): return (7, 99, 42, "https://example.test/search", "Retryville, NSW 2999")
        def fetchall(self): return []
    class Conn:
        def cursor(self): return Cursor()
        def commit(self): executed.append(("commit", ()))
        def rollback(self): pass

    monkeypatch.setattr(db_layer, "ensure_telegram_bot_tables", lambda conn: None)
    monkeypatch.setattr(db_layer, "ensure_monitoring_state_tables", lambda conn: None)
    monkeypatch.setattr(db_layer, "get_area_monitoring_state", lambda conn, area_id: {"setup_status": "failed"})
    monkeypatch.setattr(db_layer, "upsert_area_monitoring_state", lambda conn, area_id, **kwargs: executed.append(("state", (area_id, kwargs))))
    monkeypatch.setattr(db_layer, "upsert_user_area_subscription_state", lambda conn, user_id, area_id, **kwargs: executed.append(("substate", (user_id, area_id, kwargs))))

    first = db_layer.retry_setup_area(Conn(), user_area_id=7)
    second = db_layer.retry_setup_area(Conn(), user_area_id=7)

    assert first["created"] is True
    assert second["created"] is False
    assert second["reason"] == "baseline_job_already_active"
    assert len(job_queue._TEST_STORE) == 1
    state_call = next(item for item in executed if item[0] == "state")
    assert state_call[1][1]["setup_status"] == "preparing"


def test_failed_setup_status_label_and_keyboard_offer_retry_action(monkeypatch):
    import telegram_bot

    class FakeButton:
        def __init__(self, text, callback_data=None):
            self.text = text
            self.callback_data = callback_data

    class FakeMarkup:
        def __init__(self, rows):
            self.inline_keyboard = rows

    monkeypatch.setattr(telegram_bot, "InlineKeyboardButton", FakeButton)
    monkeypatch.setattr(telegram_bot, "InlineKeyboardMarkup", FakeMarkup)
    sub = {"UserAreaID": 7, "AreaLabel": "Retryville, NSW 2999", "AreaSetupStatus": "failed", "BaselineStatus": "pending", "DetailBaselineStatus": "pending", "PriceBaselineStatus": "pending"}
    assert telegram_bot._status_label(sub) == "Failed — tap Retry setup"
    keyboard = telegram_bot._my_suburbs_keyboard([sub])
    buttons = [button for row in keyboard.inline_keyboard for button in row]
    assert any(button.callback_data == "retry_setup:7" for button in buttons)
    assert any("Retry setup" in button.text for button in buttons)


class _NotificationFakeBot:
    def __init__(self, exc=None):
        self.exc = exc
        self.sent = []

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        if self.exc:
            raise self.exc


def test_sender_skips_removed_area_notification_before_send(monkeypatch):
    import telegram_sender

    calls = []
    monkeypatch.setattr(telegram_sender.db_layer, "recover_stale_sending_notifications", lambda conn: {})
    monkeypatch.setattr(telegram_sender.db_layer, "get_queued_notifications", lambda conn, limit, channel: [{"NotificationID": 1, "EventID": 10, "ChatID": "100", "MessageText": "hi"}])
    monkeypatch.setattr(telegram_sender.db_layer, "cancel_notification_if_unsafe", lambda conn, nid, reason_prefix="send_time_revalidation": {"valid": False, "reason": "subscription_inactive_or_missing", "cancelled": True})
    monkeypatch.setattr(telegram_sender.db_layer, "mark_notification_sending", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not send")))
    class Conn:
        def commit(self): calls.append("commit")
    bot = _NotificationFakeBot()

    out = asyncio.run(telegram_sender.send_queued_notifications(bot, conn=Conn()))

    assert out["skipped"] == 1
    assert out["sent"] == 0
    assert bot.sent == []


def test_sender_skips_disabled_notification_before_send(monkeypatch):
    import telegram_sender

    monkeypatch.setattr(telegram_sender.db_layer, "recover_stale_sending_notifications", lambda conn: {})
    monkeypatch.setattr(telegram_sender.db_layer, "get_queued_notifications", lambda conn, limit, channel: [{"NotificationID": 2, "EventID": 11, "ChatID": "100", "MessageText": "hi"}])
    monkeypatch.setattr(telegram_sender.db_layer, "cancel_notification_if_unsafe", lambda conn, nid, reason_prefix="send_time_revalidation": {"valid": False, "reason": "notify_disabled", "cancelled": True})
    class Conn:
        def commit(self): pass
    bot = _NotificationFakeBot()

    out = asyncio.run(telegram_sender.send_queued_notifications(bot, conn=Conn()))

    assert out["skipped"] == 1
    assert bot.sent == []


def test_send_time_validation_rejects_area_not_ready(monkeypatch):
    row = {
        "NotificationID": 3,
        "Status": "queued",
        "ExistingEventID": 12,
        "ShouldNotify": 1,
        "EventType": "price_changed",
        "Reason": "price_changed",
        "UserAreaID": 7,
        "SubscriptionIsActive": 1,
        "TelegramUserIsActive": 1,
        "EffectiveNotifyEnabled": 1,
        "EffectiveSubscriptionStatus": "active",
        "AreaSetupStatus": "preparing",
        "NotificationReadyAt": datetime(2026, 6, 10, 9, 0, 0),
        "EventCreatedAt": datetime(2026, 6, 10, 9, 5, 0),
    }
    class Cursor:
        def execute(self, *a): return self
    class Conn:
        def cursor(self): return Cursor()
    monkeypatch.setattr(db_layer, "ensure_notification_tables", lambda conn: None)
    monkeypatch.setattr(db_layer, "_rows_to_dicts", lambda cur: [row])

    out = db_layer.validate_notification_for_send(Conn(), 3)

    assert out["valid"] is False
    assert out["reason"] == "area_not_ready"


def test_send_time_validation_rejects_event_before_notification_ready(monkeypatch):
    row = {
        "NotificationID": 4,
        "Status": "queued",
        "ExistingEventID": 13,
        "ShouldNotify": 1,
        "EventType": "price_changed",
        "Reason": "price_changed",
        "UserAreaID": 7,
        "SubscriptionIsActive": 1,
        "TelegramUserIsActive": 1,
        "EffectiveNotifyEnabled": 1,
        "EffectiveSubscriptionStatus": "active",
        "AreaSetupStatus": "ready",
        "NotificationReadyAt": datetime(2026, 6, 10, 9, 0, 0),
        "EventCreatedAt": datetime(2026, 6, 10, 8, 59, 0),
    }
    class Cursor:
        def execute(self, *a): return self
    class Conn:
        def cursor(self): return Cursor()
    monkeypatch.setattr(db_layer, "ensure_notification_tables", lambda conn: None)
    monkeypatch.setattr(db_layer, "_rows_to_dicts", lambda cur: [row])

    out = db_layer.validate_notification_for_send(Conn(), 4)

    assert out["valid"] is False
    assert out["reason"] == "event_before_notification_ready"


def test_send_time_validation_rejects_should_notify_false_false_sold(monkeypatch):
    row = {"NotificationID": 5, "Status": "queued", "ExistingEventID": 14, "ShouldNotify": 0, "EventType": "sold", "Reason": "weak_sold_evidence"}
    class Cursor:
        def execute(self, *a): return self
    class Conn:
        def cursor(self): return Cursor()
    monkeypatch.setattr(db_layer, "ensure_notification_tables", lambda conn: None)
    monkeypatch.setattr(db_layer, "_rows_to_dicts", lambda cur: [row])

    out = db_layer.validate_notification_for_send(Conn(), 5)

    assert out["valid"] is False
    assert out["reason"] == "event_should_notify_false"


def test_recover_stale_sending_notifications_requeues_or_fails(monkeypatch):
    updates = []
    class Cursor:
        def execute(self, sql, *params):
            self.sql = str(sql)
            updates.append((self.sql, params))
            return self
        def fetchall(self):
            if "SELECT NotificationID" in self.sql:
                return [(1, 1), (2, 5)]
            return []
    class Conn:
        def cursor(self): return Cursor()
    monkeypatch.setattr(db_layer, "ensure_notification_tables", lambda conn: None)

    out = db_layer.recover_stale_sending_notifications(Conn(), stale_minutes=30, max_attempts=5)

    assert out["stale_sending_recovered"] == 1
    assert out["stale_sending_failed"] == 1
    assert out["recovered_notification_ids"] == [1]
    assert out["failed_notification_ids"] == [2]
    assert any("Status='queued'" in sql for sql, _ in updates)
    assert any("Status='failed'" in sql for sql, _ in updates)


def test_recover_fresh_sending_notifications_does_nothing(monkeypatch):
    class Cursor:
        def execute(self, sql, *params):
            self.sql = str(sql)
            return self
        def fetchall(self): return []
    class Conn:
        def cursor(self): return Cursor()
    monkeypatch.setattr(db_layer, "ensure_notification_tables", lambda conn: None)

    out = db_layer.recover_stale_sending_notifications(Conn(), stale_minutes=30, max_attempts=5)

    assert out["stale_sending_recovered"] == 0
    assert out["stale_sending_failed"] == 0


def test_startup_sanitizer_cancels_old_false_sold_queued_notification(monkeypatch):
    calls = []
    class Cursor:
        def execute(self, sql, *params):
            self.sql = str(sql)
            return self
        def fetchall(self): return [(9,)]
    class Conn:
        def cursor(self): return Cursor()
    monkeypatch.setattr(db_layer, "ensure_notification_tables", lambda conn: None)
    monkeypatch.setattr(db_layer, "recover_stale_sending_notifications", lambda conn: {"stale_sending_recovered": 0, "stale_sending_failed": 0})
    monkeypatch.setattr(db_layer, "cancel_notification_if_unsafe", lambda conn, nid, reason_prefix="startup_outbox_sanitizer": calls.append((nid, reason_prefix)) or {"valid": False, "reason": "event_should_notify_false", "cancelled": True})

    out = db_layer.sanitize_notification_outbox(Conn(), limit=10)

    assert out["notifications_skipped_by_revalidation"] == 1
    assert calls == [(9, "startup_outbox_sanitizer")]


def test_cancel_notifications_for_subscription_scopes_to_one_user_shared_area(monkeypatch):
    executed = []
    class Cursor:
        rowcount = 1
        def execute(self, sql, *params):
            executed.append((str(sql), params))
            return self
        def fetchone(self): return None
    class Conn:
        def cursor(self): return Cursor()
    monkeypatch.setattr(db_layer, "ensure_notification_tables", lambda conn: None)

    count = db_layer.cancel_notifications_for_subscription(Conn(), telegram_user_id=101, search_id=7, reason="removed")

    assert count == 1
    sql, params = executed[-1]
    assert "SearchID=?" in sql
    assert "UserID=?" in sql
    assert "ChatID IN" in sql
    assert params[-3:] == (7, 101, 101)


def test_telegram_transient_failure_requeues_with_backoff(monkeypatch):
    import telegram_sender

    calls = []
    monkeypatch.setattr(telegram_sender.db_layer, "recover_stale_sending_notifications", lambda conn: {})
    monkeypatch.setattr(telegram_sender.db_layer, "get_queued_notifications", lambda conn, limit, channel: [{"NotificationID": 20, "EventID": 30, "ChatID": "100", "MessageText": "hi", "AttemptCount": 0}])
    monkeypatch.setattr(telegram_sender.db_layer, "cancel_notification_if_unsafe", lambda conn, nid, reason_prefix="send_time_revalidation": {"valid": True, "reason": "ok"})
    monkeypatch.setattr(telegram_sender.db_layer, "mark_notification_sending", lambda conn, nid: calls.append(("sending", nid)))
    monkeypatch.setattr(telegram_sender.db_layer, "mark_notification_send_error", lambda conn, nid, error, max_attempts, backoff_seconds: calls.append(("retry", nid, backoff_seconds)) or {"status": "queued", "attempts": 1, "backoff_seconds": backoff_seconds})
    class Conn:
        def commit(self): calls.append(("commit",))
    bot = _NotificationFakeBot(exc=TimeoutError("temporary network timeout"))

    out = asyncio.run(telegram_sender.send_queued_notifications(bot, conn=Conn()))

    assert out["retried"] == 1
    assert out["failed"] == 0
    assert any(call[0] == "retry" for call in calls)
