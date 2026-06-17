import asyncio
import json
import sys
import types
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

sys.modules.setdefault("pyodbc", types.SimpleNamespace(connect=lambda *args, **kwargs: None))

import pytest

import area_light_checker
import db_layer
import job_queue
import module1_list_scraper
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
    monkeypatch.setattr(monitoring_scheduler.db_layer, "ensure_runtime_monitoring_schema", lambda conn: {"schema_ok": True})
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
    monkeypatch.setattr(monitoring_scheduler.config, "ENABLE_OPERATIONAL_PRICE_MONITORING", True, raising=False)
    monkeypatch.setattr(monitoring_scheduler.db_layer, "connect", lambda path=None: SchedulerConn())
    monkeypatch.setattr(monitoring_scheduler.db_layer, "ensure_runtime_monitoring_schema", lambda conn: {"schema_ok": True})
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


def test_operational_price_disabled_does_not_enqueue_price_jobs(monkeypatch):
    now = datetime(2026, 6, 8, 12, 30, 0)
    sub = _ready_price_subscription(now)
    sub["LastPriceRefreshAt"] = None
    monkeypatch.setattr(monitoring_scheduler.config, "SCHEDULE_TIMEZONE", "UTC")
    monkeypatch.setattr(monitoring_scheduler.config, "PRICE_REFRESH_TIMES", "12:00")
    monkeypatch.setattr(monitoring_scheduler.config, "PRICE_INFERENCE_ENABLED", True)
    monkeypatch.setattr(monitoring_scheduler.config, "ENABLE_OPERATIONAL_PRICE_MONITORING", False, raising=False)
    monkeypatch.setattr(monitoring_scheduler.db_layer, "connect", lambda path=None: SchedulerConn())
    monkeypatch.setattr(monitoring_scheduler.db_layer, "ensure_runtime_monitoring_schema", lambda conn: {"schema_ok": True})
    monkeypatch.setattr(monitoring_scheduler.db_layer, "ensure_telegram_bot_tables", lambda conn: None)
    monkeypatch.setattr(monitoring_scheduler.db_layer, "get_active_user_area_subscriptions", lambda conn: [sub])
    monkeypatch.setattr(monitoring_scheduler.db_layer, "get_due_price_retry_listing_ids", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not query retry ids when disabled")))

    out = monitoring_scheduler.enqueue_due_monitoring_jobs(now=now)

    price_types = {
        job_queue.JOB_TYPE_PRICE_RETRY_UNKNOWNS,
        job_queue.JOB_TYPE_PRICE_REFRESH_EXISTING,
        job_queue.JOB_TYPE_MODULE2_PRICE_REFRESH_AREA,
    }
    assert not [job for job in out["created"] if job.get("JobType") in price_types]
    assert any(row.get("reason") == "operational_price_monitoring_disabled" for row in out["not_due"])


def test_setup_price_baseline_still_runs_when_operational_price_disabled(monkeypatch):
    calls = []
    monkeypatch.setattr(monitoring_scheduler.config, "PRICE_INFERENCE_ENABLED", True)
    monkeypatch.setattr(monitoring_scheduler.config, "ENABLE_OPERATIONAL_PRICE_MONITORING", False, raising=False)
    monkeypatch.setattr(monitoring_scheduler, "_load_search_subscription", lambda search_id, preferred_user_area_id=None: {"SearchURL": "url"})
    monkeypatch.setattr(monitoring_scheduler, "_price_sweep_history", lambda conn, search_id: {})
    monkeypatch.setattr(monitoring_scheduler, "_default_price_sweep_mode", lambda setup, sweep_mode, history: "setup_full_sweep")

    class Conn:
        def close(self): pass
        def commit(self): pass

    monkeypatch.setattr(monitoring_scheduler.db_layer, "connect", lambda path=None: Conn())
    monkeypatch.setattr(monitoring_scheduler.db_layer, "get_active_listings_for_price_inference", lambda *a, **k: calls.append(("targets", k)) or [])
    out = monitoring_scheduler.run_price_baseline_for_search(42, setup=True, dry_run=False, mark_search_complete=False)

    assert out["status"] == "completed_no_targets"
    assert calls


def test_execute_existing_price_job_skips_when_operational_price_disabled(monkeypatch):
    monkeypatch.setattr(monitoring_scheduler.config, "ENABLE_OPERATIONAL_PRICE_MONITORING", False, raising=False)
    monkeypatch.setattr(monitoring_scheduler, "_search_is_active_for_monitoring", lambda search_id: True)
    monkeypatch.setattr(monitoring_scheduler, "_search_ready_for_operational_monitoring", lambda search_id: True)
    monkeypatch.setattr(monitoring_scheduler, "_search_ready_for_notification_dispatch", lambda search_id: True)

    out = monitoring_scheduler.execute_job({
        "JobID": 0,
        "JobType": job_queue.JOB_TYPE_MODULE2_PRICE_REFRESH_AREA,
        "SearchID": 42,
    })

    assert out["status"] == "skipped_operational_price_disabled"


def test_zero_candidate_price_refresh_marks_search_refreshed(monkeypatch):
    marked = []
    class Conn:
        def close(self): pass
    monkeypatch.setattr(monitoring_scheduler, "_search_is_active_for_monitoring", lambda search_id: True)
    monkeypatch.setattr(monitoring_scheduler.config, "ENABLE_OPERATIONAL_PRICE_MONITORING", True, raising=False)
    monkeypatch.setattr(monitoring_scheduler, "run_price_baseline_for_search", lambda *a, **k: {"status": "completed", "processed_count": 0})
    monkeypatch.setattr(monitoring_scheduler.db_layer, "connect", lambda path=None: Conn())
    monkeypatch.setattr(monitoring_scheduler.db_layer, "get_active_listings_for_price_inference", lambda *a, **k: [])
    monkeypatch.setattr(monitoring_scheduler.db_layer, "mark_search_price_refreshed", lambda conn, search_id: marked.append(search_id))
    out = monitoring_scheduler.run_price_refresh_existing_for_search(42, payload={"run_started_at": datetime(2026, 6, 8, 12, 30).isoformat()}, dry_run=False)
    assert out["status"] == "completed_no_targets"
    assert out["enqueued_next_batch"] is None
    assert marked == [42]


def test_run_price_baseline_zero_candidates_can_advance_price_refresh(monkeypatch):
    marked = []
    class Conn:
        def close(self): pass
    monkeypatch.setattr(monitoring_scheduler.config, "ENABLE_OPERATIONAL_PRICE_MONITORING", True, raising=False)
    monkeypatch.setattr(monitoring_scheduler, "_load_search_subscription", lambda search_id, preferred_user_area_id=None: {"SearchURL": "url"})
    monkeypatch.setattr(monitoring_scheduler, "_price_sweep_history", lambda conn, search_id: {})
    monkeypatch.setattr(monitoring_scheduler, "_default_price_sweep_mode", lambda setup, sweep_mode, history: "smart_refresh")
    monkeypatch.setattr(monitoring_scheduler.db_layer, "connect", lambda path=None: Conn())
    monkeypatch.setattr(monitoring_scheduler.db_layer, "get_active_listings_for_price_inference", lambda *a, **k: [])
    monkeypatch.setattr(monitoring_scheduler.db_layer, "mark_search_price_refreshed", lambda conn, search_id: marked.append(search_id))
    out = monitoring_scheduler.run_price_baseline_for_search(42, dry_run=False, setup=False, mark_search_complete=True)
    assert out["status"] == "completed_no_targets"
    assert out["candidates_count"] == 0
    assert marked == [42]


def test_price_retry_unknowns_marks_direct_price_rows_skipped(monkeypatch):
    updates = []

    class Conn:
        def close(self): pass
        def commit(self): pass

    candidate = {
        "db_listing_id": 99,
        "listing_id": "abc",
        "external_id": "abc",
        "price_display": "$700,000",
        "inferred_price_low": None,
        "inferred_price_high": None,
        "price_inference_status": "unknown_pending_retry",
    }
    monkeypatch.setattr(monitoring_scheduler, "_search_is_active_for_monitoring", lambda search_id: True)
    monkeypatch.setattr(monitoring_scheduler.config, "ENABLE_OPERATIONAL_PRICE_MONITORING", True, raising=False)
    monkeypatch.setattr(monitoring_scheduler, "_load_search_subscription", lambda search_id, preferred_user_area_id=None: {"SearchID": search_id, "SearchURL": "url"})
    monkeypatch.setattr(monitoring_scheduler, "_price_sweep_history", lambda conn, search_id: {})
    monkeypatch.setattr(monitoring_scheduler.db_layer, "connect", lambda path=None: Conn())
    monkeypatch.setattr(monitoring_scheduler.db_layer, "get_active_listings_for_price_inference", lambda *a, **k: [candidate])
    monkeypatch.setattr(monitoring_scheduler.db_layer, "parse_price_bounds_from_text", lambda text: (700000, 700000))
    monkeypatch.setattr(monitoring_scheduler.db_layer, "update_listing_price_inference", lambda conn, search_id, listing_id, low, high, method, source, status, **kwargs: updates.append((listing_id, low, high, status)))

    out = monitoring_scheduler.run_price_retry_unknowns_for_search(42, payload={"listing_external_ids": ["abc"]}, dry_run=False)

    assert out["status"] == "skipped_no_price_targets"
    assert out["price_retry"]["status"] == "skipped_no_price_targets"
    assert out["next_retry_job"] is None
    assert updates == [(99, 700000, 700000, "skipped_direct_price")]


def test_price_retry_unknowns_no_targets_cleans_or_postpones_retry_state(monkeypatch):
    cleanup_calls = []

    class Conn:
        def close(self): pass
        def commit(self): pass

    monkeypatch.setattr(monitoring_scheduler, "_search_is_active_for_monitoring", lambda search_id: True)
    monkeypatch.setattr(monitoring_scheduler.config, "ENABLE_OPERATIONAL_PRICE_MONITORING", True, raising=False)
    monkeypatch.setattr(monitoring_scheduler, "_load_search_subscription", lambda search_id, preferred_user_area_id=None: {"SearchID": search_id, "SearchURL": "url"})
    monkeypatch.setattr(monitoring_scheduler, "_price_sweep_history", lambda conn, search_id: {})
    monkeypatch.setattr(monitoring_scheduler.db_layer, "connect", lambda path=None: Conn())
    monkeypatch.setattr(monitoring_scheduler.db_layer, "get_active_listings_for_price_inference", lambda *a, **k: [])
    monkeypatch.setattr(
        monitoring_scheduler.db_layer,
        "cleanup_price_retry_no_target_ids",
        lambda conn, search_id, ids, next_retry_at=None: cleanup_calls.append((search_id, list(ids), next_retry_at)) or {"postponed_not_due": list(ids)},
    )

    out = monitoring_scheduler.run_price_retry_unknowns_for_search(42, payload={"listing_external_ids": ["204091840"]}, dry_run=False)

    assert out["status"] == "completed_no_targets"
    assert out["price_retry"]["status"] == "completed_no_targets"
    assert out["next_retry_job"] is None
    assert cleanup_calls and cleanup_calls[0][1] == ["204091840"]
    assert cleanup_calls[0][2] > datetime.now()


def test_noop_detail_refresh_marks_cooldown(monkeypatch):
    marked = []

    class Conn:
        def close(self): pass

    monkeypatch.setattr(monitoring_scheduler, "_search_is_active_for_monitoring", lambda search_id: True)
    monkeypatch.setattr(monitoring_scheduler, "_load_search_subscription", lambda search_id, preferred_user_area_id=None: {"SearchID": search_id, "SearchURL": "url"})
    monkeypatch.setattr(monitoring_scheduler.db_layer, "connect", lambda path=None: Conn())
    monkeypatch.setattr(monitoring_scheduler.db_layer, "get_detail_refresh_candidate_debug_counts", lambda conn, search_id: {"total_state_rows": 5, "active_state_rows": 5, "valid_url_rows": 5})
    monkeypatch.setattr(monitoring_scheduler, "refresh_active_listings", lambda *a, **k: {"candidates_count": 0, "processed_count": 0, "refreshed_count": 0, "failed_count": 0, "errors": []})
    monkeypatch.setattr(monitoring_scheduler.db_layer, "mark_search_detail_refreshed", lambda conn, search_id: marked.append(search_id))

    out = monitoring_scheduler.run_detail_refresh_existing_for_search(42, dry_run=False, send_telegram=False)

    assert out["status"] == "completed"
    assert out["detail_refresh"]["processed_count"] == 0
    assert marked == [42]


def test_light_check_new_listings_enqueues_notification_dispatch(monkeypatch):
    now = datetime(2026, 6, 14, 11, 50, 0)

    class Conn:
        def close(self): pass

    monkeypatch.setattr(monitoring_scheduler, "_search_is_active_for_monitoring", lambda search_id: True)
    monkeypatch.setattr(monitoring_scheduler, "_search_ready_for_operational_monitoring", lambda search_id: True)
    monkeypatch.setattr(monitoring_scheduler, "_search_ready_for_notification_dispatch", lambda search_id: True)
    monkeypatch.setattr(monitoring_scheduler, "_load_search_subscription", lambda search_id, preferred_user_area_id=None: {"UserAreaID": 7, "SearchURL": "url"})
    monkeypatch.setattr(monitoring_scheduler, "light_check_area", lambda *a, **k: {"scan_status": "ok", "new_listings": [{"listing_id": "204091840"}]})
    monkeypatch.setattr(monitoring_scheduler.db_layer, "connect", lambda path=None: Conn())
    monkeypatch.setattr(monitoring_scheduler.db_layer, "mark_search_light_checked", lambda conn, search_id: None)

    result = monitoring_scheduler.execute_job({
        "JobID": 1,
        "JobType": job_queue.JOB_TYPE_LIGHT_CHECK_NEW_LISTINGS,
        "SearchID": 42,
        "UserAreaID": 7,
    })

    assert result["notification_dispatch_job"]["JobType"] == job_queue.JOB_TYPE_NOTIFICATION_DISPATCH
    assert result["new_listing_jobs"][0]["JobType"] == job_queue.JOB_TYPE_PROCESS_NEW_LISTING


def test_new_listing_dispatch_does_not_depend_on_process_new_listing_success(monkeypatch):
    monkeypatch.setattr(monitoring_scheduler, "_search_is_active_for_monitoring", lambda search_id: True)
    monkeypatch.setattr(monitoring_scheduler, "_search_ready_for_operational_monitoring", lambda search_id: True)
    monkeypatch.setattr(monitoring_scheduler, "_search_ready_for_notification_dispatch", lambda search_id: True)
    dispatch = monitoring_scheduler._enqueue_notification_dispatch_for_search(42, user_area_id=7, reason="light_check_new_listings")
    with pytest.raises(RuntimeError):
        with monkeypatch.context() as m:
            m.setattr(monitoring_scheduler, "run_process_new_listing_for_search", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("detail failed")))
            monitoring_scheduler.execute_job({
                "JobID": 2,
                "JobType": job_queue.JOB_TYPE_PROCESS_NEW_LISTING,
                "SearchID": 42,
                "UserAreaID": 7,
                "PayloadJson": json.dumps({"listing_ids": ["204091840"]}),
            })
    active_dispatch = [
        row for row in job_queue.get_active_jobs()
        if row.get("JobType") == job_queue.JOB_TYPE_NOTIFICATION_DISPATCH and int(row.get("SearchID") or 0) == 42
    ]
    assert dispatch and active_dispatch
    assert active_dispatch[0]["JobID"] == dispatch["JobID"]


def test_notification_dispatch_queues_telegram_send_when_outbox_created(monkeypatch):
    monkeypatch.setattr(monitoring_scheduler, "_search_is_active_for_monitoring", lambda search_id: True)
    monkeypatch.setattr(monitoring_scheduler, "_search_ready_for_operational_monitoring", lambda search_id: True)
    monkeypatch.setattr(monitoring_scheduler, "_queue_notifications_for_search", lambda search_id, dry_run=False: [{"queued_count": 2}])

    out = monitoring_scheduler.execute_job({
        "JobID": 10,
        "JobType": job_queue.JOB_TYPE_NOTIFICATION_DISPATCH,
        "SearchID": 42,
        "UserAreaID": 7,
    })

    assert out["telegram_send_job"]["JobType"] == job_queue.JOB_TYPE_TELEGRAM_SEND
    assert out["telegram_send_job"]["Priority"] == job_queue.PRIORITY_NOTIFICATION_DISPATCH


def _patch_light_check_boundaries(monkeypatch, rows, meta=None):
    ingest_calls = []

    class Conn:
        def close(self): pass

    page_meta = {
        "url": "https://www.realestate.com.au/buy/in-noona,+nsw+2835/list-1?activeSort=list-date",
        "current_url": "https://www.realestate.com.au/buy/in-noona,+nsw+2835/list-1?activeSort=list-date",
        "cards_found": len(rows),
        "has_next_page": False,
        "total_pages_detected": 1,
        "stop_reason": "listings",
    }
    page_meta.update(meta or {})
    monkeypatch.setattr(area_light_checker, "connect", lambda path=None: Conn())
    monkeypatch.setattr(area_light_checker, "get_existing_external_ids_for_search", lambda conn, search_url: set())
    monkeypatch.setattr(module1_list_scraper, "scrape_search_page", lambda **kwargs: (list(rows), dict(page_meta)))
    monkeypatch.setattr(area_light_checker, "ingest_light_check_rows", lambda *args, **kwargs: ingest_calls.append((args, kwargs)) or {"run_id": 37})
    return ingest_calls


def test_same_postcode_wrong_suburb_light_check_rejected(monkeypatch):
    rows = [{
        "listing_id": "151500948",
        "address": "79 Marshall Street, Cobar, NSW 2835",
        "url": "https://www.realestate.com.au/property-house-nsw-cobar-151500948",
        "price": "$350,000",
    }]
    ingest_calls = _patch_light_check_boundaries(monkeypatch, rows)

    out = area_light_checker.light_check_area(
        "db",
        "https://www.realestate.com.au/buy/in-noona,+nsw+2835/list-1?activeSort=list-date",
        enforce_target_area=True,
    )

    assert out["scan_status"] == "skipped_untrusted"
    assert out["new_count"] == 0
    assert out["rows_area_rejected"] == 1
    assert out["area_rejection_reasons"]["wrong_suburb_same_postcode"] == 1
    assert ingest_calls == []


def test_wrong_state_and_wrong_suburb_rows_are_all_rejected(monkeypatch):
    rows = [
        {"listing_id": "1", "address": "1 Test Street, Randwick, NSW 2031", "url": "https://www.realestate.com.au/property-house-nsw-randwick-1"},
        {"listing_id": "2", "address": "2 Test Street, Burnside, SA 5066", "url": "https://www.realestate.com.au/property-house-sa-burnside-2"},
        {"listing_id": "3", "address": "3 Test Street, Bundamba, QLD 4304", "url": "https://www.realestate.com.au/property-house-qld-bundamba-3"},
        {"listing_id": "4", "address": "4 Test Street, Moonee Ponds, VIC 3039", "url": "https://www.realestate.com.au/property-house-vic-moonee-ponds-4"},
        {"listing_id": "5", "address": "5 Test Street, Darwin, NT 0800", "url": "https://www.realestate.com.au/property-house-nt-darwin-5"},
        {"listing_id": "6", "address": "6 Test Street, Cockburn, WA 6164", "url": "https://www.realestate.com.au/property-house-wa-cockburn-6"},
    ]
    ingest_calls = _patch_light_check_boundaries(monkeypatch, rows)

    out = area_light_checker.light_check_area("db", "https://www.realestate.com.au/buy/in-noona,+nsw+2835/list-1?activeSort=list-date", enforce_target_area=True)

    assert out["scan_status"] == "skipped_untrusted"
    assert out["rows_area_matched"] == 0
    assert out["rows_area_rejected"] == 6
    assert ingest_calls == []


def test_valid_area_light_check_accepts_noona_and_sets_area_label(monkeypatch):
    rows = [{
        "listing_id": "204091840",
        "address": "12 Example Road, Noona, NSW 2835",
        "url": "https://www.realestate.com.au/property-house-nsw-noona-204091840",
        "price": "$200,000",
    }]
    ingest_calls = _patch_light_check_boundaries(monkeypatch, rows)

    out = area_light_checker.light_check_area("db", "https://www.realestate.com.au/buy/in-noona,+nsw+2835/list-1?activeSort=list-date", enforce_target_area=True)

    assert out["scan_status"] == "ok"
    assert out["trusted_scan"] is True
    assert out["new_count"] == 1
    assert out["new_listings"][0]["area_label"] == "Noona, NSW 2835"
    assert ingest_calls
    ingested_rows = ingest_calls[0][0][2]
    assert ingested_rows[0]["area_label"] == "Noona, NSW 2835"


def test_untrusted_light_check_creates_no_enrichment_or_dispatch(monkeypatch):
    marked = []

    class Conn:
        def close(self): pass

    monkeypatch.setattr(monitoring_scheduler, "_search_is_active_for_monitoring", lambda search_id: True)
    monkeypatch.setattr(monitoring_scheduler, "_search_ready_for_operational_monitoring", lambda search_id: True)
    monkeypatch.setattr(monitoring_scheduler, "_load_search_subscription", lambda search_id, preferred_user_area_id=None: {"UserAreaID": 7, "SearchURL": "url"})
    monkeypatch.setattr(monitoring_scheduler.db_layer, "connect", lambda path=None: Conn())
    monkeypatch.setattr(monitoring_scheduler.db_layer, "mark_search_light_checked", lambda conn, search_id: marked.append(search_id))
    monkeypatch.setattr(monitoring_scheduler, "light_check_area", lambda *a, **k: {"scan_status": "skipped_untrusted", "trusted_scan": False, "stop_reason": "wrong_area", "new_count": 0, "new_listings": []})

    result = monitoring_scheduler.execute_job({"JobID": 10, "JobType": job_queue.JOB_TYPE_LIGHT_CHECK_NEW_LISTINGS, "SearchID": 42, "UserAreaID": 7})

    assert result["status"] == "skipped_untrusted"
    assert result["new_listing_jobs"] == []
    assert result.get("notification_dispatch_job") is None
    assert marked == [42]


def test_notification_and_light_priorities_outrank_process_new_listing():
    now = datetime.now()
    process = job_queue.enqueue_job(job_queue.JOB_TYPE_PROCESS_NEW_LISTING, search_id=42, priority=job_queue.PRIORITY_NEW_LISTING_ENRICHMENT, run_after=now)
    dispatch = job_queue.enqueue_job(job_queue.JOB_TYPE_NOTIFICATION_DISPATCH, search_id=42, priority=job_queue.PRIORITY_NOTIFICATION_DISPATCH, run_after=now)
    light = job_queue.enqueue_job(job_queue.JOB_TYPE_LIGHT_CHECK_NEW_LISTINGS, search_id=42, priority=job_queue.PRIORITY_LIGHT_CHECK, run_after=now)

    first = job_queue.claim_next_job("worker-1")
    second = job_queue.claim_next_job("worker-2")
    third = job_queue.claim_next_job("worker-3")

    assert first["JobID"] == dispatch["JobID"]
    assert second["JobID"] == light["JobID"]
    assert third["JobID"] == process["JobID"]


def test_process_new_listing_stale_recovery_consumes_attempt_and_then_fails():
    old = datetime.now() - timedelta(minutes=60)
    row = {
        "JobID": 200,
        "JobType": job_queue.JOB_TYPE_PROCESS_NEW_LISTING,
        "SearchID": 42,
        "UserAreaID": 7,
        "Priority": job_queue.PRIORITY_NEW_LISTING_ENRICHMENT,
        "Status": "running",
        "RunAfter": old,
        "AttemptCount": 2,
        "MaxAttempts": 3,
        "LockedBy": "old-worker",
        "LockedAt": old,
        "StartedAt": old,
        "FinishedAt": None,
        "LastError": None,
        "PayloadJson": "{}",
        "DedupeKey": "process_new_listing:test",
        "CreatedAt": old,
        "UpdatedAt": old,
    }
    job_queue.enable_in_memory_store([row])

    out = job_queue.recover_stale_running_jobs(now=datetime.now())
    stored = job_queue._TEST_STORE[0]

    assert out["failed_count"] == 1
    assert stored["Status"] == "failed"
    assert stored["AttemptCount"] == 3


def test_module1_pagination_total_pages_overrides_raw_next():
    assert module1_list_scraper._normalize_has_next_page(True, page=2, total_pages=2) is False
    assert module1_list_scraper._normalize_has_next_page(True, page=1, total_pages=2) is True


def test_untrusted_full_sweep_does_not_mark_swept_or_dispatch(monkeypatch):
    marked = []

    monkeypatch.setattr(monitoring_scheduler, "_search_is_active_for_monitoring", lambda search_id: True)
    monkeypatch.setattr(monitoring_scheduler, "_load_search_subscription", lambda search_id, preferred_user_area_id=None: {"UserAreaID": 7, "SearchURL": "url"})
    monkeypatch.setattr(monitoring_scheduler, "light_check_area", lambda *a, **k: {"scan_status": "skipped_untrusted", "trusted_scan": False, "stop_reason": "normal_content_without_cards"})
    monkeypatch.setattr(monitoring_scheduler.db_layer, "mark_search_full_listing_swept", lambda conn, search_id: marked.append(search_id))

    result = monitoring_scheduler.run_daily_full_listing_sweep_for_search(42)

    assert result["status"] == "skipped_untrusted"
    assert result["new_listing_jobs"] == []
    assert marked == []


def test_fresh_detail_refresh_cooldown_is_not_rescheduled(monkeypatch):
    now = datetime(2026, 6, 14, 11, 50, 0)

    class Conn:
        def cursor(self): return self
        def execute(self, *a, **k): return self
        def fetchone(self): return [now]
        def close(self): pass
        def commit(self): pass

    sub = {
        "UserAreaID": 20,
        "SearchID": 42,
        "SearchURL": "url",
        "AreaSetupStatus": "ready",
        "SubscriptionStatus": "active",
        "SubscriptionNotifyEnabled": 1,
        "BaselineStatus": "completed",
        "DetailBaselineStatus": "completed",
        "PriceBaselineStatus": "completed",
        "NotificationReadyAt": now,
        "LastLightCheckAt": now,
        "LastDetailRefreshAt": now,
        "LastPriceRefreshAt": now,
    }
    monkeypatch.setattr(monitoring_scheduler.db_layer, "connect", lambda path=None: Conn())
    monkeypatch.setattr(monitoring_scheduler.db_layer, "ensure_runtime_monitoring_schema", lambda conn: {"schema_ok": True})
    monkeypatch.setattr(monitoring_scheduler.db_layer, "ensure_telegram_bot_tables", lambda conn: None)
    monkeypatch.setattr(monitoring_scheduler.db_layer, "get_active_user_area_subscriptions", lambda conn: [sub])

    out = monitoring_scheduler.enqueue_due_monitoring_jobs(now=now)

    assert not [job for job in out["created"] if job.get("JobType") == job_queue.JOB_TYPE_DETAIL_REFRESH_EXISTING]


def test_price_retry_unknowns_dedupes_same_retry_window(monkeypatch):
    now = datetime(2026, 6, 8, 12, 30, 0)
    monkeypatch.setattr(monitoring_scheduler.config, "PRICE_UNKNOWN_RETRY_INTERVAL_SECONDS", 3600)
    monkeypatch.setattr(monitoring_scheduler.config, "ENABLE_OPERATIONAL_PRICE_MONITORING", True, raising=False)
    first = monitoring_scheduler._enqueue_price_retry_unknowns(42, ["a", "b"], run_after=now)
    second = monitoring_scheduler._enqueue_price_retry_unknowns(42, ["b", "c"], run_after=now + timedelta(minutes=10))
    assert second["created"] is False
    assert second["reason"] == "merged_existing_price_retry_for_window"
    payload = json.loads(job_queue.get_jobs_by_dedupe_key(first["DedupeKey"])[0]["PayloadJson"])
    assert payload["listing_external_ids"] == ["a", "b", "c"]


def test_running_price_retry_unknowns_merges_same_window(monkeypatch):
    now = datetime(2026, 6, 8, 12, 30, 0)
    monkeypatch.setattr(monitoring_scheduler.config, "PRICE_UNKNOWN_RETRY_INTERVAL_SECONDS", 3600)
    monkeypatch.setattr(monitoring_scheduler.config, "ENABLE_OPERATIONAL_PRICE_MONITORING", True, raising=False)
    first = monitoring_scheduler._enqueue_price_retry_unknowns(42, ["a"], run_after=now)
    claimed = job_queue.claim_next_job("worker-price")
    assert claimed["JobID"] == first["JobID"]

    second = monitoring_scheduler._enqueue_price_retry_unknowns(42, ["b", "a"], run_after=now + timedelta(minutes=5))

    assert second["created"] is False
    assert second["reason"] == "merged_existing_price_retry_for_window"
    jobs = [row for row in job_queue.get_active_jobs() if row.get("JobType") == job_queue.JOB_TYPE_PRICE_RETRY_UNKNOWNS]
    assert len(jobs) == 1
    payload = json.loads(jobs[0]["PayloadJson"])
    assert payload["listing_external_ids"] == ["a", "b"]


def test_price_refresh_stops_after_no_progress_windows(monkeypatch):
    import module2_infer_prices

    class Conn:
        def close(self): pass
        def commit(self): pass

    candidate = {
        "db_listing_id": 99,
        "listing_id": "abc",
        "external_id": "abc",
        "price_display": "Contact agent",
        "inferred_price_low": None,
        "inferred_price_high": None,
        "price_inference_status": "unknown_pending_retry",
    }
    cleanup_calls = []
    unknown_calls = []
    monkeypatch.setattr(monitoring_scheduler.config, "ENABLE_OPERATIONAL_PRICE_MONITORING", True, raising=False)
    monkeypatch.setattr(monitoring_scheduler.config, "OPERATIONAL_PRICE_MAX_WINDOWS_PER_JOB", 2, raising=False)
    monkeypatch.setattr(monitoring_scheduler.config, "OPERATIONAL_PRICE_MAX_NO_PROGRESS_WINDOWS", 2, raising=False)
    monkeypatch.setattr(monitoring_scheduler, "_search_is_active_for_monitoring", lambda search_id: True)
    monkeypatch.setattr(monitoring_scheduler, "_load_search_subscription", lambda search_id, preferred_user_area_id=None: {"SearchID": search_id, "SearchURL": "url"})
    monkeypatch.setattr(monitoring_scheduler, "_price_sweep_history", lambda conn, search_id: {"has_enough_history": True})
    monkeypatch.setattr(monitoring_scheduler, "_default_price_sweep_mode", lambda setup, sweep_mode, history: "smart_refresh")
    monkeypatch.setattr(monitoring_scheduler, "_write_price_inference_input", lambda rows, search_id: "input.json")
    monkeypatch.setattr(monitoring_scheduler, "_load_module2_output_json", lambda path: {})
    monkeypatch.setattr(monitoring_scheduler.db_layer, "connect", lambda path=None: Conn())
    monkeypatch.setattr(monitoring_scheduler.db_layer, "get_active_listings_for_price_inference", lambda *a, **k: [candidate])
    monkeypatch.setattr(monitoring_scheduler.db_layer, "parse_price_bounds_from_text", lambda text: (None, None))
    monkeypatch.setattr(monitoring_scheduler.db_layer, "mark_price_inference_unknown_pending_retry", lambda conn, search_id, listing_id, reason=None: unknown_calls.append((listing_id, reason)))
    monkeypatch.setattr(monitoring_scheduler.db_layer, "cleanup_price_retry_no_target_ids", lambda conn, search_id, ids, next_retry_at=None: cleanup_calls.append((list(ids), next_retry_at)) or {"postponed_not_due": list(ids)})
    monkeypatch.setattr(monitoring_scheduler.db_layer, "mark_search_price_refreshed", lambda *a, **k: None)

    def fake_module2_run(*args, **kwargs):
        module2_infer_prices.module2_run.last_result = {"status": "done", "target_count": 1, "remaining_count": 1, "windows_checked": 2, "sweep_mode": kwargs.get("sweep_mode")}
        return "out.csv", "out.json"

    monkeypatch.setattr(module2_infer_prices, "module2_run", fake_module2_run)

    out = monitoring_scheduler.run_price_refresh_existing_for_search(42, payload={"run_started_at": datetime(2026, 6, 8, 12, 30).isoformat()}, dry_run=False)

    assert out["status"] == "partial_no_progress"
    assert out["price_refresh"]["stop_reason"] == "max_no_progress_windows"
    assert out["enqueued_next_batch"] is None
    assert cleanup_calls and cleanup_calls[0][0] == ["abc"]
    assert unknown_calls


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
    assert any("[recovery] requested" in item and "old_profile=" in item and "old_profile" in item and "job_id=123" in item and "search_id=42" in item for item in logs)
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
    monkeypatch.setattr(telegram_bot.db_layer, "ensure_runtime_monitoring_schema", lambda conn: {"schema_ok": True, "setup_detail_schema_ok": True, "area_monitoring_schema_ok": True})
    monkeypatch.setattr(telegram_bot.db_layer, "ensure_telegram_bot_tables", lambda conn: None)
    monkeypatch.setattr(telegram_bot.db_layer, "sanitize_notification_outbox", lambda conn: {"notifications_skipped_by_revalidation": 0})
    monkeypatch.setattr(telegram_bot.job_queue, "ensure_job_tables", lambda conn=None: None)
    monkeypatch.setattr(telegram_bot.job_queue, "recover_stale_running_jobs", lambda conn=None: called.append(conn) or {"recovered_count": 1, "failed_count": 0, "stale_job_ids": [1296], "recovered_job_types": [job_queue.JOB_TYPE_BASELINE_SETUP_AREA]})
    telegram_bot.ensure_runtime_schema()
    assert len(called) == 1


def test_startup_logs_queue_runtime_mode(monkeypatch, caplog):
    import logging
    import telegram_bot

    class Conn:
        def commit(self): pass
        def close(self): pass

    monkeypatch.setattr(telegram_bot, "_connect", lambda: Conn())
    monkeypatch.setattr(telegram_bot.db_layer, "ensure_runtime_monitoring_schema", lambda conn: {"schema_ok": True, "setup_detail_schema_ok": True, "area_monitoring_schema_ok": True})
    monkeypatch.setattr(telegram_bot.db_layer, "ensure_telegram_bot_tables", lambda conn: None)
    monkeypatch.setattr(telegram_bot.db_layer, "sanitize_notification_outbox", lambda conn: {"notifications_skipped_by_revalidation": 0})
    monkeypatch.setattr(telegram_bot.job_queue, "ensure_job_tables", lambda conn=None: None)
    monkeypatch.setattr(telegram_bot.job_queue, "recover_stale_running_jobs", lambda conn=None: {"recovered_count": 0, "failed_count": 0, "stale_job_ids": [], "recovered_job_types": []})

    with caplog.at_level(logging.INFO, logger="telegram_bot"):
        telegram_bot.ensure_runtime_schema()

    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "runtime_mode=queue" in text
    assert "legacy_tick_enabled=false" in text


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


def test_large_baseline_orchestration_batches_detail_without_inline_module3_or_module2(monkeypatch):
    import monitor

    rows = [{"listing_id": str(i), "url": f"https://example.test/{i}", "price": "N/A"} for i in range(924)]
    calls = []
    class Conn:
        def commit(self): calls.append(("commit",))
        def close(self): pass

    monkeypatch.setattr(monitor.config, "BASELINE_DETAIL_BATCH_SIZE", 50, raising=False)
    monkeypatch.setattr(monitor, "init_db", lambda path: None)
    monkeypatch.setattr(monitor, "connect", lambda path: Conn())
    monkeypatch.setattr(monitor, "get_or_create_area", lambda conn, url: 42)
    monkeypatch.setattr(monitor, "upsert_area_monitoring_state", lambda conn, area_id, **kwargs: calls.append(("state", area_id, kwargs)))
    monkeypatch.setattr(monitor, "ingest_full_rows", lambda *a, **k: calls.append(("ingest", k)) or 77)
    monkeypatch.setattr(monitor.db_layer, "mark_search_baseline_completed", lambda conn, search_id, **kwargs: calls.append(("baseline_completed", search_id, kwargs)))
    monkeypatch.setattr(monitor.db_layer, "enqueue_setup_detail_baseline_job", lambda conn, search_id, **kwargs: calls.append(("detail_job", search_id, kwargs)) or {"created": True})
    monkeypatch.setattr(monitor.module1_list_scraper, "scrape_search", lambda *a, **k: rows)
    monkeypatch.setattr(monitor.module3_enrich_details, "module3_run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("Module3 must not run inline")))
    monkeypatch.setattr(monitor.module2_infer_prices, "module2_run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("Module2 must not run inline")))

    out = monitor.baseline_setup_area("https://example.test/search")

    assert out["status"] == "setup_batched"
    assert out["rows_module1"] == 924
    assert out["detail_batch_size"] == 50
    assert out["detail_batches_planned"] == 19
    assert any(item[0] == "detail_job" for item in calls)
    ingest_call = next(item for item in calls if item[0] == "ingest")
    assert ingest_call[1]["emit_events"] is False


def test_setup_detail_batch_processes_configured_limit_and_requeues_next(monkeypatch):
    import monitoring_scheduler

    calls = []
    sub = {"UserAreaID": 7, "SearchID": 42, "SearchURL": "https://example.test/search", "DetailBaselineStatus": "running"}
    class Conn:
        def commit(self): calls.append(("commit",))
        def close(self): pass

    monkeypatch.setattr(monitoring_scheduler.config, "BASELINE_DETAIL_BATCH_SIZE", 50, raising=False)
    monkeypatch.setattr(monitoring_scheduler, "_search_is_active_for_monitoring", lambda search_id: True)
    monkeypatch.setattr(monitoring_scheduler, "_load_search_subscription", lambda search_id, user_area_id=None: sub)
    monkeypatch.setattr(monitoring_scheduler, "refresh_active_listings", lambda search_url, **kwargs: calls.append(("refresh", kwargs)) or {"processed_count": 50, "refreshed_count": 50, "failed_count": 0, "errors": []})
    monkeypatch.setattr(monitoring_scheduler.db_layer, "connect", lambda path=None: Conn())
    monkeypatch.setattr(monitoring_scheduler.db_layer, "get_user_area_subscription", lambda conn, user_area_id: sub)
    monkeypatch.setattr(monitoring_scheduler.db_layer, "mark_subscription_detail_baseline_started", lambda *a, **k: calls.append(("detail_started",)))
    monkeypatch.setattr(monitoring_scheduler.db_layer, "mark_subscription_detail_baseline_batch_succeeded", lambda *a, **k: calls.append(("batch_ok",)))
    monkeypatch.setattr(monitoring_scheduler.db_layer, "get_detail_baseline_progress", lambda conn, subscription: {"detail_baseline_total_count": 924, "detail_baseline_completed_count": 50, "detail_baseline_remaining_count": 874})
    monkeypatch.setattr(monitoring_scheduler.db_layer, "upsert_area_monitoring_state", lambda conn, search_id, **kwargs: calls.append(("state", kwargs)))
    monkeypatch.setattr(monitoring_scheduler.db_layer, "enqueue_setup_detail_baseline_job", lambda conn, search_id, **kwargs: calls.append(("next_detail", kwargs)) or {"created": True})
    monkeypatch.setattr(monitoring_scheduler.db_layer, "enqueue_setup_price_baseline_job", lambda *a, **k: (_ for _ in ()).throw(AssertionError("price must not enqueue before details complete")))

    out = monitoring_scheduler._run_setup_detail_batch({"JobID": 100, "SearchID": 42}, send_telegram=False)

    assert out["status"] == "detail_baseline_running"
    refresh = next(item for item in calls if item[0] == "refresh")
    assert refresh[1]["limit"] == 50
    assert refresh[1]["suppress_notifications"] is True
    assert any(item[0] == "next_detail" for item in calls)
    assert not any(item[0] == "activate" for item in calls)


def test_final_setup_detail_batch_enqueues_one_full_price_job(monkeypatch):
    import monitoring_scheduler

    calls = []
    sub = {"UserAreaID": 7, "SearchID": 42, "SearchURL": "https://example.test/search", "DetailBaselineStatus": "running"}
    class Conn:
        def commit(self): pass
        def close(self): pass

    monkeypatch.setattr(monitoring_scheduler, "_search_is_active_for_monitoring", lambda search_id: True)
    monkeypatch.setattr(monitoring_scheduler, "_load_search_subscription", lambda search_id, user_area_id=None: sub)
    monkeypatch.setattr(monitoring_scheduler, "refresh_active_listings", lambda *a, **k: {"processed_count": 24, "refreshed_count": 24, "failed_count": 0, "errors": []})
    monkeypatch.setattr(monitoring_scheduler.db_layer, "connect", lambda path=None: Conn())
    monkeypatch.setattr(monitoring_scheduler.db_layer, "get_user_area_subscription", lambda conn, user_area_id: sub)
    monkeypatch.setattr(monitoring_scheduler.db_layer, "mark_subscription_detail_baseline_completed", lambda *a, **k: calls.append(("detail_completed",)))
    monkeypatch.setattr(monitoring_scheduler.db_layer, "get_detail_baseline_progress", lambda conn, subscription: {"detail_baseline_total_count": 924, "detail_baseline_completed_count": 924, "detail_baseline_remaining_count": 0})
    monkeypatch.setattr(monitoring_scheduler.db_layer, "upsert_area_monitoring_state", lambda conn, search_id, **kwargs: calls.append(("state", kwargs)))
    monkeypatch.setattr(monitoring_scheduler.db_layer, "enqueue_setup_price_baseline_job", lambda conn, search_id, **kwargs: calls.append(("price_job", kwargs)) or {"created": True})
    monkeypatch.setattr(monitoring_scheduler.db_layer, "enqueue_setup_detail_baseline_job", lambda *a, **k: (_ for _ in ()).throw(AssertionError("detail should not requeue when complete")))

    out = monitoring_scheduler._run_setup_detail_batch({"JobID": 101, "SearchID": 42}, send_telegram=False)

    assert out["status"] == "price_baseline_pending"
    assert sum(1 for item in calls if item[0] == "price_job") == 1
    assert any(item[0] == "state" and item[1].get("module3_status") == "completed" for item in calls)


def test_setup_price_baseline_runs_full_module2_once_and_marks_ready_with_unknowns(monkeypatch):
    import monitoring_scheduler

    calls = []
    sub = {"UserAreaID": 7, "SearchID": 42, "SearchURL": "https://example.test/search", "PriceBaselineStatus": "pending"}
    class Conn:
        def commit(self): calls.append(("commit",))
        def close(self): pass

    def fake_price(search_id, **kwargs):
        calls.append(("price", kwargs))
        return {"status": "completed_with_unknowns", "processed_count": 924, "inferred_count": 800, "unknown_count": 124, "module2_runs": [{"sweep_mode": "setup_full_sweep"}]}

    monkeypatch.setattr(monitoring_scheduler, "_search_is_active_for_monitoring", lambda search_id: True)
    monkeypatch.setattr(monitoring_scheduler, "_load_search_subscription", lambda search_id, user_area_id=None: sub)
    monkeypatch.setattr(monitoring_scheduler, "run_price_baseline_for_search", fake_price)
    monkeypatch.setattr(monitoring_scheduler.db_layer, "connect", lambda path=None: Conn())
    monkeypatch.setattr(monitoring_scheduler.db_layer, "get_area_monitoring_state", lambda conn, search_id: {"setup_status": "preparing", "module1_status": "completed", "module3_status": "completed", "module2_status": "pending"})
    monkeypatch.setattr(monitoring_scheduler.db_layer, "mark_subscription_price_baseline_started", lambda *a, **k: calls.append(("price_started",)))
    monkeypatch.setattr(monitoring_scheduler.db_layer, "mark_subscription_price_baseline_completed", lambda *a, **k: calls.append(("price_completed",)))
    monkeypatch.setattr(monitoring_scheduler.db_layer, "get_user_area_subscription", lambda conn, user_area_id: sub)
    monkeypatch.setattr(monitoring_scheduler.db_layer, "get_price_baseline_progress", lambda conn, subscription: {"price_baseline_total_count": 924, "price_baseline_completed_count": 924, "price_baseline_remaining_count": 0})
    monkeypatch.setattr(monitoring_scheduler.db_layer, "upsert_area_monitoring_state", lambda conn, search_id, **kwargs: calls.append(("state", kwargs)))
    monkeypatch.setattr(monitoring_scheduler.db_layer, "is_area_setup_ready", lambda conn, search_id: True)
    monkeypatch.setattr(monitoring_scheduler.db_layer, "activate_area_subscriptions", lambda conn, search_id: calls.append(("activate", search_id)))
    monkeypatch.setattr(monitoring_scheduler, "_send_setup_summary_once", lambda sub, kind: calls.append(("summary", kind)))

    out = monitoring_scheduler._run_setup_price_batch({"JobID": 200, "SearchID": 42}, send_telegram=True)

    assert out["status"] == "ready"
    price_call = next(item for item in calls if item[0] == "price")
    assert price_call[1]["limit"] is None
    assert price_call[1]["setup"] is True
    assert ("activate", 42) in calls
    assert ("summary", "ready") in calls
    assert any(item[0] == "state" and item[1].get("unknown_price_count") == 124 for item in calls)


def test_telegram_setup_status_labels_show_detail_and_price_progress():
    import telegram_bot

    detail = {
        "AreaSetupStatus": "preparing",
        "AreaModule1Status": "completed",
        "AreaModule3Status": "running",
        "AreaModule2Status": "pending",
        "AreaLastError": "details 150/924",
        "BaselineStatus": "completed",
        "DetailBaselineStatus": "running",
        "PriceBaselineStatus": "pending",
    }
    price = {**detail, "AreaModule3Status": "completed", "AreaModule2Status": "running", "DetailBaselineStatus": "completed", "PriceBaselineStatus": "running"}
    ready = {**price, "AreaSetupStatus": "ready", "NotificationReadyAt": "2026-06-10", "BaselineListingsCollected": 924, "PriceBaselineStatus": "completed"}

    assert telegram_bot._status_label(detail) == "Preparing — details 150/924"
    assert telegram_bot._status_label(price) == "Preparing — running price setup"
    assert telegram_bot._status_label(ready) == "Ready — 924 listings monitored"


def test_auction_time_label_sanitizer_extracts_short_label_from_full_card_text():
    raw = """Auction Guide $500,000
2/50-58 Roslyn Gardens, Rushcutters Bay
1 bed 1 bath 0 cars
Inspection tomorrow"""

    assert db_layer.sanitize_auction_time_label(raw) == "Auction Guide $500,000"


def test_auction_time_label_sanitizer_preserves_valid_short_label():
    assert db_layer.sanitize_auction_time_label("Auction Sat 22 Jun") == "Auction Sat 22 Jun"


def test_limited_snapshot_fields_are_normalized_before_persistence():
    row = db_layer.normalize_listing_row({
        "listing_id": "safe-limits-1",
        "url": "https://example.test/listing",
        "address": "12 Example Street" + " very long" * 100,
        "property_type": "House" + "x" * 200,
        "AdPriceDisplay": "$500,000" + " guide" * 100,
        "inspection_short_label": "Inspection tomorrow\n2 bed 1 bath\x00extra text",
        "auction_label": "Auction Guide $500,000\n2/50-58 Roslyn Gardens\nInspection tomorrow",
        "agency_name": "Agency" + "x" * 400,
        "agents": [{"name": "Agent" + "x" * 300, "phone": "123\x00456"}],
    })

    assert row["auction_label"] == "Auction Guide $500,000"
    assert "Roslyn" not in row["auction_label"]
    assert "\n" not in (row["inspection_short_label"] or "")
    assert "\x00" not in row["agents"][0]["phone"]
    assert len(row["price_display"]) <= 300
    assert len(row["address"]) <= 500
    assert len(row["property_type"]) <= 100
    assert len(row["agency_name"]) <= 300
    assert len(row["agents"][0]["name"]) <= 200


def test_baseline_setup_with_long_auction_label_batches_without_persistence_failure(monkeypatch):
    import monitor

    raw_card = """Auction Guide $500,000
2/50-58 Roslyn Gardens, Rushcutters Bay
1 bed 1 bath
Inspection tomorrow"""
    rows = [{"listing_id": "darlinghurst-1", "url": "https://example.test/1", "price": "$500,000", "auction_label": raw_card}]
    calls = []

    class Conn:
        def commit(self): calls.append(("commit",))
        def close(self): pass

    def fake_ingest(*args, **kwargs):
        normalized = [db_layer.normalize_listing_row(row) for row in kwargs.get("rows", args[2] if len(args) > 2 else [])]
        assert normalized[0]["auction_label"] == "Auction Guide $500,000"
        calls.append(("ingest", kwargs))
        return 1

    monkeypatch.setattr(monitor.config, "BASELINE_DETAIL_BATCH_SIZE", 50, raising=False)
    monkeypatch.setattr(monitor, "init_db", lambda path: None)
    monkeypatch.setattr(monitor, "connect", lambda path: Conn())
    monkeypatch.setattr(monitor, "get_or_create_area", lambda conn, url: 42)
    monkeypatch.setattr(monitor, "upsert_area_monitoring_state", lambda conn, area_id, **kwargs: calls.append(("state", area_id, kwargs)))
    monkeypatch.setattr(monitor, "ingest_full_rows", fake_ingest)
    monkeypatch.setattr(monitor.db_layer, "mark_search_baseline_completed", lambda conn, search_id, **kwargs: calls.append(("baseline_completed", search_id, kwargs)))
    monkeypatch.setattr(monitor.db_layer, "enqueue_setup_detail_baseline_job", lambda conn, search_id, **kwargs: calls.append(("detail_job", search_id, kwargs)) or {"created": True})
    monkeypatch.setattr(monitor.module1_list_scraper, "scrape_search", lambda *a, **k: rows)
    monkeypatch.setattr(monitor.module3_enrich_details, "module3_run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("Module3 must not run inline")))
    monkeypatch.setattr(monitor.module2_infer_prices, "module2_run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("Module2 must not run inline")))

    out = monitor.baseline_setup_area("https://example.test/search")

    assert out["status"] == "setup_batched"
    assert any(item[0] == "detail_job" for item in calls)


def test_telegram_ready_label_uses_area_active_count():
    import telegram_bot

    label = telegram_bot._status_label({
        "AreaSetupStatus": "ready",
        "DetailBaselineStatus": "completed",
        "PriceBaselineStatus": "completed",
        "NotificationReadyAt": "2026-06-10",
        "AreaActiveListingCount": 28,
        "LiveActiveListingCount": 28,
        "BaselineListingsCollected": 0,
    })

    assert label == "Ready — 28 listings monitored"
    assert "no active listings" not in label


def test_telegram_ready_label_uses_live_active_count_fallback():
    import telegram_bot

    label = telegram_bot._status_label({
        "AreaSetupStatus": "ready",
        "DetailBaselineStatus": "completed",
        "PriceBaselineStatus": "completed",
        "NotificationReadyAt": "2026-06-10",
        "AreaActiveListingCount": 0,
        "LiveActiveListingCount": 28,
        "BaselineListingsCollected": 0,
    })

    assert label == "Ready — 28 listings monitored"
    assert "no active listings" not in label


def test_telegram_ready_label_allows_true_empty_ready_area():
    import telegram_bot

    label = telegram_bot._status_label({
        "AreaSetupStatus": "ready",
        "DetailBaselineStatus": "completed",
        "PriceBaselineStatus": "completed",
        "NotificationReadyAt": "2026-06-10",
        "AreaActiveListingCount": 0,
        "LiveActiveListingCount": 0,
        "BaselineListingsCollected": 0,
    })

    assert label == "Ready — no active listings"


def _patch_not_ready_setup_scheduler(monkeypatch, now, subscriptions, area_state=None):
    class Conn:
        def cursor(self): return self
        def close(self): pass
        def commit(self): pass
        def execute(self, *a, **k): return self
        def fetchone(self): return [now]
    monkeypatch.setattr(monitoring_scheduler.db_layer, "connect", lambda path=None: Conn())
    monkeypatch.setattr(monitoring_scheduler.db_layer, "ensure_runtime_monitoring_schema", lambda conn: {"schema_ok": True})
    monkeypatch.setattr(monitoring_scheduler.db_layer, "ensure_telegram_bot_tables", lambda conn: None)
    monkeypatch.setattr(monitoring_scheduler.db_layer, "get_active_user_area_subscriptions", lambda conn: subscriptions)
    monkeypatch.setattr(monitoring_scheduler.db_layer, "get_area_monitoring_state", lambda conn, area_id: area_state or {"setup_status": "preparing"})


def test_scheduler_blocks_baseline_rerun_while_detail_setup_job_active(monkeypatch):
    now = datetime(2026, 6, 10, 10, 0, 0)
    sub = {
        "UserAreaID": 20,
        "SearchID": 2,
        "SearchURL": "url",
        "AreaSetupStatus": "preparing",
        "AreaModule1Status": "completed",
        "AreaModule3Status": "pending",
        "AreaModule2Status": "pending",
        "BaselineStatus": "completed",
        "DetailBaselineStatus": "running",
        "PriceBaselineStatus": "pending",
    }
    job_queue.enqueue_job_once(job_queue.JOB_TYPE_SETUP_DETAIL_BASELINE, search_id=2, user_area_id=20, run_after=now)
    _patch_not_ready_setup_scheduler(monkeypatch, now, [sub])

    out = monitoring_scheduler.enqueue_due_monitoring_jobs(now=now)

    assert not [job for job in out["created"] if job.get("JobType") == job_queue.JOB_TYPE_BASELINE_SETUP_AREA]
    assert out["setup_phase_active_blocked_count"] == 1
    assert out["setup_phase_blocked"][0]["active_job_types"] == [job_queue.JOB_TYPE_SETUP_DETAIL_BASELINE]


def test_scheduler_repairs_module1_completed_module3_pending_with_detail_job(monkeypatch):
    now = datetime(2026, 6, 10, 10, 0, 0)
    sub = {
        "UserAreaID": 21,
        "SearchID": 3,
        "SearchURL": "url",
        "AreaSetupStatus": "preparing",
        "AreaModule1Status": "completed",
        "AreaModule3Status": "pending",
        "AreaModule2Status": "pending",
        "BaselineStatus": "completed",
        "DetailBaselineStatus": "pending",
        "PriceBaselineStatus": "pending",
    }
    _patch_not_ready_setup_scheduler(monkeypatch, now, [sub])

    out = monitoring_scheduler.enqueue_due_monitoring_jobs(now=now)

    assert out["setup_detail_repair_enqueued_count"] == 1
    assert [job for job in out["created"] if job.get("JobType") == job_queue.JOB_TYPE_SETUP_DETAIL_BASELINE]
    assert not [job for job in out["created"] if job.get("JobType") == job_queue.JOB_TYPE_BASELINE_SETUP_AREA]
    assert not [job for job in out["created"] if job.get("JobType") == job_queue.JOB_TYPE_SETUP_PRICE_BASELINE]


def test_scheduler_repairs_module3_completed_module2_pending_with_price_job(monkeypatch):
    now = datetime(2026, 6, 10, 10, 0, 0)
    sub = {
        "UserAreaID": 22,
        "SearchID": 4,
        "SearchURL": "url",
        "AreaSetupStatus": "preparing",
        "AreaModule1Status": "completed",
        "AreaModule3Status": "completed",
        "AreaModule2Status": "pending",
        "BaselineStatus": "completed",
        "DetailBaselineStatus": "completed",
        "PriceBaselineStatus": "pending",
    }
    _patch_not_ready_setup_scheduler(monkeypatch, now, [sub])

    out = monitoring_scheduler.enqueue_due_monitoring_jobs(now=now)

    assert out["setup_price_repair_enqueued_count"] == 1
    assert [job for job in out["created"] if job.get("JobType") == job_queue.JOB_TYPE_SETUP_PRICE_BASELINE]
    assert not [job for job in out["created"] if job.get("JobType") == job_queue.JOB_TYPE_BASELINE_SETUP_AREA]


def test_scheduler_does_not_duplicate_active_price_setup_or_rerun_baseline(monkeypatch):
    now = datetime(2026, 6, 10, 10, 0, 0)
    sub = {
        "UserAreaID": 23,
        "SearchID": 5,
        "SearchURL": "url",
        "AreaSetupStatus": "preparing",
        "AreaModule1Status": "completed",
        "AreaModule3Status": "completed",
        "AreaModule2Status": "running",
        "BaselineStatus": "completed",
        "DetailBaselineStatus": "completed",
        "PriceBaselineStatus": "running",
    }
    job_queue.enqueue_job_once(job_queue.JOB_TYPE_SETUP_PRICE_BASELINE, search_id=5, user_area_id=23, run_after=now)
    _patch_not_ready_setup_scheduler(monkeypatch, now, [sub])

    out = monitoring_scheduler.enqueue_due_monitoring_jobs(now=now)

    assert out["setup_phase_active_blocked_count"] == 1
    assert not [job for job in out["created"] if job.get("JobType") in {job_queue.JOB_TYPE_BASELINE_SETUP_AREA, job_queue.JOB_TYPE_SETUP_PRICE_BASELINE}]


def test_detail_progress_uses_baseline_total_to_prevent_premature_price_enqueue():
    class Cursor:
        def execute(self, *a, **k): return self
        def fetchone(self): return [2, 2]
    class Conn:
        def cursor(self): return Cursor()

    progress = db_layer.get_detail_baseline_progress(Conn(), {"SearchID": 9, "DetailBaselineStartedAt": datetime(2026, 6, 10), "BaselineListingsCollected": 928})

    assert progress["detail_baseline_total_count"] == 928
    assert progress["detail_baseline_completed_count"] == 2
    assert progress["detail_baseline_remaining_count"] == 926


def test_setup_price_guard_requeues_detail_when_module3_not_completed(monkeypatch):
    calls = []
    sub = {"UserAreaID": 24, "SearchID": 6, "SearchURL": "url", "PriceBaselineStatus": "pending"}
    class Conn:
        def commit(self): calls.append(("commit",))
        def close(self): pass

    monkeypatch.setattr(monitoring_scheduler, "_search_is_active_for_monitoring", lambda search_id: True)
    monkeypatch.setattr(monitoring_scheduler, "_load_search_subscription", lambda search_id, user_area_id=None: sub)
    monkeypatch.setattr(monitoring_scheduler.db_layer, "connect", lambda path=None: Conn())
    monkeypatch.setattr(monitoring_scheduler.db_layer, "get_area_monitoring_state", lambda conn, search_id: {"setup_status": "preparing", "module1_status": "completed", "module3_status": "running", "module2_status": "pending"})
    monkeypatch.setattr(monitoring_scheduler.db_layer, "enqueue_setup_detail_baseline_job", lambda conn, search_id, **kwargs: calls.append(("detail_job", search_id, kwargs)) or {"created": True})
    monkeypatch.setattr(monitoring_scheduler, "run_price_baseline_for_search", lambda *a, **k: (_ for _ in ()).throw(AssertionError("price must not run before detail completion")))

    out = monitoring_scheduler._run_setup_price_batch({"JobID": 300, "SearchID": 6, "UserAreaID": 24}, send_telegram=False)

    assert out["status"] == "skipped"
    assert out["reason"] == "detail_baseline_not_completed"
    assert any(item[0] == "detail_job" for item in calls)


def test_setup_detail_target_selection_uses_canonical_status_and_started_cutoff(monkeypatch):
    executed = []

    class Cursor:
        description = [("db_listing_id",), ("external_id",), ("listing_id",), ("url",), ("address",), ("property_type",), ("price",), ("price_display",), ("bedrooms",), ("bathrooms",), ("parking",), ("current_status",), ("ListingLifecycleStatus",), ("last_detail_refresh_at",), ("setup_detail_status",), ("setup_detail_attempt_count",), ("setup_detail_next_retry_at",)]
        def execute(self, sql, *params):
            executed.append((str(sql), params))
            return self
        def fetchall(self): return []
        def fetchone(self): return None
    class Conn:
        def cursor(self): return Cursor()

    monkeypatch.setattr(db_layer, "ensure_listing_search_state_detail_refresh_column", lambda conn: None)
    started = datetime(2026, 6, 10, 8, 0, 0)
    db_layer.get_active_listings_for_detail_refresh(Conn(), "url", limit=10, stale_hours=0, subscription={"SearchID": 2, "DetailBaselineStartedAt": started})

    sql, params = executed[-1]
    assert "SetupDetailStatus" in sql
    assert "SetupDetailNextRetryAt" in sql
    assert "LastDetailRefreshAt < ?" in sql
    assert "detail_complete" in sql
    assert started in params


def test_mark_listing_search_state_detail_refreshed_sets_setup_detail_complete(monkeypatch):
    executed = []
    class Cursor:
        def execute(self, sql, *params):
            executed.append((str(sql), params))
            return self
    class Conn:
        def cursor(self): return Cursor()

    monkeypatch.setattr(db_layer, "ensure_listing_search_state_detail_refresh_column", lambda conn: None)
    db_layer.mark_listing_search_state_detail_refreshed(Conn(), 2, 99, setup_detail_status="detail_partial_complete")

    sql, params = executed[-1]
    assert "SetupDetailStatus" in sql
    assert "SetupDetailCompletedAt" in sql
    assert params[0] == "detail_partial_complete"


def test_darlinghurst_scale_remaining_decreases_with_completed_count():
    class Cursor:
        def execute(self, *a, **k): return self
        def fetchone(self): return [928, 30, 0, 0, 0]
    class Conn:
        def cursor(self): return Cursor()

    progress = db_layer.get_detail_baseline_progress(Conn(), {"SearchID": 2, "DetailBaselineStartedAt": datetime(2026, 6, 10), "BaselineListingsCollected": 928})

    assert progress["detail_baseline_remaining_count"] == 898
    assert progress["detail_baseline_completed_count"] == 30


def test_setup_detail_batch_structured_logs_and_delay(monkeypatch, caplog):
    import logging
    import monitoring_scheduler

    calls = []
    sub = {"UserAreaID": 7, "SearchID": 42, "SearchURL": "https://example.test/search", "DetailBaselineStatus": "running", "DetailBaselineStartedAt": datetime(2026, 6, 10), "BaselineListingsCollected": 30}
    class Conn:
        def commit(self): calls.append(("commit",))
        def close(self): pass

    monkeypatch.setattr(monitoring_scheduler.config, "BASELINE_DETAIL_BATCH_SIZE", 10, raising=False)
    monkeypatch.setattr(monitoring_scheduler.config, "SETUP_DETAIL_BATCH_DELAY_SECONDS", 0, raising=False)
    monkeypatch.setattr(monitoring_scheduler.config, "SETUP_DETAIL_BATCH_DELAY_JITTER_SECONDS", 0, raising=False)
    monkeypatch.setattr(monitoring_scheduler, "_search_is_active_for_monitoring", lambda search_id: True)
    monkeypatch.setattr(monitoring_scheduler, "_load_search_subscription", lambda search_id, user_area_id=None: sub)
    monkeypatch.setattr(monitoring_scheduler, "refresh_active_listings", lambda search_url, **kwargs: {"processed_count": 10, "refreshed_count": 9, "failed_count": 1, "errors": [], "candidates_count": 10})
    monkeypatch.setattr(monitoring_scheduler.db_layer, "connect", lambda path=None: Conn())
    monkeypatch.setattr(monitoring_scheduler.db_layer, "get_user_area_subscription", lambda conn, user_area_id: sub)
    progress_values = [
        {"detail_baseline_total_count": 30, "detail_baseline_completed_count": 0, "detail_baseline_remaining_count": 30},
        {"detail_baseline_total_count": 30, "detail_baseline_completed_count": 10, "detail_baseline_remaining_count": 20, "detail_baseline_partial_count": 1},
    ]
    monkeypatch.setattr(monitoring_scheduler.db_layer, "get_detail_baseline_progress", lambda conn, subscription: progress_values.pop(0) if progress_values else {"detail_baseline_total_count": 30, "detail_baseline_completed_count": 10, "detail_baseline_remaining_count": 20, "detail_baseline_partial_count": 1})
    monkeypatch.setattr(monitoring_scheduler.db_layer, "count_succeeded_setup_detail_jobs", lambda conn, search_id: 0)
    monkeypatch.setattr(monitoring_scheduler.db_layer, "get_active_listings_for_detail_refresh", lambda *a, **k: [{"listing_id": str(i)} for i in range(10)])
    monkeypatch.setattr(monitoring_scheduler.db_layer, "upsert_area_monitoring_state", lambda *a, **k: calls.append(("state", k)))
    monkeypatch.setattr(monitoring_scheduler.db_layer, "mark_subscription_detail_baseline_batch_succeeded", lambda *a, **k: calls.append(("batch_ok",)))
    monkeypatch.setattr(monitoring_scheduler.db_layer, "enqueue_setup_detail_baseline_job", lambda *a, **k: calls.append(("next_detail", k)) or {"created": True})
    monkeypatch.setattr(monitoring_scheduler.db_layer, "enqueue_setup_price_baseline_job", lambda *a, **k: (_ for _ in ()).throw(AssertionError("price must not enqueue")))

    with caplog.at_level(logging.INFO, logger="monitoring_scheduler"):
        out = monitoring_scheduler._run_setup_detail_batch({"JobID": 501, "SearchID": 42}, send_telegram=False)

    assert out["status"] == "detail_baseline_running"
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "setup_detail_batch_start" in text
    assert "setup_detail_batch_complete" in text
    assert "remaining_before=30" in text
    assert "remaining_after=20" in text
    assert "processed=10" in text
    assert "succeeded=9" in text
    assert "technical_failed=1" in text
    assert "batch_number=1" in text
    assert any(call[0] == "next_detail" for call in calls)


def test_setup_detail_stalled_progress_guard_fails_setup(monkeypatch):
    import monitoring_scheduler

    calls = []
    sub = {"UserAreaID": 7, "SearchID": 42, "SearchURL": "https://example.test/search", "DetailBaselineStatus": "running", "DetailBaselineStartedAt": datetime(2026, 6, 10), "BaselineListingsCollected": 100}
    class Conn:
        def commit(self): calls.append(("commit",))
        def close(self): pass

    monkeypatch.setattr(monitoring_scheduler.config, "BASELINE_DETAIL_BATCH_SIZE", 10, raising=False)
    monkeypatch.setattr(monitoring_scheduler, "_search_is_active_for_monitoring", lambda search_id: True)
    monkeypatch.setattr(monitoring_scheduler, "_load_search_subscription", lambda search_id, user_area_id=None: sub)
    monkeypatch.setattr(monitoring_scheduler, "refresh_active_listings", lambda *a, **k: {"processed_count": 0, "refreshed_count": 0, "failed_count": 0, "errors": [], "candidates_count": 10})
    monkeypatch.setattr(monitoring_scheduler.db_layer, "connect", lambda path=None: Conn())
    monkeypatch.setattr(monitoring_scheduler.db_layer, "get_user_area_subscription", lambda conn, user_area_id: sub)
    monkeypatch.setattr(monitoring_scheduler.db_layer, "get_detail_baseline_progress", lambda conn, subscription: {"detail_baseline_total_count": 100, "detail_baseline_completed_count": 3, "detail_baseline_remaining_count": 97})
    monkeypatch.setattr(monitoring_scheduler.db_layer, "count_succeeded_setup_detail_jobs", lambda conn, search_id, started_at=None: 160)
    monkeypatch.setattr(monitoring_scheduler.db_layer, "get_active_listings_for_detail_refresh", lambda *a, **k: [{"listing_id": str(i)} for i in range(10)])
    monkeypatch.setattr(monitoring_scheduler.db_layer, "upsert_area_monitoring_state", lambda *a, **k: calls.append(("state", k)))
    monkeypatch.setattr(monitoring_scheduler.db_layer, "mark_subscription_detail_baseline_failed", lambda conn, user_area_id, error: calls.append(("failed", error)))

    out = monitoring_scheduler._run_setup_detail_batch({"JobID": 502, "SearchID": 42}, send_telegram=False)

    assert out["status"] == "detail_baseline_failed"
    assert any(call[0] == "failed" and "setup detail progress stalled" in call[1] for call in calls)


def test_successful_partial_setup_detail_batch_ignores_historical_jobs_and_requeues(monkeypatch):
    import monitoring_scheduler

    calls = []
    started = datetime(2026, 6, 10, 8, 0, 0)
    sub = {
        "UserAreaID": 7,
        "SearchID": 42,
        "SearchURL": "https://example.test/search",
        "DetailBaselineStatus": "running",
        "DetailBaselineStartedAt": started,
        "BaselineListingsCollected": 928,
    }

    class Conn:
        def commit(self): calls.append(("commit",))
        def close(self): pass

    progress_values = [
        {"detail_baseline_total_count": 928, "detail_baseline_completed_count": 0, "detail_baseline_remaining_count": 928},
        {"detail_baseline_total_count": 928, "detail_baseline_completed_count": 10, "detail_baseline_remaining_count": 918, "detail_baseline_partial_count": 0},
    ]

    def count_jobs(conn, search_id, started_at=None):
        assert started_at == started
        return 0

    monkeypatch.setattr(monitoring_scheduler.config, "BASELINE_DETAIL_BATCH_SIZE", 10, raising=False)
    monkeypatch.setattr(monitoring_scheduler.config, "SETUP_DETAIL_BATCH_DELAY_SECONDS", 0, raising=False)
    monkeypatch.setattr(monitoring_scheduler.config, "SETUP_DETAIL_BATCH_DELAY_JITTER_SECONDS", 0, raising=False)
    monkeypatch.setattr(monitoring_scheduler, "_search_is_active_for_monitoring", lambda search_id: True)
    monkeypatch.setattr(monitoring_scheduler, "_load_search_subscription", lambda search_id, user_area_id=None: sub)
    monkeypatch.setattr(monitoring_scheduler, "refresh_active_listings", lambda *a, **k: {"processed_count": 10, "refreshed_count": 10, "failed_count": 0, "errors": [], "candidates_count": 10})
    monkeypatch.setattr(monitoring_scheduler.db_layer, "connect", lambda path=None: Conn())
    monkeypatch.setattr(monitoring_scheduler.db_layer, "get_user_area_subscription", lambda conn, user_area_id: sub)
    monkeypatch.setattr(monitoring_scheduler.db_layer, "get_detail_baseline_progress", lambda conn, subscription: progress_values.pop(0) if progress_values else {"detail_baseline_total_count": 928, "detail_baseline_completed_count": 10, "detail_baseline_remaining_count": 918})
    monkeypatch.setattr(monitoring_scheduler.db_layer, "count_succeeded_setup_detail_jobs", count_jobs)
    monkeypatch.setattr(monitoring_scheduler.db_layer, "count_remaining_setup_detail_targets", lambda *a, **k: 918)
    monkeypatch.setattr(monitoring_scheduler.db_layer, "get_active_listings_for_detail_refresh", lambda *a, **k: [{"listing_id": str(i)} for i in range(10)])
    monkeypatch.setattr(monitoring_scheduler.db_layer, "upsert_area_monitoring_state", lambda conn, search_id, **kwargs: calls.append(("state", kwargs)))
    monkeypatch.setattr(monitoring_scheduler.db_layer, "mark_subscription_detail_baseline_batch_succeeded", lambda *a, **k: calls.append(("batch_ok",)))
    monkeypatch.setattr(monitoring_scheduler.db_layer, "mark_subscription_detail_baseline_failed", lambda *a, **k: calls.append(("failed",)))
    monkeypatch.setattr(monitoring_scheduler.db_layer, "enqueue_setup_detail_baseline_job", lambda conn, search_id, **kwargs: calls.append(("next_detail", kwargs)) or {"created": True})
    monkeypatch.setattr(monitoring_scheduler.db_layer, "enqueue_setup_price_baseline_job", lambda *a, **k: (_ for _ in ()).throw(AssertionError("price must not enqueue before details complete")))

    out = monitoring_scheduler._run_setup_detail_batch({"JobID": 1509, "SearchID": 42}, send_telegram=False)

    assert out["status"] == "detail_baseline_running"
    assert out["detail_baseline_completed_count"] == 10
    assert out["detail_baseline_remaining_count"] == 918
    assert out["batch_number"] == 1
    assert ("batch_ok",) in calls
    assert not [call for call in calls if call[0] == "failed"]
    assert sum(1 for call in calls if call[0] == "next_detail") == 1
    assert any(call[0] == "state" and call[1].get("setup_status") == "preparing" and call[1].get("module3_status") == "running" for call in calls)


def test_scheduler_repairs_missing_next_detail_job_after_successful_batch(monkeypatch):
    now = datetime(2026, 6, 10, 10, 0, 0)
    started = datetime(2026, 6, 10, 8, 0, 0)
    calls = []
    sub = {
        "UserAreaID": 25,
        "SearchID": 8,
        "SearchURL": "url",
        "AreaSetupStatus": "failed",
        "AreaModule1Status": "completed",
        "AreaModule3Status": "failed",
        "AreaModule2Status": "pending",
        "BaselineStatus": "completed",
        "DetailBaselineStatus": "failed",
        "DetailBaselineStartedAt": started,
        "PriceBaselineStatus": "pending",
        "BaselineListingsCollected": 928,
    }
    _patch_not_ready_setup_scheduler(monkeypatch, now, [sub], area_state={"setup_status": "failed", "module1_status": "completed", "module3_status": "failed", "module2_status": "pending"})
    monkeypatch.setattr(monitoring_scheduler.db_layer, "get_user_area_subscription", lambda conn, user_area_id: sub)
    monkeypatch.setattr(monitoring_scheduler.db_layer, "get_detail_baseline_progress", lambda conn, subscription: {"detail_baseline_total_count": 928, "detail_baseline_completed_count": 10, "detail_baseline_remaining_count": 918})
    monkeypatch.setattr(monitoring_scheduler.db_layer, "get_latest_setup_detail_job", lambda conn, search_id, started_at=None: {"JobID": 1509, "Status": "succeeded", "LastError": ""})
    monkeypatch.setattr(monitoring_scheduler.db_layer, "upsert_area_monitoring_state", lambda conn, search_id, **kwargs: calls.append(("state", kwargs)))

    out = monitoring_scheduler.enqueue_due_monitoring_jobs(now=now)

    assert out["setup_detail_repair_enqueued_count"] == 1
    assert [job for job in out["created"] if job.get("JobType") == job_queue.JOB_TYPE_SETUP_DETAIL_BASELINE]
    assert not [job for job in out["created"] if job.get("JobType") == job_queue.JOB_TYPE_SETUP_PRICE_BASELINE]
    assert any(call[0] == "state" and call[1].get("setup_status") == "preparing" and call[1].get("module3_status") == "running" for call in calls)


def test_heartbeat_failed_jobs_hide_superseded_setup_failures():
    now = datetime(2026, 6, 10, 10, 0, 0)
    job_queue.enable_in_memory_store([
        {
            "JobID": 1504,
            "JobType": job_queue.JOB_TYPE_BASELINE_SETUP_AREA,
            "SearchID": 2,
            "UserAreaID": 20,
            "Priority": 0,
            "Status": "failed",
            "RunAfter": now,
            "AttemptCount": 3,
            "MaxAttempts": 3,
            "LockedBy": None,
            "LockedAt": None,
            "StartedAt": now,
            "FinishedAt": now,
            "LastError": "old baseline failure",
            "PayloadJson": None,
            "DedupeKey": "old",
            "CreatedAt": now,
            "UpdatedAt": now,
            "AreaActive": True,
        },
        {
            "JobID": 1508,
            "JobType": job_queue.JOB_TYPE_BASELINE_SETUP_AREA,
            "SearchID": 2,
            "UserAreaID": 20,
            "Priority": 0,
            "Status": "succeeded",
            "RunAfter": now,
            "AttemptCount": 1,
            "MaxAttempts": 3,
            "LockedBy": None,
            "LockedAt": None,
            "StartedAt": now,
            "FinishedAt": now,
            "LastError": None,
            "PayloadJson": None,
            "DedupeKey": "new",
            "CreatedAt": now,
            "UpdatedAt": now,
            "AreaActive": True,
        },
    ])

    out = job_queue.get_failed_job_summary_by_lifecycle()

    assert out["active_failed_jobs"] == []


def test_setup_detail_batches_gate_price_until_remaining_zero(monkeypatch):
    import monitoring_scheduler

    calls = []
    sub = {"UserAreaID": 7, "SearchID": 42, "SearchURL": "https://example.test/search", "DetailBaselineStatus": "running", "DetailBaselineStartedAt": datetime(2026, 6, 10), "BaselineListingsCollected": 928}

    class Conn:
        def commit(self): pass
        def close(self): pass

    completed = {"value": 0}

    def progress(conn, subscription):
        done = completed["value"]
        return {"detail_baseline_total_count": 928, "detail_baseline_completed_count": done, "detail_baseline_remaining_count": max(0, 928 - done)}

    def refresh(*args, **kwargs):
        completed["value"] = min(928, completed["value"] + 10)
        return {"processed_count": 10, "refreshed_count": 10, "failed_count": 0, "errors": [], "candidates_count": 10}

    monkeypatch.setattr(monitoring_scheduler.config, "BASELINE_DETAIL_BATCH_SIZE", 10, raising=False)
    monkeypatch.setattr(monitoring_scheduler, "_search_is_active_for_monitoring", lambda search_id: True)
    monkeypatch.setattr(monitoring_scheduler, "_load_search_subscription", lambda search_id, user_area_id=None: sub)
    monkeypatch.setattr(monitoring_scheduler, "refresh_active_listings", refresh)
    monkeypatch.setattr(monitoring_scheduler.db_layer, "connect", lambda path=None: Conn())
    monkeypatch.setattr(monitoring_scheduler.db_layer, "get_user_area_subscription", lambda conn, user_area_id: sub)
    monkeypatch.setattr(monitoring_scheduler.db_layer, "get_detail_baseline_progress", progress)
    monkeypatch.setattr(monitoring_scheduler.db_layer, "count_succeeded_setup_detail_jobs", lambda conn, search_id, started_at=None: completed["value"] // 10)
    monkeypatch.setattr(monitoring_scheduler.db_layer, "count_remaining_setup_detail_targets", lambda conn, search_id, subscription=None: max(0, 928 - completed["value"]))
    monkeypatch.setattr(monitoring_scheduler.db_layer, "get_active_listings_for_detail_refresh", lambda *a, **k: [{"listing_id": str(i)} for i in range(10)])
    monkeypatch.setattr(monitoring_scheduler.db_layer, "upsert_area_monitoring_state", lambda *a, **k: None)
    monkeypatch.setattr(monitoring_scheduler.db_layer, "mark_subscription_detail_baseline_batch_succeeded", lambda *a, **k: None)
    monkeypatch.setattr(monitoring_scheduler.db_layer, "mark_subscription_detail_baseline_completed", lambda *a, **k: calls.append(("detail_completed",)))
    monkeypatch.setattr(monitoring_scheduler.db_layer, "enqueue_setup_detail_baseline_job", lambda *a, **k: calls.append(("next_detail", k)) or {"created": True})
    monkeypatch.setattr(monitoring_scheduler.db_layer, "enqueue_setup_price_baseline_job", lambda *a, **k: calls.append(("price", k)) or {"created": True})

    first = monitoring_scheduler._run_setup_detail_batch({"JobID": 1, "SearchID": 42}, send_telegram=False)
    second = monitoring_scheduler._run_setup_detail_batch({"JobID": 2, "SearchID": 42}, send_telegram=False)
    completed["value"] = 918
    final = monitoring_scheduler._run_setup_detail_batch({"JobID": 99, "SearchID": 42}, send_telegram=False)

    assert first["detail_baseline_remaining_count"] == 918
    assert second["detail_baseline_remaining_count"] == 908
    assert final["status"] == "price_baseline_pending"
    assert sum(1 for call in calls if call[0] == "price") == 1
    assert sum(1 for call in calls if call[0] == "next_detail") == 2


def test_setup_detail_schema_migration_adds_required_columns_and_index():
    conn = MigrationConn()
    db_layer.ensure_listing_search_state_detail_refresh_column(conn)
    sql = "\n".join(conn.sql)
    for column in db_layer.SETUP_DETAIL_REQUIRED_COLUMNS:
        assert column in sql
    assert "IX_ListingSearchState_SetupDetailDue" in sql
    assert "SetupDetailNextRetryAt" in sql
    assert "LastDetailRefreshAt" in sql
    assert "ListingID" in sql


def test_setup_detail_schema_migration_is_idempotent_sql():
    conn = MigrationConn()
    db_layer.ensure_listing_search_state_detail_refresh_column(conn)
    db_layer.ensure_listing_search_state_detail_refresh_column(conn)
    sql = "\n".join(conn.sql)
    assert sql.count("COL_LENGTH('dbo.ListingSearchState', 'SetupDetailStatus') IS NULL") == 2
    assert "IF OBJECT_ID('dbo.ListingSearchState') IS NOT NULL" in sql


def test_area_monitoring_state_code_does_not_require_notification_ready_at():
    import inspect

    combined = "\n".join(
        [
            inspect.getsource(db_layer.ensure_monitoring_state_tables),
            inspect.getsource(db_layer.get_area_monitoring_state),
            inspect.getsource(db_layer.upsert_area_monitoring_state),
        ]
    ).lower()
    assert "notification_ready_at" not in combined


def test_startup_schema_ensure_runs_before_startup_sanitizers(monkeypatch):
    import telegram_bot

    calls = []

    class Conn:
        def commit(self): calls.append("commit")
        def close(self): calls.append("close")

    monkeypatch.setattr(telegram_bot, "_connect", lambda: Conn())
    monkeypatch.setattr(telegram_bot.db_layer, "ensure_runtime_monitoring_schema", lambda conn: calls.append("runtime_schema") or {"schema_ok": True, "setup_detail_schema_ok": True, "area_monitoring_schema_ok": True})
    monkeypatch.setattr(telegram_bot.db_layer, "ensure_telegram_bot_tables", lambda conn: calls.append("telegram_tables"))
    monkeypatch.setattr(telegram_bot.job_queue, "ensure_job_tables", lambda conn: calls.append("job_tables"))
    monkeypatch.setattr(telegram_bot.db_layer, "sanitize_notification_outbox", lambda conn: calls.append("sanitize") or {})
    monkeypatch.setattr(telegram_bot.job_queue, "recover_stale_running_jobs", lambda conn=None: calls.append("stale_recovery") or {})

    telegram_bot.ensure_runtime_schema()

    assert calls[:4] == ["runtime_schema", "telegram_tables", "job_tables", "sanitize"]
    assert "stale_recovery" in calls


class ProgressCursor:
    def __init__(self):
        self.description = [("JobID",), ("JobType",), ("Status",), ("CreatedAt",), ("StartedAt",), ("FinishedAt",), ("LastError",)]
    def execute(self, *args, **kwargs): return self
    def fetchall(self): return []
    def fetchone(self): return None


class ProgressConn:
    def cursor(self): return ProgressCursor()
    def commit(self): pass
    def close(self): pass


def test_print_setup_progress_includes_schema_health_after_migration(monkeypatch):
    import tools.print_setup_progress as progress_tool

    monkeypatch.setattr(progress_tool.db_layer, "connect", lambda path=None: ProgressConn())
    monkeypatch.setattr(progress_tool.db_layer, "ensure_runtime_monitoring_schema", lambda conn: {"schema_ok": True})
    monkeypatch.setattr(progress_tool.db_layer, "get_setup_detail_schema_status", lambda conn: {"setup_detail_schema_ok": True, "missing_columns": []})
    monkeypatch.setattr(progress_tool.db_layer, "get_area_monitoring_schema_status", lambda conn: {"area_monitoring_schema_ok": True, "missing_columns": []})
    monkeypatch.setattr(progress_tool.db_layer, "get_area_monitoring_state", lambda conn, search_id: {"setup_status": "preparing", "module1_status": "completed", "module3_status": "running", "module2_status": "pending", "active_listing_count": 30})
    monkeypatch.setattr(progress_tool.db_layer, "get_active_user_area_subscriptions_for_search", lambda conn, search_id: [{"SearchID": search_id, "AreaLabel": "Darlinghurst, NSW 2010", "BaselineListingsCollected": 30}])
    monkeypatch.setattr(progress_tool.db_layer, "get_detail_baseline_progress", lambda conn, sub: {"detail_baseline_total_count": 30, "detail_baseline_completed_count": 10, "detail_baseline_remaining_count": 20})
    monkeypatch.setattr(progress_tool.db_layer, "get_active_setup_pipeline_jobs", lambda conn, search_id: [])

    out = progress_tool.setup_progress(2)

    assert out["schema_ok"] is True
    assert out["setup_detail_schema_ok"] is True
    assert out["area_monitoring_schema_ok"] is True
    assert out["detail_done"] == 10
    assert out["detail_remaining"] == 20


def test_scraper_noisy_logs_are_suppressed_at_info(monkeypatch):
    import module1_list_scraper
    import module2_infer_prices
    import module3_enrich_details

    for mod in (module1_list_scraper, module2_infer_prices, module3_enrich_details):
        monkeypatch.setattr(mod.config, "SCRAPER_LOG_LEVEL", "INFO", raising=False)
        monkeypatch.setattr(mod.config, "SCRAPER_VERBOSE_PAGE_STATE", False, raising=False)
    monkeypatch.setattr(module1_list_scraper.config, "SCRAPER_VERBOSE_NETWORK", False, raising=False)

    assert module1_list_scraper._should_emit_default_log("setup_batched search_id=2") is True
    assert module1_list_scraper._should_emit_default_log("Module1 page_state=blocked html_length=851 body_text_length=0") is False
    assert module1_list_scraper._should_emit_default_log("Page network summary:") is False
    assert module2_infer_prices._should_emit_default_log("Module2 page_state=blocked html_length=851 body_text_length=0") is False
    assert module3_enrich_details._should_emit_default_log("Module3 Detail page_state=blocked html_length=851 body_text_length=0") is False


def test_reset_setup_state_tool_does_not_reference_area_notification_ready_column():
    import inspect
    import tools.reset_setup_state as reset_tool

    source = inspect.getsource(reset_tool).lower()
    assert "notification_ready_at" not in source
    assert "setupdetailstatus" in source
    assert "setup_detail_baseline" in source
