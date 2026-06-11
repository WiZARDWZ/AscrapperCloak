from __future__ import annotations

import json
import os
import socket
import uuid
from datetime import datetime, timedelta
from typing import Any

import config
import db_layer

JOB_STATUS_ACTIVE = {"pending", "paused", "queued", "running", "retry_wait", "scheduled"}
JOB_STATUS_TERMINAL = {"succeeded", "failed", "cancelled", "skipped"}


PHASE2B_JOB_STATUSES = ("queued", "running", "succeeded", "failed", "retry_wait", "cancelled", "skipped")
LEGACY_JOB_STATUS_NORMALIZATION = {
    "pending": "queued",
    "paused": "retry_wait",
    "done": "succeeded",
    "success": "succeeded",
    "error": "failed",
}


def _quoted_status_list(statuses: tuple[str, ...] = PHASE2B_JOB_STATUSES) -> str:
    return ", ".join(f"'{status}'" for status in statuses)

JOB_TYPE_SETUP_FULL_BASELINE = "setup_full_baseline"
JOB_TYPE_SETUP_DETAIL_BASELINE = "setup_detail_baseline"
JOB_TYPE_SETUP_PRICE_BASELINE = "setup_price_baseline"
JOB_TYPE_BASELINE_SETUP_AREA = "baseline_setup_area"
JOB_TYPE_ENRICH_LISTING = "enrich_listing"
JOB_TYPE_MODULE3_REFRESH_AREA = "module3_refresh_area"
JOB_TYPE_MODULE2_PRICE_REFRESH_AREA = "module2_price_refresh_area"
JOB_TYPE_MODULE1_FULL_SAFETY_SWEEP = "module1_full_safety_sweep"
JOB_TYPE_PROCESS_NEW_LISTING = "process_new_listing"
JOB_TYPE_LIGHT_CHECK_NEW_LISTINGS = "light_check_new_listings"
JOB_TYPE_DETAIL_REFRESH_EXISTING = "detail_refresh_existing"
JOB_TYPE_PRICE_REFRESH_EXISTING = "price_refresh_existing"
JOB_TYPE_PRICE_RETRY_UNKNOWNS = "price_retry_unknowns"
JOB_TYPE_LISTING_STATUS_RECHECK = "listing_status_recheck"
JOB_TYPE_DAILY_FULL_LISTING_SWEEP = "daily_full_listing_safety_sweep"
JOB_TYPE_DETAIL_REFRESH_NEW_LISTING = "detail_refresh_new_listing"
JOB_TYPE_MANUAL_CHECK_NOW = "manual_check_now"
JOB_TYPE_NOTIFICATION_DISPATCH = "notification_dispatch"
JOB_TYPE_TELEGRAM_SEND = "telegram_send"

PRIORITY_SETUP = 0
PRIORITY_MANUAL_OR_NEW_DETAIL = 10
PRIORITY_NEW_LISTING_ENRICHMENT = 15
PRIORITY_LISTING_STATUS_RECHECK = 12
PRIORITY_LIGHT_CHECK = 20
PRIORITY_DETAIL_REFRESH = 30
PRIORITY_PRICE_RETRY_UNKNOWNS = 25
PRIORITY_PRICE_REFRESH = 35
PRIORITY_DAILY_SWEEP = 45
PRIORITY_MAINTENANCE = 40

JOB_STALE_TIMEOUT_ENV_NAMES: dict[str, str] = {
    JOB_TYPE_LIGHT_CHECK_NEW_LISTINGS: "JOB_STALE_TIMEOUT_MINUTES_LIGHT_CHECK",
    JOB_TYPE_PROCESS_NEW_LISTING: "JOB_STALE_TIMEOUT_MINUTES_PROCESS_NEW_LISTING",
    JOB_TYPE_DETAIL_REFRESH_EXISTING: "JOB_STALE_TIMEOUT_MINUTES_DETAIL_REFRESH",
    JOB_TYPE_MODULE2_PRICE_REFRESH_AREA: "JOB_STALE_TIMEOUT_MINUTES_MODULE2_REFRESH",
    JOB_TYPE_MODULE1_FULL_SAFETY_SWEEP: "JOB_STALE_TIMEOUT_MINUTES_MODULE1_SWEEP",
    JOB_TYPE_BASELINE_SETUP_AREA: "JOB_STALE_TIMEOUT_MINUTES_BASELINE_SETUP",
    JOB_TYPE_SETUP_DETAIL_BASELINE: "JOB_STALE_TIMEOUT_MINUTES_SETUP_DETAIL_BASELINE",
    JOB_TYPE_SETUP_PRICE_BASELINE: "JOB_STALE_TIMEOUT_MINUTES_SETUP_PRICE_BASELINE",
    JOB_TYPE_PRICE_RETRY_UNKNOWNS: "JOB_STALE_TIMEOUT_MINUTES_PRICE_RETRY_UNKNOWNS",
    JOB_TYPE_LISTING_STATUS_RECHECK: "JOB_STALE_TIMEOUT_MINUTES_LISTING_STATUS_RECHECK",
}

JOB_STALE_TIMEOUT_DEFAULT_MINUTES: dict[str, int] = {
    JOB_TYPE_LIGHT_CHECK_NEW_LISTINGS: 45,
    JOB_TYPE_PROCESS_NEW_LISTING: 90,
    JOB_TYPE_DETAIL_REFRESH_EXISTING: 180,
    JOB_TYPE_MODULE2_PRICE_REFRESH_AREA: 300,
    JOB_TYPE_MODULE1_FULL_SAFETY_SWEEP: 120,
    JOB_TYPE_BASELINE_SETUP_AREA: 480,
    JOB_TYPE_SETUP_DETAIL_BASELINE: 120,
    JOB_TYPE_SETUP_PRICE_BASELINE: 300,
    JOB_TYPE_PRICE_RETRY_UNKNOWNS: 180,
    JOB_TYPE_LISTING_STATUS_RECHECK: 45,
}

_TEST_STORE: list[dict[str, Any]] | None = None
_TEST_NEXT_ID = 1


def enable_in_memory_store(seed: list[dict[str, Any]] | None = None) -> None:
    """Enable a deterministic in-memory queue used by tools/test_job_queue.py only."""
    global _TEST_STORE, _TEST_NEXT_ID
    _TEST_STORE = []
    _TEST_NEXT_ID = 1
    for row in seed or []:
        item = dict(row)
        item.setdefault("JobID", _TEST_NEXT_ID)
        _TEST_NEXT_ID = max(_TEST_NEXT_ID, int(item["JobID"]) + 1)
        _TEST_STORE.append(item)


def disable_in_memory_store() -> None:
    global _TEST_STORE, _TEST_NEXT_ID
    _TEST_STORE = None
    _TEST_NEXT_ID = 1


def _now() -> datetime:
    # Phase 2B queue timestamps are SQL Server-local naive datetimes.
    # The in-memory test store mirrors that with Python local naive time.
    return datetime.now()


def _connect():
    return db_layer.connect(config.DB_PATH)


def _rows_to_dicts(cur) -> list[dict[str, Any]]:
    cols = [col[0] for col in cur.description]
    return [{cols[i]: row[i] for i in range(len(cols))} for row in cur.fetchall()]


def _payload_to_json(payload: Any) -> str | None:
    if payload is None:
        return None
    if isinstance(payload, str):
        return payload
    return json.dumps(payload, ensure_ascii=False, default=str)


def default_dedupe_key(job_type: str, search_id: int | None = None, user_area_id: int | None = None, payload: Any = None) -> str | None:
    if search_id is not None:
        return f"{job_type}:search_id={int(search_id)}"
    if user_area_id is not None:
        return f"{job_type}:user_area_id={int(user_area_id)}"
    return None


JOB_REQUIRED_COLUMN_DEFINITIONS: dict[str, str] = {
    "JobType": "NVARCHAR(100) NOT NULL CONSTRAINT DF_Job_JobType DEFAULT ('unknown')",
    "SearchID": "INT NULL",
    "UserAreaID": "INT NULL",
    "Priority": "INT NOT NULL CONSTRAINT DF_Job_Priority DEFAULT (30)",
    "Status": "NVARCHAR(50) NOT NULL CONSTRAINT DF_Job_Status DEFAULT ('queued')",
    "RunAfter": "DATETIME2 NOT NULL CONSTRAINT DF_Job_RunAfter DEFAULT (SYSDATETIME())",
    "AttemptCount": "INT NOT NULL CONSTRAINT DF_Job_AttemptCount DEFAULT (0)",
    "MaxAttempts": "INT NOT NULL CONSTRAINT DF_Job_MaxAttempts DEFAULT (3)",
    "LockedBy": "NVARCHAR(200) NULL",
    "LockedAt": "DATETIME2 NULL",
    "StartedAt": "DATETIME2 NULL",
    "FinishedAt": "DATETIME2 NULL",
    "LastError": "NVARCHAR(MAX) NULL",
    "PayloadJson": "NVARCHAR(MAX) NULL",
    "DedupeKey": "NVARCHAR(300) NULL",
    "CreatedAt": "DATETIME2 NOT NULL CONSTRAINT DF_Job_CreatedAt DEFAULT (SYSDATETIME())",
    "UpdatedAt": "DATETIME2 NOT NULL CONSTRAINT DF_Job_UpdatedAt DEFAULT (SYSDATETIME())",
}

JOB_REQUIRED_COLUMNS = {"JobID", *JOB_REQUIRED_COLUMN_DEFINITIONS.keys()}
JOB_DEFAULTED_EXISTING_COLUMNS = {
    "JobType": ("NVARCHAR(100)", "'unknown'"),
    "Status": ("NVARCHAR(50)", "'queued'"),
    "Priority": ("INT", "30"),
    "RunAfter": ("DATETIME2", "SYSDATETIME()"),
    "AttemptCount": ("INT", "0"),
    "MaxAttempts": ("INT", "3"),
    "CreatedAt": ("DATETIME2", "SYSDATETIME()"),
    "UpdatedAt": ("DATETIME2", "SYSDATETIME()"),
}


def _add_job_column_if_missing(conn, column_name: str, column_definition: str) -> None:
    """Add one dbo.Job column using a full SQL Server column specification.

    Some customer DBs already have a partial dbo.Job table. The migration must use
    the full type/nullability/default clause, not only the column name. If a named
    default constraint already exists from a previous partial migration attempt, the
    fallback branch adds the column with an unnamed default rather than crashing.
    """
    constraint_name = f"DF_Job_{column_name}"
    if f"CONSTRAINT {constraint_name}" in column_definition:
        unnamed_definition = column_definition.replace(f" CONSTRAINT {constraint_name}", "")
        sql = f"""
        IF OBJECT_ID('dbo.Job') IS NOT NULL
        AND COL_LENGTH('dbo.Job', '{column_name}') IS NULL
        BEGIN
            IF OBJECT_ID('dbo.{constraint_name}', 'D') IS NULL
                ALTER TABLE dbo.Job ADD {column_name} {column_definition};
            ELSE
                ALTER TABLE dbo.Job ADD {column_name} {unnamed_definition};
        END
        """
    else:
        sql = f"""
        IF OBJECT_ID('dbo.Job') IS NOT NULL
        AND COL_LENGTH('dbo.Job', '{column_name}') IS NULL
        ALTER TABLE dbo.Job ADD {column_name} {column_definition};
        """
    db_layer._execute_ddl_safely(conn, sql, description=f"add dbo.Job.{column_name}", required=False)


def _normalize_existing_defaulted_job_column(conn, column_name: str, sql_type: str, default_expression: str) -> None:
    """Backfill NULLs and add a default to existing dbo.Job columns safely."""
    constraint_name = f"DF_Job_{column_name}"
    db_layer._execute_ddl_safely(conn, f"""
    IF OBJECT_ID('dbo.Job') IS NOT NULL
    AND COL_LENGTH('dbo.Job', '{column_name}') IS NOT NULL
    BEGIN
        UPDATE dbo.Job SET {column_name}=COALESCE({column_name}, {default_expression}) WHERE {column_name} IS NULL;

        IF EXISTS (
            SELECT 1
            FROM sys.columns
            WHERE object_id=OBJECT_ID('dbo.Job') AND name='{column_name}' AND is_nullable=1
        )
        AND NOT EXISTS (SELECT 1 FROM dbo.Job WHERE {column_name} IS NULL)
            ALTER TABLE dbo.Job ALTER COLUMN {column_name} {sql_type} NOT NULL;

        IF NOT EXISTS (
            SELECT 1
            FROM sys.default_constraints dc
            JOIN sys.columns c ON c.object_id=dc.parent_object_id AND c.column_id=dc.parent_column_id
            WHERE dc.parent_object_id=OBJECT_ID('dbo.Job') AND c.name='{column_name}'
        )
        BEGIN
            IF OBJECT_ID('dbo.{constraint_name}', 'D') IS NULL
                ALTER TABLE dbo.Job ADD CONSTRAINT {constraint_name} DEFAULT ({default_expression}) FOR {column_name};
            ELSE
                ALTER TABLE dbo.Job ADD DEFAULT ({default_expression}) FOR {column_name};
        END
    END
    """, description=f"normalize dbo.Job.{column_name}", required=False)


def _ensure_job_status_default(conn) -> None:
    """Ensure dbo.Job.Status defaults to the Phase 2B queued status.

    Existing deployments may have a default for older status values such as
    'pending'. Drop only incompatible Status defaults, then add the queued default
    idempotently. Use an unnamed fallback if DF_Job_Status already exists elsewhere.
    """
    db_layer._execute_ddl_safely(conn, """
    IF OBJECT_ID('dbo.Job') IS NOT NULL
    AND COL_LENGTH('dbo.Job', 'Status') IS NOT NULL
    BEGIN
        DECLARE @default_name SYSNAME;
        SELECT @default_name = dc.name
        FROM sys.default_constraints dc
        JOIN sys.columns c ON c.object_id=dc.parent_object_id AND c.column_id=dc.parent_column_id
        WHERE dc.parent_object_id=OBJECT_ID('dbo.Job')
          AND c.name='Status'
          AND dc.definition NOT LIKE '%queued%';

        IF @default_name IS NOT NULL
        BEGIN
            DECLARE @default_drop_sql NVARCHAR(MAX) = N'ALTER TABLE dbo.Job DROP CONSTRAINT ' + QUOTENAME(@default_name);
            EXEC sp_executesql @default_drop_sql;
        END

        IF NOT EXISTS (
            SELECT 1
            FROM sys.default_constraints dc
            JOIN sys.columns c ON c.object_id=dc.parent_object_id AND c.column_id=dc.parent_column_id
            WHERE dc.parent_object_id=OBJECT_ID('dbo.Job') AND c.name='Status'
        )
        BEGIN
            IF OBJECT_ID('dbo.DF_Job_Status', 'D') IS NULL
                ALTER TABLE dbo.Job ADD CONSTRAINT DF_Job_Status DEFAULT ('queued') FOR Status;
            ELSE
                ALTER TABLE dbo.Job ADD DEFAULT ('queued') FOR Status;
        END
    END
    """, description="ensure dbo.Job.Status default", required=False)


def _ensure_job_status_values_and_constraint(conn) -> None:
    """Normalize legacy Job statuses and enforce the Phase 2B status domain.

    Unknown existing statuses are mapped to failed because an unknown queued/running
    state is unsafe to execute automatically. The old value is preserved in LastError
    for operator auditability before CK_Job_Status is recreated.
    """
    allowed = _quoted_status_list()
    stale_check_predicate = " OR ".join(f"cc.definition NOT LIKE '%{status}%'" for status in PHASE2B_JOB_STATUSES)
    db_layer._execute_ddl_safely(conn, f"""
    IF OBJECT_ID('dbo.Job') IS NOT NULL
    AND COL_LENGTH('dbo.Job', 'Status') IS NOT NULL
    BEGIN
        DECLARE @drop_sql NVARCHAR(MAX) = N'';
        SELECT @drop_sql = @drop_sql + N'ALTER TABLE dbo.Job DROP CONSTRAINT ' + QUOTENAME(cc.name) + N';'
        FROM sys.check_constraints cc
        WHERE cc.parent_object_id=OBJECT_ID('dbo.Job')
          AND cc.definition LIKE '%Status%'
          AND ({stale_check_predicate});
        IF @drop_sql <> N''
            EXEC sp_executesql @drop_sql;

        UPDATE dbo.Job SET Status='queued' WHERE Status IS NULL;

        UPDATE dbo.Job
        SET Status = CASE LOWER(LTRIM(RTRIM(Status)))
            WHEN 'pending' THEN 'queued'
            WHEN 'paused' THEN 'retry_wait'
            WHEN 'done' THEN 'succeeded'
            WHEN 'success' THEN 'succeeded'
            WHEN 'error' THEN 'failed'
            WHEN 'queued' THEN 'queued'
            WHEN 'running' THEN 'running'
            WHEN 'succeeded' THEN 'succeeded'
            WHEN 'failed' THEN 'failed'
            WHEN 'retry_wait' THEN 'retry_wait'
            WHEN 'cancelled' THEN 'cancelled'
            WHEN 'skipped' THEN 'skipped'
            ELSE Status
        END
        WHERE Status IS NOT NULL;

        IF COL_LENGTH('dbo.Job', 'LastError') IS NOT NULL
        BEGIN
            UPDATE dbo.Job
            SET LastError = CONCAT('Unknown legacy Job.Status normalized to failed: ', Status,
                                   CASE WHEN LastError IS NULL OR LTRIM(RTRIM(LastError))='' THEN '' ELSE CONCAT(CHAR(10), LastError) END),
                Status = 'failed'
            WHERE Status IS NOT NULL AND Status NOT IN ({allowed});
        END
        ELSE
        BEGIN
            UPDATE dbo.Job
            SET Status = 'failed'
            WHERE Status IS NOT NULL AND Status NOT IN ({allowed});
        END

        IF NOT EXISTS (
            SELECT 1
            FROM sys.check_constraints cc
            WHERE cc.parent_object_id=OBJECT_ID('dbo.Job')
              AND cc.name='CK_Job_Status'
        )
            ALTER TABLE dbo.Job WITH CHECK ADD CONSTRAINT CK_Job_Status
            CHECK (Status IN ({allowed}));
    END
    """, description="ensure dbo.Job.Status values and CK_Job_Status", required=False)


def _all_job_columns_exist_condition(columns: tuple[str, ...]) -> str:
    return "\n        AND ".join(f"COL_LENGTH('dbo.Job', '{column}') IS NOT NULL" for column in columns)


def ensure_job_tables(conn=None) -> None:
    """Create/upgrade dbo.Job and active-job dedupe index idempotently."""
    if _TEST_STORE is not None:
        return
    owns_conn = conn is None
    conn = conn or _connect()
    try:
        db_layer._execute_ddl_safely(conn, """
        IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA='dbo' AND TABLE_NAME='Job')
        CREATE TABLE dbo.Job (
            JobID INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_Job PRIMARY KEY,
            JobType NVARCHAR(100) NOT NULL,
            SearchID INT NULL,
            UserAreaID INT NULL,
            Priority INT NOT NULL CONSTRAINT DF_Job_Priority DEFAULT (30),
            Status NVARCHAR(50) NOT NULL CONSTRAINT DF_Job_Status DEFAULT ('queued'),
            RunAfter DATETIME2 NOT NULL CONSTRAINT DF_Job_RunAfter DEFAULT (SYSDATETIME()),
            AttemptCount INT NOT NULL CONSTRAINT DF_Job_AttemptCount DEFAULT (0),
            MaxAttempts INT NOT NULL CONSTRAINT DF_Job_MaxAttempts DEFAULT (3),
            LockedBy NVARCHAR(200) NULL,
            LockedAt DATETIME2 NULL,
            StartedAt DATETIME2 NULL,
            FinishedAt DATETIME2 NULL,
            LastError NVARCHAR(MAX) NULL,
            PayloadJson NVARCHAR(MAX) NULL,
            DedupeKey NVARCHAR(300) NULL,
            CreatedAt DATETIME2 NOT NULL CONSTRAINT DF_Job_CreatedAt DEFAULT (SYSDATETIME()),
            UpdatedAt DATETIME2 NOT NULL CONSTRAINT DF_Job_UpdatedAt DEFAULT (SYSDATETIME())
        )
        """, description="create dbo.Job", required=False)
        for column_name, definition in JOB_REQUIRED_COLUMN_DEFINITIONS.items():
            _add_job_column_if_missing(conn, column_name, definition)
        for column_name, (sql_type, default_expression) in JOB_DEFAULTED_EXISTING_COLUMNS.items():
            _normalize_existing_defaulted_job_column(conn, column_name, sql_type, default_expression)
        _ensure_job_status_default(conn)
        _ensure_job_status_values_and_constraint(conn)
        db_layer._execute_ddl_safely(conn, """
        IF OBJECT_ID('dbo.Job') IS NOT NULL
        AND COL_LENGTH('dbo.Job', 'RunAfter') IS NOT NULL
        UPDATE dbo.Job
        SET RunAfter=SYSDATETIME(), LockedAt=NULL, LockedBy=NULL, UpdatedAt=SYSDATETIME()
        WHERE Status='retry_wait' AND RunAfter IS NULL
        """, description="repair dbo.Job retry_wait rows with missing RunAfter", required=False)
        dedupe_index_columns = ("DedupeKey", "Status")
        db_layer._execute_ddl_safely(conn, f"""
        IF OBJECT_ID('dbo.Job') IS NOT NULL
        AND {_all_job_columns_exist_condition(dedupe_index_columns)}
        AND NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='UX_Job_Active_DedupeKey' AND object_id=OBJECT_ID('dbo.Job'))
        CREATE UNIQUE INDEX UX_Job_Active_DedupeKey
        ON dbo.Job(DedupeKey)
        WHERE DedupeKey IS NOT NULL AND Status IN ('queued','running','retry_wait')
        """, description="create UX_Job_Active_DedupeKey", required=False)
        due_index_columns = ("Status", "RunAfter", "Priority", "CreatedAt", "JobType", "SearchID", "UserAreaID", "DedupeKey")
        db_layer._execute_ddl_safely(conn, f"""
        IF OBJECT_ID('dbo.Job') IS NOT NULL
        AND {_all_job_columns_exist_condition(due_index_columns)}
        AND NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_Job_Due' AND object_id=OBJECT_ID('dbo.Job'))
        CREATE INDEX IX_Job_Due ON dbo.Job(Status, RunAfter, Priority, CreatedAt) INCLUDE (JobType, SearchID, UserAreaID, DedupeKey)
        """, description="create IX_Job_Due", required=False)
        conn.commit()
    finally:
        if owns_conn:
            conn.close()

def _insert_memory_job(job_type, search_id, user_area_id, priority, run_after, payload, dedupe_key, max_attempts) -> dict[str, Any]:
    global _TEST_NEXT_ID
    now = _now()
    row = {
        "JobID": _TEST_NEXT_ID,
        "JobType": job_type,
        "SearchID": search_id,
        "UserAreaID": user_area_id,
        "Priority": int(priority),
        "Status": "queued",
        "RunAfter": run_after or now,
        "AttemptCount": 0,
        "MaxAttempts": int(max_attempts),
        "LockedBy": None,
        "LockedAt": None,
        "StartedAt": None,
        "FinishedAt": None,
        "LastError": None,
        "PayloadJson": _payload_to_json(payload),
        "DedupeKey": dedupe_key,
        "CreatedAt": now,
        "UpdatedAt": now,
        "created": True,
    }
    _TEST_NEXT_ID += 1
    _TEST_STORE.append(row)
    return dict(row)


def enqueue_job(job_type: str, search_id: int | None = None, user_area_id: int | None = None, priority: int = PRIORITY_DETAIL_REFRESH, run_after=None, payload=None, dedupe_key: str | None = None, max_attempts: int = 3) -> dict[str, Any]:
    ensure_job_tables()
    dedupe_key = dedupe_key or default_dedupe_key(job_type, search_id, user_area_id, payload)
    if _TEST_STORE is not None:
        return _insert_memory_job(job_type, search_id, user_area_id, priority, run_after or _now(), payload, dedupe_key, max_attempts)
    conn = _connect()
    try:
        cur = conn.cursor()
        run_after_sql = "SYSDATETIME()" if run_after is None else "?"
        params = [job_type, search_id, user_area_id, int(priority)]
        if run_after is not None:
            params.append(run_after)
        params.extend([int(max_attempts), _payload_to_json(payload), dedupe_key])
        cur.execute(f"""
            INSERT INTO dbo.Job(JobType, SearchID, UserAreaID, Priority, Status, RunAfter, MaxAttempts, PayloadJson, DedupeKey)
            OUTPUT INSERTED.JobID, INSERTED.JobType, INSERTED.SearchID, INSERTED.UserAreaID, INSERTED.Priority, INSERTED.Status,
                   INSERTED.RunAfter, INSERTED.AttemptCount, INSERTED.MaxAttempts, INSERTED.LockedBy, INSERTED.LockedAt,
                   INSERTED.StartedAt, INSERTED.FinishedAt, INSERTED.LastError, INSERTED.PayloadJson, INSERTED.DedupeKey,
                   INSERTED.CreatedAt, INSERTED.UpdatedAt
            VALUES (?, ?, ?, ?, 'queued', {run_after_sql}, ?, ?, ?)
        """, *params)
        row = _rows_to_dicts(cur)[0]
        conn.commit()
        row["created"] = True
        return row
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def enqueue_job_once(job_type: str, search_id: int | None = None, user_area_id: int | None = None, priority: int = PRIORITY_DETAIL_REFRESH, run_after=None, payload=None, dedupe_key: str | None = None, max_attempts: int = 3) -> dict[str, Any]:
    ensure_job_tables()
    dedupe_key = dedupe_key or default_dedupe_key(job_type, search_id, user_area_id, payload)
    if _TEST_STORE is not None:
        run_after = run_after or _now()
        if dedupe_key:
            for row in _TEST_STORE:
                if row.get("DedupeKey") == dedupe_key and row.get("Status") in JOB_STATUS_ACTIVE:
                    existing = dict(row)
                    existing["created"] = False
                    existing["duplicate"] = True
                    return existing
        return _insert_memory_job(job_type, search_id, user_area_id, priority, run_after, payload, dedupe_key, max_attempts)
    conn = _connect()
    try:
        cur = conn.cursor()
        if dedupe_key:
            cur.execute("""
                SELECT TOP 1 JobID, JobType, SearchID, UserAreaID, Priority, Status, RunAfter, AttemptCount, MaxAttempts,
                       LockedBy, LockedAt, StartedAt, FinishedAt, LastError, PayloadJson, DedupeKey, CreatedAt, UpdatedAt
                FROM dbo.Job WITH (UPDLOCK, HOLDLOCK)
                WHERE DedupeKey=? AND Status IN ('pending','paused','queued','running','retry_wait','scheduled')
                ORDER BY CreatedAt ASC
            """, dedupe_key)
            rows = _rows_to_dicts(cur)
            if rows:
                conn.commit()
                rows[0]["created"] = False
                rows[0]["duplicate"] = True
                return rows[0]
        run_after_sql = "SYSDATETIME()" if run_after is None else "?"
        params = [job_type, search_id, user_area_id, int(priority)]
        if run_after is not None:
            params.append(run_after)
        params.extend([int(max_attempts), _payload_to_json(payload), dedupe_key])
        cur.execute(f"""
            INSERT INTO dbo.Job(JobType, SearchID, UserAreaID, Priority, Status, RunAfter, MaxAttempts, PayloadJson, DedupeKey)
            OUTPUT INSERTED.JobID, INSERTED.JobType, INSERTED.SearchID, INSERTED.UserAreaID, INSERTED.Priority, INSERTED.Status,
                   INSERTED.RunAfter, INSERTED.AttemptCount, INSERTED.MaxAttempts, INSERTED.LockedBy, INSERTED.LockedAt,
                   INSERTED.StartedAt, INSERTED.FinishedAt, INSERTED.LastError, INSERTED.PayloadJson, INSERTED.DedupeKey,
                   INSERTED.CreatedAt, INSERTED.UpdatedAt
            VALUES (?, ?, ?, ?, 'queued', {run_after_sql}, ?, ?, ?)
        """, *params)
        row = _rows_to_dicts(cur)[0]
        conn.commit()
        row["created"] = True
        return row
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def claim_next_job(worker_id: str | None = None) -> dict[str, Any] | None:
    ensure_job_tables()
    recover_stale_running_jobs()
    worker_id = (worker_id or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}")[:200]
    now = _now()
    if _TEST_STORE is not None:
        for row in _TEST_STORE:
            if row["Status"] == "retry_wait" and row.get("RunAfter") is not None and row["RunAfter"] <= now:
                row.update({"Status": "queued", "LockedBy": None, "LockedAt": None, "UpdatedAt": now})
        due = [r for r in _TEST_STORE if r["Status"] == "queued" and r["RunAfter"] <= now]
        if not due:
            return None
        due.sort(key=lambda r: (int(r["Priority"]), r["RunAfter"], r["CreatedAt"], int(r["JobID"])))
        row = due[0]
        row.update({"Status": "running", "LockedBy": worker_id, "LockedAt": now, "StartedAt": now, "UpdatedAt": now})
        return dict(row)
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("""
        UPDATE dbo.Job
            SET Status='queued', LockedBy=NULL, LockedAt=NULL, UpdatedAt=SYSDATETIME()
            WHERE Status='retry_wait' AND RunAfter <= SYSDATETIME()
        """)
        cur.execute("""
            ;WITH next_job AS (
                SELECT TOP (1) *
                FROM dbo.Job WITH (UPDLOCK, READPAST, ROWLOCK)
                WHERE Status='queued' AND RunAfter <= SYSDATETIME()
                ORDER BY Priority ASC, RunAfter ASC, CreatedAt ASC, JobID ASC
            )
            UPDATE next_job
            SET Status='running', LockedBy=?, LockedAt=SYSDATETIME(), StartedAt=COALESCE(StartedAt, SYSDATETIME()), UpdatedAt=SYSDATETIME()
            OUTPUT INSERTED.JobID, INSERTED.JobType, INSERTED.SearchID, INSERTED.UserAreaID, INSERTED.Priority, INSERTED.Status,
                   INSERTED.RunAfter, INSERTED.AttemptCount, INSERTED.MaxAttempts, INSERTED.LockedBy, INSERTED.LockedAt,
                   INSERTED.StartedAt, INSERTED.FinishedAt, INSERTED.LastError, INSERTED.PayloadJson, INSERTED.DedupeKey,
                   INSERTED.CreatedAt, INSERTED.UpdatedAt
        """, worker_id)
        rows = _rows_to_dicts(cur)
        conn.commit()
        return rows[0] if rows else None
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def mark_job_succeeded(job_id: int, result_summary: Any = None) -> dict[str, Any]:
    ensure_job_tables()
    _payload_to_json(result_summary)  # Validate serializability for callers; dbo.Job has no result-summary column yet.
    now = _now()
    if _TEST_STORE is not None:
        row = next(r for r in _TEST_STORE if int(r["JobID"]) == int(job_id))
        row.update({"Status": "succeeded", "FinishedAt": now, "LastError": None, "UpdatedAt": now})
        return dict(row)
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE dbo.Job
            SET Status='succeeded', FinishedAt=SYSDATETIME(), LastError=NULL, UpdatedAt=SYSDATETIME()
            WHERE JobID=?
        """, int(job_id))
        conn.commit()
        return {"job_id": int(job_id), "status": "succeeded"}
    finally:
        conn.close()


def mark_job_failed(job_id: int, error: Any, retryable: bool = True, retry_after_seconds: int | None = None) -> dict[str, Any]:
    ensure_job_tables()
    error_text = config.mask_sensitive_text(error)
    now = _now()
    retry_after_seconds = int(retry_after_seconds if retry_after_seconds is not None else 300)
    if _TEST_STORE is not None:
        row = next(r for r in _TEST_STORE if int(r["JobID"]) == int(job_id))
        row["AttemptCount"] = int(row.get("AttemptCount") or 0) + 1
        if retryable and row["AttemptCount"] < int(row.get("MaxAttempts") or 3):
            row.update({"Status": "retry_wait", "RunAfter": now + timedelta(seconds=retry_after_seconds), "LockedAt": None, "LockedBy": None, "FinishedAt": now, "LastError": error_text, "UpdatedAt": now})
        else:
            row.update({"Status": "failed", "LockedAt": None, "LockedBy": None, "FinishedAt": now, "LastError": error_text, "UpdatedAt": now})
        return dict(row)
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE dbo.Job
            SET AttemptCount=AttemptCount+1,
                Status=CASE WHEN ?=1 AND AttemptCount+1 < MaxAttempts THEN 'retry_wait' ELSE 'failed' END,
                RunAfter=CASE WHEN ?=1 AND AttemptCount+1 < MaxAttempts THEN DATEADD(second, ?, SYSDATETIME()) ELSE RunAfter END,
                LockedAt=NULL,
                LockedBy=NULL,
                FinishedAt=SYSDATETIME(), LastError=?, UpdatedAt=SYSDATETIME()
            WHERE JobID=?
        """, 1 if retryable else 0, 1 if retryable else 0, retry_after_seconds, error_text, int(job_id))
        conn.commit()
        return {"job_id": int(job_id), "status": "retry_wait_or_failed", "retryable": bool(retryable)}
    finally:
        conn.close()


def mark_job_retry_wait(job_id: int, error: Any, retry_after_seconds: int | None = None) -> dict[str, Any]:
    """Put a job back into retry_wait without consuming an attempt.

    Used for source-site temporary blocks/rate limits where retrying too soon is
    harmful and treating the job as permanently failed is misleading.
    """
    ensure_job_tables()
    error_text = config.mask_sensitive_text(error)
    now = _now()
    retry_after_seconds = int(retry_after_seconds if retry_after_seconds is not None else 300)
    if _TEST_STORE is not None:
        row = next(r for r in _TEST_STORE if int(r["JobID"]) == int(job_id))
        row.update({"Status": "retry_wait", "RunAfter": now + timedelta(seconds=retry_after_seconds), "LockedAt": None, "LockedBy": None, "FinishedAt": now, "LastError": error_text, "UpdatedAt": now})
        return dict(row)
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE dbo.Job
            SET Status='retry_wait',
                RunAfter=DATEADD(second, ?, SYSDATETIME()),
                LockedAt=NULL,
                LockedBy=NULL,
                FinishedAt=SYSDATETIME(),
                LastError=?,
                UpdatedAt=SYSDATETIME()
            WHERE JobID=?
        """, retry_after_seconds, error_text, int(job_id))
        conn.commit()
        return {"job_id": int(job_id), "status": "retry_wait", "retry_after_seconds": retry_after_seconds}
    finally:
        conn.close()


def mark_job_cancelled(job_id: int, reason: Any = "cancelled") -> dict[str, Any]:
    ensure_job_tables()
    reason_text = config.mask_sensitive_text(reason)
    now = _now()
    if _TEST_STORE is not None:
        row = next(r for r in _TEST_STORE if int(r["JobID"]) == int(job_id))
        row.update({"Status": "cancelled", "FinishedAt": now, "LastError": reason_text, "UpdatedAt": now})
        return dict(row)
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE dbo.Job
            SET Status='cancelled',
                FinishedAt=COALESCE(FinishedAt, SYSDATETIME()),
                LastError=?,
                UpdatedAt=SYSDATETIME()
            WHERE JobID=?
        """, reason_text, int(job_id))
        conn.commit()
        return {"job_id": int(job_id), "status": "cancelled"}
    finally:
        conn.close()


def cancel_jobs_for_search(search_id: int, reason: Any = "cancelled because area has no active subscribers", conn=None) -> dict[str, Any]:
    reason_text = config.mask_sensitive_text(reason)
    active_cancel_statuses = {"pending", "paused", "queued", "retry_wait", "scheduled"}
    if _TEST_STORE is not None:
        count = 0
        now = _now()
        for row in _TEST_STORE:
            if int(row.get("SearchID") or 0) == int(search_id) and str(row.get("Status") or "").lower() in active_cancel_statuses:
                row.update({"Status": "cancelled", "FinishedAt": now, "LastError": reason_text, "UpdatedAt": now})
                count += 1
        return {"search_id": int(search_id), "cancelled_count": count}
    owns_conn = conn is None
    if owns_conn:
        ensure_job_tables()
    conn = conn or _connect()
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE dbo.Job
            SET Status='cancelled',
                FinishedAt=COALESCE(FinishedAt, SYSDATETIME()),
                LastError=?,
                UpdatedAt=SYSDATETIME()
            WHERE SearchID=?
              AND Status IN ('pending','paused','queued','retry_wait','scheduled')
        """, reason_text, int(search_id))
        count = int(cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else 0)
        if owns_conn:
            conn.commit()
        return {"search_id": int(search_id), "cancelled_count": count}
    except Exception:
        if owns_conn:
            conn.rollback()
        raise
    finally:
        if owns_conn:
            conn.close()



def stale_timeout_minutes_for_job_type(job_type: str | None) -> int:
    """Return the job-type-aware stale-running timeout in minutes.

    Environment-backed values live in config.py so production can tune recovery
    without code changes. Unknown job types use JOB_STALE_TIMEOUT_MINUTES_DEFAULT.
    """
    normalized = str(job_type or "").strip()
    env_attr = JOB_STALE_TIMEOUT_ENV_NAMES.get(normalized)
    fallback = JOB_STALE_TIMEOUT_DEFAULT_MINUTES.get(normalized, int(getattr(config, "JOB_STALE_TIMEOUT_MINUTES_DEFAULT", 120)))
    if env_attr:
        return int(getattr(config, env_attr, fallback))
    return int(getattr(config, "JOB_STALE_TIMEOUT_MINUTES_DEFAULT", fallback))


def _job_heartbeat_at(row: dict[str, Any]):
    values = [row.get("UpdatedAt"), row.get("LockedAt"), row.get("StartedAt")]
    values = [value for value in values if value is not None]
    return max(values) if values else None


def recover_stale_running_jobs(conn=None, now: datetime | None = None) -> dict[str, Any]:
    """Requeue or fail stale running jobs so dead worker locks cannot block areas.

    A running job is considered stale only when its latest heartbeat timestamp
    (UpdatedAt preferred via max with LockedAt/StartedAt) is older than the
    job-type-specific timeout. Jobs with attempts left go back to queued with
    cleared locks; exhausted jobs move to failed and no longer block dedupe.
    """
    ensure_job_tables(conn)
    now = now or _now()
    result: dict[str, Any] = {
        "recovered_count": 0,
        "failed_count": 0,
        "stale_job_ids": [],
        "recovered_job_types": [],
        "failed_job_types": [],
    }
    recovery_note_prefix = "recovered stale running job after worker timeout"
    failed_note_prefix = "failed after stale running timeout and max attempts reached"

    if _TEST_STORE is not None:
        for row in _TEST_STORE:
            if str(row.get("Status") or "").lower() != "running":
                continue
            heartbeat_at = _job_heartbeat_at(row)
            timeout_minutes = stale_timeout_minutes_for_job_type(row.get("JobType"))
            if heartbeat_at is None or heartbeat_at > now - timedelta(minutes=timeout_minutes):
                continue
            job_id = int(row["JobID"])
            job_type = str(row.get("JobType") or "unknown")
            result["stale_job_ids"].append(job_id)
            attempts = int(row.get("AttemptCount") or 0)
            max_attempts = int(row.get("MaxAttempts") or 3)
            if attempts < max_attempts:
                row.update({
                    "Status": "queued",
                    "RunAfter": now,
                    "LockedAt": None,
                    "LockedBy": None,
                    "StartedAt": None,
                    "FinishedAt": None,
                    "LastError": f"{recovery_note_prefix}: job_type={job_type}, timeout_minutes={timeout_minutes}",
                    "UpdatedAt": now,
                })
                result["recovered_count"] += 1
                result["recovered_job_types"].append(job_type)
            else:
                row.update({
                    "Status": "failed",
                    "FinishedAt": now,
                    "LockedAt": None,
                    "LockedBy": None,
                    "LastError": f"{failed_note_prefix}: job_type={job_type}, timeout_minutes={timeout_minutes}",
                    "UpdatedAt": now,
                })
                result["failed_count"] += 1
                result["failed_job_types"].append(job_type)
        result["recovered_job_types"] = sorted(set(result["recovered_job_types"]))
        result["failed_job_types"] = sorted(set(result["failed_job_types"]))
        return result

    owns_conn = conn is None
    conn = conn or _connect()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT JobID, JobType, AttemptCount, MaxAttempts, LockedAt, StartedAt, UpdatedAt
            FROM dbo.Job WITH (UPDLOCK, HOLDLOCK)
            WHERE Status='running'
        """)
        running_rows = _rows_to_dicts(cur)
        for row in running_rows:
            heartbeat_at = _job_heartbeat_at(row)
            timeout_minutes = stale_timeout_minutes_for_job_type(row.get("JobType"))
            if heartbeat_at is None or heartbeat_at > now - timedelta(minutes=timeout_minutes):
                continue
            job_id = int(row["JobID"])
            job_type = str(row.get("JobType") or "unknown")
            attempts = int(row.get("AttemptCount") or 0)
            max_attempts = int(row.get("MaxAttempts") or 3)
            result["stale_job_ids"].append(job_id)
            if attempts < max_attempts:
                cur.execute("""
                    UPDATE dbo.Job
                    SET Status='queued',
                        RunAfter=SYSDATETIME(),
                        LockedAt=NULL,
                        LockedBy=NULL,
                        StartedAt=NULL,
                        FinishedAt=NULL,
                        LastError=?,
                        UpdatedAt=SYSDATETIME()
                    WHERE JobID=? AND Status='running'
                """, f"{recovery_note_prefix}: job_type={job_type}, timeout_minutes={timeout_minutes}", job_id)
                result["recovered_count"] += int(cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0)
                result["recovered_job_types"].append(job_type)
            else:
                cur.execute("""
                    UPDATE dbo.Job
                    SET Status='failed',
                        FinishedAt=SYSDATETIME(),
                        LastError=?,
                        LockedAt=NULL,
                        LockedBy=NULL,
                        UpdatedAt=SYSDATETIME()
                    WHERE JobID=? AND Status='running'
                """, f"{failed_note_prefix}: job_type={job_type}, timeout_minutes={timeout_minutes}", job_id)
                result["failed_count"] += int(cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0)
                result["failed_job_types"].append(job_type)
        conn.commit()
        result["recovered_job_types"] = sorted(set(result["recovered_job_types"]))
        result["failed_job_types"] = sorted(set(result["failed_job_types"]))
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        if owns_conn:
            conn.close()


# Backward-compatible aliases requested by the production incident runbook.
requeue_stale_running_jobs = recover_stale_running_jobs
cleanup_stale_job_locks = recover_stale_running_jobs


def touch_job_heartbeat(job_id: int) -> dict[str, Any]:
    """Update UpdatedAt/LockedAt for an actively running job without schema changes."""
    ensure_job_tables()
    now = _now()
    if _TEST_STORE is not None:
        row = next(r for r in _TEST_STORE if int(r["JobID"]) == int(job_id))
        if str(row.get("Status") or "").lower() == "running":
            row.update({"UpdatedAt": now, "LockedAt": now})
        return dict(row)
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE dbo.Job
            SET UpdatedAt=SYSDATETIME(), LockedAt=SYSDATETIME()
            WHERE JobID=? AND Status='running'
        """, int(job_id))
        conn.commit()
        return {"job_id": int(job_id), "heartbeat_updated": int(cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0)}
    finally:
        conn.close()


def requeue_running_job(job_id: int, reason: Any = "job interrupted before completion", retry_after_seconds: int = 0) -> dict[str, Any]:
    """Safely release a running job without consuming an attempt."""
    ensure_job_tables()
    reason_text = config.mask_sensitive_text(reason)
    now = _now()
    if _TEST_STORE is not None:
        row = next(r for r in _TEST_STORE if int(r["JobID"]) == int(job_id))
        row.update({"Status": "queued", "RunAfter": now + timedelta(seconds=int(retry_after_seconds)), "LockedAt": None, "LockedBy": None, "StartedAt": None, "FinishedAt": None, "LastError": reason_text, "UpdatedAt": now})
        return dict(row)
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE dbo.Job
            SET Status='queued',
                RunAfter=DATEADD(second, ?, SYSDATETIME()),
                LockedAt=NULL,
                LockedBy=NULL,
                StartedAt=NULL,
                FinishedAt=NULL,
                LastError=?,
                UpdatedAt=SYSDATETIME()
            WHERE JobID=? AND Status='running'
        """, int(retry_after_seconds), reason_text, int(job_id))
        conn.commit()
        return {"job_id": int(job_id), "status": "queued", "released_count": int(cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0)}
    finally:
        conn.close()

def release_stale_running_jobs(max_age_minutes: int = 60) -> dict[str, Any]:
    ensure_job_tables()
    cutoff = _now() - timedelta(minutes=int(max_age_minutes))
    if _TEST_STORE is not None:
        count = 0
        for row in _TEST_STORE:
            if row["Status"] == "running" and row.get("LockedAt") and row["LockedAt"] < cutoff:
                row.update({"Status": "queued", "LockedBy": None, "LockedAt": None, "UpdatedAt": _now()})
                count += 1
        return {"released_count": count}
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE dbo.Job
            SET Status='queued', LockedBy=NULL, LockedAt=NULL, UpdatedAt=SYSDATETIME()
            WHERE Status='running' AND LockedAt < DATEADD(minute, -?, SYSDATETIME())
        """, int(max_age_minutes))
        count = cur.rowcount
        conn.commit()
        return {"released_count": count}
    finally:
        conn.close()


def get_queue_summary() -> dict[str, Any]:
    ensure_job_tables()
    if _TEST_STORE is not None:
        counts: dict[str, int] = {}
        for row in _TEST_STORE:
            key = f"P{row['Priority']}:{row['Status']}"
            counts[key] = counts.get(key, 0) + 1
        return {"counts": counts, "total": len(_TEST_STORE)}
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT Priority, Status, COUNT(1) AS Count
            FROM dbo.Job
            GROUP BY Priority, Status
            ORDER BY Priority ASC, Status ASC
        """)
        return {"counts": _rows_to_dicts(cur)}
    finally:
        conn.close()


def get_failed_job_summary(limit: int = 10, active_only: bool | None = None) -> list[dict[str, Any]]:
    ensure_job_tables()
    setup_job_types = (
        JOB_TYPE_BASELINE_SETUP_AREA,
        JOB_TYPE_SETUP_DETAIL_BASELINE,
        JOB_TYPE_SETUP_PRICE_BASELINE,
    )
    if _TEST_STORE is not None:
        rows = [dict(r) for r in _TEST_STORE if str(r.get("Status") or "").lower() == "failed"]
        rows = [
            row for row in rows
            if not (
                row.get("JobType") in setup_job_types
                and any(
                    other.get("JobType") == row.get("JobType")
                    and int(other.get("SearchID") or 0) == int(row.get("SearchID") or 0)
                    and str(other.get("Status") or "").lower() == "succeeded"
                    and int(other.get("JobID") or 0) > int(row.get("JobID") or 0)
                    for other in _TEST_STORE
                )
            )
        ]
        if active_only is not None:
            rows = [
                row for row in rows
                if bool(row.get("AreaActive", row.get("IsAreaActive", True))) is bool(active_only)
            ]
        rows.sort(key=lambda r: r.get("UpdatedAt") or r.get("FinishedAt") or r.get("CreatedAt") or _now(), reverse=True)
        return [
            {
                "job_id": row.get("JobID"),
                "job_type": row.get("JobType"),
                "search_id": row.get("SearchID"),
                "status": row.get("Status"),
                "attempts": row.get("AttemptCount"),
                "last_error": row.get("LastError"),
                "updated_at": row.get("UpdatedAt"),
            }
            for row in rows[: int(limit)]
        ]
    conn = _connect()
    try:
        cur = conn.cursor()
        active_filter = ""
        if active_only is True:
            active_filter = """
              AND COALESCE(ams.setup_status, 'not_started') <> 'inactive'
              AND EXISTS (
                    SELECT 1
                    FROM dbo.UserAreaSubscription uas
                    JOIN dbo.TelegramUser tu ON tu.TelegramUserID=uas.TelegramUserID
                    LEFT JOIN dbo.user_area_subscription_state us
                      ON us.user_id=uas.TelegramUserID AND us.area_id=uas.SearchID
                    WHERE uas.SearchID=j.SearchID
                      AND uas.IsActive=1
                      AND tu.IsActive=1
                      AND COALESCE(us.status, uas.SubscriptionStatus, 'active') IN ('active','preparing')
              )
            """
        elif active_only is False:
            active_filter = """
              AND (
                    j.SearchID IS NULL
                    OR COALESCE(ams.setup_status, 'not_started') = 'inactive'
                    OR NOT EXISTS (
                        SELECT 1
                        FROM dbo.UserAreaSubscription uas
                        JOIN dbo.TelegramUser tu ON tu.TelegramUserID=uas.TelegramUserID
                        LEFT JOIN dbo.user_area_subscription_state us
                          ON us.user_id=uas.TelegramUserID AND us.area_id=uas.SearchID
                        WHERE uas.SearchID=j.SearchID
                          AND uas.IsActive=1
                          AND tu.IsActive=1
                          AND COALESCE(us.status, uas.SubscriptionStatus, 'active') IN ('active','preparing')
                    )
              )
            """
        setup_placeholders = ", ".join("?" for _ in setup_job_types)
        superseded_setup_filter = f"""
              AND NOT (
                    j.JobType IN ({setup_placeholders})
                    AND EXISTS (
                        SELECT 1
                        FROM dbo.Job newer
                        WHERE newer.SearchID=j.SearchID
                          AND newer.JobType=j.JobType
                          AND newer.Status='succeeded'
                          AND newer.JobID > j.JobID
                    )
              )
            """
        cur.execute(f"""
            SELECT TOP ({int(limit)})
                   j.JobID AS job_id,
                   j.JobType AS job_type,
                   j.SearchID AS search_id,
                   j.Status AS status,
                   j.AttemptCount AS attempts,
                   j.LastError AS last_error,
                   j.UpdatedAt AS updated_at,
                   ams.setup_status AS area_setup_status
            FROM dbo.Job j
            LEFT JOIN dbo.area_monitoring_state ams ON ams.area_id=j.SearchID
            WHERE j.Status='failed'
            {active_filter}
            {superseded_setup_filter}
            ORDER BY j.UpdatedAt DESC, j.FinishedAt DESC, j.JobID DESC
        """, *setup_job_types)
        return _rows_to_dicts(cur)
    finally:
        conn.close()


def get_failed_job_summary_by_lifecycle(limit: int = 10, include_inactive: bool = True) -> dict[str, list[dict[str, Any]]]:
    active_failed = get_failed_job_summary(limit=limit, active_only=True)
    inactive_failed = get_failed_job_summary(limit=limit, active_only=False) if include_inactive else []
    return {"active_failed_jobs": active_failed, "inactive_failed_jobs": inactive_failed}


def get_next_due_jobs(limit: int = 10) -> list[dict[str, Any]]:
    ensure_job_tables()
    if _TEST_STORE is not None:
        rows = [dict(r) for r in _TEST_STORE if r["Status"] == "queued" and r["RunAfter"] <= _now()]
        rows.sort(key=lambda r: (int(r["Priority"]), r["RunAfter"], r["CreatedAt"], int(r["JobID"])))
        return rows[: int(limit)]
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(f"""
            SELECT TOP ({int(limit)}) j.JobID, j.JobType, j.SearchID, j.UserAreaID, j.Priority, j.Status, j.RunAfter,
                   j.AttemptCount, j.MaxAttempts, j.LockedBy, j.LockedAt, j.StartedAt, j.FinishedAt, j.LastError,
                   j.PayloadJson, j.DedupeKey, j.CreatedAt, j.UpdatedAt,
                   uas.AreaLabel
            FROM dbo.Job j
            OUTER APPLY (SELECT TOP 1 AreaLabel FROM dbo.UserAreaSubscription uas WHERE uas.SearchID=j.SearchID ORDER BY uas.UserAreaID ASC) uas
            WHERE j.Status='queued' AND j.RunAfter <= SYSDATETIME()
            ORDER BY j.Priority ASC, j.RunAfter ASC, j.CreatedAt ASC, j.JobID ASC
        """)
        return _rows_to_dicts(cur)
    finally:
        conn.close()


def get_jobs_by_dedupe_key(dedupe_key: str, statuses: set[str] | None = None) -> list[dict[str, Any]]:
    """Return jobs for an exact dedupe key, optionally limited by status."""
    ensure_job_tables()
    if not dedupe_key:
        return []
    if _TEST_STORE is not None:
        rows = [dict(r) for r in _TEST_STORE if r.get("DedupeKey") == dedupe_key]
        if statuses is not None:
            allowed = {str(status) for status in statuses}
            rows = [row for row in rows if str(row.get("Status")) in allowed]
        rows.sort(key=lambda r: (r.get("CreatedAt") or _now(), int(r.get("JobID") or 0)))
        return rows
    conn = _connect()
    try:
        cur = conn.cursor()
        if statuses is None:
            cur.execute("SELECT * FROM dbo.Job WHERE DedupeKey=? ORDER BY CreatedAt, JobID", dedupe_key)
        else:
            safe_statuses = [str(status) for status in statuses]
            placeholders = ",".join("?" for _ in safe_statuses)
            cur.execute(f"SELECT * FROM dbo.Job WHERE DedupeKey=? AND Status IN ({placeholders}) ORDER BY CreatedAt, JobID", dedupe_key, *safe_statuses)
        return _rows_to_dicts(cur)
    finally:
        conn.close()


def get_active_jobs(dedupe_key: str | None = None) -> list[dict[str, Any]]:
    ensure_job_tables()
    if _TEST_STORE is not None:
        return [dict(r) for r in _TEST_STORE if r["Status"] in JOB_STATUS_ACTIVE and (dedupe_key is None or r.get("DedupeKey") == dedupe_key)]
    conn = _connect()
    try:
        cur = conn.cursor()
        if dedupe_key:
            cur.execute("SELECT * FROM dbo.Job WHERE DedupeKey=? AND Status IN ('pending','paused','queued','running','retry_wait','scheduled') ORDER BY CreatedAt", dedupe_key)
        else:
            cur.execute("SELECT * FROM dbo.Job WHERE Status IN ('pending','paused','queued','running','retry_wait','scheduled') ORDER BY Priority, RunAfter, CreatedAt")
        return _rows_to_dicts(cur)
    finally:
        conn.close()
