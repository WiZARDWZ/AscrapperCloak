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
