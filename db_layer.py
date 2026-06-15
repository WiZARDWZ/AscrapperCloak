import hashlib
import json
import logging
import re
import sqlite3
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote_plus, urlparse

import pyodbc

import config
import listing_change_detector
from area_parser import parse_area_to_sqm

logger = logging.getLogger(__name__)

MISSING_TOKENS = {"", "n/a", "na", "-", "—", "null", "none"}
NUMERIC_MISSING_EXTRA = {"unknown"}
ACTIVE_SUBSCRIPTION_STATE_VALUES = {"active", "preparing"}
INACTIVE_SUBSCRIPTION_STATE_VALUES = {"removed", "inactive", "cancelled", "failed", "deleted"}
INACTIVE_AREA_REASON_NO_SUBSCRIBERS = "no_active_subscriptions"
LISTING_LIFECYCLE_STATUSES = {"active", "sold", "removed", "not_found"}
MODULE2_ELIGIBLE_LIFECYCLE_STATUSES = {"active"}


SETUP_DETAIL_REQUIRED_COLUMNS = {
    "SetupDetailStatus",
    "SetupDetailAttemptCount",
    "SetupDetailLastAttemptAt",
    "SetupDetailNextRetryAt",
    "SetupDetailLastError",
    "SetupDetailCompletedAt",
}

AREA_MONITORING_REQUIRED_COLUMNS = {
    "area_id",
    "setup_status",
    "module1_status",
    "module3_status",
    "module2_status",
    "active_listing_count",
    "inferred_price_count",
    "unknown_price_count",
    "setup_started_at",
    "ready_at",
    "last_error",
    "updated_at",
    "deactivated_at",
    "deactivated_reason",
    "reactivated_at",
    "last_subscription_count",
}

SCHEMA_REQUIREMENTS = {
    "dbo.State": {"StateID", "Name", "Abbreviation"},
    "dbo.Suburb": {"ID", "StateID", "Name", "PostalCode"},
    "dbo.SuburbSearch": {"SearchID", "SuburbID", "SearchURL", "NormalizedSearchURL", "SearchHash", "DisplayName", "IsActive"},
    "dbo.PropertyType": {"ID", "PropertyType"},
    "dbo.Property": {"PropertyID", "PropertyTypeID", "SuburbID", "Address", "NumberOfBedroom", "NumberOfBath", "Parkingslot", "LandAreaSqm", "BuildingAreaSqm", "AddressRaw", "AddressNormalized", "AddressHash"},
    "dbo.Agency": {"AgencyID", "Name", "Telephone", "Address", "AgencyProfileURL", "AgencyExternalCode"},
    "dbo.Agent": {"AgentID", "AgencyID", "AgentProfileURL", "AgentName", "AgentPhoneNumber", "AgentExternalID"},
    "dbo.Listing": {"listingID", "ExternalID", "AgencyID", "AgentID", "PropertyID", "Price", "Description", "ListingURL", "CurrentStatus", "CurrentPriceDisplay", "CurrentDescriptionHash"},
    "dbo.ListingSearchState": {"ListingID", "SearchID", "FirstSeenAt", "LastSeenAt", "Status"},
    "dbo.ListingSnapshot": {"SnapshotID", "ListingID", "SnapshotDate", "Price", "PriceDisplay", "AgentID", "AgencyID", "Status", "Description", "URL", "RunID", "SearchID", "PriceLow", "PriceHigh", "PriceMethod", "PrimaryAgentID", "DescriptionHash", "InspectionShort", "InspectionLong", "AuctionTimeLabel", "SnapshotHash", "LandSizeDisplay", "LandSizeSqm", "BuildingSizeDisplay", "BuildingSizeSqm", "FloorAreaDisplay", "FloorAreaSqm"},
    "dbo.ListingSnapshotAgent": {"SnapshotAgentID", "SnapshotID", "AgentID", "Position"},
    "dbo.ListingAgentAssignment": {"AssignmentID", "ListingID", "AgentID", "SearchID", "StartedAt", "EndedAt"},
    "dbo.ListingEvent": {"EventID", "RunID", "SearchID", "ListingID", "EventType", "OldValueJson", "NewValueJson", "EventHash"},
    "dbo.ScrapeRun": {"RunID", "SearchID", "RunType", "Status"},
    "dbo.Job": {"JobID", "JobType", "Status"},
    "dbo.TelegramMessage": {"TelegramMessageLogID", "UserID", "ListingID", "MessageID", "MessageType"},
    "dbo.[User]": {"UserID", "ChatID", "AccessStatusCode"},
    "dbo.UserSetting": {"UserID", "PollIntervalMinutes"},
    "dbo.UserSuburbMonitor": {"UserID", "SearchID", "IsActive"},
}



def json_dumps_safe(value, **kwargs) -> str:
    """Serialize DB event/snapshot payloads without failing on Decimal values."""
    kwargs.setdefault("ensure_ascii", False)
    kwargs.setdefault("default", str)
    return json.dumps(value, **kwargs)


def ensure_area_numeric_capacity(conn) -> None:
    """Idempotently widen property/listing area columns for rural/farming land sizes."""
    if not hasattr(conn, "commit"):
        return
    for table_name, column_name in (
        ("Property", "LandAreaSqm"),
        ("Property", "BuildingAreaSqm"),
        ("ListingSnapshot", "LandSizeSqm"),
        ("ListingSnapshot", "BuildingSizeSqm"),
        ("ListingSnapshot", "FloorAreaSqm"),
    ):
        _execute_ddl_safely(conn, f"""
            IF OBJECT_ID('dbo.{table_name}') IS NOT NULL
            AND COL_LENGTH('dbo.{table_name}', '{column_name}') IS NOT NULL
            AND EXISTS (
                SELECT 1
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA='dbo' AND TABLE_NAME='{table_name}' AND COLUMN_NAME='{column_name}'
                  AND (DATA_TYPE NOT IN ('decimal','numeric') OR NUMERIC_PRECISION < 18 OR NUMERIC_SCALE < 2)
            )
            ALTER TABLE dbo.{table_name} ALTER COLUMN {column_name} DECIMAL(18,2) NULL
            """, description=f"widen dbo.{table_name}.{column_name} to DECIMAL(18,2)", required=False)

def connect(_db_path: Optional[str] = None):
    return pyodbc.connect(
        config.build_sqlserver_connection_string(include_password=True),
        autocommit=False,
        timeout=config.DB_TIMEOUT,
    )

def init_db(_db_path: Optional[str] = None):
    """Run lightweight compatibility migrations.

    The operational path uses SQL Server through pyodbc, but older/local test
    databases are SQLite files. Keep init_db idempotent for those files without
    changing the SQL Server connection behavior used by the rest of the module.
    """
    db_path = _db_path or config.DB_PATH
    if not db_path or str(db_path).strip().lower() in {":memory:", "memory"} or str(db_path).lower().endswith((".db", ".sqlite", ".sqlite3")):
        conn = sqlite3.connect(db_path)
        try:
            _init_sqlite_monitoring_tables(conn)
        except sqlite3.OperationalError:
            if str(db_path) != str(config.DB_PATH):
                raise
        finally:
            conn.close()
    return None


def _init_sqlite_monitoring_tables(conn) -> None:
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS area_monitoring_state (
            area_id INTEGER PRIMARY KEY,
            setup_status TEXT NOT NULL DEFAULT 'not_started',
            module1_status TEXT,
            module3_status TEXT,
            module2_status TEXT,
            active_listing_count INTEGER DEFAULT 0,
            inferred_price_count INTEGER DEFAULT 0,
            unknown_price_count INTEGER DEFAULT 0,
            setup_started_at TEXT,
            ready_at TEXT,
            deactivated_at TEXT,
            deactivated_reason TEXT,
            reactivated_at TEXT,
            last_subscription_count INTEGER DEFAULT 0,
            last_error TEXT,
            updated_at TEXT
        )
    """)
    cur.execute("PRAGMA table_info(area_monitoring_state)")
    existing_area_state = {row[1] for row in cur.fetchall()}
    for column_name, column_type in {
        "deactivated_at": "TEXT",
        "deactivated_reason": "TEXT",
        "reactivated_at": "TEXT",
        "last_subscription_count": "INTEGER DEFAULT 0",
    }.items():
        if column_name not in existing_area_state:
            try:
                cur.execute(f"ALTER TABLE area_monitoring_state ADD COLUMN {column_name} {column_type}")
            except sqlite3.OperationalError:
                pass
    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_area_subscription_state (
            user_id INTEGER NOT NULL,
            area_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'preparing',
            notify_enabled INTEGER DEFAULT 0,
            created_at TEXT,
            activated_at TEXT,
            removed_at TEXT,
            updated_at TEXT,
            PRIMARY KEY(user_id, area_id)
        )
    """)
    cur.execute("PRAGMA table_info(user_area_subscription_state)")
    existing_sub_state = {row[1] for row in cur.fetchall()}
    if "removed_at" not in existing_sub_state:
        try:
            cur.execute("ALTER TABLE user_area_subscription_state ADD COLUMN removed_at TEXT")
        except sqlite3.OperationalError:
            pass
    cur.execute("""
        CREATE TABLE IF NOT EXISTS listing_price_inference_state (
            listing_id TEXT NOT NULL,
            area_id INTEGER NOT NULL,
            status TEXT,
            last_error TEXT,
            last_attempt_at TEXT,
            next_retry_at TEXT,
            attempts INTEGER DEFAULT 0,
            inferred_low INTEGER,
            inferred_high INTEGER,
            method TEXT,
            updated_at TEXT,
            PRIMARY KEY(listing_id, area_id)
        )
    """)
    cur.execute("PRAGMA table_info(ListingSnapshot)")
    existing = {row[1] for row in cur.fetchall()}
    for column_name, column_type in {
        "LandSizeDisplay": "TEXT",
        "LandSizeSqm": "REAL",
        "BuildingSizeDisplay": "TEXT",
        "BuildingSizeSqm": "REAL",
        "FloorAreaDisplay": "TEXT",
        "FloorAreaSqm": "REAL",
    }.items():
        if existing and column_name not in existing:
            try:
                cur.execute(f"ALTER TABLE ListingSnapshot ADD COLUMN {column_name} {column_type}")
            except sqlite3.OperationalError:
                pass
    cur.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            job_id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_type TEXT NOT NULL,
            area_id INTEGER,
            search_url TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            run_after TEXT,
            payload_json TEXT,
            dedupe_key TEXT,
            created_at TEXT,
            updated_at TEXT
        )
    """)
    cur.execute("PRAGMA table_info(ListingSearchState)")
    existing_lss = {row[1] for row in cur.fetchall()}
    for column_name, column_type in {
        "ListingLifecycleStatus": "TEXT DEFAULT 'active'",
        "StatusReason": "TEXT",
        "StatusEvidence": "TEXT",
        "NotFoundCount": "INTEGER DEFAULT 0",
        "FirstNotFoundAt": "TEXT",
        "LastNotFoundAt": "TEXT",
        "RemovedAt": "TEXT",
        "SoldAt": "TEXT",
        "LastStatusChangeAt": "TEXT",
        "StatusNotificationSentAt": "TEXT",
    }.items():
        if existing_lss and column_name not in existing_lss:
            try:
                cur.execute(f"ALTER TABLE ListingSearchState ADD COLUMN {column_name} {column_type}")
            except sqlite3.OperationalError:
                pass
    cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS ux_jobs_active_baseline_area
        ON jobs(job_type, area_id)
        WHERE job_type='baseline_setup_area' AND status IN ('pending','running','paused')
    """)
    conn.commit()

def ensure_sort_list_date(url: str) -> str:
    return url if "activeSort=list-date" in url else url + ("&" if "?" in url else "?") + "activeSort=list-date"

def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

LISTING_SNAPSHOT_INSERT_SQL = """
INSERT INTO dbo.ListingSnapshot(
    ListingID,
    SnapshotDate,
    SearchID,
    RunID,
    SnapshotHash,
    Price,
    PriceDisplay,
    PriceLow,
    PriceHigh,
    PriceMethod,
    AgentID,
    PrimaryAgentID,
    Status,
    Description,
    DescriptionHash,
    AgencyID,
    InspectionShort,
    InspectionLong,
    AuctionTimeLabel,
    URL,
    LandSizeDisplay,
    LandSizeSqm,
    BuildingSizeDisplay,
    BuildingSizeSqm,
    FloorAreaDisplay,
    FloorAreaSqm
)
OUTPUT INSERTED.SnapshotID
VALUES (
    ?,
    SYSDATETIME(),
    ?,
    ?,
    ?,
    ?,
    ?,
    ?,
    ?,
    ?,
    ?,
    ?,
    ?,
    ?,
    ?,
    ?,
    ?,
    ?,
    ?,
    ?,
    ?,
    ?,
    ?,
    ?,
    ?,
    ?
)
"""

def _assert_param_marker_count(sql: str, params: Tuple[Any, ...]) -> None:
    marker_count = sql.count("?")
    if marker_count != len(params):
        raise RuntimeError(f"SQL parameter mismatch: markers={marker_count}, params={len(params)}")

def _one(cur, q, *p):
    cur.execute(q, p)
    return cur.fetchone()

def is_missing(value: Any, numeric_or_date: bool = False) -> bool:
    if value is None:
        return True
    s = str(value).strip().lower()
    if s in MISSING_TOKENS:
        return True
    return numeric_or_date and s in NUMERIC_MISSING_EXTRA

def clean_text(value: Any, max_len: Optional[int] = None, none_if_missing: bool = True) -> Optional[str]:
    if value is None:
        return None if none_if_missing else ""
    out = str(value).strip()
    if none_if_missing and is_missing(out):
        return None
    if max_len and len(out) > max_len:
        out = out[:max_len]
    return out


def _strip_unsafe_control_chars(value: str) -> str:
    # SQL Server string columns should never receive embedded NULs or low
    # control characters from scraped DOM text. Preserve normal whitespace here;
    # callers decide whether to collapse it for labels.
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", value)


def clean_limited_text(value: Any, max_len: int, *, none_if_missing: bool = True, field_name: str | None = None, normalize_whitespace: bool = True) -> Optional[str]:
    if value is None:
        return None if none_if_missing else ""
    out = _strip_unsafe_control_chars(str(value)).strip()
    if normalize_whitespace:
        out = re.sub(r"\s+", " ", out).strip()
    if none_if_missing and is_missing(out):
        return None
    if max_len and len(out) > max_len:
        logger.debug("Truncating scraped text field %s from %s to %s characters", field_name or "unknown", len(out), max_len)
        out = out[:max_len].rstrip()
    return out


_AUCTION_LABEL_RE = re.compile(r"\bauction\b", re.I)
_FULL_CARD_HINT_RE = re.compile(
    r"\b(?:bed|beds|bath|baths|car|cars|parking|inspection|inspect|open\s+home|agent|agency|sqm|m²|studio)\b|\d+\s*/\s*\d+|\d+\s+[A-Za-z].*(?:street|st|road|rd|avenue|ave|lane|ln|drive|dr|gardens|gdn|crescent|cres)\b",
    re.I,
)


def _short_label_lines(value: Any) -> list[str]:
    if value is None:
        return []
    raw = _strip_unsafe_control_chars(str(value)).replace("\r", "\n")
    return [re.sub(r"\s+", " ", line).strip() for line in raw.split("\n") if re.sub(r"\s+", " ", line).strip()]


def sanitize_auction_time_label(value: Any, max_len: int = 100) -> Optional[str]:
    """Return a short auction label or None; never persist full listing-card text."""
    lines = _short_label_lines(value)
    if not lines:
        return None
    source = str(value)
    candidate = next((line for line in lines if _AUCTION_LABEL_RE.search(line)), lines[0] if len(lines) == 1 else None)
    if not candidate:
        logger.debug("Dropping multi-line auction label without auction marker")
        return None
    multi_line = len(lines) > 1
    if not _AUCTION_LABEL_RE.search(candidate):
        return None
    if len(candidate) > max_len * 2:
        logger.debug("Dropping overlong auction label candidate (%s chars)", len(candidate))
        return None
    if multi_line and _FULL_CARD_HINT_RE.search(candidate) and len(candidate) > 60:
        logger.debug("Dropping auction label candidate that resembles full card text")
        return None
    sanitized = clean_limited_text(candidate, max_len, field_name="AuctionTimeLabel")
    if sanitized and sanitized != re.sub(r"\s+", " ", _strip_unsafe_control_chars(source)).strip():
        logger.debug("Sanitized AuctionTimeLabel from scraped card text")
    return sanitized


def sanitize_snapshot_label(value: Any, max_len: int, field_name: str) -> Optional[str]:
    lines = _short_label_lines(value)
    if not lines:
        return None
    if len(lines) > 1:
        preferred = next((line for line in lines if re.search(r"\b(?:inspection|inspect|open|auction)\b", line, re.I)), lines[0])
    else:
        preferred = lines[0]
    return clean_limited_text(preferred, max_len, field_name=field_name)

def to_int(value: Any) -> Optional[int]:
    if is_missing(value, numeric_or_date=True):
        return None
    s = str(value).strip().lower().replace(",", "")
    m = re.search(r"-?\d+", s)
    if not m:
        return None
    try:
        return int(m.group(0))
    except ValueError:
        return None

def to_tinyint(value: Any) -> Optional[int]:
    n = to_int(value)
    return n if n is not None and 0 <= n <= 255 else None

def _to_decimal_number(raw: str) -> Optional[Decimal]:
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None

PRICE_MONTH_PATTERN = re.compile(
    r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t|tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b",
    re.I,
)
NO_PRICE_PHRASE_PATTERN = re.compile(
    r"\b(?:contact\s+agent(?:\s+for\s+pricing)?|price\s+on\s+application|price\s+on\s+request|poa|expressions?\s+of\s+interest|eoi|by\s+negotiation|auction|in-room\s+auction|must\s+be\s+sold|just\s+listed|(?:all\s+)?offers?\s+by)\b|a\s+grade\s+beachfront\s+home|price/development/joint\s+venture\s+by\s+negotiation",
    re.I,
)
_DATE_DEADLINE_PATTERN = re.compile(
    r"\b(?:offers?\s+by|all\s+offers\s+by|by)\s+\d{1,2}(?:st|nd|rd|th)?\s+" + PRICE_MONTH_PATTERN.pattern[2:-2] + r"(?:\s+\d{4})?\b",
    re.I,
)
_MONETARY_TOKEN_RE = re.compile(
    r"(?P<dollar>\$)\s*(?P<num>\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(?P<suffix>[mk])?\b|(?<![A-Za-z0-9])(?P<num2>\d+(?:\.\d+)?)\s*(?P<suffix2>[mk])\b|(?<![A-Za-z0-9])(?P<num3>\d{1,3}(?:,\d{3})+)\b",
    re.I,
)
_RANGE_TAIL_RE = re.compile(
    r"^\s*(?:-|–|—|to|and)\s*\$?\s*(?P<num>\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(?P<suffix>[mk])?\b",
    re.I,
)


def _decimal_from_price_token(num_text: str, suffix: str | None = None) -> Optional[Decimal]:
    number = _to_decimal_number(str(num_text or "").replace(",", ""))
    if number is None:
        return None
    suffix = (suffix or "").lower()
    if suffix == "m":
        number *= Decimal("1000000")
    elif suffix == "k":
        number *= Decimal("1000")
    return number.quantize(Decimal("1")) if number == number.to_integral_value() else number


def _price_token_value_and_suffix(num_text: str, suffix: str | None, has_dollar: bool) -> tuple[Optional[Decimal], str]:
    effective_suffix = (suffix or "").lower()
    raw_number = _to_decimal_number(str(num_text or "").replace(",", ""))
    if has_dollar and not effective_suffix and raw_number is not None and Decimal("0") < raw_number < Decimal("100") and "." in str(num_text):
        effective_suffix = "m"
    return _decimal_from_price_token(num_text, effective_suffix), effective_suffix


def _is_valid_residential_price(value: Decimal | None, had_suffix: bool) -> bool:
    if value is None:
        return False
    if had_suffix:
        return True
    # Residential sale prices below $100k are far more likely to be dates,
    # counts, or marketing copy than a real sale price in this product.
    return value >= Decimal("100000")


def to_decimal(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    s = str(value).strip()
    if is_missing(s, numeric_or_date=True):
        return None
    compact = s.replace(",", "")
    if re.fullmatch(r"-?\d+(?:\.\d+)?", compact):
        return _to_decimal_number(compact)
    low, high = parse_price_range(s)
    if low is not None and high is not None and low == high:
        return low
    if low is not None and high is None:
        return low
    return None


def _first_row_value(row: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if not is_missing(value, numeric_or_date=True):
            return value
    return None


def _area_decimal(row: Dict[str, Any], numeric_key: str, display_key: str) -> Optional[Decimal]:
    value = _first_row_value(row, numeric_key, _snake_case_size_key(numeric_key))
    parsed = to_decimal(value)
    if parsed is not None:
        return parsed
    display = _first_row_value(row, display_key, _snake_case_size_key(display_key))
    sqm = parse_area_to_sqm(display)
    return to_decimal(sqm)


def _snake_case_size_key(key: str) -> str:
    return {
        "LandSizeDisplay": "land_size_display",
        "LandSizeSqm": "land_size_sqm",
        "BuildingSizeDisplay": "building_size_display",
        "BuildingSizeSqm": "building_size_sqm",
        "FloorAreaDisplay": "floor_area_display",
        "FloorAreaSqm": "floor_area_sqm",
    }.get(key, key)


def price_text_has_no_price_phrase(value: Any) -> bool:
    return bool(value is not None and NO_PRICE_PHRASE_PATTERN.search(str(value)))


def price_text_has_date_or_deadline(value: Any) -> bool:
    if value is None:
        return False
    text = str(value)
    return bool(
        PRICE_MONTH_PATTERN.search(text)
        or _DATE_DEADLINE_PATTERN.search(text)
        or re.search(r"\b\d{1,2}(?:st|nd|rd|th)?\s+" + PRICE_MONTH_PATTERN.pattern[2:-2], text, re.I)
        or re.search(r"\b20\d{2}\b", text)
        or re.search(r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b", text)
        or re.search(r"\bclosing\b", text, re.I)
    )


def parse_price_range(value: Any) -> Tuple[Optional[Decimal], Optional[Decimal]]:
    if is_missing(value, numeric_or_date=True):
        return None, None
    text = str(value).strip()
    if not text:
        return None, None

    parsed: list[tuple[Decimal, bool, str, int, int]] = []
    has_price_context = bool(re.search(r"\b(?:price|guide|guiding|buyer|buyers|offers?|from)\b", text, re.I))
    for match in _MONETARY_TOKEN_RE.finditer(text):
        num = match.group("num") or match.group("num2") or match.group("num3")
        suffix = match.group("suffix") or match.group("suffix2")
        had_suffix = bool(suffix)
        has_dollar = bool(match.group("dollar"))
        if not has_dollar and had_suffix and not has_price_context:
            continue
        amount, effective_suffix = _price_token_value_and_suffix(num, suffix, has_dollar)
        had_effective_suffix = bool(effective_suffix)
        if not _is_valid_residential_price(amount, had_effective_suffix):
            continue
        parsed.append((amount, had_effective_suffix, effective_suffix, match.start(), match.end()))

    if not parsed:
        return None, None

    first_value, first_had_suffix, first_suffix, _first_start, first_end = parsed[0]
    values = [first_value]

    # If the first explicit monetary token is followed by a range separator,
    # allow the second side to omit "$" (for example "$1,125,000 to 1,245,000").
    tail = _RANGE_TAIL_RE.search(text[first_end:])
    if tail:
        suffix = tail.group("suffix") or (first_suffix if first_had_suffix else "")
        second_value, effective_suffix = _price_token_value_and_suffix(tail.group("num"), suffix, has_dollar=True)
        if _is_valid_residential_price(second_value, bool(effective_suffix)):
            values.append(second_value)
    elif len(parsed) >= 2:
        values.append(parsed[1][0])

    low = min(values)
    high = max(values)
    return low, high


def assess_price_quality(
    price_display: Any,
    estimated_price_low: Any,
    estimated_price_high: Any,
    price_method: Any = None,
    property_type: Any = None,
) -> list[str]:
    reasons: list[str] = []
    low = estimated_price_low if isinstance(estimated_price_low, Decimal) else _to_decimal_number(str(estimated_price_low)) if estimated_price_low not in (None, "") else None
    high = estimated_price_high if isinstance(estimated_price_high, Decimal) else _to_decimal_number(str(estimated_price_high)) if estimated_price_high not in (None, "") else None
    display = str(price_display or "")
    if low is not None and high is not None and high < low:
        reasons.append("high_less_than_low")
    if low is not None and low < Decimal("100000"):
        reasons.append("low_under_100000")
    for label, value in (("low", low), ("high", high)):
        if value is not None and Decimal("1") <= value <= Decimal("9999"):
            reasons.append(f"{label}_date_like_1_9999")
    if (low is not None or high is not None) and price_text_has_date_or_deadline(display):
        small = any(v is not None and v < Decimal("100000") for v in (low, high))
        if small:
            reasons.append("date_deadline_numeric_price")
    if (low is not None or high is not None) and price_text_has_no_price_phrase(display):
        parsed_low, parsed_high = parse_price_range(display)
        if parsed_low is None and parsed_high is None:
            reasons.append("no_price_phrase_with_numeric_estimate")
    return sorted(set(reasons))

def parse_price_bounds_from_text(value: Any) -> Tuple[Optional[int], Optional[int]]:
    """Backward-compatible wrapper for legacy callers expecting integer bounds."""
    low, high = parse_price_range(value)
    return (
        int(low) if low is not None else None,
        int(high) if high is not None else None,
    )

def _parse_search_url(url: str) -> Tuple[str, str, Optional[str], str, str]:
    normalized = ensure_sort_list_date(url)
    parsed = urlparse(normalized)
    path = unquote_plus(parsed.path or "")
    m = re.search(r"/in-([^/]+)/list-\d+", path, flags=re.I)
    if not m:
        raise ValueError(f"Cannot parse suburb/state/postcode from URL: {url}")
    raw = re.sub(r"\s+", " ", m.group(1).replace(",", " ")).strip()
    m2 = re.match(r"(.+?)\s+([A-Za-z]{2,3})\s+(\d{4})$", raw)
    if m2:
        suburb_name, state_abbr, postcode = m2.group(1).title(), m2.group(2).upper(), m2.group(3)
    else:
        parts = raw.split(); suburb_name = raw.title(); state_abbr = parts[-2].upper() if len(parts) >= 2 else ""; postcode = parts[-1] if parts and parts[-1].isdigit() else None
    return normalized, suburb_name or "Unknown", postcode, state_abbr, f"{suburb_name}, {state_abbr} {postcode or ''}".strip()



def _looks_like_external_listing_id(value: Any, row: Dict[str, Any] | None = None) -> bool:
    if value is None:
        return False
    text = str(value).strip()
    if not text:
        return False
    if row:
        for internal_key in ["db_listing_id", "internal_listing_id"]:
            internal_id = row.get(internal_key)
            if internal_id is not None and str(internal_id).strip() == text:
                return False
    return True


def extract_external_listing_id(row: Dict[str, Any], allow_listing_id_fallback: bool = True) -> str:
    for key in ["external_id", "ExternalID", "realestate_listing_id", "listing_external_id"]:
        value = clean_text(row.get(key), 50)
        if value:
            return value
    if allow_listing_id_fallback:
        listing_id = clean_text(row.get("listing_id"), 50)
        if listing_id and _looks_like_external_listing_id(listing_id, row):
            return listing_id
    raise ValueError("Missing external_id for change detection")

def normalize_listing_row(row: Dict[str, Any]) -> Dict[str, Any]:
    try:
        ext = extract_external_listing_id(row)
    except ValueError as exc:
        raise ValueError("bad_listing_id") from exc
    detail_price_display = clean_limited_text(row.get("detail_price_display"), 300, field_name="PriceDisplay")
    row_price_display = clean_limited_text(row.get("AdPriceDisplay") or row.get("ad_price_display") or row.get("price_display") or row.get("CurrentPriceDisplay") or row.get("price"), 300, field_name="PriceDisplay")

    detail_low, detail_high = parse_price_range(detail_price_display) if detail_price_display else (None, None)
    row_low, row_high = parse_price_range(row_price_display) if row_price_display else (None, None)

    if detail_low is not None or detail_high is not None:
        low, high = detail_low, detail_high
        pm = "direct_from_pdp"
    elif row_low is not None or row_high is not None:
        low, high = row_low, row_high
        pm = "parsed_display"
    else:
        low, high = None, None
        pm = "unknown"

    price_display = detail_price_display or row_price_display
    if low is not None and high is None:
        high = low
    if high is not None and low is None:
        low = high
    price_value = low if low is not None and high is not None and low == high else None
    parking_raw = row.get("parking")
    parking = 0 if isinstance(parking_raw, str) and parking_raw.strip().lower() in {"no parking", "no garage"} else to_tinyint(parking_raw)
    land_size_display = clean_limited_text(_first_row_value(row, "LandSizeDisplay", "land_size_display"), 100, field_name="LandSizeDisplay")
    building_size_display = clean_limited_text(_first_row_value(row, "BuildingSizeDisplay", "building_size_display"), 100, field_name="BuildingSizeDisplay")
    floor_area_display = clean_limited_text(_first_row_value(row, "FloorAreaDisplay", "floor_area_display"), 100, field_name="FloorAreaDisplay")
    auction_label_source = _first_row_value(row, "auction_label", "AuctionTimeLabel", "auction_time_label", "auction_time")
    agents_in = row.get("agents") or []
    agents = []
    for a in agents_in:
        if isinstance(a, str):
            a = {"name": a}
        name = clean_limited_text(a.get("name"), 200, field_name="AgentName")
        if not name:
            continue
        agents.append({"name": name, "phone": clean_limited_text(a.get("phone"), 80, field_name="AgentPhoneNumber"), "profile_url": clean_limited_text(a.get("profile_url"), 600, field_name="AgentProfileURL"), "external_id": clean_limited_text(a.get("external_id"), 120, field_name="AgentExternalID")})
    return {
        "external_id": ext,
        "address": clean_limited_text(row.get("address"), 500, field_name="Address"),
        "property_type": clean_limited_text(row.get("property_type"), 100, field_name="PropertyType") or "unknown",
        "bedrooms": to_tinyint(row.get("bedrooms")),
        "bathrooms": to_tinyint(row.get("bathrooms")),
        "parking": parking,
        "land_area_sqm": _area_decimal(row, "LandSizeSqm", "LandSizeDisplay") or to_decimal(row.get("land_area_sqm")),
        "building_area_sqm": _area_decimal(row, "BuildingSizeSqm", "BuildingSizeDisplay") or to_decimal(row.get("building_area_sqm")),
        "land_size_display": land_size_display,
        "land_size_sqm": _area_decimal(row, "LandSizeSqm", "LandSizeDisplay"),
        "building_size_display": building_size_display,
        "building_size_sqm": _area_decimal(row, "BuildingSizeSqm", "BuildingSizeDisplay"),
        "floor_area_display": floor_area_display,
        "floor_area_sqm": _area_decimal(row, "FloorAreaSqm", "FloorAreaDisplay"),
        "agency_name": clean_limited_text(row.get("agency_name") or row.get("agency"), 300, field_name="AgencyName"),
        "agency_external_code": clean_limited_text(row.get("agency_code"), 120, field_name="AgencyExternalCode"),
        "agency_profile_url": clean_limited_text(row.get("agency_profile_url"), 600, field_name="AgencyProfileURL"),
        "agency_phone": clean_limited_text(row.get("agency_phone"), 80, field_name="AgencyPhone"),
        "agency_address": clean_limited_text(row.get("agency_address"), 500, field_name="AgencyAddress"),
        "agents": agents,
        "price_display": price_display,
        "price_value": price_value,
        "price_low": low,
        "price_high": high,
        "price_method": pm,
        "description": clean_limited_text(row.get("description"), 4000, field_name="Description", normalize_whitespace=False),
        "inspection_short_label": sanitize_snapshot_label(row.get("inspection_short_label"), 200, "InspectionShort"),
        "inspection_long_label": sanitize_snapshot_label(row.get("inspection_long_label"), 500, "InspectionLong"),
        "auction_label": sanitize_auction_time_label(auction_label_source),
        "url": clean_limited_text(row.get("url"), 1000, field_name="ListingURL"),
    }

def _upsert_search(conn, url):
    search_id, _created = get_or_create_suburb_search(conn, url)
    return search_id


def _is_unique_search_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return ("suburbsearch" in text or "searchhash" in text or "normalizedsearchurl" in text) and ("duplicate" in text or "unique" in text or "2601" in text or "2627" in text)


def get_or_create_suburb_search(
    conn,
    search_url: str,
    area_label: str | None = None,
    suburb: str | None = None,
    postcode: str | None = None,
) -> tuple[int, bool]:
    """Race-safe SearchHash/NormalizedSearchURL get-or-create for a monitored search area."""
    normalized, parsed_suburb, parsed_postcode, state_abbr, parsed_display = _parse_search_url(search_url)
    suburb_name = clean_text(suburb, 255) or parsed_suburb
    postcode_value = clean_text(postcode, 16) or parsed_postcode
    display = clean_text(area_label, 255) or parsed_display
    h = _sha(normalized)
    cur = conn.cursor()
    row = _one(cur, "SELECT SearchID FROM dbo.SuburbSearch WITH (UPDLOCK, HOLDLOCK) WHERE SearchHash=? OR NormalizedSearchURL=?", h, normalized)
    if row:
        return int(row[0]), False
    st = _one(cur, "SELECT StateID FROM dbo.State WHERE UPPER(Abbreviation)=?", state_abbr)
    state_id = int(st[0]) if st else int(cur.execute("INSERT INTO dbo.State(Name,Abbreviation) OUTPUT INSERTED.StateID VALUES (?,?)", state_abbr, state_abbr).fetchone()[0])
    sb = _one(cur, "SELECT ID FROM dbo.Suburb WHERE StateID=? AND UPPER(Name)=? AND (PostalCode=? OR (? IS NULL AND PostalCode IS NULL))", state_id, suburb_name.upper(), postcode_value, postcode_value)
    suburb_id = int(sb[0]) if sb else int(cur.execute("INSERT INTO dbo.Suburb(StateID,Name,PostalCode) OUTPUT INSERTED.ID VALUES (?,?,?)", state_id, suburb_name, postcode_value).fetchone()[0])
    try:
        cur.execute("INSERT INTO dbo.SuburbSearch(SuburbID,SearchURL,NormalizedSearchURL,SearchHash,DisplayName) OUTPUT INSERTED.SearchID VALUES (?,?,?,?,?)", suburb_id, search_url, normalized, h, display)
        return int(cur.fetchone()[0]), True
    except Exception as exc:
        if not _is_unique_search_error(exc):
            raise
        try:
            conn.rollback()
        except Exception:
            pass
        row = _one(conn.cursor(), "SELECT SearchID FROM dbo.SuburbSearch WHERE SearchHash=? OR NormalizedSearchURL=?", h, normalized)
        if row:
            return int(row[0]), False
        raise

def _upsert_property_type(cur, property_type: Optional[str]) -> Optional[int]:
    pt = clean_text(property_type, 100) or "unknown"
    r = _one(cur, "SELECT ID FROM dbo.PropertyType WHERE UPPER(PropertyType)=?", pt.upper())
    if r: return int(r[0])
    cur.execute("INSERT INTO dbo.PropertyType(PropertyType) OUTPUT INSERTED.ID VALUES (?)", pt)
    return int(cur.fetchone()[0])

def _upsert_property(cur, n: Dict[str, Any], suburb_id: int, property_type_id: Optional[int]) -> int:
    addr = n["address"]; addr_norm = clean_text(addr.lower() if addr else None, 500)
    addr_hash = _sha(f"{suburb_id}|{addr_norm}|{property_type_id or 0}") if addr_norm else _sha(f"{n['external_id']}|{suburb_id}|{property_type_id or 0}")
    r = _one(cur, "SELECT PropertyID FROM dbo.Property WHERE AddressHash=?", addr_hash)
    vals = (addr, addr, addr_norm, suburb_id, property_type_id, n["bedrooms"], n["bathrooms"], n["parking"], n["land_area_sqm"], n["building_area_sqm"])
    if r:
        pid = int(r[0]); cur.execute("UPDATE dbo.Property SET Address=?,AddressRaw=?,AddressNormalized=?,SuburbID=?,PropertyTypeID=?,NumberOfBedroom=?,NumberOfBath=?,Parkingslot=?,LandAreaSqm=?,BuildingAreaSqm=?,UpdatedAt=SYSDATETIME() WHERE PropertyID=?", *vals, pid); return pid
    cur.execute("INSERT INTO dbo.Property(Address,AddressRaw,AddressNormalized,AddressHash,SuburbID,PropertyTypeID,NumberOfBedroom,NumberOfBath,Parkingslot,LandAreaSqm,BuildingAreaSqm,FirstSeenAt,LastSeenAt) OUTPUT INSERTED.PropertyID VALUES (?,?,?,?,?,?,?,?,?,?,?,SYSDATETIME(),SYSDATETIME())", addr, addr, addr_norm, addr_hash, suburb_id, property_type_id, n["bedrooms"], n["bathrooms"], n["parking"], n["land_area_sqm"], n["building_area_sqm"])
    return int(cur.fetchone()[0])

def _upsert_agency(cur, n: Dict[str, Any]) -> Optional[int]:
    name = n["agency_name"]
    if not name: return None
    r = _one(cur, "SELECT AgencyID FROM dbo.Agency WHERE UPPER(Name)=?", name.upper())
    if r:
        aid = int(r[0]); cur.execute("UPDATE dbo.Agency SET Telephone=COALESCE(?,Telephone), Address=COALESCE(?,Address), AgencyProfileURL=COALESCE(?,AgencyProfileURL), AgencyExternalCode=COALESCE(?,AgencyExternalCode), UpdatedAt=SYSDATETIME() WHERE AgencyID=?", n["agency_phone"], n["agency_address"], n["agency_profile_url"], n["agency_external_code"], aid); return aid
    cur.execute("INSERT INTO dbo.Agency(Name,Telephone,Address,AgencyProfileURL,AgencyExternalCode,FirstSeenAt,LastSeenAt) OUTPUT INSERTED.AgencyID VALUES (?,?,?,?,?,SYSDATETIME(),SYSDATETIME())", name, n["agency_phone"], n["agency_address"], n["agency_profile_url"], n["agency_external_code"])
    return int(cur.fetchone()[0])

def _upsert_agents(cur, agents: List[Dict[str, Any]], agency_id: Optional[int]) -> List[Tuple[int, Dict[str, Any]]]:
    out=[]
    for a in agents:
        r = None
        if a.get("external_id"):
            r = _one(cur, "SELECT AgentID FROM dbo.Agent WHERE AgentExternalID=?", a["external_id"])
        if not r and a.get("profile_url"):
            r = _one(cur, "SELECT AgentID FROM dbo.Agent WHERE AgentProfileURL=?", a["profile_url"])
        if not r:
            r = _one(cur, "SELECT AgentID FROM dbo.Agent WHERE UPPER(AgentName)=? AND (AgentPhoneNumber=? OR (? IS NULL AND AgentPhoneNumber IS NULL))", a["name"].upper(), a.get("phone"), a.get("phone"))
        if r:
            agid=int(r[0]); cur.execute("UPDATE dbo.Agent SET AgencyID=COALESCE(?,AgencyID), AgentPhoneNumber=COALESCE(?,AgentPhoneNumber), AgentProfileURL=COALESCE(?,AgentProfileURL), AgentExternalID=COALESCE(?,AgentExternalID), UpdatedAt=SYSDATETIME() WHERE AgentID=?", agency_id, a.get("phone"), a.get("profile_url"), a.get("external_id"), agid)
        else:
            cur.execute("INSERT INTO dbo.Agent(AgencyID,AgentProfileURL,AgentName,AgentPhoneNumber,AgentExternalID,FirstSeenAt,LastSeenAt) OUTPUT INSERTED.AgentID VALUES (?,?,?,?,?,SYSDATETIME(),SYSDATETIME())", agency_id, a.get("profile_url"), a["name"], a.get("phone"), a.get("external_id")); agid=int(cur.fetchone()[0])
        out.append((agid,a))
    return out

def ingest_full_rows(db_path_or_conn, search_url, rows, full_scan=False, emit_events=True):
    conn = db_path_or_conn if hasattr(db_path_or_conn, "cursor") else connect(); own = not hasattr(db_path_or_conn, "cursor")
    summary={"rows_input":len(rows),"rows_processed":0,"rows_skipped":0,"properties_upserted":0,"listings_upserted":0,"snapshots_inserted":0,"events_created":0,"skipped_reasons":{}}
    try:
        ensure_listing_event_metadata_columns(conn)
        ensure_area_numeric_capacity(conn)
        ensure_listing_snapshot_size_columns(conn)
        ensure_listing_lifecycle_columns(conn)
        cur = conn.cursor(); validate_required_schema(conn); sid = _upsert_search(conn, search_url)
        suburb_id = int(_one(cur, "SELECT SuburbID FROM dbo.SuburbSearch WHERE SearchID=?", sid)[0])
        cur.execute("INSERT INTO dbo.ScrapeRun(SearchID,RunType,Status) OUTPUT INSERTED.RunID VALUES (?,?,'running')", sid, "full" if full_scan else "light"); run_id=int(cur.fetchone()[0])
        seen=set()
        for rr in rows:
            try:
                n = normalize_listing_row(rr)
            except ValueError:
                summary["rows_skipped"] += 1; summary["skipped_reasons"]["bad_listing_id"]=summary["skipped_reasons"].get("bad_listing_id",0)+1; continue
            summary["rows_processed"] += 1; ext=n["external_id"]; seen.add(ext)
            ptype_id=_upsert_property_type(cur, n["property_type"]); property_id=_upsert_property(cur, n, suburb_id, ptype_id); summary["properties_upserted"]+=1
            agency_id=_upsert_agency(cur,n); agents_data=_upsert_agents(cur,n["agents"],agency_id); agent_ids=[x[0] for x in agents_data]; primary_agent_id=agent_ids[0] if agent_ids else None
            desc_hash=_sha(str(n["description"] or ""))
            r=_one(cur,"SELECT listingID FROM dbo.Listing WHERE ExternalID=?",ext); is_new=not bool(r)
            if r:
                lid=int(r[0]); cur.execute("UPDATE dbo.Listing SET PropertyID=?,AgencyID=?,AgentID=?,ListingURL=?,CurrentPriceDisplay=?,Price=?,Description=?,CurrentDescriptionHash=?,CurrentStatus='active',LastTimeSeen=SYSDATETIME(),UpdatedAt=SYSDATETIME() WHERE listingID=?", property_id, agency_id, primary_agent_id, n["url"], n["price_display"], n["price_value"], n["description"], desc_hash, lid)
            else:
                cur.execute("INSERT INTO dbo.Listing(ExternalID,AgencyID,AgentID,PropertyID,FirstTimeSeen,LastTimeSeen,Price,Description,ListingURL,CurrentStatus,CurrentPriceDisplay,CurrentDescriptionHash) OUTPUT INSERTED.listingID VALUES (?,?,?, ?,SYSDATETIME(),SYSDATETIME(),?,?,?,'active',?,?)", ext, agency_id, primary_agent_id, property_id, n["price_value"], n["description"], n["url"], n["price_display"], desc_hash); lid=int(cur.fetchone()[0])
            summary["listings_upserted"]+=1
            cur.execute("MERGE dbo.ListingSearchState t USING (SELECT ? ListingID, ? SearchID) s ON t.ListingID=s.ListingID AND t.SearchID=s.SearchID WHEN MATCHED THEN UPDATE SET LastSeenAt=SYSDATETIME(), Status='active', ListingLifecycleStatus='active', StatusReason='active_list_observed', StatusEvidence=NULL, NotFoundCount=0, FirstNotFoundAt=NULL, LastNotFoundAt=NULL, RemovedAt=NULL, SoldAt=NULL, UpdatedAt=SYSDATETIME() WHEN NOT MATCHED THEN INSERT(ListingID,SearchID,FirstSeenAt,LastSeenAt,Status,ListingLifecycleStatus,NotFoundCount) VALUES(?,?,SYSDATETIME(),SYSDATETIME(),'active','active',0);", lid,sid,lid,sid)
            if listing_lifecycle_status_from_row(rr) == "sold":
                apply_listing_lifecycle_signal(conn, sid, lid, "sold", rr.get("StatusReason") or "sold_evidence", rr.get("StatusEvidence"), run_id=run_id, create_event=emit_events)
            snap_payload={"price_display":n["price_display"],"price_low":str(n["price_low"] or ""),"price_high":str(n["price_high"] or ""),"price_method":n["price_method"],"primary_agent_id":primary_agent_id,"inspection_short":n["inspection_short_label"],"inspection_long":n["inspection_long_label"],"auction_label":n["auction_label"],"description_hash":desc_hash,"url":n["url"],"land_size_display":n["land_size_display"],"land_size_sqm":str(n["land_size_sqm"] or ""),"building_size_display":n["building_size_display"],"building_size_sqm":str(n["building_size_sqm"] or ""),"floor_area_display":n["floor_area_display"],"floor_area_sqm":str(n["floor_area_sqm"] or "")}
            snap_hash=_sha(json_dumps_safe(snap_payload, sort_keys=True))
            prev=_one(cur,"SELECT TOP 1 SnapshotID,PriceLow,PriceHigh,PriceDisplay,PrimaryAgentID,InspectionShort,InspectionLong,AuctionTimeLabel,DescriptionHash FROM dbo.ListingSnapshot WHERE ListingID=? AND SearchID=? ORDER BY SnapshotID DESC", lid,sid)
            snapshot_params = (
                lid,
                sid,
                run_id,
                snap_hash,
                n["price_value"],
                n["price_display"],
                n["price_low"],
                n["price_high"],
                n["price_method"],
                primary_agent_id,
                primary_agent_id,
                "active",
                n["description"],
                desc_hash,
                agency_id,
                n["inspection_short_label"],
                n["inspection_long_label"],
                n["auction_label"],
                n["url"],
                n["land_size_display"],
                n["land_size_sqm"],
                n["building_size_display"],
                n["building_size_sqm"],
                n["floor_area_display"],
                n["floor_area_sqm"],
            )
            _assert_param_marker_count(LISTING_SNAPSHOT_INSERT_SQL, snapshot_params)
            cur.execute(LISTING_SNAPSHOT_INSERT_SQL, *snapshot_params)
            snap_id=int(cur.fetchone()[0]); summary["snapshots_inserted"]+=1
            for pos, (agid, agent_data) in enumerate(agents_data, start=1):
                cur.execute("INSERT INTO dbo.ListingSnapshotAgent(SnapshotID,AgentID,Position,PhoneAtSnapshot,RatingAtSnapshot,ReviewsTextAtSnapshot) VALUES (?,?,?,?,?,?)", snap_id, agid, pos, agent_data.get("phone"), None, None)
                cur.execute("IF NOT EXISTS (SELECT 1 FROM dbo.ListingAgentAssignment WHERE ListingID=? AND AgentID=? AND SearchID=? AND EndedAt IS NULL) INSERT INTO dbo.ListingAgentAssignment(ListingID,AgentID,SearchID,StartedAt) VALUES (?,?,?,SYSDATETIME())", lid,agid,sid,lid,agid,sid)
            event_context = listing_change_detector.build_event_payload({"external_id": ext, "address": n["address"], "listing_url": n["url"], "property_type": n["property_type"], "bedrooms": n["bedrooms"], "bathrooms": n["bathrooms"], "car_spaces": n["parking"], "price_display": n["price_display"], "price_low": n["price_low"], "price_high": n["price_high"], "price_method": n["price_method"], "agency_name": n["agency_name"], "agents": n["agents"], "inspection_short": n["inspection_short_label"], "inspection_long": n["inspection_long_label"], "auction_label": n["auction_label"]})
            events=[]
            if is_new: events.append(("new_listing",None,event_context))
            if prev and (prev[1],prev[2]) != (n["price_low"],n["price_high"]): events.append(("price_changed",{"price_display":prev[3],"estimated_price_low":str(prev[1] or ""),"estimated_price_high":str(prev[2] or "")},{"price_display":n["price_display"],"estimated_price_low":str(n["price_low"] or ""),"estimated_price_high":str(n["price_high"] or ""),"price_method":n["price_method"]}))
            if prev and prev[4] != primary_agent_id: events.append(("agent_change",{"primary_agent_id":prev[4]},{"primary_agent_id":primary_agent_id}))
            if prev and (prev[5],prev[6]) != (n["inspection_short_label"],n["inspection_long_label"]): events.append(("inspection_changed",{"inspection_summary":" | ".join(value for value in (prev[5],prev[6]) if value),"inspection_times":[value for value in (prev[5],prev[6]) if value]},{"inspection_summary":" | ".join(value for value in (n["inspection_short_label"],n["inspection_long_label"]) if value),"inspection_times":[value for value in (n["inspection_short_label"],n["inspection_long_label"]) if value]}))
            if prev and prev[7] != n["auction_label"]: events.append(("auction_changed",{"auction_label":prev[7],"auction_time":None},{"auction_label":n["auction_label"],"auction_time":None}))
            if prev and prev[8] != desc_hash: events.append(("description_change",{"description_hash":prev[8]},{"description_hash":desc_hash}))
            if emit_events:
                for et,ov,nv in events:
                    eh=_sha(json_dumps_safe({"sid": sid, "lid": lid, "et": et, "ov": ov, "nv": nv}, sort_keys=True))
                    should_notify = et in {"new_listing", "price_changed", "inspection_changed", "auction_changed"}
                    reason = "agent_metadata_enrichment" if et == "agent_change" else None
                    payload = {**event_context, "event_type": et, "old_value": ov, "new_value": nv, "should_notify": should_notify, "reason": reason}
                    cur.execute("IF NOT EXISTS (SELECT 1 FROM dbo.ListingEvent WHERE EventHash=?) INSERT INTO dbo.ListingEvent(RunID,SearchID,ListingID,EventType,EventHash,OldValueJson,NewValueJson,ShouldNotify,Reason,EventPayloadJson) VALUES (?,?,?,?,?,?,?,?,?,?)", eh,run_id,sid,lid,et,eh,json_dumps_safe(ov) if ov else None,json_dumps_safe(nv) if nv else None,should_notify,reason,json_dumps_safe(payload))
                    summary["events_created"] += 1
        if full_scan:
            cur.execute("SELECT l.listingID,l.ExternalID FROM dbo.Listing l JOIN dbo.ListingSearchState s ON s.ListingID=l.listingID WHERE s.SearchID=? AND s.Status='active'", sid)
            for lid, ext in cur.fetchall():
                if str(ext).strip() in seen: continue
                apply_listing_lifecycle_signal(conn, sid, int(lid), "not_found", "missing_from_full_scan", "listing missing from full scan", run_id=run_id, create_event=emit_events)
        cur.execute("UPDATE dbo.ScrapeRun SET Status='success',FinishedAt=SYSDATETIME(),RowsFull=? WHERE RunID=?", summary["rows_processed"], run_id)
        conn.commit(); print("Ingest summary:", summary); return run_id
    except Exception:
        conn.rollback();
        raise
    finally:
        if own:
            conn.close()


def ingest_light_check_rows(
    db_path_or_conn,
    search_url,
    rows,
    new_external_ids: Optional[set[str]] = None,
    full_scan: bool = False,
) -> dict:
    conn = db_path_or_conn if hasattr(db_path_or_conn, "cursor") else connect()
    own = not hasattr(db_path_or_conn, "cursor")
    ensure_listing_event_metadata_columns(conn)
    ensure_area_numeric_capacity(conn)
    ensure_listing_snapshot_size_columns(conn)
    summary = {
        "rows_input": len(rows),
        "rows_processed": 0,
        "new_inserted": 0,
        "existing_touched": 0,
        "snapshots_inserted": 0,
        "events_created": 0,
        "run_id": None,
    }
    only_new_ids = {str(x).strip() for x in (new_external_ids or set()) if x is not None}
    try:
        cur = conn.cursor()
        validate_required_schema(conn)
        sid = _upsert_search(conn, search_url)
        suburb_id = int(_one(cur, "SELECT SuburbID FROM dbo.SuburbSearch WHERE SearchID=?", sid)[0])
        cur.execute("INSERT INTO dbo.ScrapeRun(SearchID,RunType,Status) OUTPUT INSERTED.RunID VALUES (?,?,'running')", sid, "light")
        run_id = int(cur.fetchone()[0])
        summary["run_id"] = run_id

        for rr in rows:
            try:
                n = normalize_listing_row(rr)
            except ValueError:
                continue
            summary["rows_processed"] += 1
            ext = n["external_id"]
            r = _one(cur, "SELECT listingID FROM dbo.Listing WHERE ExternalID=?", ext)
            if r:
                lid = int(r[0])
                cur.execute(
                    "UPDATE dbo.Listing SET ListingURL=COALESCE(?,ListingURL), CurrentStatus='active', LastTimeSeen=SYSDATETIME(), UpdatedAt=SYSDATETIME() WHERE listingID=?",
                    n["url"],
                    lid,
                )
                cur.execute(
                    "MERGE dbo.ListingSearchState t USING (SELECT ? ListingID, ? SearchID) s ON t.ListingID=s.ListingID AND t.SearchID=s.SearchID "
                    "WHEN MATCHED THEN UPDATE SET LastSeenAt=SYSDATETIME(), Status='active', UpdatedAt=SYSDATETIME() "
                    "WHEN NOT MATCHED THEN INSERT(ListingID,SearchID,FirstSeenAt,LastSeenAt,Status) VALUES(?,?,SYSDATETIME(),SYSDATETIME(),'active');",
                    lid, sid, lid, sid,
                )
                summary["existing_touched"] += 1
                continue

            ptype_id = _upsert_property_type(cur, n["property_type"])
            property_id = _upsert_property(cur, n, suburb_id, ptype_id)
            agency_id = _upsert_agency(cur, n)
            agents_data = _upsert_agents(cur, n["agents"], agency_id)
            agent_ids = [x[0] for x in agents_data]
            primary_agent_id = agent_ids[0] if agent_ids else None
            desc_hash = _sha(str(n["description"] or ""))
            cur.execute(
                "INSERT INTO dbo.Listing(ExternalID,AgencyID,AgentID,PropertyID,FirstTimeSeen,LastTimeSeen,Price,Description,ListingURL,CurrentStatus,CurrentPriceDisplay,CurrentDescriptionHash) "
                "OUTPUT INSERTED.listingID VALUES (?,?,?, ?,SYSDATETIME(),SYSDATETIME(),?,?,?,'active',?,?)",
                ext, agency_id, primary_agent_id, property_id, n["price_value"], n["description"], n["url"], n["price_display"], desc_hash,
            )
            lid = int(cur.fetchone()[0])
            summary["new_inserted"] += 1
            cur.execute(
                "INSERT INTO dbo.ListingSearchState(ListingID,SearchID,FirstSeenAt,LastSeenAt,Status) VALUES(?,?,SYSDATETIME(),SYSDATETIME(),'active')",
                lid, sid,
            )

            if not only_new_ids or str(ext) in only_new_ids:
                snap_payload = {
                    "price_display": n["price_display"],
                    "price_low": str(n["price_low"] or ""),
                    "price_high": str(n["price_high"] or ""),
                    "price_method": n["price_method"],
                    "primary_agent_id": primary_agent_id,
                    "inspection_short": n["inspection_short_label"],
                    "inspection_long": n["inspection_long_label"],
                    "auction_label": n["auction_label"],
                    "description_hash": desc_hash,
                    "url": n["url"],
                    "land_size_display": n["land_size_display"],
                    "land_size_sqm": str(n["land_size_sqm"] or ""),
                    "building_size_display": n["building_size_display"],
                    "building_size_sqm": str(n["building_size_sqm"] or ""),
                    "floor_area_display": n["floor_area_display"],
                    "floor_area_sqm": str(n["floor_area_sqm"] or ""),
                }
                snap_hash = _sha(json_dumps_safe(snap_payload, sort_keys=True))
                snapshot_params = (
                    lid, sid, run_id, snap_hash, n["price_value"], n["price_display"], n["price_low"], n["price_high"],
                    n["price_method"], primary_agent_id, primary_agent_id, "active", n["description"], desc_hash, agency_id,
                    n["inspection_short_label"], n["inspection_long_label"], n["auction_label"], n["url"],
                    n["land_size_display"], n["land_size_sqm"], n["building_size_display"], n["building_size_sqm"],
                    n["floor_area_display"], n["floor_area_sqm"],
                )
                _assert_param_marker_count(LISTING_SNAPSHOT_INSERT_SQL, snapshot_params)
                cur.execute(LISTING_SNAPSHOT_INSERT_SQL, *snapshot_params)
                snap_id = int(cur.fetchone()[0])
                summary["snapshots_inserted"] += 1

                for pos, (agid, agent_data) in enumerate(agents_data, start=1):
                    cur.execute(
                        "INSERT INTO dbo.ListingSnapshotAgent(SnapshotID,AgentID,Position,PhoneAtSnapshot,RatingAtSnapshot,ReviewsTextAtSnapshot) VALUES (?,?,?,?,?,?)",
                        snap_id, agid, pos, agent_data.get("phone"), None, None,
                    )
                    cur.execute(
                        "IF NOT EXISTS (SELECT 1 FROM dbo.ListingAgentAssignment WHERE ListingID=? AND AgentID=? AND SearchID=? AND EndedAt IS NULL) "
                        "INSERT INTO dbo.ListingAgentAssignment(ListingID,AgentID,SearchID,StartedAt) VALUES (?,?,?,SYSDATETIME())",
                        lid, agid, sid, lid, agid, sid,
                    )

                event_context = listing_change_detector.build_event_payload({"external_id": ext, "address": n["address"], "listing_url": n["url"], "property_type": n["property_type"], "bedrooms": n["bedrooms"], "bathrooms": n["bathrooms"], "car_spaces": n["parking"], "price_display": n["price_display"], "price_low": n["price_low"], "price_high": n["price_high"], "price_method": n["price_method"], "agency_name": n["agency_name"], "agents": n["agents"], "inspection_short": n["inspection_short_label"], "inspection_long": n["inspection_long_label"], "auction_label": n["auction_label"]})
                ev_hash = _sha(json_dumps_safe({"sid": sid, "lid": lid, "et": "new_listing", "ext": str(ext)}, sort_keys=True))
                cur.execute(
                    "IF NOT EXISTS (SELECT 1 FROM dbo.ListingEvent WHERE EventHash=?) "
                    "INSERT INTO dbo.ListingEvent(RunID,SearchID,ListingID,EventType,EventHash,OldValueJson,NewValueJson,ShouldNotify,Severity,EventPayloadJson) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    ev_hash, run_id, sid, lid, "new_listing", ev_hash, None, json_dumps_safe(event_context), True, "normal", json_dumps_safe({**event_context, "event_type": "new_listing", "field": "listing", "new_value": event_context, "should_notify": True, "severity": "normal"}),
                )
                if cur.rowcount and cur.rowcount > 0:
                    summary["events_created"] += 1

        cur.execute("UPDATE dbo.ScrapeRun SET Status='success',FinishedAt=SYSDATETIME(),RowsFull=? WHERE RunID=?", summary["rows_processed"], run_id)
        conn.commit()
        return summary
    except Exception:
        conn.rollback()
        raise
    finally:
        if own:
            conn.close()


def get_existing_external_ids_for_search(conn, search_url: str) -> set[str]:
    """Return existing listing ExternalID values linked to the provided search URL."""
    normalized = ensure_sort_list_date(search_url)
    search_hash = _sha(normalized)
    cur = conn.cursor()
    row = _one(cur, "SELECT SearchID FROM dbo.SuburbSearch WHERE SearchHash=?", search_hash)
    if not row:
        return set()

    sid = int(row[0])
    cur.execute(
        """
        SELECT l.ExternalID
        FROM dbo.SuburbSearch ss
        JOIN dbo.ListingSearchState lss ON lss.SearchID = ss.SearchID
        JOIN dbo.Listing l ON l.listingID = lss.ListingID
        WHERE ss.SearchID = ? AND l.ExternalID IS NOT NULL
        """,
        sid,
    )
    return {str(r[0]).strip() for r in cur.fetchall() if r and r[0] is not None}


def get_latest_listing_state(conn, external_id: str) -> dict | None:
    cur = conn.cursor()
    row = _one(cur, """
        SELECT l.listingID,l.ExternalID,l.CurrentStatus,l.CurrentPriceDisplay,l.CurrentDescriptionHash,l.AgencyID,
               l.ListingURL,l.Description,p.Address,pt.PropertyType,p.NumberOfBedroom,p.NumberOfBath,p.Parkingslot,
               p.LandAreaSqm,p.BuildingAreaSqm
        FROM dbo.Listing l
        LEFT JOIN dbo.Property p ON p.PropertyID=l.PropertyID
        LEFT JOIN dbo.PropertyType pt ON pt.ID=p.PropertyTypeID
        WHERE l.ExternalID=?
        """, external_id)
    if not row:
        return None
    listing_id = int(row[0])
    state = {
        "listing_id": listing_id,
        "external_id": str(row[1]),
        "status": row[2],
        "price_display": row[3],
        "ad_price_display": row[3],
        "description_hash": row[4],
        "agency_id": row[5],
        "listing_url": row[6],
        "url": row[6],
        "description": row[7],
        "address": row[8],
        "property_type": row[9],
        "bedrooms": row[10],
        "bathrooms": row[11],
        "car_spaces": row[12],
        "parking": row[12],
        "land_size_sqm": row[13],
        "building_size_sqm": row[14],
        "price_low": None,
        "price_high": None,
        "price_method": None,
        "detail_price_display": None,
        "agency_name": None,
        "agency_code": None,
        "agency_profile_url": None,
        "agents": [],
        "inspection_short": None,
        "inspection_long": None,
        "auction_label": None,
        "auction_time": None,
        "sold_date": None,
        "sold_price": None,
    }
    s = _one(cur, """
        SELECT TOP 1 SnapshotID,PriceDisplay,PriceLow,PriceHigh,PriceMethod,DescriptionHash,
               InspectionShort,InspectionLong,AuctionTimeLabel,Status,Description,URL,
               LandSizeDisplay,LandSizeSqm,BuildingSizeDisplay,BuildingSizeSqm,FloorAreaDisplay,FloorAreaSqm,
               AgencyID
        FROM dbo.ListingSnapshot
        WHERE ListingID=?
        ORDER BY SnapshotID DESC
        """, listing_id)
    if s:
        state.update({
            "snapshot_id": int(s[0]),
            "price_display": s[1],
            "ad_price_display": s[1],
            "detail_price_display": s[1],
            "price_low": s[2],
            "price_high": s[3],
            "price_method": s[4],
            "description_hash": s[5],
            "inspection_short": s[6],
            "inspection_long": s[7],
            "auction_label": s[8],
            "status": s[9] or state.get("status"),
            "description": s[10],
            "listing_url": s[11] or state.get("listing_url"),
            "url": s[11] or state.get("url"),
            "land_size_display": s[12],
            "land_size_sqm": s[13] if s[13] is not None else state.get("land_size_sqm"),
            "building_size_display": s[14],
            "building_size_sqm": s[15] if s[15] is not None else state.get("building_size_sqm"),
            "floor_area_display": s[16],
            "floor_area_sqm": s[17],
            "agency_id": s[18] or state.get("agency_id"),
        })
        cur.execute("SELECT a.AgentExternalID,a.AgentName,COALESCE(lsa.PhoneAtSnapshot,a.AgentPhoneNumber),a.AgentProfileURL FROM dbo.ListingSnapshotAgent lsa JOIN dbo.Agent a ON a.AgentID=lsa.AgentID WHERE lsa.SnapshotID=? ORDER BY lsa.Position", int(s[0]))
        state["agents"] = [{"agent_id": r[0], "name": r[1], "phone": r[2], "profile_url": r[3]} for r in cur.fetchall()]
    if state.get("agency_id"):
        ag = _one(cur, "SELECT Name,AgencyExternalCode,AgencyProfileURL FROM dbo.Agency WHERE AgencyID=?", state["agency_id"])
        if ag:
            state.update({"agency_name": ag[0], "agency_code": ag[1], "agency_profile_url": ag[2]})
    return state


_DETAIL_STATE_TO_ROW_KEYS = {
    "address": ("address",),
    "listing_url": ("url", "listing_url"),
    "property_type": ("property_type",),
    "bedrooms": ("bedrooms",),
    "bathrooms": ("bathrooms",),
    "car_spaces": ("parking", "car_spaces"),
    "price_display": ("AdPriceDisplay", "ad_price_display", "price_display", "price"),
    "price_low": ("AdPriceLow", "price_low"),
    "price_high": ("AdPriceHigh", "price_high"),
    "detail_price_display": ("detail_price_display",),
    "description": ("description",),
    "agency_name": ("agency_name", "agency"),
    "agency_code": ("agency_code",),
    "agency_profile_url": ("agency_profile_url",),
    "agents": ("agents",),
    "inspection_short": ("inspection_short_label", "inspection_short"),
    "inspection_long": ("inspection_long_label", "inspection_long"),
    "auction_label": ("auction_label",),
    "auction_time": ("auction_time",),
    "land_size_display": ("LandSizeDisplay", "land_size_display"),
    "land_size_sqm": ("LandSizeSqm", "land_size_sqm"),
    "building_size_display": ("BuildingSizeDisplay", "building_size_display"),
    "building_size_sqm": ("BuildingSizeSqm", "building_size_sqm"),
    "floor_area_display": ("FloorAreaDisplay", "floor_area_display"),
    "floor_area_sqm": ("FloorAreaSqm", "floor_area_sqm"),
}


def _has_non_empty_row_value(row: dict, keys: tuple[str, ...]) -> bool:
    for key in keys:
        value = row.get(key)
        if value not in (None, "", [], {}):
            return True
    return False


def merge_enriched_listing_detail_with_latest(row: dict, latest_state: dict | None) -> dict:
    """Merge Module3 detail data with latest DB state without clearing good values."""
    merged = dict(row)
    if not latest_state:
        return merged
    for state_key, row_keys in _DETAIL_STATE_TO_ROW_KEYS.items():
        if _has_non_empty_row_value(merged, row_keys):
            continue
        value = latest_state.get(state_key)
        if value in (None, "", [], {}):
            continue
        merged[row_keys[0]] = value
    return merged




def normalize_scrape_run_type(source: str | None = None, run_type: str | None = None) -> str:
    allowed = {"full", "light", "enrich_single"}
    rt = clean_text(run_type, 40)
    if rt in allowed:
        return rt

    src = (clean_text(source, 80) or "").lower()
    if src in {"light", "light_check", "area_light_check"}:
        return "light"
    if src in {"full", "full_refresh", "pipeline"}:
        return "full"
    if src in {"change_detection", "detail_refresh", "enrich", "enrich_single", "manual_change_detection"}:
        return "enrich_single"
    return "enrich_single"


def create_lightweight_scrape_run(conn, search_id: int | None, source: str = "change_detection", run_type: str | None = None) -> int:
    cur = conn.cursor()
    safe_run_type = normalize_scrape_run_type(source=source, run_type=run_type)
    cur.execute(
        "INSERT INTO dbo.ScrapeRun(SearchID,RunType,Status) OUTPUT INSERTED.RunID VALUES (?,?,'success')",
        search_id,
        safe_run_type,
    )
    return int(cur.fetchone()[0])

def _compute_event_hash(listing_id: int, event_type: str, event_payload: dict) -> str:
    base = {
        "listing_id": listing_id,
        "event_type": event_type,
        "field": event_payload.get("field"),
        "old_value": event_payload.get("old_value"),
        "new_value": event_payload.get("new_value"),
    }
    return _sha(json_dumps_safe(base, sort_keys=True))




def listing_event_exists_by_hash(conn, event_hash: str) -> bool:
    r = _one(conn.cursor(), "SELECT 1 FROM dbo.ListingEvent WHERE EventHash=?", event_hash)
    return bool(r)

def create_listing_event_if_new(conn, listing_id: int, event_type: str, event_payload: dict, event_hash: str | None = None, search_id: int | None = None, run_id: int | None = None, suppress_notifications: bool = False) -> bool:
    if run_id is None:
        raise ValueError("run_id is required for ListingEvent insert (ListingEvent.RunID is NOT NULL)")
    ensure_listing_event_metadata_columns(conn)
    payload = dict(event_payload)
    if suppress_notifications:
        payload["should_notify"] = False
        payload["reason"] = "initial_detail_baseline"
    cur = conn.cursor()
    eh = event_hash or _compute_event_hash(listing_id, event_type, payload)
    old_value = payload.get("old_value")
    new_value = payload.get("new_value")
    event_context = dict(payload.get("event_payload") or {})
    event_context.update({key: value for key, value in payload.items() if key != "event_payload"})
    cur.execute("IF NOT EXISTS (SELECT 1 FROM dbo.ListingEvent WHERE EventHash=?) INSERT INTO dbo.ListingEvent(RunID,SearchID,ListingID,EventType,EventHash,OldValueJson,NewValueJson,ShouldNotify,Severity,Reason,EventPayloadJson) VALUES (?,?,?,?,?,?,?,?,?,?,?)", eh, run_id, search_id, listing_id, event_type, eh, json_dumps_safe(old_value) if old_value is not None else None, json_dumps_safe(new_value) if new_value is not None else None, payload.get("should_notify"), payload.get("severity"), payload.get("reason"), json_dumps_safe(event_context))
    return bool(cur.rowcount and cur.rowcount > 0)


def detect_and_record_changes_for_row(conn, search_url: str, row: dict, run_id: int | None = None, create_events: bool = True, context: str | None = None, suppress_notifications: bool = False) -> dict:
    external_id = extract_external_listing_id(row)
    warnings = []
    if row.get("detail_refresh_success") is False or row.get("detail_extraction_quality") == "failed":
        warnings.append({
            "warning": "detail_refresh_failed_skip_change_detection",
            "db_listing_id": row.get("db_listing_id") or row.get("internal_listing_id"),
            "external_id": external_id,
            "error": row.get("detail_refresh_error") or row.get("detail_error"),
        })
        return {
            "external_id": external_id,
            "run_id": run_id,
            "events_detected": [],
            "events_created": 0,
            "should_notify_events": [],
            "warnings": warnings,
        }
    old_state = get_latest_listing_state(conn, external_id)
    normalized_row = merge_enriched_listing_detail_with_latest(row, old_state)
    normalized_row["external_id"] = external_id
    normalized_row["listing_id"] = external_id
    n = normalize_listing_row(normalized_row)
    new_state = {
        "external_id": external_id,
        "area_label": clean_text(normalized_row.get("area_label") or normalized_row.get("search_display_name"), 255),
        "address": n.get("address"),
        "listing_url": n.get("url"),
        "property_type": n.get("property_type"),
        "bedrooms": n.get("bedrooms"),
        "bathrooms": n.get("bathrooms"),
        "car_spaces": n.get("parking"),
        "status": listing_change_detector.normalize_status(row),
        "price_display": n.get("price_display"),
        "price_low": n.get("price_low"),
        "price_high": n.get("price_high"),
        "price_method": n.get("price_method"),
        "ad_price_display": n.get("price_display"),
        "ad_price_low": n.get("price_low"),
        "ad_price_high": n.get("price_high"),
        "detail_price_display": clean_text(normalized_row.get("detail_price_display"), 300) or n.get("price_display"),
        "land_size_display": n.get("land_size_display"),
        "land_size_sqm": n.get("land_size_sqm"),
        "building_size_display": n.get("building_size_display"),
        "building_size_sqm": n.get("building_size_sqm"),
        "floor_area_display": n.get("floor_area_display"),
        "floor_area_sqm": n.get("floor_area_sqm"),
        "description_hash": _sha(str(n.get("description") or "")),
        "description": n.get("description"),
        "agency_id": None,
        "agency_name": n.get("agency_name"),
        "agency_code": n.get("agency_external_code"),
        "agency_profile_url": n.get("agency_profile_url"),
        "agents": listing_change_detector.normalize_agents_for_compare(row),
        "inspection_short": n.get("inspection_short_label"),
        "inspection_long": n.get("inspection_long_label"),
        "auction_label": n.get("auction_label"),
        "auction_time": clean_text(row.get("auction_time"), 120),
        "sold_date": row.get("sold_date"),
        "sold_price": row.get("sold_price"),
        "detail_refresh_success": row.get("detail_refresh_success"),
        "detail_extraction_quality": row.get("detail_extraction_quality"),
        "detail_refresh_error": row.get("detail_refresh_error"),
        "detail_reliable_fields": row.get("detail_reliable_fields"),
        "detail_agents_reliable": row.get("detail_agents_reliable"),
        "detail_agency_reliable": row.get("detail_agency_reliable"),
        "detail_price_reliable": row.get("detail_price_reliable"),
        "detail_description_reliable": row.get("detail_description_reliable"),
        "detail_inspection_reliable": row.get("detail_inspection_reliable"),
        "detail_auction_reliable": row.get("detail_auction_reliable"),
        "detail_status_reliable": row.get("detail_status_reliable"),
        "agents_explicitly_absent": row.get("agents_explicitly_absent"),
        "agency_explicitly_absent": row.get("agency_explicitly_absent"),
        "auction_explicitly_absent": row.get("auction_explicitly_absent"),
        "inspection_explicitly_absent": row.get("inspection_explicitly_absent"),
        "old_agents_reliable": row.get("old_agents_reliable"),
    }
    sold_guard = _sold_guard_diagnostics(conn, search_url, row, old_state, new_state)
    warnings.extend(sold_guard.get("warnings", []))
    if row.get("detail_extraction_quality") == "partial":
        if "detail_price_display" not in row and "price" not in row and "price_display" not in row and "AdPriceDisplay" not in row:
            for key in ("price_display", "price_low", "price_high", "price_method", "detail_price_display"):
                new_state.pop(key, None)
        if not any(key in row for key in ("LandSizeDisplay", "land_size_display", "LandSizeSqm", "land_size_sqm")):
            for key in ("land_size_display", "land_size_sqm"):
                new_state.pop(key, None)
        if not any(key in row for key in ("BuildingSizeDisplay", "building_size_display", "BuildingSizeSqm", "building_size_sqm")):
            for key in ("building_size_display", "building_size_sqm"):
                new_state.pop(key, None)
        if not any(key in row for key in ("FloorAreaDisplay", "floor_area_display", "FloorAreaSqm", "floor_area_sqm")):
            for key in ("floor_area_display", "floor_area_sqm"):
                new_state.pop(key, None)
        if "description" not in row and "description_hash" not in row:
            new_state.pop("description", None)
            new_state.pop("description_hash", None)
        if not any(key in row for key in ("agency_name", "agency_code", "agency_profile_url")):
            for key in ("agency_id", "agency_name", "agency_code", "agency_profile_url"):
                new_state.pop(key, None)
        if "agents" not in row and not any(key.startswith("agent_") for key in row):
            new_state.pop("agents", None)
    if old_state is None and (row.get("db_listing_id") is not None or row.get("internal_listing_id") is not None):
        warnings.append({
            "warning": "old_state_missing_for_existing_listing",
            "db_listing_id": row.get("db_listing_id") or row.get("internal_listing_id"),
            "external_id": external_id,
        })
        events = []
    else:
        events = listing_change_detector.compare_listing_state(old_state, new_state, context=context, suppress_notifications=suppress_notifications)
    created = 0
    final_run_id = run_id
    if create_events and events and old_state and old_state.get("listing_id"):
        sid = _upsert_search(conn, search_url)
        listing_id = int(old_state["listing_id"])
        candidates = []
        for ev in events:
            eh = _compute_event_hash(listing_id, ev["event_type"], ev)
            candidates.append((ev, eh, listing_event_exists_by_hash(conn, eh)))
        if any(not exists for _, _, exists in candidates):
            if final_run_id is None:
                final_run_id = create_lightweight_scrape_run(conn, sid, source="change_detection")
            for ev, eh, exists in candidates:
                if exists:
                    continue
                if create_listing_event_if_new(conn, listing_id, ev["event_type"], ev, event_hash=eh, search_id=sid, run_id=final_run_id, suppress_notifications=suppress_notifications):
                    created += 1
    return {
        "external_id": external_id,
        "run_id": final_run_id,
        "events_detected": events,
        "events_created": created,
        "should_notify_events": [e for e in events if e.get("should_notify")],
        "warnings": warnings,
        "suppressed_sold_count": int(sold_guard.get("suppressed_sold_count", 0)),
        "weak_sold_evidence_count": int(sold_guard.get("weak_sold_evidence_count", 0)),
        "strong_sold_evidence_count": int(sold_guard.get("strong_sold_evidence_count", 0)),
        "effective_lifecycle_status": sold_guard.get("effective_lifecycle_status"),
    }


PRICE_NOT_FOUND_DISPLAY = "نتوانستیم قیمت را پیدا کنیم"


def _format_money(value):
    if value is None or value == "":
        return ""
    try:
        return f"${Decimal(str(value)):,.0f}"
    except Exception:
        return str(value)


def _format_export_price_range(low, high) -> str:
    low_text, high_text = _format_money(low), _format_money(high)
    if low_text and high_text and low_text != high_text:
        return f"{low_text} - {high_text}"
    if low_text or high_text:
        return low_text or high_text
    return ""


def _meaningful_ad_price_text(value: Any) -> str | None:
    display = clean_text(value or "", 300)
    if not display:
        return None
    return None if display.lower() in {"n/a", "na", "none", "null", "unknown"} else display


def _legacy_inferred_low_high(row: dict) -> tuple[Any, Any]:
    method = str(row.get("price_method") or row.get("price_source") or "").lower()
    if "sliding_between_window" in method or "module2" in method:
        return row.get("price_low"), row.get("price_high")
    return None, None


def _export_inferred_low_high(row: dict) -> tuple[Any, Any]:
    low = row.get("inferred_price_low")
    high = row.get("inferred_price_high")
    if low is not None or high is not None:
        return low, high
    return _legacy_inferred_low_high(row)


def _export_ad_price_display(row: dict) -> str | None:
    return _meaningful_ad_price_text(row.get("ad_price_display") or row.get("price_display") or row.get("detail_price_display"))


def _export_effective_price_display(row: dict) -> str:
    ad_display = _export_ad_price_display(row)
    if ad_display:
        return ad_display
    inferred_low, inferred_high = _export_inferred_low_high(row)
    inferred_range = _format_export_price_range(inferred_low, inferred_high)
    return inferred_range or PRICE_NOT_FOUND_DISPLAY


def _export_price_status(row: dict) -> str:
    status = clean_text(row.get("price_inference_status") or row.get("legacy_price_inference_status"), none_if_missing=True)
    return status or "not_attempted"


def _export_direct_price_text(row: dict) -> str | None:
    return _export_ad_price_display(row)


def _export_price_source(row: dict) -> str:
    if _export_ad_price_display(row):
        return "ad_price"
    inferred_low, inferred_high = _export_inferred_low_high(row)
    if inferred_low is not None or inferred_high is not None:
        return "inferred_range"
    return "unknown"


def _legacy_format_export_price_range(low, high) -> str:
    def money(value):
        if value is None or value == "":
            return ""
        try:
            return f"${Decimal(str(value)):,.0f}"
        except Exception:
            return str(value)
    low_text, high_text = money(low), money(high)
    if low_text and high_text and low_text != high_text:
        return f"Estimated range: {low_text} - {high_text}"
    if low_text or high_text:
        return f"Estimated range: {low_text or high_text}"
    return "Unknown"


def export_latest_to_rows(conn, area_url):
    ensure_monitoring_state_tables(conn)
    ensure_area_numeric_capacity(conn)
    ensure_listing_snapshot_size_columns(conn)
    ensure_listing_lifecycle_columns(conn)
    sid = _upsert_search(conn, area_url); c=conn.cursor()
    c.execute("""
    WITH last_snap AS (
      SELECT ls.*, ROW_NUMBER() OVER (PARTITION BY ls.ListingID, ls.SearchID ORDER BY ls.SnapshotDate DESC, ls.SnapshotID DESC) rn
      FROM dbo.ListingSnapshot ls WHERE ls.SearchID=?
    ),
    latest_price_state AS (
      SELECT pis.*, ROW_NUMBER() OVER (
        PARTITION BY pis.listing_id, pis.area_id
        ORDER BY pis.last_attempt_at DESC, pis.updated_at DESC
      ) rn
      FROM dbo.listing_price_inference_state pis WHERE pis.area_id=?
    )
    SELECT l.listingID internal_listing_id, l.ExternalID listing_id, COALESCE(ls.URL,l.ListingURL) url,
           p.Address address, pt.PropertyType property_type, p.NumberOfBedroom bedrooms, p.NumberOfBath bathrooms, p.Parkingslot parking,
           ls.PriceDisplay price_display, ls.PriceDisplay ad_price_display, ls.PriceLow ad_price_low, ls.PriceHigh ad_price_high,
           ls.PriceLow price_low, ls.PriceHigh price_high, ls.PriceMethod price_method,
           ls.LandSizeDisplay land_size_display, COALESCE(ls.LandSizeSqm, p.LandAreaSqm) land_size_sqm,
           ls.BuildingSizeDisplay building_size_display, COALESCE(ls.BuildingSizeSqm, p.BuildingAreaSqm) building_size_sqm,
           ls.FloorAreaDisplay floor_area_display, ls.FloorAreaSqm floor_area_sqm,
           ls.InspectionShort inspection_short, ls.InspectionLong inspection_long, ls.AuctionTimeLabel auction_label, NULL auction_time,
           ag.Name agency_name, ag.AgencyExternalCode agency_code, ag.AgencyProfileURL agency_profile_url, ag.Address agency_address,
           ls.PriceDisplay detail_price_display,
           COALESCE(pis.inferred_low, CASE WHEN LOWER(COALESCE(lss.InferredPriceMethod, ''))='sliding_between_window' THEN lss.InferredPriceLow END) inferred_price_low,
           COALESCE(pis.inferred_high, CASE WHEN LOWER(COALESCE(lss.InferredPriceMethod, ''))='sliding_between_window' THEN lss.InferredPriceHigh END) inferred_price_high,
           COALESCE(pis.method, lss.InferredPriceMethod) price_inference_method,
           COALESCE(pis.status, lss.PriceInferenceStatus) price_inference_status,
           COALESCE(pis.last_error, lss.PriceInferenceLastError) price_inference_last_error,
           COALESCE(pis.last_attempt_at, lss.LastPriceInferenceAt) last_price_check,
           COALESCE(lss.ListingLifecycleStatus, lss.Status, l.CurrentStatus, 'active') ListingLifecycleStatus,
           lss.StatusReason, lss.StatusEvidence, lss.NotFoundCount,
           lss.FirstNotFoundAt, lss.LastNotFoundAt, lss.RemovedAt, lss.SoldAt, lss.LastStatusChangeAt,
           lss.Status area_status, lss.LastSeenAt area_last_seen_at, ls.SnapshotDate scraped_at, ls.Description description,
           ls.SnapshotID snapshot_id
    FROM last_snap ls JOIN dbo.Listing l ON l.listingID=ls.ListingID
    JOIN dbo.ListingSearchState lss ON lss.ListingID=l.listingID AND lss.SearchID=ls.SearchID
    LEFT JOIN latest_price_state pis ON pis.area_id=ls.SearchID AND pis.listing_id=CAST(l.ExternalID AS NVARCHAR(100)) AND pis.rn=1
    LEFT JOIN dbo.Property p ON p.PropertyID=l.PropertyID
    LEFT JOIN dbo.PropertyType pt ON pt.ID=p.PropertyTypeID
    LEFT JOIN dbo.Agency ag ON ag.AgencyID=ls.AgencyID
    WHERE ls.rn=1
    """, sid, sid)
    cols=[x[0] for x in c.description]; out=[]
    for r in c.fetchall():
        d={cols[i]:r[i] for i in range(len(cols))}
        c2=conn.cursor(); c2.execute("SELECT a.AgentName,a.AgentPhoneNumber FROM dbo.ListingSnapshotAgent lsa JOIN dbo.Agent a ON a.AgentID=lsa.AgentID WHERE lsa.SnapshotID=? ORDER BY lsa.Position", d["snapshot_id"])
        agents=[f"{x[0]} ({x[1]})" if x[1] else x[0] for x in c2.fetchall() if x[0]]
        d["agents"] = "; ".join(agents); d.pop("snapshot_id",None)
        inferred_low, inferred_high = _export_inferred_low_high(d)
        d["AdPriceDisplay"] = d.get("ad_price_display") or ""
        d["AdPriceLow"] = d.get("ad_price_low")
        d["AdPriceHigh"] = d.get("ad_price_high")
        d["LandSizeDisplay"] = d.get("land_size_display") or ""
        d["LandSizeSqm"] = d.get("land_size_sqm")
        d["BuildingSizeDisplay"] = d.get("building_size_display") or ""
        d["BuildingSizeSqm"] = d.get("building_size_sqm")
        d["FloorAreaDisplay"] = d.get("floor_area_display") or ""
        d["FloorAreaSqm"] = d.get("floor_area_sqm")
        d["InferredPriceLow"] = inferred_low
        d["InferredPriceHigh"] = inferred_high
        d["InferredPriceRange"] = _format_export_price_range(inferred_low, inferred_high)
        d["effective_price_display"] = _export_effective_price_display(d)
        d["Price"] = d["effective_price_display"]
        d["PriceStatus"] = _export_price_status(d)
        d["PriceSource"] = _export_price_source(d)
        d["PriceLow"] = d.get("price_low")
        d["PriceHigh"] = d.get("price_high")
        d["PriceInferenceStatus"] = d["PriceStatus"]
        d["PriceInferenceLastError"] = d.get("price_inference_last_error")
        d["PriceInferenceLastAttemptAt"] = d.get("last_price_check")
        d["LastPriceCheck"] = d.get("last_price_check")
        out.append(d)
    return out

def _unquote_sql_identifier(name: str) -> str:
    name = (name or "").strip()
    if name.startswith("[") and name.endswith("]"):
        return name[1:-1]
    return name

# untouched helpers from original

def add_user_area(conn, user_id, url):
    sid = _upsert_search(conn, url)
    cur = conn.cursor(); cur.execute("INSERT INTO dbo.UserSuburbMonitor(UserID,SearchID,IsActive) SELECT ?,?,1 WHERE NOT EXISTS (SELECT 1 FROM dbo.UserSuburbMonitor WHERE UserID=? AND SearchID=? AND IsActive=1)", user_id, sid, user_id, sid)
    conn.commit(); return True, f"Area added: {sid}"

def update_user_area(conn, user_id, old_area_id, new_search_url):
    new_sid = _upsert_search(conn, new_search_url)
    cur = conn.cursor(); cur.execute("UPDATE dbo.UserSuburbMonitor SET IsActive=0,DateRemoved=SYSDATETIME() WHERE UserID=? AND SearchID=? AND IsActive=1", user_id, old_area_id)
    cur.execute("INSERT INTO dbo.UserSuburbMonitor(UserID,SearchID,IsActive) VALUES (?,?,1)", user_id, new_sid)
    conn.commit(); return True, "updated"

def upsert_tg_user(conn,user_id,chat_id,username=None,display_name=None):
    cur=conn.cursor(); approved='approved' if config.AUTO_APPROVE_TELEGRAM_USERS else 'pending'
    cur.execute("""MERGE dbo.[User] t USING (SELECT ? UserID) s ON t.UserID=s.UserID
    WHEN MATCHED THEN UPDATE SET ChatID=?, UserName=COALESCE(?,t.UserName), DisplayName=COALESCE(?,t.DisplayName), UpdatedAt=SYSDATETIME(), LastSeenAt=SYSDATETIME()
    WHEN NOT MATCHED THEN INSERT (UserID,ChatID,UserName,DisplayName,AccessStatus,AccessStatusCode,CreatedAt,LastSeenAt) VALUES (?,?,?,?,?,?,SYSDATETIME(),SYSDATETIME());""",user_id,chat_id,username,display_name,user_id,chat_id,username,display_name,1 if approved=='approved' else 0,approved)
    cur.execute("""MERGE dbo.UserSetting t USING (SELECT ? UserID) s ON t.UserID=s.UserID
    WHEN NOT MATCHED THEN INSERT (UserID, PollIntervalMinutes, PerfMode) VALUES (?, ?, 'normal');""",user_id,user_id,config.DEFAULT_POLL_INTERVAL_MINUTES)
    conn.commit()

def touch_user_seen(conn,user_id): conn.cursor().execute("UPDATE dbo.[User] SET LastSeenAt=SYSDATETIME() WHERE UserID=?",user_id); conn.commit()
def get_user_settings(conn,user_id): return {"poll_interval_minutes":config.DEFAULT_POLL_INTERVAL_MINUTES,"perf_mode":"normal"}
def set_user_interval(conn,user_id,m): conn.cursor().execute("UPDATE dbo.UserSetting SET PollIntervalMinutes=?, UpdatedAt=SYSDATETIME() WHERE UserID=?",m,user_id); conn.commit()
def set_user_next_run(conn,user_id,dt): conn.cursor().execute("UPDATE dbo.UserSetting SET NextRunAt=?, UpdatedAt=SYSDATETIME() WHERE UserID=?",dt,user_id); conn.commit()
def set_user_perf_mode(conn,user_id,mode): pass

def due_users(conn):
    cur=conn.cursor(); cur.execute("SELECT u.UserID,u.ChatID,us.PollIntervalMinutes FROM dbo.[User] u JOIN dbo.UserSetting us ON u.UserID=us.UserID WHERE u.AccessStatusCode='approved' AND (us.NextRunAt IS NULL OR us.NextRunAt<=SYSDATETIME())")
    return [dict(user_id=r[0],chat_id=r[1],poll_interval_minutes=r[2]) for r in cur.fetchall()]

def remove_user_area(conn,user_id,area_id): conn.cursor().execute("UPDATE dbo.UserSuburbMonitor SET IsActive=0,DateRemoved=SYSDATETIME() WHERE UserID=? AND SearchID=?",user_id,area_id); conn.commit()
def get_user_areas(conn,user_id):
    c=conn.cursor(); c.execute("SELECT m.SearchID,s.NormalizedSearchURL,s.DisplayName FROM dbo.UserSuburbMonitor m JOIN dbo.SuburbSearch s ON s.SearchID=m.SearchID WHERE m.UserID=? AND m.IsActive=1",user_id)
    return [{"area_id":r[0],"search_url":r[1],"name":r[2]} for r in c.fetchall()]
def get_or_create_area(conn,url): return _upsert_search(conn,url)
def listing_seen_in_area(conn,area_id,listing_id):
    r=_one(conn.cursor(),"SELECT 1 FROM dbo.Listing l JOIN dbo.ListingSearchState s ON s.ListingID=l.listingID WHERE s.SearchID=? AND l.ExternalID=?",area_id,int(listing_id)); return bool(r)
def get_unsent_events_for_area(conn, area_id, types):
    q="SELECT EventID,ListingID,EventType,OldValueJson,NewValueJson,CreatedAt FROM dbo.ListingEvent WHERE SearchID=?"+(" AND EventType IN (%s)"%(','.join('?'*len(types))) if types else "")
    c=conn.cursor(); c.execute(q, [area_id,*types] if types else [area_id]);
    return [{"event_id":r[0],"listing_id":r[1],"event_type":r[2],"payload_json":r[4],"created_at":str(r[5])} for r in c.fetchall()]
def mark_events_sent(conn,event_ids): pass

def get_listing_internal_id_by_external_id(conn, external_id):
    r=_one(conn.cursor(),"SELECT listingID FROM dbo.Listing WHERE ExternalID=?", int(external_id))
    return int(r[0]) if r else None

def enqueue_enrich_job(conn, user_id, chat_id, area_id, listing_id, listing_url):
    internal_id = get_listing_internal_id_by_external_id(conn, listing_id) or listing_id
    c=conn.cursor(); c.execute("INSERT INTO dbo.Job(JobType,UserID,ChatID,SearchID,ListingID,ListingURL,Status) OUTPUT INSERTED.JobID VALUES('enrich_listing',?,?,?,?,?,'pending')",user_id,chat_id,area_id,internal_id,listing_url); job_id = int(c.fetchone()[0]); conn.commit(); return job_id
def rescue_stale_jobs(conn, timeout_seconds=None): conn.cursor().execute("UPDATE dbo.Job SET Status='pending',LockedAt=NULL,LockedBy=NULL WHERE Status='running' AND LockedAt<DATEADD(second, -?, SYSDATETIME())", timeout_seconds or config.JOB_LOCK_TIMEOUT_SECONDS); conn.commit()
def claim_next_job(conn, worker_id='worker', allowed_types=None):
    c=conn.cursor(); c.execute("SELECT TOP 1 JobID,JobType,UserID,ChatID,SearchID,ListingID,ListingURL,Attempts FROM dbo.Job WHERE Status IN ('pending','paused') AND (NextRetryAt IS NULL OR NextRetryAt<=SYSDATETIME()) ORDER BY JobID"); r=c.fetchone();
    if not r: return None
    c.execute("UPDATE dbo.Job SET Status='running',LockedAt=SYSDATETIME(),LockedBy=?,Attempts=Attempts+1 WHERE JobID=?",worker_id,r[0]); conn.commit(); return {"job_id":r[0],"job_type":r[1],"user_id":r[2],"chat_id":r[3],"area_id":r[4],"listing_id":r[5],"listing_url":r[6],"attempts":r[7]+1}
def finish_job(conn, job_id, ok=None, err=None, status=None, error=None):
    st = status or ("done" if ok else "failed")
    e = error if error is not None else err
    c=conn.cursor(); c.execute("UPDATE dbo.Job SET Status=?,LastError=?,FinishedAt=SYSDATETIME(),UpdatedAt=SYSDATETIME() WHERE JobID=?", st, str(e or '')[:1900] if st=='failed' else None, job_id); conn.commit()
def tg_save_message(conn, user_id, chat_id, search_id, listing_id, message_id, message_type):
    internal_id = get_listing_internal_id_by_external_id(conn, listing_id) or listing_id
    conn.cursor().execute("INSERT INTO dbo.TelegramMessage(UserID,ChatID,SearchID,ListingID,MessageID,MessageType) VALUES (?,?,?,?,?,?)",user_id,chat_id,search_id,internal_id,message_id,message_type); conn.commit()
def tg_get_message_id(conn, user_id, search_id=None, listing_id=None, message_type='new_listing'):
    if search_id is None:
        r=_one(conn.cursor(),"SELECT TOP 1 MessageID FROM dbo.TelegramMessage WHERE UserID=? AND ListingID=? AND MessageType=? ORDER BY TelegramMessageLogID DESC",user_id,listing_id,message_type)
    else:
        r=_one(conn.cursor(),"SELECT TOP 1 MessageID FROM dbo.TelegramMessage WHERE UserID=? AND SearchID=? AND ListingID=? AND MessageType=? ORDER BY TelegramMessageLogID DESC",user_id,search_id,listing_id,message_type)
    return r[0] if r else None
def try_mark_notified(conn,*a,**k): return True


def validate_required_schema(conn) -> None:
    cur = conn.cursor()
    for table, required_columns in SCHEMA_REQUIREMENTS.items():
        schema_name, table_name = table.split(".", 1)
        lookup_schema_name = _unquote_sql_identifier(schema_name)
        lookup_table_name = _unquote_sql_identifier(table_name)
        cur.execute(
            """
            SELECT COLUMN_NAME
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA=? AND TABLE_NAME=?
            """,
            lookup_schema_name,
            lookup_table_name,
        )
        existing = {row[0] for row in cur.fetchall()}
        missing = sorted(required_columns - existing)
        if missing:
            cur.execute(
                """
                SELECT TABLE_SCHEMA, TABLE_NAME
                FROM INFORMATION_SCHEMA.TABLES
                WHERE TABLE_SCHEMA=? AND TABLE_NAME LIKE ?
                """,
                lookup_schema_name,
                f"%{lookup_table_name.strip('[]')}%",
            )
            nearby = [f"{r[0]}.{r[1]}" for r in cur.fetchall()]
            raise RuntimeError(
                f"Schema mismatch for {table} "
                f"(lookup={lookup_schema_name}.{lookup_table_name}). "
                f"Missing columns: {', '.join(missing)}. "
                f"Existing columns: {', '.join(sorted(existing)) or 'NONE'}. "
                f"Nearby tables: {', '.join(nearby) or 'NONE'}"
            )


def _table_columns(conn, table_name: str, schema_name: str = "dbo") -> set[str]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT COLUMN_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA=? AND TABLE_NAME=?
        """,
        schema_name,
        table_name,
    )
    return {str(row[0]) for row in cur.fetchall()}


def get_setup_detail_schema_status(conn) -> dict[str, Any]:
    try:
        columns = _table_columns(conn, "ListingSearchState")
    except Exception as exc:
        return {
            "schema_ok": False,
            "setup_detail_schema_ok": False,
            "missing_columns": sorted(SETUP_DETAIL_REQUIRED_COLUMNS),
            "error": config.mask_sensitive_text(exc),
        }
    missing = sorted(SETUP_DETAIL_REQUIRED_COLUMNS - columns)
    return {
        "schema_ok": not missing,
        "setup_detail_schema_ok": not missing,
        "missing_columns": missing,
        "existing_columns": sorted(columns),
    }


def get_area_monitoring_schema_status(conn) -> dict[str, Any]:
    try:
        columns = _table_columns(conn, "area_monitoring_state")
    except Exception as exc:
        return {
            "schema_ok": False,
            "area_monitoring_schema_ok": False,
            "missing_columns": sorted(AREA_MONITORING_REQUIRED_COLUMNS),
            "error": config.mask_sensitive_text(exc),
        }
    missing = sorted(AREA_MONITORING_REQUIRED_COLUMNS - columns)
    return {
        "schema_ok": not missing,
        "area_monitoring_schema_ok": not missing,
        "missing_columns": missing,
        "existing_columns": sorted(columns),
    }


def ensure_listing_search_state_detail_refresh_column(conn) -> None:
    """Add per SearchID/listing detail-refresh/setup-detail columns used by setup batches."""
    ensure_listing_lifecycle_columns(conn)
    if not hasattr(conn, "commit"):
        # Lightweight test doubles may only capture SELECTs; real pyodbc
        # connections always expose commit/rollback for idempotent migration.
        return
    _execute_ddl_safely(conn, """
        IF OBJECT_ID('dbo.ListingSearchState') IS NOT NULL
        AND COL_LENGTH('dbo.ListingSearchState', 'LastDetailRefreshAt') IS NULL
        ALTER TABLE dbo.ListingSearchState ADD LastDetailRefreshAt DATETIME2 NULL
        """, description="add dbo.ListingSearchState.LastDetailRefreshAt", required=True)
    for column_name, definition in {
        "SetupDetailStatus": "NVARCHAR(40) NULL",
        "SetupDetailAttemptCount": "INT NOT NULL CONSTRAINT DF_ListingSearchState_SetupDetailAttemptCount DEFAULT (0)",
        "SetupDetailLastAttemptAt": "DATETIME2 NULL",
        "SetupDetailNextRetryAt": "DATETIME2 NULL",
        "SetupDetailLastError": "NVARCHAR(1000) NULL",
        "SetupDetailCompletedAt": "DATETIME2 NULL",
    }.items():
        _execute_ddl_safely(conn, f"""
            IF OBJECT_ID('dbo.ListingSearchState') IS NOT NULL
            AND COL_LENGTH('dbo.ListingSearchState', '{column_name}') IS NULL
            ALTER TABLE dbo.ListingSearchState ADD {column_name} {definition}
            """, description=f"add dbo.ListingSearchState.{column_name}", required=True)
    _execute_ddl_safely(conn, """
        IF OBJECT_ID('dbo.ListingSearchState') IS NOT NULL
        AND COL_LENGTH('dbo.ListingSearchState', 'SetupDetailAttemptCount') IS NOT NULL
        UPDATE dbo.ListingSearchState
        SET SetupDetailAttemptCount=0
        WHERE SetupDetailAttemptCount IS NULL
        """, description="backfill dbo.ListingSearchState.SetupDetailAttemptCount", required=True)
    _execute_ddl_safely(conn, """
        IF OBJECT_ID('dbo.ListingSearchState') IS NOT NULL
        AND COL_LENGTH('dbo.ListingSearchState', 'LastDetailRefreshAt') IS NOT NULL
        AND COL_LENGTH('dbo.ListingSearchState', 'SearchID') IS NOT NULL
        AND COL_LENGTH('dbo.ListingSearchState', 'Status') IS NOT NULL
        AND COL_LENGTH('dbo.ListingSearchState', 'ListingID') IS NOT NULL
        AND NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_ListingSearchState_DetailRefreshDue' AND object_id=OBJECT_ID('dbo.ListingSearchState'))
        CREATE INDEX IX_ListingSearchState_DetailRefreshDue
        ON dbo.ListingSearchState(SearchID, Status, LastDetailRefreshAt, ListingID)
        """, description="create IX_ListingSearchState_DetailRefreshDue", required=True)
    _execute_ddl_safely(conn, """
        IF OBJECT_ID('dbo.ListingSearchState') IS NOT NULL
        AND COL_LENGTH('dbo.ListingSearchState', 'SearchID') IS NOT NULL
        AND COL_LENGTH('dbo.ListingSearchState', 'SetupDetailStatus') IS NOT NULL
        AND COL_LENGTH('dbo.ListingSearchState', 'SetupDetailNextRetryAt') IS NOT NULL
        AND COL_LENGTH('dbo.ListingSearchState', 'LastDetailRefreshAt') IS NOT NULL
        AND COL_LENGTH('dbo.ListingSearchState', 'ListingID') IS NOT NULL
        AND NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_ListingSearchState_SetupDetailDue' AND object_id=OBJECT_ID('dbo.ListingSearchState'))
        CREATE INDEX IX_ListingSearchState_SetupDetailDue
        ON dbo.ListingSearchState(SearchID, SetupDetailStatus, SetupDetailNextRetryAt, LastDetailRefreshAt, ListingID)
        """, description="create IX_ListingSearchState_SetupDetailDue", required=True)


def ensure_runtime_monitoring_schema(conn) -> dict[str, Any]:
    """Run required monitoring/setup migrations before scheduler, worker, heartbeat, or tools use setup tables."""
    ensure_monitoring_state_tables(conn)
    ensure_listing_search_state_detail_refresh_column(conn)
    setup_detail = get_setup_detail_schema_status(conn)
    area_monitoring = get_area_monitoring_schema_status(conn)
    missing = {
        "setup_detail": setup_detail.get("missing_columns") or [],
        "area_monitoring_state": area_monitoring.get("missing_columns") or [],
    }
    schema_ok = not missing["setup_detail"] and not missing["area_monitoring_state"]
    if not schema_ok:
        raise RuntimeError(
            "Runtime monitoring schema is not ready; "
            f"missing setup_detail={missing['setup_detail']} "
            f"area_monitoring_state={missing['area_monitoring_state']}"
        )
    return {
        "schema_ok": True,
        "setup_detail_schema_ok": True,
        "area_monitoring_schema_ok": True,
        "missing_columns": [],
        "setup_detail": setup_detail,
        "area_monitoring_state": area_monitoring,
    }


def ensure_listing_lifecycle_columns(conn) -> None:
    if not hasattr(conn, "commit"):
        return
    for column_name, definition in {
        "ListingLifecycleStatus": "NVARCHAR(32) NULL",
        "StatusReason": "NVARCHAR(128) NULL",
        "StatusEvidence": "NVARCHAR(MAX) NULL",
        "NotFoundCount": "INT NOT NULL CONSTRAINT DF_ListingSearchState_NotFoundCount DEFAULT 0",
        "FirstNotFoundAt": "DATETIME2 NULL",
        "LastNotFoundAt": "DATETIME2 NULL",
        "RemovedAt": "DATETIME2 NULL",
        "SoldAt": "DATETIME2 NULL",
        "LastStatusChangeAt": "DATETIME2 NULL",
        "StatusNotificationSentAt": "DATETIME2 NULL",
    }.items():
        _execute_ddl_safely(conn, f"""
        IF OBJECT_ID('dbo.ListingSearchState') IS NOT NULL
        AND COL_LENGTH('dbo.ListingSearchState', '{column_name}') IS NULL
        ALTER TABLE dbo.ListingSearchState ADD {column_name} {definition}
        """, description=f"add dbo.ListingSearchState.{column_name}", required=False)


def normalize_listing_lifecycle_status(value: Any) -> str:
    status = clean_text(value, 32)
    if not status:
        return "active"
    status = status.lower().replace("-", "_").replace(" ", "_")
    if status in {"withdrawn", "off_market", "unavailable", "missing"}:
        return "removed" if status != "missing" else "not_found"
    return status if status in LISTING_LIFECYCLE_STATUSES else "active"


def listing_lifecycle_status_from_row(row: dict | None) -> str:
    row = row or {}
    return normalize_listing_lifecycle_status(
        row.get("ListingLifecycleStatus")
        or row.get("listing_lifecycle_status")
        or row.get("lifecycle_status")
        or row.get("current_status")
        or row.get("Status")
        or row.get("status")
    )


def sold_evidence_strength_from_row(row: dict | None) -> str:
    row = row or {}
    strength = clean_text(
        row.get("SoldEvidenceStrength")
        or row.get("sold_evidence_strength")
        or row.get("StatusEvidenceStrength"),
        32,
    )
    if strength and strength.lower() in {"strong", "weak"}:
        return strength.lower()
    reason = clean_text(row.get("StatusReason") or row.get("status_reason"), 128) or ""
    evidence = clean_text(row.get("StatusEvidence") or row.get("status_evidence"), 1000) or ""
    if reason.lower() in {"sold_evidence", "strong_sold_evidence"}:
        return "strong"
    if "weak_sold_evidence" in reason.lower():
        return "weak"
    if listing_lifecycle_status_from_row(row) == "sold" and evidence:
        return "weak"
    return "none"


def _search_id_if_exists(conn, search_url: str) -> int | None:
    try:
        normalized, *_ = _parse_search_url(search_url)
        row = _one(conn.cursor(), "SELECT SearchID FROM dbo.SuburbSearch WHERE SearchHash=? OR NormalizedSearchURL=?", _sha(normalized), normalized)
        return int(row[0]) if row else None
    except Exception:
        return None


def _recent_active_list_evidence(conn, search_id: int | None, listing_id: int | None, hours: int = 72) -> dict:
    if not search_id or not listing_id:
        return {"recent_active": False}
    try:
        row = _one(
            conn.cursor(),
            """
            SELECT lss.Status, lss.ListingLifecycleStatus, lss.LastSeenAt, l.CurrentStatus
            FROM dbo.ListingSearchState lss
            JOIN dbo.Listing l ON l.listingID=lss.ListingID
            WHERE lss.SearchID=? AND lss.ListingID=?
            """,
            int(search_id),
            int(listing_id),
        )
    except Exception:
        return {"recent_active": False}
    if not row:
        return {"recent_active": False}
    area_status = normalize_listing_lifecycle_status(row[0])
    lifecycle_status = normalize_listing_lifecycle_status(row[1])
    listing_status = normalize_listing_lifecycle_status(row[3])
    recent = False
    last_seen = row[2]
    if isinstance(last_seen, datetime):
        recent = last_seen >= datetime.now() - timedelta(hours=hours)
    elif last_seen:
        # SQL Server returns DATETIME2 as datetime in production. If a fake/test
        # connector supplies any LastSeenAt value, treat it as active evidence.
        recent = True
    return {
        "recent_active": area_status == "active" and lifecycle_status == "active" and listing_status == "active" and recent,
        "area_status": area_status,
        "lifecycle_status": lifecycle_status,
        "listing_status": listing_status,
        "last_seen_at": str(last_seen) if last_seen is not None else None,
    }


def _sold_guard_diagnostics(conn, search_url: str, row: dict, old_state: dict | None, new_state: dict) -> dict:
    diagnostics = {
        "suppressed_sold_count": 0,
        "weak_sold_evidence_count": 0,
        "strong_sold_evidence_count": 0,
        "effective_lifecycle_status": listing_lifecycle_status_from_row(row),
        "warnings": [],
    }
    incoming_status = normalize_listing_lifecycle_status(new_state.get("status") or listing_lifecycle_status_from_row(row))
    strength = sold_evidence_strength_from_row(row)
    if strength == "weak":
        diagnostics["weak_sold_evidence_count"] = 1
    elif strength == "strong":
        diagnostics["strong_sold_evidence_count"] = 1
    if incoming_status != "sold":
        diagnostics["effective_lifecycle_status"] = incoming_status
        return diagnostics
    if strength == "strong":
        diagnostics["effective_lifecycle_status"] = "sold"
        return diagnostics

    listing_id = old_state.get("listing_id") if old_state else row.get("db_listing_id") or row.get("internal_listing_id")
    search_id = _search_id_if_exists(conn, search_url)
    active_evidence = _recent_active_list_evidence(conn, search_id, int(listing_id) if listing_id is not None else None)
    old_status = normalize_listing_lifecycle_status((old_state or {}).get("status") or (old_state or {}).get("CurrentStatus") or row.get("area_status"))
    if active_evidence.get("recent_active") or old_status == "active":
        new_state["status"] = old_status if old_status == "active" else "unknown"
        diagnostics["effective_lifecycle_status"] = new_state["status"]
        diagnostics["suppressed_sold_count"] = 1
        warning = {
            "warning": "suppressed_sold_due_to_recent_active_list_evidence",
            "external_id": row.get("external_id") or row.get("listing_id"),
            "sold_evidence_strength": strength,
            "active_evidence": active_evidence,
        }
        diagnostics["warnings"].append(warning)
    else:
        diagnostics["effective_lifecycle_status"] = "unknown"
        new_state["status"] = "unknown"
        diagnostics["suppressed_sold_count"] = 1
        diagnostics["warnings"].append({
            "warning": "suppressed_sold_without_strong_evidence",
            "external_id": row.get("external_id") or row.get("listing_id"),
            "sold_evidence_strength": strength,
        })
    return diagnostics


def listing_is_active_for_module2(row: dict | None) -> bool:
    return listing_lifecycle_status_from_row(row) in MODULE2_ELIGIBLE_LIFECYCLE_STATUSES


def next_listing_lifecycle_transition(old_status: str | None, not_found_count: int = 0, signal: str | None = None) -> dict:
    old = normalize_listing_lifecycle_status(old_status)
    count = int(not_found_count or 0)
    status_signal = normalize_listing_lifecycle_status(signal)
    if status_signal == "not_found":
        count += 1
        if count >= 2:
            return {"old_status": old, "new_status": "removed", "not_found_count": count, "notify": old != "removed", "event_type": "removed", "enqueue_recheck": False, "module2_eligible": False}
        return {"old_status": old, "new_status": "not_found", "not_found_count": count, "notify": False, "event_type": None, "enqueue_recheck": True, "module2_eligible": False}
    if status_signal == "sold":
        return {"old_status": old, "new_status": "sold", "not_found_count": count, "notify": old != "sold", "event_type": "sold", "enqueue_recheck": False, "module2_eligible": False}
    if status_signal == "removed":
        return {"old_status": old, "new_status": "removed", "not_found_count": count, "notify": old != "removed", "event_type": "removed", "enqueue_recheck": False, "module2_eligible": False}
    if status_signal == "active":
        return {"old_status": old, "new_status": "active", "not_found_count": 0, "notify": False, "event_type": None, "enqueue_recheck": False, "module2_eligible": True}
    return {"old_status": old, "new_status": old, "not_found_count": count, "notify": False, "event_type": None, "enqueue_recheck": False, "module2_eligible": old == "active"}


def enqueue_listing_status_recheck_job(conn, search_id: int, listing_id: int, listing_external_id: str | None = None, listing_url: str | None = None, reason: str | None = None) -> dict | None:
    import job_queue

    payload = {
        "recheck_listing_id": int(listing_id),
        "listing_external_id": clean_text(listing_external_id, 80),
        "listing_url": clean_text(listing_url, 1000),
        "search_id": int(search_id),
        "reason": clean_text(reason, 128) or "not_found_recheck",
        "skip_module2": True,
    }
    return job_queue.enqueue_job_once(
        job_queue.JOB_TYPE_LISTING_STATUS_RECHECK,
        search_id=int(search_id),
        priority=job_queue.PRIORITY_LISTING_STATUS_RECHECK,
        payload=payload,
        dedupe_key=f"{job_queue.JOB_TYPE_LISTING_STATUS_RECHECK}:search_id={int(search_id)}:listing_id={int(listing_id)}",
        max_attempts=3,
    )


def apply_listing_lifecycle_signal(
    conn,
    search_id: int,
    listing_id: int,
    signal: str,
    reason: str | None = None,
    evidence: str | None = None,
    run_id: int | None = None,
    create_event: bool = True,
) -> dict:
    ensure_listing_lifecycle_columns(conn)
    cur = conn.cursor()
    current = _one(cur, """
        SELECT COALESCE(ListingLifecycleStatus, Status, 'active'), COALESCE(NotFoundCount, 0)
        FROM dbo.ListingSearchState
        WHERE SearchID=? AND ListingID=?
        """, int(search_id), int(listing_id))
    old_status = normalize_listing_lifecycle_status(current[0] if current else "active")
    not_found_count = int(current[1] or 0) if current else 0
    transition = next_listing_lifecycle_transition(old_status, not_found_count, signal)
    new_status = transition["new_status"]
    not_found_count = int(transition["not_found_count"])
    event_type = transition.get("event_type")
    should_notify = bool(transition.get("notify"))

    cur.execute("""
        UPDATE dbo.ListingSearchState
        SET ListingLifecycleStatus=?, Status=?,
            StatusReason=?, StatusEvidence=?,
            NotFoundCount=?,
            FirstNotFoundAt=CASE WHEN ?='not_found' THEN COALESCE(FirstNotFoundAt, SYSDATETIME()) WHEN ?='active' THEN NULL ELSE FirstNotFoundAt END,
            LastNotFoundAt=CASE WHEN ?='not_found' THEN SYSDATETIME() WHEN ?='active' THEN NULL ELSE LastNotFoundAt END,
            RemovedAt=CASE WHEN ?='active' THEN NULL WHEN ?='removed' THEN COALESCE(RemovedAt, SYSDATETIME()) ELSE RemovedAt END,
            SoldAt=CASE WHEN ?='active' THEN NULL WHEN ?='sold' THEN COALESCE(SoldAt, SYSDATETIME()) ELSE SoldAt END,
            LastStatusChangeAt=CASE WHEN COALESCE(ListingLifecycleStatus, Status, 'active')<>? THEN SYSDATETIME() ELSE LastStatusChangeAt END,
            UpdatedAt=SYSDATETIME()
        WHERE SearchID=? AND ListingID=?
        """, new_status, new_status, clean_text(reason, 128), clean_text(evidence), not_found_count,
        new_status, new_status, new_status, new_status, new_status, new_status, new_status, new_status, new_status,
        int(search_id), int(listing_id))
    if new_status == "active":
        cur.execute("UPDATE dbo.Listing SET CurrentStatus='active', UpdatedAt=SYSDATETIME() WHERE listingID=?", int(listing_id))
    elif new_status in {"sold", "removed"}:
        cur.execute("UPDATE dbo.Listing SET CurrentStatus=?, UpdatedAt=SYSDATETIME() WHERE listingID=?", new_status, int(listing_id))
    if create_event and event_type and should_notify:
        final_run_id = run_id or create_lightweight_scrape_run(conn, int(search_id), source="listing_lifecycle", run_type="change_detection")
        payload = {
            "event_type": event_type,
            "field": "ListingLifecycleStatus",
            "old_value": old_status,
            "new_value": new_status,
            "status_reason": reason,
            "status_evidence": evidence,
            "should_notify": True,
            "severity": "high" if event_type == "sold" else "normal",
        }
        eh = _sha(json_dumps_safe({"search_id": int(search_id), "listing_id": int(listing_id), **payload}, sort_keys=True))
        if create_listing_event_if_new(conn, int(listing_id), event_type, payload, event_hash=eh, search_id=int(search_id), run_id=final_run_id):
            cur.execute("UPDATE dbo.ListingSearchState SET StatusNotificationSentAt=COALESCE(StatusNotificationSentAt, SYSDATETIME()) WHERE SearchID=? AND ListingID=?", int(search_id), int(listing_id))
    recheck_job = None
    if transition.get("enqueue_recheck"):
        ext_row = _one(cur, "SELECT CAST(ExternalID AS NVARCHAR(80)), ListingURL FROM dbo.Listing WHERE listingID=?", int(listing_id))
        recheck_job = enqueue_listing_status_recheck_job(
            conn,
            int(search_id),
            int(listing_id),
            listing_external_id=str(ext_row[0]) if ext_row and ext_row[0] is not None else None,
            listing_url=str(ext_row[1]) if ext_row and ext_row[1] is not None else None,
            reason=reason,
        )
    transition.update({"old_status": old_status, "new_status": new_status, "not_found_count": not_found_count, "event_type": event_type, "should_notify": should_notify})
    transition["recheck_job"] = recheck_job
    return transition


SETUP_DETAIL_DONE_STATUSES = {"detail_complete", "detail_partial_complete", "detail_failed_permanent"}


def mark_listing_search_state_detail_refreshed(conn, search_id: int, listing_id: int, setup_detail_status: str | None = None) -> None:
    ensure_listing_search_state_detail_refresh_column(conn)
    status = setup_detail_status if setup_detail_status in {"detail_complete", "detail_partial_complete"} else "detail_complete"
    conn.cursor().execute(
        """
        UPDATE dbo.ListingSearchState
        SET LastDetailRefreshAt=SYSDATETIME(),
            SetupDetailStatus=?,
            SetupDetailCompletedAt=SYSDATETIME(),
            SetupDetailAttemptCount=0,
            SetupDetailNextRetryAt=NULL,
            SetupDetailLastError=NULL,
            UpdatedAt=SYSDATETIME()
        WHERE SearchID=? AND ListingID=?
        """,
        status,
        int(search_id),
        int(listing_id),
    )


def mark_listing_setup_detail_failed(conn, search_id: int, listing_id: int | None = None, external_id: str | None = None, error: str | None = None, retry_after=None, max_attempts: int | None = None) -> dict:
    ensure_listing_search_state_detail_refresh_column(conn)
    max_attempts = int(max_attempts or getattr(config, "DETAIL_BASELINE_MAX_ATTEMPTS", 5))
    safe_error = config.mask_sensitive_text(error or "setup detail technical failure")
    cur = conn.cursor()
    if listing_id is None and external_id is not None:
        row = _one(cur, "SELECT listingID FROM dbo.Listing WHERE ExternalID=?", str(external_id))
        listing_id = int(row[0]) if row else None
    if listing_id is None:
        return {"updated": False, "reason": "listing_not_found"}
    cur.execute(
        """
        SELECT COALESCE(SetupDetailAttemptCount, 0)
        FROM dbo.ListingSearchState
        WHERE SearchID=? AND ListingID=?
        """,
        int(search_id),
        int(listing_id),
    )
    row = cur.fetchone()
    attempts = int(row[0] or 0) + 1 if row else 1
    status = "detail_failed_permanent" if attempts >= max_attempts else "detail_retry_wait"
    next_retry = None if status == "detail_failed_permanent" else retry_after
    cur.execute(
        """
        UPDATE dbo.ListingSearchState
        SET SetupDetailStatus=?,
            SetupDetailAttemptCount=?,
            SetupDetailLastAttemptAt=SYSDATETIME(),
            SetupDetailNextRetryAt=?,
            SetupDetailLastError=?,
            UpdatedAt=SYSDATETIME()
        WHERE SearchID=? AND ListingID=?
        """,
        status,
        attempts,
        next_retry,
        safe_error,
        int(search_id),
        int(listing_id),
    )
    return {"updated": True, "listing_id": int(listing_id), "status": status, "attempts": attempts, "next_retry_at": next_retry}


def mark_listing_setup_detail_failed(conn, search_id: int, listing_id: int | None = None, external_id: str | None = None, error: str | None = None, retry_after=None, max_attempts: int | None = None) -> dict:
    ensure_listing_search_state_detail_refresh_column(conn)
    max_attempts = int(max_attempts or getattr(config, "DETAIL_BASELINE_MAX_ATTEMPTS", 5))
    safe_error = config.mask_sensitive_text(error or "setup detail technical failure")
    cur = conn.cursor()
    if listing_id is None and external_id is not None:
        row = _one(cur, "SELECT listingID FROM dbo.Listing WHERE ExternalID=?", str(external_id))
        listing_id = int(row[0]) if row else None
    if listing_id is None:
        return {"updated": False, "reason": "listing_not_found"}
    cur.execute(
        """
        SELECT COALESCE(SetupDetailAttemptCount, 0)
        FROM dbo.ListingSearchState
        WHERE SearchID=? AND ListingID=?
        """,
        int(search_id),
        int(listing_id),
    )
    row = cur.fetchone()
    attempts = int(row[0] or 0) + 1 if row else 1
    status = "detail_failed_permanent" if attempts >= max_attempts else "detail_retry_wait"
    next_retry = None if status == "detail_failed_permanent" else retry_after
    cur.execute(
        """
        UPDATE dbo.ListingSearchState
        SET SetupDetailStatus=?,
            SetupDetailAttemptCount=?,
            SetupDetailLastAttemptAt=SYSDATETIME(),
            SetupDetailNextRetryAt=?,
            SetupDetailLastError=?,
            UpdatedAt=SYSDATETIME()
        WHERE SearchID=? AND ListingID=?
        """,
        status,
        attempts,
        next_retry,
        safe_error,
        int(search_id),
        int(listing_id),
    )
    return {"updated": True, "listing_id": int(listing_id), "status": status, "attempts": attempts, "next_retry_at": next_retry}


def force_listing_search_state_detail_refresh_due(conn, search_id: int, hours: int | None = 3, set_null: bool = False) -> dict:
    ensure_listing_search_state_detail_refresh_column(conn)
    cur = conn.cursor()
    if set_null:
        cur.execute("""
            UPDATE dbo.ListingSearchState
            SET LastDetailRefreshAt=NULL, UpdatedAt=SYSDATETIME()
            WHERE SearchID=? AND LOWER(COALESCE(ListingLifecycleStatus, Status, 'active'))='active'
            """, int(search_id))
    else:
        cur.execute("""
            UPDATE dbo.ListingSearchState
            SET LastDetailRefreshAt=DATEADD(hour, -?, SYSDATETIME()), UpdatedAt=SYSDATETIME()
            WHERE SearchID=? AND LOWER(COALESCE(ListingLifecycleStatus, Status, 'active'))='active'
            """, int(hours or 3), int(search_id))
    listing_rows = cur.rowcount
    cur.execute("""
        UPDATE dbo.UserAreaSubscription
        SET LastDetailRefreshAt=DATEADD(hour, -?, SYSDATETIME()), UpdatedAt=SYSDATETIME()
        WHERE SearchID=? AND IsActive=1
        """, int(hours or 3), int(search_id))
    subscription_rows = cur.rowcount
    conn.commit()
    return {"search_id": int(search_id), "listing_rows_updated": listing_rows, "subscription_rows_updated": subscription_rows, "set_null": bool(set_null), "hours": hours}

def _detail_refresh_search_id(conn, search_url: str, subscription: dict | None = None) -> int:
    """Resolve the SearchID used for detail-refresh candidate selection."""
    if subscription and subscription.get("SearchID") is not None:
        return int(subscription["SearchID"])
    return int(_upsert_search(conn, search_url))


def get_detail_refresh_candidate_debug_counts(conn, search_id: int) -> dict[str, int]:
    """Return SearchID-scoped candidate-selection counters for diagnostics."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT
            COUNT(1) AS total_state_rows,
            SUM(CASE WHEN LOWER(COALESCE(lss.ListingLifecycleStatus, lss.Status, 'active')) = 'active' THEN 1 ELSE 0 END) AS active_state_rows,
            SUM(CASE
                    WHEN LOWER(COALESCE(lss.ListingLifecycleStatus, lss.Status, 'active')) = 'active'
                     AND NULLIF(LTRIM(RTRIM(COALESCE(l.ListingURL, ''))), '') IS NOT NULL
                    THEN 1 ELSE 0
                END) AS valid_url_rows
        FROM dbo.ListingSearchState lss
        LEFT JOIN dbo.Listing l ON l.listingID = lss.ListingID
        WHERE lss.SearchID = ?
        """,
        int(search_id),
    )
    row = cur.fetchone()
    return {
        "total_state_rows": int(row[0] or 0) if row else 0,
        "active_state_rows": int(row[1] or 0) if row else 0,
        "valid_url_rows": int(row[2] or 0) if row else 0,
    }


def get_active_listings_for_detail_refresh(
    conn,
    search_url: str,
    limit: int = 10,
    stale_hours: int | None = 24,
    listing_external_id: str | None = None,
    subscription: dict | None = None,
) -> list[dict]:
    ensure_listing_search_state_detail_refresh_column(conn)
    sid = _detail_refresh_search_id(conn, search_url, subscription=subscription)
    targeted_external_id = clean_text(listing_external_id, 50)
    safe_limit = 1 if targeted_external_id else max(1, int(limit or 1))
    setup_started_at = (subscription or {}).get("DetailBaselineStartedAt")
    setup_mode = bool(not targeted_external_id and setup_started_at is not None and stale_hours is not None and int(stale_hours) <= 0)
    effective_stale_hours = None if targeted_external_id or setup_mode else (None if stale_hours is None or int(stale_hours) <= 0 else int(stale_hours))
    cur = conn.cursor()
    sql = """
    SELECT TOP (?)
        l.listingID AS db_listing_id,
        CAST(l.ExternalID AS NVARCHAR(50)) AS external_id,
        CAST(l.ExternalID AS NVARCHAR(50)) AS listing_id,
        COALESCE(l.ListingURL, '') AS url,
        p.Address AS address,
        pt.PropertyType AS property_type,
        l.Price AS price,
        l.CurrentPriceDisplay AS price_display,
        p.NumberOfBedroom AS bedrooms,
        p.NumberOfBath AS bathrooms,
        p.Parkingslot AS parking,
        COALESCE(lss.Status, l.CurrentStatus) AS current_status,
        COALESCE(lss.ListingLifecycleStatus, lss.Status, l.CurrentStatus, 'active') AS ListingLifecycleStatus,
        lss.LastDetailRefreshAt AS last_detail_refresh_at,
        lss.SetupDetailStatus AS setup_detail_status,
        lss.SetupDetailAttemptCount AS setup_detail_attempt_count,
        lss.SetupDetailNextRetryAt AS setup_detail_next_retry_at
    FROM dbo.ListingSearchState lss
    JOIN dbo.Listing l ON l.listingID = lss.ListingID
    LEFT JOIN dbo.Property p ON p.PropertyID = l.PropertyID
    LEFT JOIN dbo.PropertyType pt ON pt.ID = p.PropertyTypeID
    WHERE lss.SearchID = ?
      AND LOWER(COALESCE(lss.ListingLifecycleStatus, lss.Status, 'active')) = 'active'
      AND NULLIF(LTRIM(RTRIM(COALESCE(l.ListingURL, ''))), '') IS NOT NULL
      AND (? IS NULL OR CAST(l.ExternalID AS NVARCHAR(50)) = ?)
      AND (
            ? = 0
            OR (
                COALESCE(lss.SetupDetailStatus, '') NOT IN ('detail_complete','detail_partial_complete','detail_failed_permanent')
                AND (lss.SetupDetailNextRetryAt IS NULL OR lss.SetupDetailNextRetryAt <= SYSDATETIME())
                AND (lss.LastDetailRefreshAt IS NULL OR lss.LastDetailRefreshAt < ?)
            )
      )
      AND (
            ? IS NULL
            OR lss.LastDetailRefreshAt IS NULL
            OR DATEADD(hour, ?, lss.LastDetailRefreshAt) <= SYSDATETIME()
      )
    ORDER BY CASE WHEN ? = 1 AND lss.LastDetailRefreshAt IS NULL THEN 0 ELSE 1 END ASC,
             CASE WHEN ? = 1 THEN l.listingID ELSE 0 END ASC,
             CASE WHEN ? = 0 AND lss.LastDetailRefreshAt IS NULL THEN 0 ELSE 1 END ASC,
             lss.LastDetailRefreshAt ASC,
             l.listingID ASC
    """
    setup_flag = 1 if setup_mode else 0
    cur.execute(sql, safe_limit, sid, targeted_external_id, targeted_external_id, setup_flag, setup_started_at, effective_stale_hours, effective_stale_hours, setup_flag, setup_flag, setup_flag)
    cols = [c[0] for c in cur.description]
    return [{cols[i]: row[i] for i in range(len(cols))} for row in cur.fetchall()]


def get_detail_refresh_skip_reason(conn, search_url: str, listing_external_id: str) -> str | None:
    external_id = clean_text(listing_external_id, 50)
    if not external_id:
        return "listing_not_found"
    cur = conn.cursor()
    row = _one(cur, "SELECT listingID, CurrentStatus FROM dbo.Listing WHERE CAST(ExternalID AS NVARCHAR(50))=?", external_id)
    if not row:
        return "listing_not_found"
    listing_id = int(row[0])
    current_status = clean_text(row[1], 80)
    sid = _upsert_search(conn, search_url)
    assoc = _one(
        cur,
        """
        SELECT lss.Status
        FROM dbo.ListingSearchState lss
        WHERE lss.SearchID=? AND lss.ListingID=?
        """,
        sid,
        listing_id,
    )
    if not assoc:
        return "listing_not_associated_with_search_url"
    search_status = clean_text(assoc[0], 80)
    blocked_statuses = {"sold", "removed", "not_found"}
    refreshable_current = {None, "active", "unknown"}
    current_norm = current_status.lower() if current_status else None
    search_norm = search_status.lower() if search_status else None
    if current_norm in blocked_statuses or search_norm in blocked_statuses or current_norm not in refreshable_current:
        return "listing_not_refreshable_status"
    return None


def should_create_listing_events_for_context(context: str | None, suppress_notifications: bool = False) -> bool:
    if context == "initial_detail_baseline" or suppress_notifications:
        return bool(config.CREATE_AUDIT_EVENTS_DURING_INITIAL_BASELINE)
    return True


def ingest_detail_refresh_rows_conn(
    conn,
    search_url: str,
    rows: list[dict],
    run_id: int | None = None,
    dry_run: bool = False,
    context: str | None = None,
    suppress_notifications: bool = False,
) -> dict:
    ensure_listing_event_metadata_columns(conn)
    ensure_listing_snapshot_size_columns(conn)
    sid = _upsert_search(conn, search_url)
    summary = {
        "rows_input": len(rows),
        "rows_processed": 0,
        "snapshots_inserted": 0,
        "events_created": 0,
        "run_id": run_id,
        "items": [],
        "suppressed_sold_count": 0,
        "weak_sold_evidence_count": 0,
        "strong_sold_evidence_count": 0,
    }
    cur = conn.cursor()
    for row in rows:
        try:
            ext = extract_external_listing_id(row)
        except ValueError:
            continue
        old_state = get_latest_listing_state(conn, ext)
        persisted_row = merge_enriched_listing_detail_with_latest(row, old_state)
        change_result = detect_and_record_changes_for_row(conn, search_url, persisted_row, run_id=run_id, create_events=False, context=context, suppress_notifications=suppress_notifications)
        item = {
            "external_id": ext,
            "db_listing_id": row.get("db_listing_id") or row.get("internal_listing_id"),
            "events_detected": change_result.get("events_detected", []),
            "events_created": 0,
            "should_notify_events": [e for e in change_result.get("events_detected", []) if e.get("should_notify")],
            "warnings": change_result.get("warnings", []),
            "suppressed_sold_count": int(change_result.get("suppressed_sold_count", 0)),
            "weak_sold_evidence_count": int(change_result.get("weak_sold_evidence_count", 0)),
            "strong_sold_evidence_count": int(change_result.get("strong_sold_evidence_count", 0)),
            "effective_lifecycle_status": change_result.get("effective_lifecycle_status"),
        }
        summary["suppressed_sold_count"] += item["suppressed_sold_count"]
        summary["weak_sold_evidence_count"] += item["weak_sold_evidence_count"]
        summary["strong_sold_evidence_count"] += item["strong_sold_evidence_count"]
        if dry_run:
            summary["rows_processed"] += 1
            summary["items"].append(item)
            continue

        if run_id is None:
            run_id = create_lightweight_scrape_run(conn, sid, source="detail_refresh", run_type="enrich_single")
            summary["run_id"] = run_id

        n = normalize_listing_row(persisted_row)
        suburb_id = int(_one(cur, "SELECT SuburbID FROM dbo.SuburbSearch WHERE SearchID=?", sid)[0])
        ptype_id = _upsert_property_type(cur, n["property_type"])
        property_id = _upsert_property(cur, n, suburb_id, ptype_id)
        agency_id = _upsert_agency(cur, n)
        agents_data = _upsert_agents(cur, n["agents"], agency_id)
        primary_agent_id = agents_data[0][0] if agents_data else None
        desc_hash = _sha(str(n["description"] or ""))
        if old_state and old_state.get("listing_id"):
            lid = int(old_state["listing_id"])
        elif row.get("db_listing_id") or row.get("internal_listing_id"):
            lid = int(row.get("db_listing_id") or row.get("internal_listing_id"))
        else:
            lid = int(_one(cur, "SELECT listingID FROM dbo.Listing WHERE ExternalID=?", n["external_id"])[0])
        cur.execute("UPDATE dbo.Listing SET PropertyID=?,AgencyID=?,AgentID=?,ListingURL=?,CurrentPriceDisplay=?,Price=?,Description=?,CurrentDescriptionHash=?,CurrentStatus='active',LastTimeSeen=SYSDATETIME(),UpdatedAt=SYSDATETIME() WHERE listingID=?", property_id, agency_id, primary_agent_id, n["url"], n["price_display"], n["price_value"], n["description"], desc_hash, lid)
        snap_payload = {"price_display": n["price_display"], "price_low": str(n["price_low"] or ""), "price_high": str(n["price_high"] or ""), "price_method": n["price_method"], "primary_agent_id": primary_agent_id, "inspection_short": n["inspection_short_label"], "inspection_long": n["inspection_long_label"], "auction_label": n["auction_label"], "description_hash": desc_hash, "url": n["url"], "land_size_display": n["land_size_display"], "land_size_sqm": str(n["land_size_sqm"] or ""), "building_size_display": n["building_size_display"], "building_size_sqm": str(n["building_size_sqm"] or ""), "floor_area_display": n["floor_area_display"], "floor_area_sqm": str(n["floor_area_sqm"] or "")}
        snap_hash = _sha(json_dumps_safe(snap_payload, sort_keys=True))
        snapshot_params = (
            lid, sid, run_id, snap_hash, n["price_value"], n["price_display"], n["price_low"], n["price_high"],
            n["price_method"], primary_agent_id, primary_agent_id, "active", n["description"], desc_hash, agency_id,
            n["inspection_short_label"], n["inspection_long_label"], n["auction_label"], n["url"],
            n["land_size_display"], n["land_size_sqm"], n["building_size_display"], n["building_size_sqm"],
            n["floor_area_display"], n["floor_area_sqm"],
        )
        _assert_param_marker_count(LISTING_SNAPSHOT_INSERT_SQL, snapshot_params)
        cur.execute(LISTING_SNAPSHOT_INSERT_SQL, *snapshot_params)
        snap_id = int(cur.fetchone()[0])
        summary["snapshots_inserted"] += 1
        for pos, (agid, agent_data) in enumerate(agents_data, start=1):
            cur.execute("INSERT INTO dbo.ListingSnapshotAgent(SnapshotID,AgentID,Position,PhoneAtSnapshot,RatingAtSnapshot,ReviewsTextAtSnapshot) VALUES (?,?,?,?,?,?)", snap_id, agid, pos, agent_data.get("phone"), None, None)
            cur.execute("IF NOT EXISTS (SELECT 1 FROM dbo.ListingAgentAssignment WHERE ListingID=? AND AgentID=? AND SearchID=? AND EndedAt IS NULL) INSERT INTO dbo.ListingAgentAssignment(ListingID,AgentID,SearchID,StartedAt) VALUES (?,?,?,SYSDATETIME())", lid, agid, sid, lid, agid, sid)
        lifecycle_status = normalize_listing_lifecycle_status(item.get("effective_lifecycle_status") or listing_lifecycle_status_from_row(persisted_row))
        if lifecycle_status == "sold" and sold_evidence_strength_from_row(persisted_row) == "strong":
            apply_listing_lifecycle_signal(conn, sid, lid, "sold", persisted_row.get("StatusReason") or "sold_evidence", persisted_row.get("StatusEvidence"), run_id=run_id, create_event=True)
        else:
            apply_listing_lifecycle_signal(conn, sid, lid, "active", None, None, run_id=run_id, create_event=False)
        setup_detail_status = None
        if context == "initial_detail_baseline":
            quality = str(persisted_row.get("detail_extraction_quality") or persisted_row.get("detail_quality") or "").lower()
            setup_detail_status = "detail_partial_complete" if quality in {"partial", "sparse", "partial_complete"} else "detail_complete"
        mark_listing_search_state_detail_refreshed(conn, sid, lid, setup_detail_status=setup_detail_status)
        create_audit_events = should_create_listing_events_for_context(context, suppress_notifications)
        if create_audit_events and old_state and old_state.get("listing_id") and item["events_detected"]:
            created = 0
            for ev in item["events_detected"]:
                eh = _compute_event_hash(lid, ev["event_type"], ev)
                if listing_event_exists_by_hash(conn, eh):
                    continue
                if create_listing_event_if_new(conn, lid, ev["event_type"], ev, event_hash=eh, search_id=sid, run_id=run_id, suppress_notifications=suppress_notifications):
                    created += 1
            item["events_created"] = created
        summary["events_created"] += item["events_created"]
        summary["rows_processed"] += 1
        summary["items"].append(item)
    return summary


def ingest_detail_refresh_rows(db_path: str, search_url: str, rows: list[dict], dry_run: bool = False, context: str | None = None, suppress_notifications: bool = False) -> dict:
    conn = connect(db_path)
    try:
        out = ingest_detail_refresh_rows_conn(conn, search_url, rows, run_id=None, dry_run=dry_run, context=context, suppress_notifications=suppress_notifications)
        if dry_run:
            conn.rollback()
        else:
            conn.commit()
        return out
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


NOTIFICATION_OUTBOX_COLUMNS = {
    "EventID": "INT NULL",
    "SearchID": "INT NULL",
    "ListingID": "INT NULL",
    "UserID": "INT NULL",
    "ChatID": "NVARCHAR(64) NULL",
    "Channel": "NVARCHAR(32) NOT NULL CONSTRAINT DF_NotificationOutbox_Channel DEFAULT 'telegram'",
    "NotificationKey": "NVARCHAR(128) NULL",
    "EventType": "NVARCHAR(64) NULL",
    "MessageText": "NVARCHAR(MAX) NULL",
    "Status": "NVARCHAR(32) NOT NULL CONSTRAINT DF_NotificationOutbox_Status DEFAULT 'queued'",
    "AttemptCount": "INT NOT NULL CONSTRAINT DF_NotificationOutbox_AttemptCount DEFAULT 0",
    "LastError": "NVARCHAR(MAX) NULL",
    "CreatedAt": "DATETIME2 NOT NULL CONSTRAINT DF_NotificationOutbox_CreatedAt DEFAULT SYSDATETIME()",
    "QueuedAt": "DATETIME2 NOT NULL CONSTRAINT DF_NotificationOutbox_QueuedAt DEFAULT SYSDATETIME()",
    "SentAt": "DATETIME2 NULL",
    "SkippedAt": "DATETIME2 NULL",
}


_NOTIFICATION_TABLES_ENSURED = False
_MIGRATION_WARNINGS_EMITTED: set[str] = set()


def _migration_warning_once(key: str, message: str) -> None:
    if key in _MIGRATION_WARNINGS_EMITTED:
        return
    _MIGRATION_WARNINGS_EMITTED.add(key)
    print(f"[migration warning] {message}")


def _execute_ddl_safely(conn, sql: str, description: str = "", required: bool = False) -> bool:
    try:
        cur = conn.cursor()
        cur.execute(sql)
        conn.commit()
        return True
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        if required:
            raise
        _migration_warning_once(description or sql.strip()[:120], f"{description}: {exc}")
        return False


_MONITORING_STATE_TABLES_ENSURED = False


def _alter_text_columns_to_max(conn, table_name: str, column_names: tuple[str, ...]) -> None:
    for column_name in column_names:
        _execute_ddl_safely(conn, f"""
        IF OBJECT_ID('dbo.{table_name}') IS NOT NULL
        AND COL_LENGTH('dbo.{table_name}', '{column_name}') IS NOT NULL
        ALTER TABLE dbo.{table_name} ALTER COLUMN {column_name} NVARCHAR(MAX) NULL
        """, description=f"widen dbo.{table_name}.{column_name} to NVARCHAR(MAX)", required=False)


def ensure_lifecycle_text_column_capacity(conn) -> None:
    """Widen error/payload columns that can legitimately exceed legacy limits."""
    for table_name, column_names in {
        "ListingSearchState": (
            "LastError", "ErrorMessage", "StatusMessage", "Notes", "PayloadJson",
            "CheckpointJson", "RawError", "DebugInfo", "LastRunMessage",
            "PriceInferenceLastError",
        ),
        "Job": (
            "LastError", "ErrorMessage", "StatusMessage", "Notes", "PayloadJson",
            "CheckpointJson", "RawError", "DebugInfo", "LastRunMessage",
        ),
        "JobQueue": (
            "LastError", "ErrorMessage", "StatusMessage", "Notes", "PayloadJson",
            "CheckpointJson", "RawError", "DebugInfo", "LastRunMessage",
        ),
        "area_monitoring_state": (
            "last_error", "error_message", "status_message", "notes", "payload_json",
            "checkpoint_json", "raw_error", "debug_info", "last_run_message",
        ),
        "UserAreaSubscription": (
            "BaselineLastError", "DetailBaselineLastError", "PriceBaselineLastError",
            "LastError", "ErrorMessage", "StatusMessage", "Notes", "PayloadJson",
            "CheckpointJson", "RawError", "DebugInfo", "LastRunMessage",
        ),
        "user_area_subscription_state": (
            "last_error", "error_message", "status_message", "notes", "payload_json",
            "checkpoint_json", "raw_error", "debug_info", "last_run_message",
        ),
    }.items():
        _alter_text_columns_to_max(conn, table_name, column_names)


def ensure_monitoring_state_tables(conn) -> None:
    """Create/upgrade area, subscription, and per-listing monitoring state."""
    global _MONITORING_STATE_TABLES_ENSURED
    if _MONITORING_STATE_TABLES_ENSURED:
        return
    _execute_ddl_safely(conn, """
        IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA='dbo' AND TABLE_NAME='area_monitoring_state')
        CREATE TABLE dbo.area_monitoring_state (
            area_id INT NOT NULL CONSTRAINT PK_area_monitoring_state PRIMARY KEY,
            setup_status NVARCHAR(32) NOT NULL CONSTRAINT DF_area_monitoring_state_setup_status DEFAULT 'not_started',
            module1_status NVARCHAR(32) NULL,
            module3_status NVARCHAR(32) NULL,
            module2_status NVARCHAR(32) NULL,
            active_listing_count INT NOT NULL CONSTRAINT DF_area_monitoring_state_active_listing_count DEFAULT 0,
            inferred_price_count INT NOT NULL CONSTRAINT DF_area_monitoring_state_inferred_price_count DEFAULT 0,
            unknown_price_count INT NOT NULL CONSTRAINT DF_area_monitoring_state_unknown_price_count DEFAULT 0,
            setup_started_at DATETIME2 NULL,
            ready_at DATETIME2 NULL,
            deactivated_at DATETIME2 NULL,
            deactivated_reason NVARCHAR(255) NULL,
            reactivated_at DATETIME2 NULL,
            last_subscription_count INT NOT NULL CONSTRAINT DF_area_monitoring_state_last_subscription_count DEFAULT 0,
            last_error NVARCHAR(MAX) NULL,
            updated_at DATETIME2 NOT NULL CONSTRAINT DF_area_monitoring_state_updated_at DEFAULT SYSDATETIME()
        )
        """, description="create dbo.area_monitoring_state", required=False)
    for column_name, column_definition in {
        "setup_status": "NVARCHAR(32) NOT NULL CONSTRAINT DF_area_monitoring_state_setup_status DEFAULT 'not_started'",
        "module1_status": "NVARCHAR(32) NULL",
        "module3_status": "NVARCHAR(32) NULL",
        "module2_status": "NVARCHAR(32) NULL",
        "active_listing_count": "INT NOT NULL CONSTRAINT DF_area_monitoring_state_active_listing_count DEFAULT 0",
        "inferred_price_count": "INT NOT NULL CONSTRAINT DF_area_monitoring_state_inferred_price_count DEFAULT 0",
        "unknown_price_count": "INT NOT NULL CONSTRAINT DF_area_monitoring_state_unknown_price_count DEFAULT 0",
        "setup_started_at": "DATETIME2 NULL",
        "ready_at": "DATETIME2 NULL",
        "deactivated_at": "DATETIME2 NULL",
        "deactivated_reason": "NVARCHAR(255) NULL",
        "reactivated_at": "DATETIME2 NULL",
        "last_subscription_count": "INT NOT NULL CONSTRAINT DF_area_monitoring_state_last_subscription_count DEFAULT 0",
        "last_error": "NVARCHAR(MAX) NULL",
        "updated_at": "DATETIME2 NOT NULL CONSTRAINT DF_area_monitoring_state_updated_at DEFAULT SYSDATETIME()",
    }.items():
        _execute_ddl_safely(conn, f"""
            IF OBJECT_ID('dbo.area_monitoring_state') IS NOT NULL
            AND COL_LENGTH('dbo.area_monitoring_state', '{column_name}') IS NULL
            ALTER TABLE dbo.area_monitoring_state ADD {column_name} {column_definition}
            """, description=f"add dbo.area_monitoring_state.{column_name}", required=False)
    _execute_ddl_safely(conn, """
        IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA='dbo' AND TABLE_NAME='user_area_subscription_state')
        CREATE TABLE dbo.user_area_subscription_state (
            user_id INT NOT NULL,
            area_id INT NOT NULL,
            status NVARCHAR(32) NOT NULL CONSTRAINT DF_user_area_subscription_state_status DEFAULT 'preparing',
            notify_enabled BIT NOT NULL CONSTRAINT DF_user_area_subscription_state_notify_enabled DEFAULT 0,
            created_at DATETIME2 NOT NULL CONSTRAINT DF_user_area_subscription_state_created_at DEFAULT SYSDATETIME(),
            activated_at DATETIME2 NULL,
            removed_at DATETIME2 NULL,
            updated_at DATETIME2 NOT NULL CONSTRAINT DF_user_area_subscription_state_updated_at DEFAULT SYSDATETIME(),
            CONSTRAINT PK_user_area_subscription_state PRIMARY KEY(user_id, area_id)
        )
        """, description="create dbo.user_area_subscription_state", required=False)
    _execute_ddl_safely(conn, """
        IF OBJECT_ID('dbo.user_area_subscription_state') IS NOT NULL
        AND COL_LENGTH('dbo.user_area_subscription_state', 'removed_at') IS NULL
        ALTER TABLE dbo.user_area_subscription_state ADD removed_at DATETIME2 NULL
        """, description="add dbo.user_area_subscription_state.removed_at", required=False)
    ensure_lifecycle_text_column_capacity(conn)
    _execute_ddl_safely(conn, """
        IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA='dbo' AND TABLE_NAME='listing_price_inference_state')
        CREATE TABLE dbo.listing_price_inference_state (
            listing_id NVARCHAR(100) NOT NULL,
            area_id INT NOT NULL,
            status NVARCHAR(64) NULL,
            last_error NVARCHAR(MAX) NULL,
            last_attempt_at DATETIME2 NULL,
            next_retry_at DATETIME2 NULL,
            attempts INT NOT NULL CONSTRAINT DF_listing_price_inference_state_attempts DEFAULT 0,
            inferred_low DECIMAL(18,2) NULL,
            inferred_high DECIMAL(18,2) NULL,
            method NVARCHAR(100) NULL,
            updated_at DATETIME2 NOT NULL CONSTRAINT DF_listing_price_inference_state_updated_at DEFAULT SYSDATETIME(),
            CONSTRAINT PK_listing_price_inference_state PRIMARY KEY(listing_id, area_id)
        )
        """, description="create dbo.listing_price_inference_state", required=False)
    _execute_ddl_safely(conn, """
        IF OBJECT_ID('dbo.area_monitoring_state') IS NOT NULL
        AND NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_area_monitoring_state_ready' AND object_id=OBJECT_ID('dbo.area_monitoring_state'))
        CREATE INDEX IX_area_monitoring_state_ready ON dbo.area_monitoring_state(setup_status, area_id)
        """, description="create IX_area_monitoring_state_ready", required=False)
    _execute_ddl_safely(conn, """
        IF OBJECT_ID('dbo.user_area_subscription_state') IS NOT NULL
        AND NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_user_area_subscription_state_active' AND object_id=OBJECT_ID('dbo.user_area_subscription_state'))
        CREATE INDEX IX_user_area_subscription_state_active ON dbo.user_area_subscription_state(status, notify_enabled, area_id)
        """, description="create IX_user_area_subscription_state_active", required=False)
    _execute_ddl_safely(conn, """
        IF OBJECT_ID('dbo.listing_price_inference_state') IS NOT NULL
        AND NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_listing_price_inference_state_due' AND object_id=OBJECT_ID('dbo.listing_price_inference_state'))
        CREATE INDEX IX_listing_price_inference_state_due ON dbo.listing_price_inference_state(area_id, status, next_retry_at)
        """, description="create IX_listing_price_inference_state_due", required=False)
    _MONITORING_STATE_TABLES_ENSURED = True


def get_excel_export_zero_row_diagnostics(conn, telegram_user_id: int | None, user_area_id: int | None, search_id: int | None) -> dict:
    """Collect SQL-backed diagnostics when an authorized Excel export has no rows."""
    diagnostics = {
        "telegram_user_id": telegram_user_id,
        "user_area_id": user_area_id,
        "resolved_search_id": search_id,
        "listing_search_state_count": None,
        "active_listing_search_state_count": None,
        "listing_snapshot_count": None,
        "latest_scrape_run_status": None,
        "latest_scrape_run_error": None,
        "subscription_statuses": [],
        "area_monitoring_state": None,
    }
    if not search_id:
        return diagnostics
    cur = conn.cursor()
    try:
        row = _one(cur, "SELECT COUNT(1) FROM dbo.ListingSearchState WHERE SearchID=?", int(search_id))
        diagnostics["listing_search_state_count"] = int(row[0]) if row else 0
        row = _one(cur, "SELECT COUNT(1) FROM dbo.ListingSearchState WHERE SearchID=? AND COALESCE(ListingLifecycleStatus, Status, 'active')='active'", int(search_id))
        diagnostics["active_listing_search_state_count"] = int(row[0]) if row else 0
        row = _one(cur, "SELECT COUNT(1) FROM dbo.ListingSnapshot WHERE SearchID=?", int(search_id))
        diagnostics["listing_snapshot_count"] = int(row[0]) if row else 0
        cur.execute("""
            SELECT TOP 1 RunID, Status
            FROM dbo.ScrapeRun
            WHERE SearchID=?
            ORDER BY RunID DESC
        """, int(search_id))
        rows = _rows_to_dicts(cur)
        if rows:
            diagnostics["latest_scrape_run_status"] = rows[0].get("Status")
            diagnostics["latest_scrape_run_error"] = None
    except Exception as exc:
        diagnostics["count_error"] = config.mask_sensitive_text(exc)
    try:
        cur.execute("""
            SELECT UserAreaID, BaselineStatus, DetailBaselineStatus, PriceBaselineStatus, SubscriptionStatus, NotifyEnabled, NotificationReadyAt
            FROM dbo.UserAreaSubscription
            WHERE SearchID=? AND (? IS NULL OR UserAreaID=?)
        """, int(search_id), user_area_id, user_area_id)
        diagnostics["subscription_statuses"] = _rows_to_dicts(cur)
    except Exception as exc:
        diagnostics["subscription_error"] = config.mask_sensitive_text(exc)
    try:
        diagnostics["area_monitoring_state"] = get_area_monitoring_state(conn, int(search_id))
    except Exception as exc:
        diagnostics["area_monitoring_state_error"] = config.mask_sensitive_text(exc)
    return diagnostics


def get_area_monitoring_state(conn, area_id: int) -> dict | None:
    ensure_monitoring_state_tables(conn)
    cur = conn.cursor()
    cur.execute("""
        SELECT area_id, setup_status, module1_status, module3_status, module2_status,
               active_listing_count, inferred_price_count, unknown_price_count,
               setup_started_at, ready_at, deactivated_at, deactivated_reason,
               reactivated_at, last_subscription_count, last_error, updated_at
        FROM dbo.area_monitoring_state
        WHERE area_id=?
        """, int(area_id))
    rows = _rows_to_dicts(cur)
    return rows[0] if rows else None


def upsert_area_monitoring_state(
    conn,
    area_id: int,
    setup_status: str | None = None,
    module1_status: str | None = None,
    module3_status: str | None = None,
    module2_status: str | None = None,
    active_listing_count: int | None = None,
    inferred_price_count: int | None = None,
    unknown_price_count: int | None = None,
    last_error: str | None = None,
    set_started: bool = False,
    set_ready: bool = False,
    set_deactivated: bool = False,
    deactivated_reason: str | None = None,
    set_reactivated: bool = False,
    last_subscription_count: int | None = None,
) -> None:
    ensure_monitoring_state_tables(conn)
    allowed = {"not_started", "preparing", "ready", "failed", "paused", "inactive"}
    if setup_status is not None and setup_status not in allowed:
        raise ValueError(f"Unsupported setup_status: {setup_status}")
    cur = conn.cursor()
    cur.execute("""
        MERGE dbo.area_monitoring_state AS target
        USING (SELECT ? AS area_id) AS source
        ON target.area_id=source.area_id
        WHEN MATCHED THEN UPDATE SET
            setup_status=COALESCE(?, target.setup_status),
            module1_status=COALESCE(?, target.module1_status),
            module3_status=COALESCE(?, target.module3_status),
            module2_status=COALESCE(?, target.module2_status),
            active_listing_count=COALESCE(?, target.active_listing_count),
            inferred_price_count=COALESCE(?, target.inferred_price_count),
            unknown_price_count=COALESCE(?, target.unknown_price_count),
            setup_started_at=CASE WHEN ?=1 THEN COALESCE(target.setup_started_at, SYSDATETIME()) ELSE target.setup_started_at END,
            ready_at=CASE WHEN ?=1 THEN SYSDATETIME() WHEN ? IN ('preparing','inactive','not_started') THEN NULL ELSE target.ready_at END,
            deactivated_at=CASE WHEN ?=1 THEN SYSDATETIME() WHEN ?=1 THEN NULL ELSE target.deactivated_at END,
            deactivated_reason=CASE WHEN ?=1 THEN ? WHEN ?=1 THEN NULL ELSE target.deactivated_reason END,
            reactivated_at=CASE WHEN ?=1 THEN SYSDATETIME() ELSE target.reactivated_at END,
            last_subscription_count=COALESCE(?, target.last_subscription_count),
            last_error=?,
            updated_at=SYSDATETIME()
        WHEN NOT MATCHED THEN INSERT (
            area_id, setup_status, module1_status, module3_status, module2_status,
            active_listing_count, inferred_price_count, unknown_price_count,
            setup_started_at, ready_at, deactivated_at, deactivated_reason,
            reactivated_at, last_subscription_count, last_error, updated_at
        ) VALUES (
            ?, COALESCE(?, 'not_started'), ?, ?, ?,
            COALESCE(?, 0), COALESCE(?, 0), COALESCE(?, 0),
            CASE WHEN ?=1 THEN SYSDATETIME() ELSE NULL END,
            CASE WHEN ?=1 THEN SYSDATETIME() ELSE NULL END,
            CASE WHEN ?=1 THEN SYSDATETIME() ELSE NULL END,
            CASE WHEN ?=1 THEN ? ELSE NULL END,
            CASE WHEN ?=1 THEN SYSDATETIME() ELSE NULL END,
            COALESCE(?, 0),
            ?, SYSDATETIME()
        );
        """,
        int(area_id),
        setup_status, module1_status, module3_status, module2_status,
        active_listing_count, inferred_price_count, unknown_price_count,
        1 if set_started else 0, 1 if set_ready else 0, setup_status,
        1 if set_deactivated else 0, 1 if set_reactivated else 0,
        1 if set_deactivated else 0, deactivated_reason, 1 if set_reactivated else 0,
        1 if set_reactivated else 0,
        last_subscription_count,
        config.mask_sensitive_text(last_error) if last_error else None,
        int(area_id), setup_status, module1_status, module3_status, module2_status,
        active_listing_count, inferred_price_count, unknown_price_count,
        1 if set_started else 0, 1 if set_ready else 0,
        1 if set_deactivated else 0, 1 if set_deactivated else 0, deactivated_reason,
        1 if set_reactivated else 0, last_subscription_count,
        config.mask_sensitive_text(last_error) if last_error else None)


def activate_area_subscriptions(conn, area_id: int) -> None:
    ensure_monitoring_state_tables(conn)
    cur = conn.cursor()
    cur.execute("""
        UPDATE dbo.user_area_subscription_state
        SET status='active', notify_enabled=1, activated_at=COALESCE(activated_at, SYSDATETIME()), removed_at=NULL, updated_at=SYSDATETIME()
        WHERE area_id=? AND status NOT IN ('removed','inactive','cancelled')
        """, int(area_id))
    cur.execute("""
        UPDATE dbo.UserAreaSubscription
        SET SubscriptionStatus='active',
            NotifyEnabled=1,
            RemovedAt=NULL,
            BaselineStatus='completed',
            BaselineCompletedAt=COALESCE(BaselineCompletedAt, SYSDATETIME()),
            DetailBaselineStatus='completed',
            DetailBaselineStartedAt=COALESCE(DetailBaselineStartedAt, SYSDATETIME()),
            DetailBaselineCompletedAt=COALESCE(DetailBaselineCompletedAt, SYSDATETIME()),
            PriceBaselineStatus='completed',
            PriceBaselineStartedAt=COALESCE(PriceBaselineStartedAt, SYSDATETIME()),
            PriceBaselineCompletedAt=COALESCE(PriceBaselineCompletedAt, SYSDATETIME()),
            NotificationReadyAt=COALESCE(NotificationReadyAt, SYSDATETIME()),
            LastLightCheckAt=COALESCE(LastLightCheckAt, SYSDATETIME()),
            LastDetailRefreshAt=COALESCE(LastDetailRefreshAt, SYSDATETIME()),
            LastPriceRefreshAt=COALESCE(LastPriceRefreshAt, SYSDATETIME()),
            UpdatedAt=SYSDATETIME()
        WHERE SearchID=? AND IsActive=1
        """, int(area_id))


def _active_subscription_status_sql() -> str:
    return "'active','preparing'"


def count_active_subscriptions_for_area(conn, area_id: int | None = None, search_id: int | None = None) -> int:
    """Count active/preparing subscriptions for one shared SearchID/area_id."""
    ensure_telegram_bot_tables(conn)
    resolved_id = int(search_id if search_id is not None else area_id)
    cur = conn.cursor()
    cur.execute(f"""
        SELECT COUNT(1)
        FROM dbo.UserAreaSubscription uas WITH (UPDLOCK, HOLDLOCK)
        JOIN dbo.TelegramUser tu ON tu.TelegramUserID=uas.TelegramUserID
        LEFT JOIN dbo.user_area_subscription_state us
          ON us.user_id=uas.TelegramUserID AND us.area_id=uas.SearchID
        WHERE uas.SearchID=?
          AND uas.IsActive=1
          AND tu.IsActive=1
          AND COALESCE(us.status, uas.SubscriptionStatus, 'active') IN ({_active_subscription_status_sql()})
        """, resolved_id)
    row = cur.fetchone()
    return int(row[0] or 0) if row else 0


def is_area_active_for_monitoring(conn, area_id: int | None = None, search_id: int | None = None) -> bool:
    """Return True only when the shared search is monitorable and has subscribers."""
    ensure_telegram_bot_tables(conn)
    resolved_id = int(search_id if search_id is not None else area_id)
    area_state = get_area_monitoring_state(conn, resolved_id)
    if area_state and str(area_state.get("setup_status") or "").lower() == "inactive":
        return False
    return count_active_subscriptions_for_area(conn, search_id=resolved_id) > 0


def cancel_jobs_for_inactive_area(conn, area_id: int | None = None, search_id: int | None = None, reason: str = INACTIVE_AREA_REASON_NO_SUBSCRIBERS) -> int:
    """Cancel queued/retry jobs for an inactive search without deleting history."""
    resolved_id = int(search_id if search_id is not None else area_id)
    safe_reason = config.mask_sensitive_text(reason or INACTIVE_AREA_REASON_NO_SUBSCRIBERS)
    cur = conn.cursor()
    cur.execute("""
        IF OBJECT_ID('dbo.Job') IS NOT NULL
        UPDATE dbo.Job
        SET Status='cancelled',
            FinishedAt=COALESCE(FinishedAt, SYSDATETIME()),
            UpdatedAt=SYSDATETIME(),
            LastError=?
        WHERE SearchID=?
          AND Status IN ('pending','paused','queued','retry_wait','scheduled')
        """, f"cancelled because area has no active subscribers: {safe_reason}", resolved_id)
    try:
        return int(cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else 0)
    except Exception:
        return 0


def deactivate_area_if_unused(conn, area_id: int | None = None, search_id: int | None = None, reason: str = INACTIVE_AREA_REASON_NO_SUBSCRIBERS) -> dict:
    """Mark the shared search inactive and cancel pending work when no subscribers remain."""
    resolved_id = int(search_id if search_id is not None else area_id)
    remaining = count_active_subscriptions_for_area(conn, search_id=resolved_id)
    if remaining > 0:
        upsert_area_monitoring_state(conn, resolved_id, last_subscription_count=remaining)
        return {
            "search_id": resolved_id,
            "area_id": resolved_id,
            "remaining_active_subscriptions": remaining,
            "action": "kept_active",
            "cancelled_jobs": 0,
        }
    upsert_area_monitoring_state(
        conn,
        resolved_id,
        setup_status="inactive",
        module1_status=None,
        module3_status=None,
        module2_status=None,
        last_error=None,
        set_deactivated=True,
        deactivated_reason=reason,
        last_subscription_count=0,
    )
    cur = conn.cursor()
    cur.execute("""
        UPDATE dbo.user_area_subscription_state
        SET status=CASE WHEN status IN ('removed','cancelled') THEN status ELSE 'inactive' END,
            notify_enabled=0,
            removed_at=COALESCE(removed_at, SYSDATETIME()),
            updated_at=SYSDATETIME()
        WHERE area_id=?
        """, resolved_id)
    cur.execute("""
        UPDATE dbo.UserAreaSubscription
        SET NotifyEnabled=0,
            SubscriptionStatus=CASE WHEN SubscriptionStatus IN ('removed','cancelled') THEN SubscriptionStatus ELSE 'inactive' END,
            UpdatedAt=SYSDATETIME()
        WHERE SearchID=?
        """, resolved_id)
    cancelled_jobs = cancel_jobs_for_inactive_area(conn, search_id=resolved_id, reason=reason)
    return {
        "search_id": resolved_id,
        "area_id": resolved_id,
        "remaining_active_subscriptions": 0,
        "action": "inactivated",
        "cancelled_jobs": cancelled_jobs,
    }


def upsert_user_area_subscription_state(conn, user_id: int, area_id: int, status: str = "preparing", notify_enabled: bool = False) -> None:
    ensure_monitoring_state_tables(conn)
    allowed = {"preparing", "active", "paused", "removed", "inactive", "cancelled"}
    if status not in allowed:
        raise ValueError(f"Unsupported subscription status: {status}")
    cur = conn.cursor()
    cur.execute("""
        MERGE dbo.user_area_subscription_state AS target
        USING (SELECT ? AS user_id, ? AS area_id) AS source
        ON target.user_id=source.user_id AND target.area_id=source.area_id
        WHEN MATCHED THEN UPDATE SET
            status=?, notify_enabled=?,
            activated_at=CASE WHEN ?='active' THEN COALESCE(target.activated_at, SYSDATETIME()) ELSE target.activated_at END,
            removed_at=CASE WHEN ? IN ('removed','inactive','cancelled') THEN COALESCE(target.removed_at, SYSDATETIME()) WHEN ? IN ('active','preparing') THEN NULL ELSE target.removed_at END,
            updated_at=SYSDATETIME()
        WHEN NOT MATCHED THEN INSERT(user_id, area_id, status, notify_enabled, created_at, activated_at, removed_at, updated_at)
            VALUES(?, ?, ?, ?, SYSDATETIME(), CASE WHEN ?='active' THEN SYSDATETIME() ELSE NULL END, CASE WHEN ? IN ('removed','inactive','cancelled') THEN SYSDATETIME() ELSE NULL END, SYSDATETIME());
        """, int(user_id), int(area_id), status, 1 if notify_enabled else 0, status, status, status, int(user_id), int(area_id), status, 1 if notify_enabled else 0, status, status)


def upsert_price_inference_state(
    conn,
    listing_id: str,
    area_id: int,
    status: str,
    last_error: str | None = None,
    next_retry_at=None,
    inferred_low: Any = None,
    inferred_high: Any = None,
    method: str | None = None,
    increment_attempts: bool = True,
) -> None:
    ensure_monitoring_state_tables(conn)
    allowed = {"completed", "unknown_pending_retry", "technical_failed", "skipped_direct_price"}
    if status not in allowed:
        raise ValueError(f"Unsupported price inference status: {status}")
    cur = conn.cursor()
    cur.execute("""
        MERGE dbo.listing_price_inference_state AS target
        USING (SELECT ? AS listing_id, ? AS area_id) AS source
        ON target.listing_id=source.listing_id AND target.area_id=source.area_id
        WHEN MATCHED THEN UPDATE SET
            status=?, last_error=?, last_attempt_at=SYSDATETIME(), next_retry_at=?,
            attempts=CASE WHEN ?=1 THEN COALESCE(target.attempts, 0)+1 ELSE target.attempts END,
            inferred_low=?, inferred_high=?, method=?, updated_at=SYSDATETIME()
        WHEN NOT MATCHED THEN INSERT(listing_id, area_id, status, last_error, last_attempt_at, next_retry_at, attempts, inferred_low, inferred_high, method, updated_at)
            VALUES(?, ?, ?, ?, SYSDATETIME(), ?, CASE WHEN ?=1 THEN 1 ELSE 0 END, ?, ?, ?, SYSDATETIME());
        """,
        str(listing_id), int(area_id),
        status, config.mask_sensitive_text(last_error) if last_error else None, next_retry_at,
        1 if increment_attempts else 0, inferred_low, inferred_high, method,
        str(listing_id), int(area_id), status, config.mask_sensitive_text(last_error) if last_error else None, next_retry_at,
        1 if increment_attempts else 0, inferred_low, inferred_high, method)


def get_due_price_retry_listing_ids(conn, area_id: int, now_value=None, limit: int = 100) -> list[str]:
    ensure_monitoring_state_tables(conn)
    now_value = now_value or datetime.now()
    cur = conn.cursor()
    cur.execute(f"""
        SELECT TOP ({max(1, int(limit or 1))})
            pis.listing_id,
            l.CurrentPriceDisplay,
            lss.InferredPriceLow,
            lss.InferredPriceHigh,
            lss.PriceInferenceStatus
        FROM dbo.listing_price_inference_state pis
        LEFT JOIN dbo.Listing l ON CAST(l.ExternalID AS NVARCHAR(100))=pis.listing_id
        LEFT JOIN dbo.ListingSearchState lss ON lss.SearchID=pis.area_id AND lss.ListingID=l.listingID
        WHERE pis.area_id=?
          AND pis.status IN ('unknown_pending_retry','technical_failed')
          AND (pis.next_retry_at IS NULL OR pis.next_retry_at <= ?)
          AND (
                l.listingID IS NULL
                OR NULLIF(LTRIM(RTRIM(COALESCE(l.CurrentPriceDisplay, ''))), '') IS NULL
                OR LOWER(LTRIM(RTRIM(COALESCE(l.CurrentPriceDisplay, '')))) IN ('n/a','na','unknown','contact agent','price withheld','price on request')
              )
          AND NOT (
                LOWER(COALESCE(lss.PriceInferenceStatus, ''))='completed'
                AND (lss.InferredPriceLow IS NOT NULL OR lss.InferredPriceHigh IS NOT NULL)
              )
        ORDER BY COALESCE(pis.next_retry_at, '19000101') ASC, pis.updated_at ASC
        """, int(area_id), now_value)
    return [str(row[0]) for row in cur.fetchall() if row and row[0] is not None]





SETUP_PIPELINE_JOB_TYPES = {"baseline_setup_area", "setup_detail_baseline", "setup_price_baseline"}
SETUP_PIPELINE_ACTIVE_STATUSES = {"queued", "running", "retry_wait"}


def get_active_setup_pipeline_jobs(conn, search_id: int) -> list[dict]:
    """Return active setup-pipeline jobs that should block fresh baseline repair."""
    try:
        row = _one(conn.cursor(), "SELECT OBJECT_ID('dbo.Job')")
        if not row or row[0] is None:
            return []
    except Exception:
        return []
    cur = conn.cursor()
    cur.execute(
        """
        SELECT JobID, JobType, SearchID, UserAreaID, Status, RunAfter, AttemptCount, MaxAttempts, DedupeKey, CreatedAt, UpdatedAt
        FROM dbo.Job
        WHERE SearchID=?
          AND JobType IN ('baseline_setup_area','setup_detail_baseline','setup_price_baseline')
          AND Status IN ('queued','running','retry_wait')
        ORDER BY Priority ASC, RunAfter ASC, CreatedAt ASC, JobID ASC
        """,
        int(search_id),
    )
    try:
        return _rows_to_dicts(cur)
    except Exception:
        return []


def has_active_setup_pipeline_job(conn, search_id: int) -> bool:
    return bool(get_active_setup_pipeline_jobs(conn, int(search_id)))

def cancel_setup_phase_jobs_for_search(conn, search_id: int, reason: str = "setup retry reset cancels queued setup phase jobs") -> int:
    """Cancel queued/retry setup detail and full-price phase jobs without cancelling the orchestrator."""
    safe_reason = config.mask_sensitive_text(reason or "setup phase reset")
    cur = conn.cursor()
    cur.execute(
        """
        IF OBJECT_ID('dbo.Job') IS NOT NULL
        UPDATE dbo.Job
        SET Status='cancelled',
            FinishedAt=COALESCE(FinishedAt, SYSDATETIME()),
            UpdatedAt=SYSDATETIME(),
            LastError=?
        WHERE SearchID=?
          AND JobType IN ('setup_detail_baseline','setup_price_baseline')
          AND Status IN ('pending','paused','queued','retry_wait','scheduled')
        """,
        safe_reason,
        int(search_id),
    )
    try:
        return int(cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else 0)
    except Exception:
        return 0

def retry_setup_area(conn, user_area_id: int | None = None, search_id: int | None = None) -> dict:
    """Reset a failed/preparing setup to a supported preparing state and enqueue baseline once."""
    ensure_telegram_bot_tables(conn)
    cur = conn.cursor()
    if user_area_id is not None:
        cur.execute(
            """
            SELECT TOP 1 UserAreaID, TelegramUserID, SearchID, SearchURL, AreaLabel
            FROM dbo.UserAreaSubscription WITH (UPDLOCK, HOLDLOCK)
            WHERE UserAreaID=? AND IsActive=1
            """,
            int(user_area_id),
        )
    elif search_id is not None:
        cur.execute(
            """
            SELECT TOP 1 UserAreaID, TelegramUserID, SearchID, SearchURL, AreaLabel
            FROM dbo.UserAreaSubscription WITH (UPDLOCK, HOLDLOCK)
            WHERE SearchID=? AND IsActive=1
            ORDER BY UserAreaID ASC
            """,
            int(search_id),
        )
    else:
        raise ValueError("user_area_id or search_id is required")
    row = cur.fetchone()
    if not row:
        conn.commit()
        return {"created": False, "reason": "active_subscription_not_found", "user_area_id": user_area_id, "search_id": search_id}

    resolved_user_area_id = int(row[0])
    resolved_telegram_user_id = int(row[1])
    resolved_search_id = int(row[2])
    search_url = str(row[3])
    area_label = str(row[4] or search_url)
    upsert_area_monitoring_state(
        conn,
        resolved_search_id,
        setup_status="preparing",
        module1_status="pending",
        module3_status="pending",
        module2_status="pending",
        active_listing_count=0,
        inferred_price_count=0,
        unknown_price_count=0,
        last_error=None,
        set_started=True,
    )
    cur.execute(
        """
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
            BaselineSummarySentAt=NULL,
            DetailBaselineStartedSummarySentAt=NULL,
            ReadySummarySentAt=NULL,
            UpdatedAt=SYSDATETIME()
        WHERE SearchID=? AND IsActive=1
        """,
        resolved_search_id,
    )
    upsert_user_area_subscription_state(conn, resolved_telegram_user_id, resolved_search_id, status="preparing", notify_enabled=False)
    cancelled_setup_phase_jobs = cancel_setup_phase_jobs_for_search(conn, resolved_search_id, reason="setup retry reset cancels queued setup phase jobs")
    conn.commit()
    baseline_job = enqueue_baseline_setup_job(conn, resolved_search_id, search_url)
    conn.commit()
    return {
        "created": bool(baseline_job and baseline_job.get("created")),
        "reason": "setup_retry_enqueued" if bool(baseline_job and baseline_job.get("created")) else "baseline_job_already_active",
        "user_area_id": resolved_user_area_id,
        "search_id": resolved_search_id,
        "area_label": area_label,
        "baseline_job": baseline_job,
        "cancelled_setup_phase_jobs": cancelled_setup_phase_jobs,
    }



def enqueue_setup_detail_baseline_job(conn, search_id: int, user_area_id: int | None = None, run_after=None, dedupe_suffix: str = "initial") -> dict | None:
    """Enqueue one bounded Module3 setup-detail batch for a search."""
    import job_queue

    return job_queue.enqueue_job_once(
        job_queue.JOB_TYPE_SETUP_DETAIL_BASELINE,
        search_id=int(search_id),
        user_area_id=int(user_area_id) if user_area_id is not None else None,
        priority=job_queue.PRIORITY_SETUP,
        run_after=run_after or datetime.now(),
        payload={"search_id": int(search_id), "phase": "module3_detail_baseline", "dedupe_suffix": str(dedupe_suffix or "initial")},
        dedupe_key=f"{job_queue.JOB_TYPE_SETUP_DETAIL_BASELINE}:search_id={int(search_id)}:{dedupe_suffix or 'initial'}",
        max_attempts=5,
    )


def enqueue_setup_price_baseline_job(conn, search_id: int, user_area_id: int | None = None, run_after=None) -> dict | None:
    """Enqueue exactly one full Module2 setup-price job for a search."""
    import job_queue

    return job_queue.enqueue_job_once(
        job_queue.JOB_TYPE_SETUP_PRICE_BASELINE,
        search_id=int(search_id),
        user_area_id=int(user_area_id) if user_area_id is not None else None,
        priority=job_queue.PRIORITY_SETUP,
        run_after=run_after or datetime.now(),
        payload={"search_id": int(search_id), "phase": "module2_full_setup", "module2_batching": False},
        dedupe_key=f"{job_queue.JOB_TYPE_SETUP_PRICE_BASELINE}:search_id={int(search_id)}:full",
        max_attempts=3,
    )


def is_area_setup_ready(conn, search_id: int) -> bool:
    """Central setup readiness guard used before activating subscriptions."""
    state = get_area_monitoring_state(conn, int(search_id)) or {}
    setup_status = str(state.get("setup_status") or "").lower()
    if setup_status in {"inactive", "failed"}:
        return False
    module1 = str(state.get("module1_status") or "").lower()
    module3 = str(state.get("module3_status") or "").lower()
    module2 = str(state.get("module2_status") or "").lower()
    return module1 == "completed" and module3 in {"completed", "skipped"} and module2 in {"completed", "completed_with_unknowns", "skipped"}

def enqueue_baseline_setup_job(conn, area_id: int, search_url: str) -> dict | None:
    """Enqueue one active baseline_setup_area job per area."""
    ensure_monitoring_state_tables(conn)
    state = get_area_monitoring_state(conn, int(area_id))
    active_setup_jobs = get_active_setup_pipeline_jobs(conn, int(area_id))
    if active_setup_jobs:
        return {"created": False, "duplicate": True, "reason": "active_setup_pipeline_job", "search_id": int(area_id), "active_job_types": sorted({str(job.get("JobType")) for job in active_setup_jobs}), "active_job_ids": [job.get("JobID") for job in active_setup_jobs]}
    if state and str(state.get("setup_status") or "").lower() == "ready":
        return {"created": False, "skipped": True, "reason": "area_ready", "search_id": int(area_id)}
    current_status = str(state.get("setup_status") or "not_started").lower() if state else "not_started"
    if current_status in {"failed", "inactive"}:
        upsert_area_monitoring_state(
            conn,
            int(area_id),
            setup_status="preparing",
            module1_status="pending",
            module3_status="pending",
            module2_status="pending",
            last_error=None,
            set_started=True,
            set_reactivated=current_status == "inactive",
        )
    elif not state or str(state.get("setup_status") or "not_started").lower() == "not_started":
        upsert_area_monitoring_state(conn, int(area_id), setup_status="preparing", last_error=None, set_started=True)
    import job_queue

    return job_queue.enqueue_job_once(
        job_queue.JOB_TYPE_BASELINE_SETUP_AREA,
        search_id=int(area_id),
        priority=job_queue.PRIORITY_SETUP,
        run_after=datetime.now(),
        payload={"area_id": int(area_id), "search_url": ensure_sort_list_date(search_url)},
        dedupe_key=f"{job_queue.JOB_TYPE_BASELINE_SETUP_AREA}:area_id={int(area_id)}",
        max_attempts=3,
    )


def _notification_table_exists(conn) -> bool:
    row = _one(
        conn.cursor(),
        "SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA='dbo' AND TABLE_NAME='NotificationOutbox'",
    )
    return bool(row)


def _notification_column_exists(conn, column_name: str) -> bool:
    row = _one(
        conn.cursor(),
        "SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA='dbo' AND TABLE_NAME='NotificationOutbox' AND COLUMN_NAME=?",
        column_name,
    )
    return bool(row)


def _column_type_signature(conn, schema_name: str, table_name: str, column_name: str) -> str | None:
    row = _one(
        conn.cursor(),
        """
        SELECT DATA_TYPE, CHARACTER_MAXIMUM_LENGTH, NUMERIC_PRECISION, NUMERIC_SCALE, IS_NULLABLE
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA=? AND TABLE_NAME=? AND COLUMN_NAME=?
        """,
        schema_name,
        table_name,
        column_name,
    )
    if not row:
        return None
    data_type = str(row[0]).lower()
    char_len = row[1]
    precision = row[2]
    scale = row[3]
    nullable = str(row[4]).upper() == "YES"
    if data_type in {"nvarchar", "varchar", "nchar", "char"}:
        size = "max" if char_len == -1 else str(char_len)
        type_part = f"{data_type}({size})"
    elif data_type in {"decimal", "numeric"}:
        type_part = f"{data_type}({precision},{scale})"
    else:
        type_part = data_type
    return f"{type_part} {'null' if nullable else 'not null'}"


def _column_base_type(conn, schema_name: str, table_name: str, column_name: str) -> str | None:
    signature = _column_type_signature(conn, schema_name, table_name, column_name)
    if not signature:
        return None
    return signature.rsplit(" ", 2)[0]


def _listing_event_id_sql_type(conn) -> str:
    return _column_base_type(conn, "dbo", "ListingEvent", "EventID") or "int"


def _normalize_sql_type_for_compare(type_signature: str | None) -> str | None:
    if not type_signature:
        return None
    return type_signature.rsplit(" ", 2)[0].lower()


def _try_align_notification_event_id_type(conn) -> None:
    target_type = _listing_event_id_sql_type(conn)
    current_type = _column_base_type(conn, "dbo", "NotificationOutbox", "EventID")
    if not current_type or current_type.lower() == target_type.lower():
        return
    _execute_ddl_safely(
        conn,
        f"ALTER TABLE dbo.NotificationOutbox ALTER COLUMN EventID {target_type} NOT NULL",
        description="align NotificationOutbox.EventID type with ListingEvent.EventID",
        required=False,
    )


def _fk_column_types_match(
    conn,
    fk_name: str,
    source_table: str,
    source_column: str,
    target_table: str,
    target_column: str,
) -> bool:
    source_type = _column_type_signature(conn, "dbo", source_table, source_column)
    target_type = _column_type_signature(conn, "dbo", target_table, target_column)
    if _normalize_sql_type_for_compare(source_type) == _normalize_sql_type_for_compare(target_type):
        return True
    _migration_warning_once(
        f"skip-{fk_name}-type-mismatch",
        (
            f"Skipping {fk_name} because {source_column} types differ: "
            f"{target_table}.{target_column}={target_type or 'missing'}, "
            f"NotificationOutbox.{source_column}={source_type or 'missing'}"
        ),
    )
    return False


def ensure_listing_event_metadata_columns(conn) -> None:
    for column_name, definition in {
        "ShouldNotify": "BIT NULL",
        "Severity": "NVARCHAR(32) NULL",
        "Reason": "NVARCHAR(128) NULL",
        "EventPayloadJson": "NVARCHAR(MAX) NULL",
    }.items():
        _execute_ddl_safely(conn, f"""
            IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA='dbo' AND TABLE_NAME='ListingEvent' AND COLUMN_NAME='{column_name}')
            ALTER TABLE dbo.ListingEvent ADD {column_name} {definition}
            """, description=f"add dbo.ListingEvent.{column_name}", required=False)


def ensure_listing_snapshot_size_columns(conn) -> None:
    if not hasattr(conn, "commit"):
        return
    ensure_area_numeric_capacity(conn)
    for column_name, definition in {
        "LandSizeDisplay": "NVARCHAR(100) NULL",
        "LandSizeSqm": "DECIMAL(18,2) NULL",
        "BuildingSizeDisplay": "NVARCHAR(100) NULL",
        "BuildingSizeSqm": "DECIMAL(18,2) NULL",
        "FloorAreaDisplay": "NVARCHAR(100) NULL",
        "FloorAreaSqm": "DECIMAL(18,2) NULL",
    }.items():
        _execute_ddl_safely(conn, f"""
            IF OBJECT_ID('dbo.ListingSnapshot') IS NOT NULL
            AND COL_LENGTH('dbo.ListingSnapshot', '{column_name}') IS NULL
            ALTER TABLE dbo.ListingSnapshot ADD {column_name} {definition}
            """, description=f"add dbo.ListingSnapshot.{column_name}", required=False)


def ensure_notification_tables(conn) -> None:
    """Create/upgrade the notification outbox schema idempotently.

    SQL Server can mark a transaction as uncommittable after a failed DDL
    statement. Run every migration statement as its own committed unit and
    rollback immediately on optional constraint failures so runtime dry-runs
    and queue builders can continue safely.
    """
    global _NOTIFICATION_TABLES_ENSURED
    if _NOTIFICATION_TABLES_ENSURED:
        return

    ensure_listing_event_metadata_columns(conn)
    event_id_type = _listing_event_id_sql_type(conn)
    if not _notification_table_exists(conn):
        _execute_ddl_safely(
            conn,
            f"""
            CREATE TABLE dbo.NotificationOutbox (
                NotificationID INT IDENTITY(1,1) NOT NULL PRIMARY KEY,
                EventID {event_id_type} NOT NULL,
                SearchID INT NULL,
                ListingID INT NULL,
                UserID INT NULL,
                ChatID NVARCHAR(64) NULL,
                Channel NVARCHAR(32) NOT NULL CONSTRAINT DF_NotificationOutbox_Channel DEFAULT 'telegram',
                NotificationKey NVARCHAR(128) NOT NULL,
                EventType NVARCHAR(64) NOT NULL,
                MessageText NVARCHAR(MAX) NOT NULL,
                Status NVARCHAR(32) NOT NULL CONSTRAINT DF_NotificationOutbox_Status DEFAULT 'queued',
                AttemptCount INT NOT NULL CONSTRAINT DF_NotificationOutbox_AttemptCount DEFAULT 0,
                LastError NVARCHAR(MAX) NULL,
                CreatedAt DATETIME2 NOT NULL CONSTRAINT DF_NotificationOutbox_CreatedAt DEFAULT SYSDATETIME(),
                QueuedAt DATETIME2 NOT NULL CONSTRAINT DF_NotificationOutbox_QueuedAt DEFAULT SYSDATETIME(),
                SentAt DATETIME2 NULL,
                SkippedAt DATETIME2 NULL
            )
            """,
            description="create dbo.NotificationOutbox",
            required=True,
        )

    for column_name, definition in NOTIFICATION_OUTBOX_COLUMNS.items():
        if not _notification_column_exists(conn, column_name):
            column_definition = definition
            if column_name == "EventID":
                column_definition = f"{event_id_type} NULL"
            _execute_ddl_safely(
                conn,
                f"ALTER TABLE dbo.NotificationOutbox ADD {column_name} {column_definition}",
                description=f"add dbo.NotificationOutbox.{column_name}",
                required=True,
            )

    _try_align_notification_event_id_type(conn)

    _execute_ddl_safely(
        conn,
        """
        IF NOT EXISTS (
            SELECT 1 FROM sys.indexes
            WHERE name='UX_NotificationOutbox_NotificationKey'
              AND object_id = OBJECT_ID('dbo.NotificationOutbox')
        )
        CREATE UNIQUE INDEX UX_NotificationOutbox_NotificationKey
        ON dbo.NotificationOutbox(NotificationKey)
        WHERE NotificationKey IS NOT NULL
        """,
        description="create UX_NotificationOutbox_NotificationKey",
        required=True,
    )

    _execute_ddl_safely(
        conn,
        """
        IF NOT EXISTS (
            SELECT 1 FROM sys.check_constraints
            WHERE name='CK_NotificationOutbox_Status'
        )
        ALTER TABLE dbo.NotificationOutbox WITH NOCHECK
        ADD CONSTRAINT CK_NotificationOutbox_Status
        CHECK (Status IN ('queued','sending','sent','failed','skipped','cancelled'))
        """,
        description="create CK_NotificationOutbox_Status",
        required=False,
    )

    fk_specs = [
        ("FK_NotificationOutbox_Event", "EventID", "ListingEvent", "EventID"),
        ("FK_NotificationOutbox_SuburbSearch", "SearchID", "SuburbSearch", "SearchID"),
        ("FK_NotificationOutbox_Listing", "ListingID", "Listing", "listingID"),
    ]
    for fk_name, source_column, target_table, target_column in fk_specs:
        if not _fk_column_types_match(conn, fk_name, "NotificationOutbox", source_column, target_table, target_column):
            continue
        _execute_ddl_safely(
            conn,
            f"""
            IF NOT EXISTS (
                SELECT 1 FROM sys.foreign_keys
                WHERE name='{fk_name}'
            )
            ALTER TABLE dbo.NotificationOutbox WITH NOCHECK
            ADD CONSTRAINT {fk_name}
            FOREIGN KEY({source_column}) REFERENCES dbo.{target_table}({target_column})
            """,
            description=f"create {fk_name}",
            required=False,
        )

    _NOTIFICATION_TABLES_ENSURED = True


def _rows_to_dicts(cur) -> list[dict]:
    cols = [c[0] for c in cur.description]
    return [{cols[i]: row[i] for i in range(len(cols))} for row in cur.fetchall()]


def get_notifyable_listing_events(
    conn,
    search_url: str | None = None,
    since_event_id: int | None = None,
    limit: int = 100,
    include_already_queued: bool = False,
    chat_id: str | None = None,
    created_after=None,
    created_at_or_after=None,
) -> list[dict]:
    ensure_notification_tables(conn)
    safe_limit = max(1, int(limit or 1))
    params: list[Any] = []
    filters = []
    if search_url:
        normalized = ensure_sort_list_date(search_url)
        filters.append("ss.SearchHash = ?")
        params.append(_sha(normalized))
    if since_event_id is not None:
        filters.append("e.EventID > ?")
        params.append(int(since_event_id))
    if created_at_or_after is not None:
        filters.append("e.CreatedAt >= ?")
        params.append(created_at_or_after)
    if created_after is not None:
        filters.append("e.CreatedAt > ?")
        params.append(created_after)
    filters.append("e.ShouldNotify <> 0")
    filters.append("ISNULL(e.Reason, '') NOT IN ('initial_agent_enrichment','initial_agency_enrichment','initial_price_enrichment','initial_description_enrichment','initial_auction_enrichment','initial_inspection_enrichment','initial_detail_baseline','agent_metadata_enrichment','detail_refresh_failed_skip_change_detection')")
    if not include_already_queued:
        if chat_id is None:
            filters.append(
                """
                NOT EXISTS (
                    SELECT 1 FROM dbo.NotificationOutbox no
                    WHERE no.EventID = e.EventID
                      AND no.Channel = 'telegram'
                      AND no.ChatID IS NULL
                )
                """
            )
        else:
            filters.append(
                """
                NOT EXISTS (
                    SELECT 1 FROM dbo.NotificationOutbox no
                    WHERE no.EventID = e.EventID
                      AND no.Channel = 'telegram'
                      AND no.ChatID = ?
                )
                """
            )
            params.append(str(chat_id))
    where_sql = " AND ".join(filters) if filters else "1=1"
    cur = conn.cursor()
    sql = f"""
    SELECT TOP (?)
        e.EventID,
        e.EventType,
        e.OldValueJson,
        e.NewValueJson,
        e.ShouldNotify,
        e.Severity,
        e.Reason,
        e.EventPayloadJson,
        e.RunID,
        e.SearchID,
        e.ListingID,
        e.CreatedAt,
        CAST(l.ExternalID AS NVARCHAR(50)) AS ExternalID,
        COALESCE(ls.URL, l.ListingURL) AS url,
        COALESCE(p.Address, p.AddressRaw, p.AddressNormalized) AS address,
        pt.PropertyType AS property_type,
        p.NumberOfBedroom AS bedrooms,
        p.NumberOfBath AS bathrooms,
        p.Parkingslot AS parking,
        COALESCE(ls.PriceDisplay, l.CurrentPriceDisplay, CONVERT(NVARCHAR(100), l.Price)) AS price_display,
        lss.InferredPriceLow AS inferred_price_low,
        lss.InferredPriceHigh AS inferred_price_high,
        lss.InferredPriceMethod AS inferred_price_method,
        lss.InferredPriceSource AS inferred_price_source,
        COALESCE(ls.Status, l.CurrentStatus, lss.Status) AS status,
        ag.Name AS agency_name,
        ag.AgencyExternalCode AS agency_code,
        ag.AgencyProfileURL AS agency_profile_url,
        agents.agents_json AS agents_json,
        ss.NormalizedSearchURL AS search_url,
        ss.DisplayName AS search_display_name,
        ss.DisplayName AS area_label
    FROM dbo.ListingEvent e
    LEFT JOIN dbo.Listing l ON l.listingID = e.ListingID
    LEFT JOIN dbo.ListingSearchState lss ON lss.ListingID = e.ListingID AND lss.SearchID = e.SearchID
    LEFT JOIN dbo.SuburbSearch ss ON ss.SearchID = e.SearchID
    LEFT JOIN dbo.Property p ON p.PropertyID = l.PropertyID
    LEFT JOIN dbo.PropertyType pt ON pt.ID = p.PropertyTypeID
    OUTER APPLY (
        SELECT TOP 1 *
        FROM dbo.ListingSnapshot ls2
        WHERE ls2.ListingID = e.ListingID AND (e.SearchID IS NULL OR ls2.SearchID = e.SearchID)
        ORDER BY ls2.SnapshotID DESC
    ) ls
    LEFT JOIN dbo.Agency ag ON ag.AgencyID = COALESCE(ls.AgencyID, l.AgencyID)
    OUTER APPLY (
        SELECT a.AgentExternalID AS agent_id,
               a.AgentName AS name,
               COALESCE(lsa.PhoneAtSnapshot, a.AgentPhoneNumber) AS phone,
               a.AgentProfileURL AS profile_url
        FROM dbo.ListingSnapshotAgent lsa
        JOIN dbo.Agent a ON a.AgentID = lsa.AgentID
        WHERE lsa.SnapshotID = ls.SnapshotID
        ORDER BY lsa.Position
        FOR JSON PATH
    ) agents(agents_json)
    WHERE {where_sql}
    ORDER BY e.EventID ASC
    """
    cur.execute(sql, safe_limit, *params)
    return _rows_to_dicts(cur)


def _is_unique_notification_key_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "notificationkey" in text and ("duplicate" in text or "unique" in text or "2601" in text or "2627" in text)


def insert_notification_outbox_if_new(
    conn,
    event_id: int,
    event_type: str,
    message_text: str,
    notification_key: str,
    search_id: int | None = None,
    listing_id: int | None = None,
    user_id: int | None = None,
    chat_id: str | None = None,
    channel: str = "telegram",
) -> bool:
    resolved_chat_id = clean_text(chat_id)
    if not resolved_chat_id:
        raise ValueError("missing_chat_id")
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM dbo.NotificationOutbox WHERE NotificationKey=?", notification_key)
    if cur.fetchone():
        return False

    try:
        try:
            cur.execute("SAVE TRAN notification_outbox_insert")
        except Exception:
            pass
        cur.execute(
            """
            INSERT INTO dbo.NotificationOutbox(
                EventID, SearchID, ListingID, UserID, ChatID, Channel,
                NotificationKey, EventType, MessageText, Status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued')
            """,
            int(event_id),
            search_id,
            listing_id,
            user_id,
            resolved_chat_id,
            channel,
            notification_key,
            event_type,
            message_text,
        )
        return True
    except Exception as exc:
        if _is_unique_notification_key_error(exc):
            try:
                cur.execute("ROLLBACK TRAN notification_outbox_insert")
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
            return False
        raise


def insert_skipped_notification_outbox_if_new(conn, event_id: int, notification_key: str, reason: str, chat_id: str, channel: str = "telegram") -> None:
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM dbo.NotificationOutbox WHERE NotificationKey=?", notification_key)
    if cur.fetchone():
        return
    cur.execute(
        """
        INSERT INTO dbo.NotificationOutbox(EventID, ChatID, Channel, NotificationKey, EventType, MessageText, Status, SkippedAt)
        VALUES (?, ?, ?, ?, 'combined_listing_update', ?, 'skipped', SYSDATETIME())
        """,
        int(event_id),
        clean_text(chat_id),
        channel,
        notification_key,
        reason,
    )


def coalesce_listing_price_update_events(events: list[dict]) -> list[dict]:
    grouped: dict[tuple[Any, Any, Any], list[dict]] = {}
    passthrough: list[dict] = []
    for row in events:
        event_type = str(row.get("EventType") or row.get("event_type") or "").lower()
        if event_type not in {"ad_price_changed", "inferred_price_range_changed"}:
            passthrough.append(row)
            continue
        key = (row.get("SearchID") or row.get("search_id"), row.get("ListingID") or row.get("listing_id"), row.get("RunID") or row.get("run_id"))
        grouped.setdefault(key, []).append(row)
    out = list(passthrough)
    for rows in grouped.values():
        types = {str(row.get("EventType") or row.get("event_type") or "").lower() for row in rows}
        if not {"ad_price_changed", "inferred_price_range_changed"}.issubset(types):
            out.extend(rows)
            continue
        rows = sorted(rows, key=lambda row: int(row.get("EventID") or row.get("event_id") or 0))
        primary = dict(rows[0])
        payload = primary.get("EventPayloadJson") or primary.get("event_payload_json")
        try:
            payload_data = json.loads(payload) if isinstance(payload, str) else dict(payload or {})
        except Exception:
            payload_data = {}
        combined = []
        for row in rows:
            event_type = str(row.get("EventType") or row.get("event_type") or "").lower()
            old_value = row.get("OldValueJson") or row.get("old_value_json")
            new_value = row.get("NewValueJson") or row.get("new_value_json")
            try:
                old_value = json.loads(old_value) if isinstance(old_value, str) else old_value
            except Exception:
                pass
            try:
                new_value = json.loads(new_value) if isinstance(new_value, str) else new_value
            except Exception:
                pass
            combined.append({"event_id": row.get("EventID") or row.get("event_id"), "event_type": event_type, "old_value": old_value, "new_value": new_value})
        payload_data["combined_events"] = combined
        primary["EventType"] = "listing_update"
        primary["event_type"] = "listing_update"
        primary["EventPayloadJson"] = json_dumps_safe(payload_data)
        primary["_absorbed_event_ids"] = [item["event_id"] for item in combined[1:] if item.get("event_id")]
        out.append(primary)
    return sorted(out, key=lambda row: int(row.get("EventID") or row.get("event_id") or 0))


def build_notifications_for_events(
    conn,
    events: list[dict],
    chat_id: str | None = None,
    user_id: int | None = None,
    channel: str = "telegram",
    dry_run: bool = False,
) -> dict:
    import notification_engine

    ensure_notification_tables(conn)
    resolved_chat_id = clean_text(chat_id)
    result = {
        "events_input": len(events),
        "notifyable_count": 0,
        "queued_count": 0,
        "skipped_count": 0,
        "duplicates_count": 0,
        "dry_run": bool(dry_run),
        "notifications": [],
        "errors": [],
    }
    if not resolved_chat_id:
        result["skipped_count"] = len(events)
        result["skipped_reason"] = "missing_chat_id"
        result["notifications"] = [
            {
                "event_id": row.get("EventID") or row.get("event_id"),
                "event_type": row.get("EventType") or row.get("event_type"),
                "external_id": row.get("ExternalID") or row.get("external_id"),
                "status": "skipped",
                "skipped_reason": "missing_chat_id",
                "message_text": "",
            }
            for row in events
        ]
        return result
    recipient_key = resolved_chat_id
    for row in coalesce_listing_price_update_events(events):
        try:
            event = notification_engine.normalize_event_for_notification(row)
            event_id = event.get("event_id")
            event_type = event.get("event_type")
            base_item = {
                "event_id": event_id,
                "event_type": event_type,
                "external_id": event.get("external_id"),
                "address": (event.get("listing") or {}).get("address"),
            }
            if not notification_engine.is_event_notifyable(event):
                result["skipped_count"] += 1
                result["notifications"].append({**base_item, "status": "skipped", "message_text": ""})
                continue
            result["notifyable_count"] += 1
            message_text = notification_engine.build_notification_message(row)
            key = notification_engine.build_notification_key(event_id, recipient_key, channel=channel)
            if dry_run:
                result["notifications"].append({**base_item, "status": "preview", "message_text": message_text})
                continue
            inserted = insert_notification_outbox_if_new(
                conn,
                event_id=int(event_id),
                event_type=event_type,
                message_text=message_text,
                notification_key=key,
                search_id=event.get("search_id"),
                listing_id=event.get("listing_id"),
                user_id=user_id,
                chat_id=resolved_chat_id,
                channel=channel,
            )
            if inserted:
                result["queued_count"] += 1
                status = "queued"
                for absorbed_event_id in row.get("_absorbed_event_ids") or []:
                    absorbed_key = notification_engine.build_notification_key(absorbed_event_id, recipient_key, channel=channel)
                    if not dry_run:
                        insert_skipped_notification_outbox_if_new(conn, int(absorbed_event_id), absorbed_key, f"combined into listing update event {event_id}", resolved_chat_id, channel)
            else:
                result["duplicates_count"] += 1
                status = "duplicate"
            result["notifications"].append({**base_item, "status": status, "message_text": message_text})
        except Exception as exc:
            result["errors"].append({"event": row.get("EventID") or row.get("event_id"), "error": str(exc)})
    return result


def get_queued_notifications(conn, limit: int = 20, channel: str = "telegram") -> list[dict]:
    ensure_notification_tables(conn)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT TOP (?) NotificationID, EventID, SearchID, ListingID, UserID, ChatID, Channel,
               NotificationKey, EventType, MessageText, Status, AttemptCount, LastError,
               CreatedAt, QueuedAt, SentAt, SkippedAt
        FROM dbo.NotificationOutbox
        WHERE Status='queued' AND Channel=? AND COALESCE(QueuedAt, CreatedAt) <= SYSDATETIME()
        ORDER BY NotificationID ASC
        """,
        max(1, int(limit or 1)),
        channel,
    )
    return _rows_to_dicts(cur)


def mark_notification_sending(conn, notification_id):
    cur = conn.cursor()
    cur.execute(
        "UPDATE dbo.NotificationOutbox SET Status='sending', AttemptCount=AttemptCount+1, QueuedAt=SYSDATETIME(), LastError=NULL WHERE NotificationID=? AND Status='queued'",
        notification_id,
    )
    return bool(getattr(cur, "rowcount", 1) != 0)


def mark_notification_sent(conn, notification_id, sent_at=None):
    cur = conn.cursor()
    if sent_at is None:
        cur.execute(
            "UPDATE dbo.NotificationOutbox SET Status='sent', SentAt=SYSDATETIME(), LastError=NULL WHERE NotificationID=?",
            notification_id,
        )
    else:
        cur.execute(
            "UPDATE dbo.NotificationOutbox SET Status='sent', SentAt=?, LastError=NULL WHERE NotificationID=?",
            sent_at,
            notification_id,
        )


def mark_notification_failed(conn, notification_id, error: str):
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE dbo.NotificationOutbox
        SET Status='failed', AttemptCount=AttemptCount+1, LastError=?
        WHERE NotificationID=?
        """,
        str(error or ""),
        notification_id,
    )


def mark_notification_skipped(conn, notification_id, reason: str):
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE dbo.NotificationOutbox
        SET Status='skipped', SkippedAt=SYSDATETIME(), LastError=?
        WHERE NotificationID=?
        """,
        str(reason or ""),
        notification_id,
    )


def mark_notification_cancelled(conn, notification_id, reason: str):
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE dbo.NotificationOutbox
        SET Status='cancelled', SkippedAt=COALESCE(SkippedAt, SYSDATETIME()), LastError=?
        WHERE NotificationID=? AND Status IN ('queued','sending','failed')
        """,
        str(reason or ""),
        notification_id,
    )


def mark_notification_send_error(conn, notification_id, error: str, max_attempts: int | None = None, backoff_seconds: int | None = None) -> dict:
    max_attempts = int(max_attempts or getattr(config, "NOTIFICATION_MAX_ATTEMPTS", 5))
    backoff_seconds = int(backoff_seconds or getattr(config, "NOTIFICATION_RETRY_BACKOFF_SECONDS", 300))
    cur = conn.cursor()
    cur.execute("SELECT AttemptCount FROM dbo.NotificationOutbox WHERE NotificationID=?", notification_id)
    row = cur.fetchone()
    attempts = int(row[0] or 0) if row else 0
    masked = str(error or "")
    if attempts >= max_attempts:
        cur.execute(
            """
            UPDATE dbo.NotificationOutbox
            SET Status='failed', LastError=?, QueuedAt=SYSDATETIME()
            WHERE NotificationID=?
            """,
            masked,
            notification_id,
        )
        return {"status": "failed", "attempts": attempts, "backoff_seconds": 0}
    cur.execute(
        """
        UPDATE dbo.NotificationOutbox
        SET Status='queued', LastError=?, QueuedAt=DATEADD(second, ?, SYSDATETIME())
        WHERE NotificationID=?
        """,
        masked,
        max(1, backoff_seconds),
        notification_id,
    )
    return {"status": "queued", "attempts": attempts, "backoff_seconds": max(1, backoff_seconds)}


def validate_notification_for_send(conn, notification_id: int) -> dict:
    """Return send-time notification validity and a clear cancellation reason when unsafe."""
    ensure_notification_tables(conn)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT TOP 1
            no.NotificationID,
            no.Status,
            no.EventID,
            no.SearchID AS OutboxSearchID,
            no.ListingID AS OutboxListingID,
            no.UserID,
            no.ChatID,
            no.EventType AS OutboxEventType,
            e.EventID AS ExistingEventID,
            e.SearchID AS EventSearchID,
            e.ListingID AS EventListingID,
            e.EventType AS EventType,
            e.ShouldNotify,
            e.Reason,
            e.EventPayloadJson,
            e.CreatedAt AS EventCreatedAt,
            uas.UserAreaID,
            uas.IsActive AS SubscriptionIsActive,
            uas.NotifyEnabled AS UserAreaNotifyEnabled,
            uas.SubscriptionStatus AS UserAreaSubscriptionStatus,
            uas.NotificationReadyAt,
            tu.IsActive AS TelegramUserIsActive,
            tu.ChatID AS CurrentChatID,
            COALESCE(substate.status, uas.SubscriptionStatus) AS EffectiveSubscriptionStatus,
            COALESCE(substate.notify_enabled, uas.NotifyEnabled) AS EffectiveNotifyEnabled,
            ams.setup_status AS AreaSetupStatus,
            lss.ListingID AS SearchStateListingID
        FROM dbo.NotificationOutbox no
        LEFT JOIN dbo.ListingEvent e ON e.EventID = no.EventID
        LEFT JOIN dbo.UserAreaSubscription uas
          ON uas.SearchID = COALESCE(no.SearchID, e.SearchID)
         AND (no.UserID IS NULL OR uas.TelegramUserID = no.UserID)
        LEFT JOIN dbo.TelegramUser tu
          ON tu.TelegramUserID = uas.TelegramUserID
         AND (no.ChatID IS NULL OR tu.ChatID = no.ChatID)
        LEFT JOIN dbo.user_area_subscription_state substate
          ON substate.user_id = uas.TelegramUserID AND substate.area_id = uas.SearchID
        LEFT JOIN dbo.area_monitoring_state ams ON ams.area_id = COALESCE(no.SearchID, e.SearchID)
        LEFT JOIN dbo.ListingSearchState lss
          ON lss.SearchID = COALESCE(no.SearchID, e.SearchID)
         AND lss.ListingID = COALESCE(no.ListingID, e.ListingID)
        WHERE no.NotificationID=?
        ORDER BY CASE WHEN no.ChatID IS NOT NULL AND tu.ChatID = no.ChatID THEN 0 ELSE 1 END, uas.UserAreaID ASC
        """,
        int(notification_id),
    )
    rows = _rows_to_dicts(cur)
    row = rows[0] if rows else None
    if not row:
        return {"valid": False, "reason": "notification_missing", "notification_id": notification_id}
    status = str(row.get("Status") or "").lower()
    if status != "queued":
        return {"valid": False, "reason": f"notification_status_{status or 'unknown'}", "notification": row}
    if row.get("ExistingEventID") is None:
        return {"valid": False, "reason": "event_missing", "notification": row}
    should_notify = row.get("ShouldNotify")
    if should_notify is None or int(should_notify or 0) == 0:
        return {"valid": False, "reason": "event_should_notify_false", "notification": row}
    event_type = str(row.get("EventType") or row.get("OutboxEventType") or "").lower()
    reason_text = str(row.get("Reason") or "").lower()
    if event_type in {"sold", "status_changed", "listing_sold"} and any(flag in reason_text for flag in ("weak_sold", "false_sold", "suppressed_sold", "suppressed")):
        return {"valid": False, "reason": "sold_event_suppressed_or_weak", "notification": row}
    if row.get("UserAreaID") is None or int(row.get("SubscriptionIsActive") or 0) != 1:
        return {"valid": False, "reason": "subscription_inactive_or_missing", "notification": row}
    if int(row.get("TelegramUserIsActive") or 0) != 1:
        return {"valid": False, "reason": "telegram_user_inactive", "notification": row}
    if int(row.get("EffectiveNotifyEnabled") or 0) != 1:
        return {"valid": False, "reason": "notify_disabled", "notification": row}
    if str(row.get("EffectiveSubscriptionStatus") or "").lower() != "active":
        return {"valid": False, "reason": "subscription_not_active", "notification": row}
    if str(row.get("AreaSetupStatus") or "").lower() != "ready":
        return {"valid": False, "reason": "area_not_ready", "notification": row}
    ready_at = row.get("NotificationReadyAt")
    if ready_at is None:
        return {"valid": False, "reason": "notification_ready_at_missing", "notification": row}
    event_created = row.get("EventCreatedAt")
    if event_created is not None and ready_at is not None and event_created < ready_at:
        return {"valid": False, "reason": "event_before_notification_ready", "notification": row}
    search_id = row.get("OutboxSearchID") or row.get("EventSearchID")
    listing_id = row.get("OutboxListingID") or row.get("EventListingID")
    if search_id is not None and listing_id is not None and row.get("SearchStateListingID") is None:
        return {"valid": False, "reason": "listing_not_in_search_context", "notification": row}
    return {"valid": True, "reason": "ok", "notification": row}


def cancel_notification_if_unsafe(conn, notification_id: int, reason_prefix: str = "send_time_revalidation") -> dict:
    validation = validate_notification_for_send(conn, int(notification_id))
    if validation.get("valid"):
        return validation
    reason = f"{reason_prefix}: {validation.get('reason') or 'unsafe'}"
    if validation.get("reason") != "notification_missing":
        mark_notification_cancelled(conn, int(notification_id), reason)
    validation["cancelled"] = validation.get("reason") != "notification_missing"
    validation["cancel_reason"] = reason
    return validation


def recover_stale_sending_notifications(conn, stale_minutes: int | None = None, max_attempts: int | None = None) -> dict:
    ensure_notification_tables(conn)
    stale_minutes = int(stale_minutes or getattr(config, "NOTIFICATION_STALE_SENDING_MINUTES", 30))
    max_attempts = int(max_attempts or getattr(config, "NOTIFICATION_MAX_ATTEMPTS", 5))
    cur = conn.cursor()
    cur.execute(
        """
        SELECT NotificationID, AttemptCount
        FROM dbo.NotificationOutbox
        WHERE Status='sending'
          AND COALESCE(QueuedAt, CreatedAt) < DATEADD(minute, ?, SYSDATETIME())
        ORDER BY NotificationID ASC
        """,
        -abs(stale_minutes),
    )
    rows = cur.fetchall()
    recovered_ids: list[int] = []
    failed_ids: list[int] = []
    for row in rows:
        notification_id = int(row[0])
        attempts = int(row[1] or 0)
        if attempts >= max_attempts:
            cur.execute(
                """
                UPDATE dbo.NotificationOutbox
                SET Status='failed', LastError=?, QueuedAt=SYSDATETIME()
                WHERE NotificationID=? AND Status='sending'
                """,
                f"failed after stale sending timeout and max attempts reached ({attempts}/{max_attempts})",
                notification_id,
            )
            failed_ids.append(notification_id)
        else:
            cur.execute(
                """
                UPDATE dbo.NotificationOutbox
                SET Status='queued', LastError=?, QueuedAt=SYSDATETIME()
                WHERE NotificationID=? AND Status='sending'
                """,
                f"recovered stale sending notification after {stale_minutes} minute timeout (attempts={attempts}/{max_attempts})",
                notification_id,
            )
            recovered_ids.append(notification_id)
    return {
        "stale_sending_recovered": len(recovered_ids),
        "stale_sending_failed": len(failed_ids),
        "recovered_notification_ids": recovered_ids,
        "failed_notification_ids": failed_ids,
    }


def sanitize_notification_outbox(conn, limit: int = 500) -> dict:
    ensure_notification_tables(conn)
    recovery = recover_stale_sending_notifications(conn)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT TOP (?) NotificationID
        FROM dbo.NotificationOutbox
        WHERE Status='queued'
        ORDER BY NotificationID ASC
        """,
        max(1, int(limit or 1)),
    )
    rows = cur.fetchall()
    cancelled_ids: list[int] = []
    reasons: dict[str, int] = {}
    for row in rows:
        notification_id = int(row[0])
        validation = cancel_notification_if_unsafe(conn, notification_id, reason_prefix="startup_outbox_sanitizer")
        if validation.get("cancelled"):
            cancelled_ids.append(notification_id)
            reason = str(validation.get("reason") or "unsafe")
            reasons[reason] = reasons.get(reason, 0) + 1
    return {
        **recovery,
        "notifications_skipped_by_revalidation": len(cancelled_ids),
        "cancelled_notification_ids": cancelled_ids,
        "cancelled_reasons": reasons,
    }


def cancel_notifications_for_subscription(conn, telegram_user_id: int, search_id: int | None = None, user_area_id: int | None = None, reason: str = "subscription_removed") -> int:
    ensure_notification_tables(conn)
    resolved_search_id = search_id
    if resolved_search_id is None and user_area_id is not None:
        cur_lookup = conn.cursor()
        cur_lookup.execute("SELECT SearchID FROM dbo.UserAreaSubscription WHERE TelegramUserID=? AND UserAreaID=?", int(telegram_user_id), int(user_area_id))
        row = cur_lookup.fetchone()
        if row:
            resolved_search_id = int(row[0])
    if resolved_search_id is None:
        return 0
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE dbo.NotificationOutbox
        SET Status='cancelled', SkippedAt=COALESCE(SkippedAt, SYSDATETIME()), LastError=?
        WHERE SearchID=?
          AND (UserID=? OR ChatID IN (SELECT ChatID FROM dbo.TelegramUser WHERE TelegramUserID=?))
          AND Status IN ('queued','sending','failed')
        """,
        str(reason or "subscription_removed"),
        int(resolved_search_id),
        int(telegram_user_id),
        int(telegram_user_id),
    )
    return int(getattr(cur, "rowcount", 0) or 0)


def get_notification_outbox_diagnostics(conn) -> dict:
    ensure_notification_tables(conn)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT Status, EventType, COUNT(*) AS CountValue, MIN(COALESCE(QueuedAt, CreatedAt)) AS OldestAt
        FROM dbo.NotificationOutbox
        WHERE Status IN ('queued','sending','failed')
        GROUP BY Status, EventType
        """
    )
    rows = _rows_to_dicts(cur)
    queued_by_event_type = {str(row.get("EventType") or "unknown"): int(row.get("CountValue") or 0) for row in rows if str(row.get("Status") or "").lower() == "queued"}
    sending_rows = [row for row in rows if str(row.get("Status") or "").lower() == "sending"]
    return {
        "queued_by_event_type": queued_by_event_type,
        "sending_count": sum(int(row.get("CountValue") or 0) for row in sending_rows),
        "oldest_sending_at": min((row.get("OldestAt") for row in sending_rows if row.get("OldestAt") is not None), default=None),
    }

# Phase 5 Telegram bot and user-area subscription helpers
_TELEGRAM_BOT_TABLES_ENSURED = False


def ensure_telegram_bot_tables(conn) -> None:
    """Create/upgrade TelegramUser and UserAreaSubscription tables idempotently."""
    global _TELEGRAM_BOT_TABLES_ENSURED
    if _TELEGRAM_BOT_TABLES_ENSURED:
        return
    ensure_notification_tables(conn)
    ensure_monitoring_state_tables(conn)
    ensure_area_numeric_capacity(conn)
    _execute_ddl_safely(conn, """
        IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA='dbo' AND TABLE_NAME='TelegramUser')
        CREATE TABLE dbo.TelegramUser (
            TelegramUserID INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_TelegramUser PRIMARY KEY,
            ChatID NVARCHAR(64) NOT NULL,
            TelegramUserName NVARCHAR(255) NULL,
            FirstName NVARCHAR(255) NULL,
            LastName NVARCHAR(255) NULL,
            IsActive BIT NOT NULL CONSTRAINT DF_TelegramUser_IsActive DEFAULT 1,
            CreatedAt DATETIME2 NOT NULL CONSTRAINT DF_TelegramUser_CreatedAt DEFAULT SYSDATETIME(),
            UpdatedAt DATETIME2 NOT NULL CONSTRAINT DF_TelegramUser_UpdatedAt DEFAULT SYSDATETIME(),
            LastSeenAt DATETIME2 NULL
        )
        """, description="create dbo.TelegramUser", required=False)
    _execute_ddl_safely(conn, """
        IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='UX_TelegramUser_ChatID' AND object_id=OBJECT_ID('dbo.TelegramUser'))
        CREATE UNIQUE INDEX UX_TelegramUser_ChatID ON dbo.TelegramUser(ChatID)
        """, description="create UX_TelegramUser_ChatID", required=False)
    _execute_ddl_safely(conn, """
        IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA='dbo' AND TABLE_NAME='UserAreaSubscription')
        CREATE TABLE dbo.UserAreaSubscription (
            UserAreaID INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_UserAreaSubscription PRIMARY KEY,
            TelegramUserID INT NOT NULL,
            SearchID INT NOT NULL,
            SearchURL NVARCHAR(1000) NOT NULL,
            AreaLabel NVARCHAR(255) NOT NULL,
            Suburb NVARCHAR(255) NULL,
            StateCode NVARCHAR(16) NULL,
            Postcode NVARCHAR(16) NULL,
            IsActive BIT NOT NULL CONSTRAINT DF_UserAreaSubscription_IsActive DEFAULT 1,
            SubscriptionStatus NVARCHAR(32) NOT NULL CONSTRAINT DF_UserAreaSubscription_SubscriptionStatus DEFAULT 'preparing',
            NotifyEnabled BIT NOT NULL CONSTRAINT DF_UserAreaSubscription_NotifyEnabled DEFAULT 0,
            RemovedAt DATETIME2 NULL,
            BaselineStatus NVARCHAR(32) NOT NULL CONSTRAINT DF_UserAreaSubscription_BaselineStatus DEFAULT 'pending',
            BaselineStartedAt DATETIME2 NULL,
            BaselineCompletedAt DATETIME2 NULL,
            NotificationStartAt DATETIME2 NULL,
            DetailBaselineStatus NVARCHAR(32) NOT NULL CONSTRAINT DF_UserAreaSubscription_DetailBaselineStatus DEFAULT 'pending',
            DetailBaselineStartedAt DATETIME2 NULL,
            DetailBaselineCompletedAt DATETIME2 NULL,
            DetailBaselineAttemptCount INT NOT NULL CONSTRAINT DF_UserAreaSubscription_DetailBaselineAttemptCount DEFAULT 0,
            DetailBaselineLastAttemptAt DATETIME2 NULL,
            DetailBaselineNextRetryAt DATETIME2 NULL,
            DetailBaselineLastError NVARCHAR(MAX) NULL,
            NotificationReadyAt DATETIME2 NULL,
            BaselineSummarySentAt DATETIME2 NULL,
            DetailBaselineStartedSummarySentAt DATETIME2 NULL,
            ReadySummarySentAt DATETIME2 NULL,
            BaselineListingsCollected INT NULL,
            BaselineNewCount INT NULL,
            BaselinePagesChecked INT NULL,
            BaselineTotalPagesDetected INT NULL,
            BaselineStopReason NVARCHAR(100) NULL,
            BaselineLastError NVARCHAR(MAX) NULL,
            LastLightCheckAt DATETIME2 NULL,
            LastDetailRefreshAt DATETIME2 NULL,
            LastNotificationQueuedAt DATETIME2 NULL,
            CreatedAt DATETIME2 NOT NULL CONSTRAINT DF_UserAreaSubscription_CreatedAt DEFAULT SYSDATETIME(),
            UpdatedAt DATETIME2 NOT NULL CONSTRAINT DF_UserAreaSubscription_UpdatedAt DEFAULT SYSDATETIME()
        )
        """, description="create dbo.UserAreaSubscription", required=False)
    for column_name, column_definition in {"SubscriptionStatus": "NVARCHAR(32) NOT NULL CONSTRAINT DF_UserAreaSubscription_SubscriptionStatus DEFAULT 'preparing'", "NotifyEnabled": "BIT NOT NULL CONSTRAINT DF_UserAreaSubscription_NotifyEnabled DEFAULT 0", "RemovedAt": "DATETIME2 NULL", "NotificationStartAt": "DATETIME2 NULL", "DetailBaselineStatus": "NVARCHAR(32) NOT NULL CONSTRAINT DF_UserAreaSubscription_DetailBaselineStatus DEFAULT 'pending'", "DetailBaselineStartedAt": "DATETIME2 NULL", "DetailBaselineCompletedAt": "DATETIME2 NULL", "DetailBaselineAttemptCount": "INT NOT NULL CONSTRAINT DF_UserAreaSubscription_DetailBaselineAttemptCount DEFAULT 0", "DetailBaselineLastAttemptAt": "DATETIME2 NULL", "DetailBaselineNextRetryAt": "DATETIME2 NULL", "DetailBaselineLastError": "NVARCHAR(MAX) NULL", "NotificationReadyAt": "DATETIME2 NULL", "BaselineSummarySentAt": "DATETIME2 NULL", "DetailBaselineStartedSummarySentAt": "DATETIME2 NULL", "ReadySummarySentAt": "DATETIME2 NULL", "BaselineListingsCollected": "INT NULL", "BaselineNewCount": "INT NULL", "BaselinePagesChecked": "INT NULL", "BaselineTotalPagesDetected": "INT NULL", "BaselineStopReason": "NVARCHAR(100) NULL", "BaselineLastError": "NVARCHAR(MAX) NULL", "LastNotificationQueuedAt": "DATETIME2 NULL", "PriceBaselineStatus": "NVARCHAR(32) NOT NULL CONSTRAINT DF_UserAreaSubscription_PriceBaselineStatus DEFAULT 'pending'", "PriceBaselineStartedAt": "DATETIME2 NULL", "PriceBaselineCompletedAt": "DATETIME2 NULL", "PriceBaselineLastError": "NVARCHAR(MAX) NULL", "LastPriceRefreshAt": "DATETIME2 NULL", "LastFullListingSweepAt": "DATETIME2 NULL"}.items():
        _execute_ddl_safely(conn, f"""
            IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA='dbo' AND TABLE_NAME='UserAreaSubscription' AND COLUMN_NAME='{column_name}')
            ALTER TABLE dbo.UserAreaSubscription ADD {column_name} {column_definition}
            """, description=f"add dbo.UserAreaSubscription.{column_name}", required=False)
    _execute_ddl_safely(conn, """
        IF OBJECT_ID('dbo.SuburbSearch') IS NOT NULL
        AND NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='UX_SuburbSearch_SearchHash' AND object_id=OBJECT_ID('dbo.SuburbSearch'))
        CREATE UNIQUE INDEX UX_SuburbSearch_SearchHash
        ON dbo.SuburbSearch(SearchHash)
        WHERE SearchHash IS NOT NULL
        """, description="create UX_SuburbSearch_SearchHash", required=False)
    _execute_ddl_safely(conn, """
        IF OBJECT_ID('dbo.SuburbSearch') IS NOT NULL
        AND NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='UX_SuburbSearch_NormalizedSearchURL' AND object_id=OBJECT_ID('dbo.SuburbSearch'))
        CREATE UNIQUE INDEX UX_SuburbSearch_NormalizedSearchURL
        ON dbo.SuburbSearch(NormalizedSearchURL)
        WHERE NormalizedSearchURL IS NOT NULL
        """, description="create UX_SuburbSearch_NormalizedSearchURL", required=False)
    _execute_ddl_safely(conn, """
        IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='UX_UserAreaSubscription_ActiveSearch' AND object_id=OBJECT_ID('dbo.UserAreaSubscription'))
        CREATE UNIQUE INDEX UX_UserAreaSubscription_ActiveSearch
        ON dbo.UserAreaSubscription(TelegramUserID, SearchURL)
        WHERE IsActive = 1
        """, description="create UX_UserAreaSubscription_ActiveSearch", required=False)
    _execute_ddl_safely(conn, """
        IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='UX_UserAreaSubscription_ActiveTelegramSearchID' AND object_id=OBJECT_ID('dbo.UserAreaSubscription'))
        CREATE UNIQUE INDEX UX_UserAreaSubscription_ActiveTelegramSearchID
        ON dbo.UserAreaSubscription(TelegramUserID, SearchID)
        WHERE IsActive = 1
        """, description="create UX_UserAreaSubscription_ActiveTelegramSearchID", required=False)
    _execute_ddl_safely(conn, """
        IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_UserAreaSubscription_Due' AND object_id=OBJECT_ID('dbo.UserAreaSubscription'))
        CREATE INDEX IX_UserAreaSubscription_Due
        ON dbo.UserAreaSubscription(IsActive, BaselineStatus, LastLightCheckAt, LastDetailRefreshAt)
        INCLUDE (TelegramUserID, SearchID)
        """, description="create IX_UserAreaSubscription_Due", required=False)
    _execute_ddl_safely(conn, """
        IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA='dbo' AND TABLE_NAME='TelegramUserSession')
        CREATE TABLE dbo.TelegramUserSession (
            TelegramUserID INT NOT NULL CONSTRAINT PK_TelegramUserSession PRIMARY KEY,
            State NVARCHAR(100) NOT NULL CONSTRAINT DF_TelegramUserSession_State DEFAULT 'idle',
            PayloadJson NVARCHAR(MAX) NULL,
            UpdatedAt DATETIME2 NOT NULL CONSTRAINT DF_TelegramUserSession_UpdatedAt DEFAULT SYSDATETIME()
        )
        """, description="create dbo.TelegramUserSession", required=False)
    ensure_lifecycle_text_column_capacity(conn)
    _TELEGRAM_BOT_TABLES_ENSURED = True


def get_user_session(conn, telegram_user_id: int) -> dict:
    ensure_telegram_bot_tables(conn)
    cur = conn.cursor()
    cur.execute("SELECT TelegramUserID, State, PayloadJson, UpdatedAt FROM dbo.TelegramUserSession WHERE TelegramUserID=?", int(telegram_user_id))
    rows = _rows_to_dicts(cur)
    if not rows:
        return {"telegram_user_id": int(telegram_user_id), "state": "idle", "payload": {}}
    row = rows[0]
    try:
        payload = json.loads(row.get("PayloadJson") or "{}")
    except (TypeError, json.JSONDecodeError):
        payload = {}
    return {"telegram_user_id": int(row["TelegramUserID"]), "state": row.get("State") or "idle", "payload": payload if isinstance(payload, dict) else {}, "updated_at": row.get("UpdatedAt")}


def set_user_session(conn, telegram_user_id: int, state: str, payload: dict | None = None) -> None:
    ensure_telegram_bot_tables(conn)
    allowed = {"idle", "waiting_for_area_input", "choosing_area_candidate", "confirming_area", "removing_area"}
    normalized_state = str(state or "idle")
    if normalized_state not in allowed:
        raise ValueError(f"Unsupported Telegram session state: {normalized_state}")
    payload_json = json_dumps_safe(payload or {})
    conn.cursor().execute("""
        MERGE dbo.TelegramUserSession AS target
        USING (SELECT ? AS TelegramUserID) AS source
        ON target.TelegramUserID = source.TelegramUserID
        WHEN MATCHED THEN UPDATE SET State=?, PayloadJson=?, UpdatedAt=SYSDATETIME()
        WHEN NOT MATCHED THEN INSERT (TelegramUserID, State, PayloadJson, UpdatedAt) VALUES (?, ?, ?, SYSDATETIME());
        """, int(telegram_user_id), normalized_state, payload_json, int(telegram_user_id), normalized_state, payload_json)
    conn.commit()


def clear_user_session(conn, telegram_user_id: int) -> None:
    set_user_session(conn, telegram_user_id, "idle", {})


def upsert_telegram_user(conn, chat_id: str, username: str | None = None, first_name: str | None = None, last_name: str | None = None) -> int:
    ensure_telegram_bot_tables(conn)
    chat_id = str(chat_id)
    cur = conn.cursor()
    cur.execute("""
        MERGE dbo.TelegramUser AS target
        USING (SELECT ? AS ChatID) AS source
        ON target.ChatID = source.ChatID
        WHEN MATCHED THEN UPDATE SET TelegramUserName=COALESCE(?, target.TelegramUserName), FirstName=COALESCE(?, target.FirstName), LastName=COALESCE(?, target.LastName), IsActive=1, UpdatedAt=SYSDATETIME(), LastSeenAt=SYSDATETIME()
        WHEN NOT MATCHED THEN INSERT (ChatID, TelegramUserName, FirstName, LastName, IsActive, CreatedAt, UpdatedAt, LastSeenAt)
            VALUES (?, ?, ?, ?, 1, SYSDATETIME(), SYSDATETIME(), SYSDATETIME());
        """, chat_id, username, first_name, last_name, chat_id, username, first_name, last_name)
    cur.execute("SELECT TelegramUserID FROM dbo.TelegramUser WHERE ChatID=?", chat_id)
    row = cur.fetchone()
    conn.commit()
    return int(row[0])


def get_telegram_user_by_chat_id(conn, chat_id: str) -> dict | None:
    ensure_telegram_bot_tables(conn)
    cur = conn.cursor()
    cur.execute("SELECT TelegramUserID, ChatID, TelegramUserName, FirstName, LastName, IsActive, LastSeenAt FROM dbo.TelegramUser WHERE ChatID=?", str(chat_id))
    rows = _rows_to_dicts(cur)
    return rows[0] if rows else None


def active_area_count_for_user(conn, telegram_user_id: int) -> int:
    ensure_telegram_bot_tables(conn)
    row = _one(conn.cursor(), "SELECT COUNT(1) FROM dbo.UserAreaSubscription WHERE TelegramUserID=? AND IsActive=1", int(telegram_user_id))
    return int(row[0]) if row else 0


def _search_setup_from_rows(rows: list[dict]) -> dict:
    if not rows:
        return {
            "state": "not_started",
            "has_subscriptions": False,
            "is_ready": False,
            "is_running": False,
            "ready_at": None,
            "baseline_status": "pending",
            "detail_baseline_status": "pending",
            "price_baseline_status": "pending",
            "baseline_completed": False,
            "detail_started": False,
            "price_started": False,
            "price_completed": False,
        }
    ready_rows = [
        row for row in rows
        if str(row.get("BaselineStatus") or "").lower() == "completed"
        and str(row.get("DetailBaselineStatus") or "").lower() == "completed"
        and str(row.get("PriceBaselineStatus") or "pending").lower() == "completed"
        and row.get("NotificationReadyAt")
    ]
    triples = {
        (
            str(row.get("BaselineStatus") or "pending").lower(),
            str(row.get("DetailBaselineStatus") or "pending").lower(),
            str(row.get("PriceBaselineStatus") or "pending").lower(),
        )
        for row in rows
    }
    baseline_completed = any(base == "completed" for base, _detail, _price in triples)
    detail_completed = any(detail == "completed" for _base, detail, _price in triples)
    price_completed = any(price == "completed" for _base, _detail, price in triples)
    detail_started = any(detail in {"running", "retry_wait", "completed"} for _base, detail, _price in triples) or any(row.get("DetailBaselineStartedAt") for row in rows)
    price_started = any(price in {"running", "completed"} for _base, _detail, price in triples) or any(row.get("PriceBaselineStartedAt") for row in rows)
    if ready_rows:
        ready_at = max(row.get("NotificationReadyAt") for row in ready_rows if row.get("NotificationReadyAt"))
        return {
            "state": "ready",
            "has_subscriptions": True,
            "is_ready": True,
            "is_running": False,
            "ready_at": ready_at,
            "baseline_status": "completed",
            "detail_baseline_status": "completed",
            "price_baseline_status": "completed",
            "baseline_completed": True,
            "detail_started": True,
            "price_started": True,
            "price_completed": True,
        }
    if any(base in {"pending", "running"} or detail in {"pending", "running", "retry_wait"} or price in {"pending", "running"} for base, detail, price in triples):
        return {
            "state": "running",
            "has_subscriptions": True,
            "is_ready": False,
            "is_running": True,
            "ready_at": None,
            "baseline_status": "completed" if baseline_completed else "pending",
            "detail_baseline_status": "completed" if detail_completed else ("running" if detail_started else "pending"),
            "price_baseline_status": "completed" if price_completed else ("running" if price_started else "pending"),
            "baseline_completed": baseline_completed,
            "detail_started": detail_started,
            "price_started": price_started,
            "price_completed": price_completed,
        }
    if any(base == "failed" or detail == "failed" or price == "failed" for base, detail, price in triples):
        return {
            "state": "failed",
            "has_subscriptions": True,
            "is_ready": False,
            "is_running": False,
            "ready_at": None,
            "baseline_status": "failed",
            "detail_baseline_status": "failed" if not detail_completed else "completed",
            "price_baseline_status": "failed" if not price_completed else "completed",
            "baseline_completed": baseline_completed,
            "detail_started": detail_started,
            "price_started": price_started,
            "price_completed": price_completed,
        }
    return {
        "state": "not_started",
        "has_subscriptions": True,
        "is_ready": False,
        "is_running": False,
        "ready_at": None,
        "baseline_status": "pending",
        "detail_baseline_status": "pending",
        "price_baseline_status": "pending",
        "baseline_completed": False,
        "detail_started": False,
        "price_started": False,
        "price_completed": False,
    }

def get_search_setup_state(conn, search_id: int) -> dict:
    ensure_telegram_bot_tables(conn)
    try:
        area_state = get_area_monitoring_state(conn, int(search_id))
    except Exception:
        area_state = None
    if area_state:
        setup_status = str(area_state.get("setup_status") or "not_started").lower()
        if setup_status == "inactive":
            return {
                "state": "inactive",
                "search_id": int(search_id),
                "has_subscriptions": False,
                "is_ready": False,
                "is_running": False,
                "ready_at": None,
                "baseline_status": "pending",
                "detail_baseline_status": "pending",
                "price_baseline_status": "pending",
                "baseline_completed": False,
                "detail_started": False,
                "price_started": False,
                "price_completed": False,
                "active_listing_count": area_state.get("active_listing_count"),
                "inferred_price_count": area_state.get("inferred_price_count"),
                "unknown_price_count": area_state.get("unknown_price_count"),
                "last_error": area_state.get("last_error"),
            }
        state = {
            "state": setup_status,
            "search_id": int(search_id),
            "has_subscriptions": True,
            "is_ready": setup_status == "ready",
            "is_running": setup_status == "preparing",
            "ready_at": area_state.get("ready_at"),
            "baseline_status": "completed" if area_state.get("module1_status") == "completed" or setup_status == "ready" else setup_status,
            "detail_baseline_status": "completed" if area_state.get("module3_status") == "completed" or setup_status == "ready" else "pending",
            "price_baseline_status": "completed" if area_state.get("module2_status") == "completed" or setup_status == "ready" else "pending",
            "baseline_completed": area_state.get("module1_status") == "completed" or setup_status == "ready",
            "detail_started": area_state.get("module3_status") in {"running", "completed"} or setup_status == "ready",
            "price_started": area_state.get("module2_status") in {"running", "completed"} or setup_status == "ready",
            "price_completed": area_state.get("module2_status") == "completed" or setup_status == "ready",
            "active_listing_count": area_state.get("active_listing_count"),
            "inferred_price_count": area_state.get("inferred_price_count"),
            "unknown_price_count": area_state.get("unknown_price_count"),
            "last_error": area_state.get("last_error"),
        }
        return state
    cur = conn.cursor()
    cur.execute("""
        SELECT BaselineStatus, DetailBaselineStatus, DetailBaselineStartedAt, PriceBaselineStatus, PriceBaselineStartedAt, NotificationReadyAt
        FROM dbo.UserAreaSubscription
        WHERE SearchID=?
        """, int(search_id))
    rows = _rows_to_dicts(cur)
    state = _search_setup_from_rows(rows)
    state["search_id"] = int(search_id)
    try:
        cur.execute("SELECT COUNT(1) FROM dbo.ListingSearchState WHERE SearchID=? AND LOWER(COALESCE(ListingLifecycleStatus, Status, 'active'))='active'", int(search_id))
        row = cur.fetchone()
        state["active_listing_count"] = int(row[0] or 0) if row else 0
    except Exception:
        state["active_listing_count"] = None
    return state


def is_search_ready(conn, search_id: int) -> bool:
    return bool(get_search_setup_state(conn, search_id).get("is_ready"))


def user_already_subscribed(conn, telegram_user_id: int, search_id: int) -> bool:
    ensure_telegram_bot_tables(conn)
    row = _one(conn.cursor(), "SELECT 1 FROM dbo.UserAreaSubscription WHERE TelegramUserID=? AND SearchID=? AND IsActive=1", int(telegram_user_id), int(search_id))
    return bool(row)


def create_or_reactivate_subscription(conn, telegram_user_id: int, search_id: int, search_url: str, area_label: str, suburb: str | None = None, state_code: str | None = None, postcode: str | None = None, setup_state: dict | None = None) -> tuple[str, dict]:
    """Create/reactivate one user subscription for a shared SearchID and reset its notification gate."""
    ensure_telegram_bot_tables(conn)
    setup = setup_state or get_search_setup_state(conn, search_id)
    ready_now = bool(setup.get("is_ready"))
    subscription_status = "active" if ready_now else "preparing"
    notify_enabled = 1 if ready_now else 0
    initial_baseline = "completed" if setup.get("is_ready") or setup.get("baseline_completed") else ("failed" if setup.get("state") == "failed" else "pending")
    initial_detail = "completed" if setup.get("is_ready") or setup.get("detail_baseline_status") == "completed" else ("failed" if setup.get("state") == "failed" else ("running" if setup.get("detail_started") else "pending"))
    initial_price = "completed" if setup.get("is_ready") or setup.get("price_completed") else ("failed" if setup.get("state") == "failed" else ("running" if setup.get("price_started") else "pending"))
    notification_ready_expr = "SYSDATETIME()" if setup.get("is_ready") else "NULL"
    suppress_baseline_summary_expr = "SYSDATETIME()" if setup.get("is_ready") or setup.get("baseline_completed") else "NULL"
    suppress_detail_started_summary_expr = "SYSDATETIME()" if setup.get("is_ready") or setup.get("detail_started") else "NULL"
    suppress_ready_summary_expr = "SYSDATETIME()" if setup.get("is_ready") else "NULL"
    cur = conn.cursor()
    cur.execute("""
        SELECT TOP 1 UserAreaID, IsActive
        FROM dbo.UserAreaSubscription WITH (UPDLOCK, HOLDLOCK)
        WHERE TelegramUserID=? AND SearchID=?
        ORDER BY IsActive DESC, UserAreaID ASC
        """, int(telegram_user_id), int(search_id))
    row = cur.fetchone()
    if row and bool(row[1]):
        return "already_active", {"user_area_id": int(row[0]), "search_id": int(search_id), "search_url": search_url, "area_label": area_label, "baseline_status": initial_baseline, "detail_baseline_status": initial_detail, "price_baseline_status": initial_price}
    if row:
        user_area_id = int(row[0])
        cur.execute(f"""
            UPDATE dbo.UserAreaSubscription
            SET SearchURL=?, AreaLabel=?, Suburb=?, StateCode=?, Postcode=?, IsActive=1,
                SubscriptionStatus=?, NotifyEnabled=?, RemovedAt=NULL,
                BaselineStatus=?, BaselineCompletedAt=CASE WHEN ?='completed' THEN COALESCE(BaselineCompletedAt, SYSDATETIME()) ELSE NULL END,
                DetailBaselineStatus=?, DetailBaselineStartedAt=CASE WHEN ? IN ('running','completed') THEN COALESCE(DetailBaselineStartedAt, SYSDATETIME()) ELSE NULL END,
                DetailBaselineCompletedAt=CASE WHEN ?='completed' THEN COALESCE(DetailBaselineCompletedAt, SYSDATETIME()) ELSE NULL END,
                PriceBaselineStatus=?, PriceBaselineStartedAt=CASE WHEN ? IN ('running','completed') THEN COALESCE(PriceBaselineStartedAt, SYSDATETIME()) ELSE NULL END,
                PriceBaselineCompletedAt=CASE WHEN ?='completed' THEN COALESCE(PriceBaselineCompletedAt, SYSDATETIME()) ELSE NULL END,
                NotificationStartAt=SYSDATETIME(), NotificationReadyAt={notification_ready_expr},
                BaselineSummarySentAt={suppress_baseline_summary_expr}, DetailBaselineStartedSummarySentAt={suppress_detail_started_summary_expr}, ReadySummarySentAt={suppress_ready_summary_expr},
                UpdatedAt=SYSDATETIME()
            WHERE UserAreaID=?
            """, search_url, area_label, suburb, state_code, postcode, subscription_status, notify_enabled, initial_baseline, initial_baseline, initial_detail, initial_detail, initial_detail, initial_price, initial_price, initial_price, user_area_id)
        return "reactivated", {"user_area_id": user_area_id, "search_id": int(search_id), "search_url": search_url, "area_label": area_label, "baseline_status": initial_baseline, "detail_baseline_status": initial_detail, "price_baseline_status": initial_price}
    cur.execute(f"""
        INSERT INTO dbo.UserAreaSubscription(
            TelegramUserID, SearchID, SearchURL, AreaLabel, Suburb, StateCode, Postcode, IsActive,
            SubscriptionStatus, NotifyEnabled, RemovedAt,
            BaselineStatus, BaselineCompletedAt, NotificationStartAt,
            DetailBaselineStatus, DetailBaselineStartedAt, DetailBaselineCompletedAt, PriceBaselineStatus, PriceBaselineStartedAt, PriceBaselineCompletedAt, NotificationReadyAt,
            BaselineSummarySentAt, DetailBaselineStartedSummarySentAt, ReadySummarySentAt,
            CreatedAt, UpdatedAt
        )
        OUTPUT INSERTED.UserAreaID
        VALUES (
            ?, ?, ?, ?, ?, ?, ?, 1,
            ?, ?, NULL,
            ?, CASE WHEN ?='completed' THEN SYSDATETIME() ELSE NULL END, SYSDATETIME(),
            ?, CASE WHEN ? IN ('running','completed') THEN SYSDATETIME() ELSE NULL END, CASE WHEN ?='completed' THEN SYSDATETIME() ELSE NULL END,
            ?, CASE WHEN ? IN ('running','completed') THEN SYSDATETIME() ELSE NULL END, CASE WHEN ?='completed' THEN SYSDATETIME() ELSE NULL END, {notification_ready_expr},
            {suppress_baseline_summary_expr}, {suppress_detail_started_summary_expr}, {suppress_ready_summary_expr},
            SYSDATETIME(), SYSDATETIME()
        )
        """, int(telegram_user_id), int(search_id), search_url, area_label, suburb, state_code, postcode, subscription_status, notify_enabled, initial_baseline, initial_baseline, initial_detail, initial_detail, initial_detail, initial_price, initial_price, initial_price)
    user_area_id = int(cur.fetchone()[0])
    return "created", {"user_area_id": user_area_id, "search_id": int(search_id), "search_url": search_url, "area_label": area_label, "baseline_status": initial_baseline, "detail_baseline_status": initial_detail, "price_baseline_status": initial_price}


def add_user_area_subscription(conn, telegram_user_id: int, search_url: str, area_label: str, suburb: str | None = None, state_code: str | None = None, postcode: str | None = None, max_active: int | None = None) -> tuple[bool, dict]:
    ensure_telegram_bot_tables(conn)
    max_allowed = config.MAX_AREAS_PER_USER if max_active is None else int(max_active)
    search_url = ensure_sort_list_date(search_url)
    search_id, search_created = get_or_create_suburb_search(conn, search_url, area_label=area_label, suburb=suburb, postcode=postcode)
    if user_already_subscribed(conn, telegram_user_id, search_id):
        conn.commit()
        return False, {"reason": "duplicate", "message": "You're already monitoring this search area.", "search_id": int(search_id), "search_created": search_created}
    if active_area_count_for_user(conn, telegram_user_id) >= max_allowed:
        conn.commit()
        return False, {"reason": "max_areas", "message": f"Maximum {max_allowed} active areas allowed", "search_id": int(search_id), "search_created": search_created}
    setup_state = get_search_setup_state(conn, search_id)
    if setup_state.get("state") in {"not_started", "failed", "inactive"}:
        upsert_area_monitoring_state(
            conn,
            int(search_id),
            setup_status="preparing",
            module1_status="pending",
            module3_status="pending",
            module2_status="pending",
            set_started=True,
            set_reactivated=setup_state.get("state") == "inactive",
            last_subscription_count=1,
            last_error=None,
        )
        setup_state = {**setup_state, "state": "preparing", "is_running": True}
    action, payload = create_or_reactivate_subscription(conn, telegram_user_id, search_id, search_url, area_label, suburb=suburb, state_code=state_code, postcode=postcode, setup_state=setup_state)
    ready_now = bool(setup_state.get("is_ready"))
    upsert_user_area_subscription_state(
        conn,
        int(telegram_user_id),
        int(search_id),
        status="active" if ready_now else "preparing",
        notify_enabled=ready_now,
    )
    conn.commit()
    if action == "already_active":
        return False, {"reason": "duplicate", "message": "You're already monitoring this search area.", **payload, "search_created": search_created, "setup_state": setup_state.get("state")}
    baseline_job = None
    if ready_now:
        reason = "ready"
        message = "Monitoring is already active for this area."
    else:
        baseline_job = enqueue_baseline_setup_job(conn, int(search_id), search_url)
        conn.commit()
        if setup_state.get("is_running") and not search_created:
            reason = "setup_running"
        else:
            reason = "setup_required"
        message = "⏳ Preparing monitoring"
    if reason == "setup_running":
        message = "⏳ Preparing monitoring"
    return True, {**payload, "reason": reason, "message": message, "search_created": search_created, "setup_state": setup_state.get("state"), "subscription_action": action, "baseline_job": baseline_job}


def list_user_area_subscriptions(conn, telegram_user_id: int, active_only: bool = True) -> list[dict]:
    ensure_telegram_bot_tables(conn)
    where = "WHERE uas.TelegramUserID=?" + (" AND uas.IsActive=1 AND COALESCE(substate.status, uas.SubscriptionStatus, 'active') IN ('active','preparing')" if active_only else "")
    cur = conn.cursor()
    cur.execute(f"""
        SELECT uas.UserAreaID, uas.TelegramUserID, tu.ChatID, uas.SearchID, uas.SearchURL, uas.AreaLabel,
               uas.Suburb, uas.StateCode, uas.Postcode, uas.IsActive, uas.BaselineStatus,
               uas.BaselineStartedAt, uas.BaselineCompletedAt, uas.NotificationStartAt,
               uas.DetailBaselineStatus, uas.DetailBaselineStartedAt, uas.DetailBaselineCompletedAt,
               uas.DetailBaselineAttemptCount, uas.DetailBaselineLastAttemptAt, uas.DetailBaselineNextRetryAt, uas.DetailBaselineLastError, uas.NotificationReadyAt,
               uas.BaselineSummarySentAt, uas.DetailBaselineStartedSummarySentAt, uas.ReadySummarySentAt,
               uas.BaselineListingsCollected, uas.BaselineNewCount, uas.BaselinePagesChecked, uas.BaselineTotalPagesDetected, uas.BaselineStopReason, uas.BaselineLastError,
               uas.LastLightCheckAt, uas.LastDetailRefreshAt, uas.LastPriceRefreshAt, uas.LastFullListingSweepAt, uas.LastNotificationQueuedAt,
               uas.PriceBaselineStatus, uas.PriceBaselineStartedAt, uas.PriceBaselineCompletedAt, uas.PriceBaselineLastError,
               uas.SubscriptionStatus AS UserAreaSubscriptionStatus, uas.NotifyEnabled AS UserAreaNotifyEnabled, uas.RemovedAt,
               ams.setup_status AS AreaSetupStatus, ams.module1_status AS AreaModule1Status, ams.module3_status AS AreaModule3Status, ams.module2_status AS AreaModule2Status, ams.active_listing_count AS AreaActiveListingCount, ams.last_error AS AreaLastError, ams.ready_at AS AreaReadyAt,
               (SELECT COUNT(1) FROM dbo.ListingSearchState lss WHERE lss.SearchID=uas.SearchID AND LOWER(COALESCE(lss.ListingLifecycleStatus, lss.Status, 'active'))='active') AS LiveActiveListingCount,
               substate.status AS SubscriptionStatus, substate.notify_enabled AS SubscriptionNotifyEnabled,
               uas.CreatedAt, uas.UpdatedAt
        FROM dbo.UserAreaSubscription uas
        JOIN dbo.TelegramUser tu ON tu.TelegramUserID = uas.TelegramUserID
        LEFT JOIN dbo.area_monitoring_state ams ON ams.area_id = uas.SearchID
        LEFT JOIN dbo.user_area_subscription_state substate ON substate.user_id = uas.TelegramUserID AND substate.area_id = uas.SearchID
        {where}
        ORDER BY uas.UserAreaID ASC
        """, int(telegram_user_id))
    return _rows_to_dicts(cur)


def get_user_area_subscription(conn, user_area_id: int) -> dict | None:
    ensure_telegram_bot_tables(conn)
    cur = conn.cursor()
    cur.execute("""
        SELECT uas.UserAreaID, uas.TelegramUserID, tu.ChatID, uas.SearchID, uas.SearchURL, uas.AreaLabel,
               uas.Suburb, uas.StateCode, uas.Postcode, uas.IsActive, uas.BaselineStatus,
               uas.BaselineStartedAt, uas.BaselineCompletedAt, uas.NotificationStartAt,
               uas.DetailBaselineStatus, uas.DetailBaselineStartedAt, uas.DetailBaselineCompletedAt,
               uas.DetailBaselineAttemptCount, uas.DetailBaselineLastAttemptAt, uas.DetailBaselineNextRetryAt, uas.DetailBaselineLastError, uas.NotificationReadyAt,
               uas.BaselineSummarySentAt, uas.DetailBaselineStartedSummarySentAt, uas.ReadySummarySentAt,
               uas.BaselineListingsCollected, uas.BaselineNewCount, uas.BaselinePagesChecked, uas.BaselineTotalPagesDetected, uas.BaselineStopReason, uas.BaselineLastError,
               uas.LastLightCheckAt, uas.LastDetailRefreshAt, uas.LastPriceRefreshAt, uas.LastFullListingSweepAt, uas.LastNotificationQueuedAt,
               uas.PriceBaselineStatus, uas.PriceBaselineStartedAt, uas.PriceBaselineCompletedAt, uas.PriceBaselineLastError,
               uas.SubscriptionStatus AS UserAreaSubscriptionStatus, uas.NotifyEnabled AS UserAreaNotifyEnabled, uas.RemovedAt,
               ams.setup_status AS AreaSetupStatus, ams.module1_status AS AreaModule1Status, ams.module3_status AS AreaModule3Status, ams.module2_status AS AreaModule2Status, ams.active_listing_count AS AreaActiveListingCount, ams.last_error AS AreaLastError, ams.ready_at AS AreaReadyAt,
               (SELECT COUNT(1) FROM dbo.ListingSearchState lss WHERE lss.SearchID=uas.SearchID AND LOWER(COALESCE(lss.ListingLifecycleStatus, lss.Status, 'active'))='active') AS LiveActiveListingCount,
               substate.status AS SubscriptionStatus, substate.notify_enabled AS SubscriptionNotifyEnabled,
               uas.CreatedAt, uas.UpdatedAt
        FROM dbo.UserAreaSubscription uas
        JOIN dbo.TelegramUser tu ON tu.TelegramUserID = uas.TelegramUserID
        LEFT JOIN dbo.area_monitoring_state ams ON ams.area_id = uas.SearchID
        LEFT JOIN dbo.user_area_subscription_state substate ON substate.user_id = uas.TelegramUserID AND substate.area_id = uas.SearchID
        WHERE uas.UserAreaID=?
        """, int(user_area_id))
    rows = _rows_to_dicts(cur)
    return rows[0] if rows else None


def remove_user_area_subscription_lifecycle(conn, telegram_user_id: int, user_area_id: int) -> dict:
    ensure_telegram_bot_tables(conn)
    cur = conn.cursor()
    cur.execute("""
        SELECT UserAreaID, SearchID, IsActive
        FROM dbo.UserAreaSubscription WITH (UPDLOCK, HOLDLOCK)
        WHERE TelegramUserID=? AND UserAreaID=?
        """, int(telegram_user_id), int(user_area_id))
    row = cur.fetchone()
    if not row:
        conn.commit()
        return {
            "removed": False,
            "telegram_user_id": int(telegram_user_id),
            "user_area_id": int(user_area_id),
            "resolved_search_id": None,
            "resolved_area_id": None,
            "remaining_active_subscriptions": None,
            "action": "missing",
            "cancelled_jobs": 0,
        }
    resolved_search_id = int(row[1])
    cur.execute("""
        UPDATE dbo.UserAreaSubscription
        SET IsActive=0,
            SubscriptionStatus='removed',
            NotifyEnabled=0,
            RemovedAt=COALESCE(RemovedAt, SYSDATETIME()),
            NotificationReadyAt=NULL,
            UpdatedAt=SYSDATETIME()
        WHERE TelegramUserID=? AND UserAreaID=? AND IsActive=1
        """, int(telegram_user_id), int(user_area_id))
    changed = cur.rowcount > 0
    cancelled_notifications = 0
    if changed:
        upsert_user_area_subscription_state(conn, int(telegram_user_id), resolved_search_id, status="removed", notify_enabled=False)
        cancelled_notifications = cancel_notifications_for_subscription(
            conn,
            int(telegram_user_id),
            search_id=resolved_search_id,
            user_area_id=int(user_area_id),
            reason="subscription_removed_before_send",
        )
        lifecycle = deactivate_area_if_unused(conn, search_id=resolved_search_id)
    else:
        remaining = count_active_subscriptions_for_area(conn, search_id=resolved_search_id)
        lifecycle = {
            "search_id": resolved_search_id,
            "area_id": resolved_search_id,
            "remaining_active_subscriptions": remaining,
            "action": "already_removed",
            "cancelled_jobs": 0,
        }
    conn.commit()
    return {
        "removed": bool(changed),
        "telegram_user_id": int(telegram_user_id),
        "user_area_id": int(user_area_id),
        "resolved_search_id": resolved_search_id,
        "resolved_area_id": resolved_search_id,
        "remaining_active_subscriptions": lifecycle.get("remaining_active_subscriptions"),
        "action": lifecycle.get("action"),
        "cancelled_jobs": lifecycle.get("cancelled_jobs", 0),
        "cancelled_notifications": cancelled_notifications if changed else 0,
    }


def deactivate_user_area_subscription(conn, telegram_user_id: int, user_area_id: int) -> bool:
    return bool(remove_user_area_subscription_lifecycle(conn, telegram_user_id, user_area_id).get("removed"))


def get_active_user_area_subscriptions(conn) -> list[dict]:
    ensure_telegram_bot_tables(conn)
    cur = conn.cursor()
    cur.execute("""
        SELECT uas.UserAreaID, uas.TelegramUserID, tu.ChatID, uas.SearchID, uas.SearchURL, uas.AreaLabel,
               uas.Suburb, uas.StateCode, uas.Postcode, uas.IsActive, uas.BaselineStatus,
               uas.BaselineStartedAt, uas.BaselineCompletedAt, uas.NotificationStartAt,
               uas.DetailBaselineStatus, uas.DetailBaselineStartedAt, uas.DetailBaselineCompletedAt,
               uas.DetailBaselineAttemptCount, uas.DetailBaselineLastAttemptAt, uas.DetailBaselineNextRetryAt, uas.DetailBaselineLastError, uas.NotificationReadyAt,
               uas.BaselineSummarySentAt, uas.DetailBaselineStartedSummarySentAt, uas.ReadySummarySentAt,
               uas.BaselineListingsCollected, uas.BaselineNewCount, uas.BaselinePagesChecked, uas.BaselineTotalPagesDetected, uas.BaselineStopReason, uas.BaselineLastError,
               uas.LastLightCheckAt, uas.LastDetailRefreshAt, uas.LastPriceRefreshAt, uas.LastFullListingSweepAt, uas.LastNotificationQueuedAt,
               uas.PriceBaselineStatus, uas.PriceBaselineStartedAt, uas.PriceBaselineCompletedAt, uas.PriceBaselineLastError,
               uas.SubscriptionStatus AS UserAreaSubscriptionStatus, uas.NotifyEnabled AS UserAreaNotifyEnabled, uas.RemovedAt,
               ams.setup_status AS AreaSetupStatus, ams.module1_status AS AreaModule1Status, ams.module3_status AS AreaModule3Status, ams.module2_status AS AreaModule2Status, ams.active_listing_count AS AreaActiveListingCount, ams.last_error AS AreaLastError, ams.ready_at AS AreaReadyAt,
               (SELECT COUNT(1) FROM dbo.ListingSearchState lss WHERE lss.SearchID=uas.SearchID AND LOWER(COALESCE(lss.ListingLifecycleStatus, lss.Status, 'active'))='active') AS LiveActiveListingCount,
               substate.status AS SubscriptionStatus, substate.notify_enabled AS SubscriptionNotifyEnabled,
               uas.CreatedAt, uas.UpdatedAt
        FROM dbo.UserAreaSubscription uas
        JOIN dbo.TelegramUser tu ON tu.TelegramUserID = uas.TelegramUserID
        LEFT JOIN dbo.area_monitoring_state ams ON ams.area_id = uas.SearchID
        LEFT JOIN dbo.user_area_subscription_state substate ON substate.user_id = uas.TelegramUserID AND substate.area_id = uas.SearchID
        WHERE uas.IsActive=1
          AND tu.IsActive=1
          AND COALESCE(substate.status, uas.SubscriptionStatus, 'active') IN ('active','preparing')
          AND COALESCE(ams.setup_status, 'not_started') <> 'inactive'
        ORDER BY uas.UserAreaID ASC
        """)
    return _rows_to_dicts(cur)


def mark_subscription_baseline_started(conn, user_area_id: int) -> None:
    conn.cursor().execute("UPDATE dbo.UserAreaSubscription SET BaselineStatus='running', BaselineStartedAt=COALESCE(BaselineStartedAt, SYSDATETIME()), BaselineCompletedAt=NULL, BaselineLastError=NULL, UpdatedAt=SYSDATETIME() WHERE SearchID=(SELECT SearchID FROM dbo.UserAreaSubscription WHERE UserAreaID=?) AND IsActive=1", int(user_area_id)); conn.commit()



def mark_search_baseline_completed(conn, search_id: int, listings_collected: int | None = None, new_count: int | None = None, pages_checked: int | None = None, total_pages_detected: int | None = None, stop_reason: str | None = None) -> None:
    if not hasattr(conn, "cursor"):
        return
    ensure_telegram_bot_tables(conn)
    conn.cursor().execute(
        """
        UPDATE dbo.UserAreaSubscription
        SET BaselineStatus='completed',
            BaselineCompletedAt=COALESCE(BaselineCompletedAt, SYSDATETIME()),
            LastLightCheckAt=SYSDATETIME(),
            BaselineListingsCollected=?,
            BaselineNewCount=?,
            BaselinePagesChecked=?,
            BaselineTotalPagesDetected=?,
            BaselineStopReason=?,
            BaselineLastError=NULL,
            DetailBaselineStatus=CASE WHEN DetailBaselineStatus IN ('completed','running','retry_wait') THEN DetailBaselineStatus ELSE 'pending' END,
            PriceBaselineStatus=CASE WHEN PriceBaselineStatus='completed' THEN PriceBaselineStatus ELSE 'pending' END,
            NotificationReadyAt=NULL,
            UpdatedAt=SYSDATETIME()
        WHERE SearchID=? AND IsActive=1
        """,
        listings_collected,
        new_count,
        pages_checked,
        total_pages_detected,
        stop_reason,
        int(search_id),
    )

def mark_subscription_baseline_completed(conn, user_area_id: int, listings_collected: int | None = None, new_count: int | None = None, pages_checked: int | None = None, total_pages_detected: int | None = None, stop_reason: str | None = None) -> None:
    conn.cursor().execute("""
        UPDATE dbo.UserAreaSubscription
        SET BaselineStatus='completed', BaselineCompletedAt=SYSDATETIME(), LastLightCheckAt=SYSDATETIME(),
            BaselineListingsCollected=?, BaselineNewCount=?, BaselinePagesChecked=?, BaselineTotalPagesDetected=?, BaselineStopReason=?, BaselineLastError=NULL,
            UpdatedAt=SYSDATETIME()
        WHERE SearchID=(SELECT SearchID FROM dbo.UserAreaSubscription WHERE UserAreaID=?) AND IsActive=1
        """, listings_collected, new_count, pages_checked, total_pages_detected, stop_reason, int(user_area_id))
    conn.commit()


def mark_subscription_baseline_failed(conn, user_area_id: int, error: str | None = None, listings_collected: int | None = None, new_count: int | None = None, pages_checked: int | None = None, total_pages_detected: int | None = None, stop_reason: str | None = None) -> None:
    conn.cursor().execute("""
        UPDATE dbo.UserAreaSubscription
        SET BaselineStatus='failed', BaselineListingsCollected=?, BaselineNewCount=?, BaselinePagesChecked=?,
            BaselineTotalPagesDetected=?, BaselineStopReason=?, BaselineLastError=?, UpdatedAt=SYSDATETIME()
        WHERE SearchID=(SELECT SearchID FROM dbo.UserAreaSubscription WHERE UserAreaID=?) AND IsActive=1
        """, listings_collected, new_count, pages_checked, total_pages_detected, stop_reason, config.mask_sensitive_text(error or ""), int(user_area_id))
    conn.commit()


def mark_subscription_light_checked(conn, user_area_id: int) -> None:
    conn.cursor().execute("UPDATE dbo.UserAreaSubscription SET LastLightCheckAt=SYSDATETIME(), UpdatedAt=SYSDATETIME() WHERE UserAreaID=?", int(user_area_id)); conn.commit()


def mark_subscription_detail_refreshed(conn, user_area_id: int) -> None:
    conn.cursor().execute("UPDATE dbo.UserAreaSubscription SET LastDetailRefreshAt=SYSDATETIME(), UpdatedAt=SYSDATETIME() WHERE UserAreaID=?", int(user_area_id)); conn.commit()



def get_user_area_baseline_summary(conn, user_area_id: int) -> dict:
    """Return persisted setup metrics plus a SearchID-scoped active-listing fallback count."""
    subscription = get_user_area_subscription(conn, user_area_id)
    if not subscription:
        return {}
    computed_listing_count = None
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT COUNT(1)
            FROM dbo.ListingSearchState lss
            JOIN dbo.Listing l ON l.listingID=lss.ListingID
            WHERE lss.SearchID=?
              AND LOWER(COALESCE(lss.ListingLifecycleStatus, lss.Status, 'active'))='active'
              AND (l.CurrentStatus IS NULL OR LOWER(l.CurrentStatus) IN ('active','unknown'))
            """, int(subscription["SearchID"]))
        row = cur.fetchone()
        computed_listing_count = int(row[0]) if row and row[0] is not None else None
    except Exception:
        # Setup summaries remain sendable from persisted metrics if the fallback query is unavailable.
        computed_listing_count = None
    try:
        detail_progress = get_detail_baseline_progress(conn, subscription)
    except Exception:
        detail_progress = {
            "detail_baseline_total_count": 0,
            "detail_baseline_completed_count": 0,
            "detail_baseline_remaining_count": 0,
            "notification_ready_at": subscription.get("NotificationReadyAt"),
        }
    return {
        "user_area_id": int(subscription["UserAreaID"]),
        "area_label": subscription.get("AreaLabel"),
        "chat_id": subscription.get("ChatID"),
        "search_id": int(subscription["SearchID"]),
        "baseline_listings_collected": subscription.get("BaselineListingsCollected"),
        "baseline_new_count": subscription.get("BaselineNewCount"),
        "baseline_pages_checked": subscription.get("BaselinePagesChecked"),
        "baseline_total_pages_detected": subscription.get("BaselineTotalPagesDetected"),
        "baseline_stop_reason": subscription.get("BaselineStopReason"),
        "computed_listing_count": computed_listing_count,
        **detail_progress,
    }

def mark_subscription_notifications_queued(conn, user_area_id: int) -> None:
    conn.cursor().execute("UPDATE dbo.UserAreaSubscription SET LastNotificationQueuedAt=SYSDATETIME(), UpdatedAt=SYSDATETIME() WHERE UserAreaID=?", int(user_area_id)); conn.commit()


def _empty_user_area_notification_result(user_area_id: int, skipped_reason: str, dry_run: bool) -> dict:
    return {
        "user_area_id": int(user_area_id),
        "events_input": 0,
        "events_considered": 0,
        "notifyable_count": 0,
        "queued_count": 0,
        "skipped_count": 0,
        "duplicates_count": 0,
        "skipped_reason": skipped_reason,
        "notifications": [],
        "errors": [],
        "dry_run": bool(dry_run),
    }


def queue_notifications_for_user_area(
    conn,
    user_area_id: int,
    since=None,
    dry_run: bool = False,
    limit: int = 100,
    since_event_id: int | None = None,
    include_already_queued: bool = False,
) -> dict:
    sub = get_user_area_subscription(conn, user_area_id)
    if not sub or not sub.get("IsActive"):
        return _empty_user_area_notification_result(user_area_id, "inactive_or_missing", dry_run)
    if sub.get("AreaSetupStatus") and str(sub.get("AreaSetupStatus") or "").lower() != "ready":
        return _empty_user_area_notification_result(user_area_id, "area_not_ready", dry_run)
    if sub.get("SubscriptionStatus") and (
        str(sub.get("SubscriptionStatus") or "").lower() != "active" or int(sub.get("SubscriptionNotifyEnabled") or 0) != 1
    ):
        return _empty_user_area_notification_result(user_area_id, "subscription_not_active", dry_run)
    if str(sub.get("BaselineStatus") or "").lower() != "completed" or not sub.get("BaselineCompletedAt"):
        return _empty_user_area_notification_result(user_area_id, "baseline_not_completed", dry_run)
    if str(sub.get("DetailBaselineStatus") or "").lower() != "completed" or not sub.get("NotificationReadyAt"):
        return _empty_user_area_notification_result(user_area_id, "notification_not_ready", dry_run)
    chat_id = clean_text(sub.get("ChatID"))
    telegram_user_id = sub.get("TelegramUserID")
    if not chat_id:
        return _empty_user_area_notification_result(user_area_id, "missing_chat_id", dry_run)
    if telegram_user_id is None:
        return _empty_user_area_notification_result(user_area_id, "missing_telegram_user_id", dry_run)
    events = get_notifyable_listing_events(
        conn,
        search_url=sub.get("SearchURL"),
        since_event_id=since_event_id,
        limit=limit,
        include_already_queued=include_already_queued,
        chat_id=chat_id,
        created_after=sub.get("NotificationReadyAt"),
        created_at_or_after=since or sub.get("NotificationStartAt"),
    )
    # SearchID/SearchURL identifies the monitored result set. Do not exact-match
    # listing addresses against the selected suburb because realestate.com.au may
    # return nearby suburbs for the same search area.
    result = build_notifications_for_events(conn, events, chat_id=chat_id, user_id=int(telegram_user_id), channel="telegram", dry_run=dry_run)
    result["user_area_id"] = int(user_area_id)
    result["telegram_user_id"] = int(telegram_user_id)
    result["chat_id"] = chat_id
    result["events_considered"] = len(events)
    if not dry_run and result.get("queued_count", 0) > 0:
        mark_subscription_notifications_queued(conn, user_area_id)
    return result


def queue_notifications_for_active_user_areas(
    conn,
    search_url: str | None = None,
    since_event_id: int | None = None,
    limit: int = 100,
    chat_id: str | None = None,
    dry_run: bool = False,
    include_already_queued: bool = False,
) -> dict:
    """Queue notifications through eligible subscriptions so recipient identity is always resolved."""
    subscriptions = get_active_user_area_subscriptions(conn)
    normalized_search_url = ensure_sort_list_date(search_url) if search_url else None
    resolved_chat_filter = clean_text(chat_id)
    selected = []
    for sub in subscriptions:
        if normalized_search_url and ensure_sort_list_date(sub.get("SearchURL") or "") != normalized_search_url:
            continue
        if resolved_chat_filter and clean_text(sub.get("ChatID")) != resolved_chat_filter:
            continue
        selected.append(sub)
    result = {
        "events_input": 0,
        "notifyable_count": 0,
        "queued_count": 0,
        "skipped_count": 0,
        "duplicates_count": 0,
        "dry_run": bool(dry_run),
        "notifications": [],
        "errors": [],
        "subscriptions_considered": len(selected),
        "subscription_results": [],
    }
    for sub in selected:
        user_area_id = int(sub["UserAreaID"])
        try:
            area_result = queue_notifications_for_user_area(
                conn,
                user_area_id,
                dry_run=dry_run,
                limit=limit,
                since_event_id=since_event_id,
                include_already_queued=include_already_queued,
            )
        except Exception as exc:
            result["errors"].append({"user_area_id": user_area_id, "error": config.mask_sensitive_text(exc)})
            continue
        result["subscription_results"].append(area_result)
        for key in ("events_input", "notifyable_count", "queued_count", "skipped_count", "duplicates_count"):
            result[key] += int(area_result.get(key, 0) or 0)
        result["notifications"].extend(area_result.get("notifications", []))
        result["errors"].extend(area_result.get("errors", []))
    return result


def mark_subscription_detail_baseline_started(conn, user_area_id: int) -> None:
    conn.cursor().execute("UPDATE dbo.UserAreaSubscription SET DetailBaselineStatus='running', DetailBaselineStartedAt=COALESCE(DetailBaselineStartedAt, SYSDATETIME()), UpdatedAt=SYSDATETIME() WHERE SearchID=(SELECT SearchID FROM dbo.UserAreaSubscription WHERE UserAreaID=?) AND IsActive=1", int(user_area_id)); conn.commit()


def mark_subscription_detail_baseline_completed(conn, user_area_id: int) -> None:
    conn.cursor().execute("UPDATE dbo.UserAreaSubscription SET DetailBaselineStatus='completed', DetailBaselineCompletedAt=SYSDATETIME(), NotificationReadyAt=NULL, DetailBaselineAttemptCount=0, DetailBaselineNextRetryAt=NULL, DetailBaselineLastError=NULL, LastDetailRefreshAt=SYSDATETIME(), UpdatedAt=SYSDATETIME() WHERE SearchID=(SELECT SearchID FROM dbo.UserAreaSubscription WHERE UserAreaID=?) AND IsActive=1", int(user_area_id)); conn.commit()


def mark_subscription_detail_baseline_failed(conn, user_area_id: int, error: str | None = None) -> None:
    conn.cursor().execute("UPDATE dbo.UserAreaSubscription SET DetailBaselineStatus='failed', NotificationReadyAt=NULL, DetailBaselineNextRetryAt=NULL, DetailBaselineLastError=?, UpdatedAt=SYSDATETIME() WHERE SearchID=(SELECT SearchID FROM dbo.UserAreaSubscription WHERE UserAreaID=?) AND IsActive=1", config.mask_sensitive_text(error or ""), int(user_area_id)); conn.commit()


def mark_subscription_detail_baseline_retry_wait(conn, user_area_id: int, error: str, next_retry_at, max_attempts: int) -> str:
    cur = conn.cursor()
    cur.execute("SELECT COALESCE(DetailBaselineAttemptCount, 0) FROM dbo.UserAreaSubscription WHERE UserAreaID=?", int(user_area_id))
    row = cur.fetchone()
    attempt_count = int(row[0] or 0) + 1 if row else 1
    status = "failed" if attempt_count >= int(max_attempts) else "retry_wait"
    cur.execute("""UPDATE dbo.UserAreaSubscription SET DetailBaselineStatus=?, DetailBaselineAttemptCount=?,
        DetailBaselineLastAttemptAt=SYSDATETIME(), DetailBaselineNextRetryAt=?, DetailBaselineLastError=?,
        NotificationReadyAt=NULL, ReadySummarySentAt=NULL, UpdatedAt=SYSDATETIME() WHERE UserAreaID=?""",
        status, attempt_count, None if status == "failed" else next_retry_at, config.mask_sensitive_text(error), int(user_area_id))
    conn.commit()
    return status


def mark_subscription_detail_baseline_retry_started(conn, user_area_id: int) -> None:
    conn.cursor().execute("UPDATE dbo.UserAreaSubscription SET DetailBaselineStatus='running', DetailBaselineNextRetryAt=NULL, UpdatedAt=SYSDATETIME() WHERE SearchID=(SELECT SearchID FROM dbo.UserAreaSubscription WHERE UserAreaID=?) AND IsActive=1", int(user_area_id)); conn.commit()


def mark_subscription_detail_baseline_batch_succeeded(conn, user_area_id: int) -> None:
    conn.cursor().execute("UPDATE dbo.UserAreaSubscription SET DetailBaselineStatus='running', DetailBaselineAttemptCount=0, DetailBaselineNextRetryAt=NULL, DetailBaselineLastError=NULL, UpdatedAt=SYSDATETIME() WHERE SearchID=(SELECT SearchID FROM dbo.UserAreaSubscription WHERE UserAreaID=?) AND IsActive=1", int(user_area_id)); conn.commit()


def reset_detail_baseline_status(conn, user_area_id: int, status: str = "pending", clear_attempts: bool = False) -> None:
    allowed = {"pending", "running", "retry_wait", "failed", "completed"}
    if status not in allowed:
        raise ValueError(f"Unsupported detail baseline status: {status}")
    clear_sql = ", DetailBaselineAttemptCount=0, DetailBaselineNextRetryAt=NULL, DetailBaselineLastError=NULL, DetailBaselineLastAttemptAt=NULL" if clear_attempts else ""
    conn.cursor().execute(f"UPDATE dbo.UserAreaSubscription SET DetailBaselineStatus=?{clear_sql}, UpdatedAt=SYSDATETIME() WHERE UserAreaID=?", status, int(user_area_id)); conn.commit()


def mark_subscription_setup_summary_sent(conn, user_area_id: int, column_name: str) -> None:
    allowed = {"BaselineSummarySentAt", "DetailBaselineStartedSummarySentAt", "ReadySummarySentAt"}
    if column_name not in allowed:
        raise ValueError(f"Unsupported setup summary column: {column_name}")
    conn.cursor().execute(f"UPDATE dbo.UserAreaSubscription SET {column_name}=COALESCE({column_name}, SYSDATETIME()), UpdatedAt=SYSDATETIME() WHERE UserAreaID=?", int(user_area_id))
    conn.commit()


def get_detail_baseline_progress(conn, subscription: dict) -> dict:
    search_id = int(subscription["SearchID"])
    started_at = subscription.get("DetailBaselineStartedAt")
    cur = conn.cursor()
    cur.execute("""
        SELECT
            COUNT(1) AS TotalCount,
            SUM(CASE
                    WHEN COALESCE(lss.SetupDetailStatus, '') IN ('detail_complete','detail_partial_complete','detail_failed_permanent') THEN 1
                    WHEN ? IS NOT NULL AND lss.LastDetailRefreshAt >= ? THEN 1
                    WHEN ? IS NOT NULL AND latest.LastSnapshotDate >= ? THEN 1
                    ELSE 0
                END) AS CompletedCount,
            SUM(CASE WHEN COALESCE(lss.SetupDetailStatus, '')='detail_partial_complete' THEN 1 ELSE 0 END) AS PartialCount,
            SUM(CASE WHEN COALESCE(lss.SetupDetailStatus, '')='detail_failed_permanent' THEN 1 ELSE 0 END) AS PermanentFailedCount,
            SUM(CASE WHEN COALESCE(lss.SetupDetailStatus, '')='detail_retry_wait' THEN 1 ELSE 0 END) AS RetryWaitCount
        FROM dbo.ListingSearchState lss
        JOIN dbo.Listing l ON l.listingID=lss.ListingID
        OUTER APPLY (SELECT MAX(ls.SnapshotDate) LastSnapshotDate FROM dbo.ListingSnapshot ls WHERE ls.ListingID=l.listingID AND ls.SearchID=lss.SearchID) latest
        WHERE lss.SearchID=?
          AND LOWER(COALESCE(lss.ListingLifecycleStatus, lss.Status, 'active'))='active'
          AND (l.CurrentStatus IS NULL OR LOWER(l.CurrentStatus) IN ('active','unknown'))
          AND NULLIF(LTRIM(RTRIM(COALESCE(l.ListingURL, ''))), '') IS NOT NULL
        """, started_at, started_at, started_at, started_at, search_id)
    row = cur.fetchone()
    total = int(row[0] or 0) if row else 0
    completed = int(row[1] or 0) if row else 0
    partial = int(row[2] or 0) if row and len(row) > 2 else 0
    permanent_failed = int(row[3] or 0) if row and len(row) > 3 else 0
    retry_wait = int(row[4] or 0) if row and len(row) > 4 else 0
    baseline_total = to_int(subscription.get("BaselineListingsCollected")) or 0
    total = max(total, baseline_total)
    completed = min(completed, total)
    return {
        "detail_baseline_total_count": total,
        "detail_baseline_completed_count": completed,
        "detail_baseline_remaining_count": max(0, total - completed),
        "detail_baseline_partial_count": partial,
        "detail_baseline_permanent_failed_count": permanent_failed,
        "detail_baseline_retry_wait_count": retry_wait,
        "notification_ready_at": subscription.get("NotificationReadyAt"),
    }



def count_succeeded_setup_detail_jobs(conn, search_id: int, started_at=None) -> int:
    try:
        row = _one(conn.cursor(), "SELECT OBJECT_ID('dbo.Job')")
        if not row or row[0] is None:
            return 0
    except Exception:
        return 0
    cur = conn.cursor()
    if started_at is not None:
        cur.execute(
            """
            SELECT COUNT(1)
            FROM dbo.Job
            WHERE SearchID=?
              AND JobType='setup_detail_baseline'
              AND Status='succeeded'
              AND COALESCE(FinishedAt, StartedAt, CreatedAt) >= ?
            """,
            int(search_id),
            started_at,
        )
    else:
        cur.execute(
            """
            SELECT COUNT(1)
            FROM dbo.Job
            WHERE SearchID=? AND JobType='setup_detail_baseline' AND Status='succeeded'
            """,
            int(search_id),
        )
    row = cur.fetchone()
    return int(row[0] or 0) if row else 0


def get_latest_setup_detail_job(conn, search_id: int, started_at=None) -> dict | None:
    try:
        row = _one(conn.cursor(), "SELECT OBJECT_ID('dbo.Job')")
        if not row or row[0] is None:
            return None
    except Exception:
        return None
    cur = conn.cursor()
    if started_at is not None:
        cur.execute(
            """
            SELECT TOP (1) JobID, JobType, Status, CreatedAt, StartedAt, FinishedAt, LastError, PayloadJson, DedupeKey
            FROM dbo.Job
            WHERE SearchID=?
              AND JobType='setup_detail_baseline'
              AND COALESCE(FinishedAt, StartedAt, CreatedAt) >= ?
            ORDER BY JobID DESC
            """,
            int(search_id),
            started_at,
        )
    else:
        cur.execute(
            """
            SELECT TOP (1) JobID, JobType, Status, CreatedAt, StartedAt, FinishedAt, LastError, PayloadJson, DedupeKey
            FROM dbo.Job
            WHERE SearchID=? AND JobType='setup_detail_baseline'
            ORDER BY JobID DESC
            """,
            int(search_id),
        )
    cols = [col[0] for col in cur.description]
    rows = [{cols[i]: row[i] for i in range(len(cols))} for row in cur.fetchall()]
    return rows[0] if rows else None

def count_remaining_setup_detail_targets(conn, search_id: int, subscription: dict | None = None) -> int:
    if subscription is None:
        try:
            subs = get_active_user_area_subscriptions_for_search(conn, int(search_id))
            subscription = subs[0] if subs else {"SearchID": int(search_id)}
        except Exception:
            subscription = {"SearchID": int(search_id)}
    else:
        subscription = {**subscription, "SearchID": int(search_id)}
    return int(get_detail_baseline_progress(conn, subscription).get("detail_baseline_remaining_count") or 0)


def ensure_listing_search_state_price_inference_columns(conn) -> None:
    ensure_monitoring_state_tables(conn)
    ensure_listing_lifecycle_columns(conn)
    for column_name, column_definition in {
        "InferredPriceLow": "DECIMAL(18,2) NULL",
        "InferredPriceHigh": "DECIMAL(18,2) NULL",
        "InferredPriceMethod": "NVARCHAR(100) NULL",
        "InferredPriceSource": "NVARCHAR(100) NULL",
        "LastPriceInferenceAt": "DATETIME2 NULL",
        "PriceInferenceStatus": "NVARCHAR(64) NULL",
        "PriceInferenceLastError": "NVARCHAR(MAX) NULL",
    }.items():
        _execute_ddl_safely(conn, f"""
            IF OBJECT_ID('dbo.ListingSearchState') IS NOT NULL
            AND COL_LENGTH('dbo.ListingSearchState', '{column_name}') IS NULL
            ALTER TABLE dbo.ListingSearchState ADD {column_name} {column_definition}
            """, description=f"add dbo.ListingSearchState.{column_name}", required=False)
    _execute_ddl_safely(conn, """
        IF OBJECT_ID('dbo.ListingSearchState') IS NOT NULL
        AND COL_LENGTH('dbo.ListingSearchState', 'PriceInferenceStatus') IS NOT NULL
        AND EXISTS (
            SELECT 1 FROM sys.columns
            WHERE object_id=OBJECT_ID('dbo.ListingSearchState')
              AND name='PriceInferenceStatus'
              AND max_length < 128
        )
        ALTER TABLE dbo.ListingSearchState ALTER COLUMN PriceInferenceStatus NVARCHAR(64) NULL
        """, description="widen dbo.ListingSearchState.PriceInferenceStatus", required=False)
    _execute_ddl_safely(conn, """
        IF OBJECT_ID('dbo.ListingSearchState') IS NOT NULL
        AND COL_LENGTH('dbo.ListingSearchState', 'PriceInferenceLastError') IS NOT NULL
        AND EXISTS (
            SELECT 1 FROM sys.columns
            WHERE object_id=OBJECT_ID('dbo.ListingSearchState')
              AND name='PriceInferenceLastError'
              AND max_length <> -1
              AND max_length < 2000
        )
        ALTER TABLE dbo.ListingSearchState ALTER COLUMN PriceInferenceLastError NVARCHAR(1000) NULL
        """, description="widen dbo.ListingSearchState.PriceInferenceLastError", required=False)
    _execute_ddl_safely(conn, """
        IF OBJECT_ID('dbo.ListingSearchState') IS NOT NULL
        AND COL_LENGTH('dbo.ListingSearchState', 'LastPriceInferenceAt') IS NOT NULL
        AND NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_ListingSearchState_PriceInferenceDue' AND object_id=OBJECT_ID('dbo.ListingSearchState'))
        CREATE INDEX IX_ListingSearchState_PriceInferenceDue
        ON dbo.ListingSearchState(SearchID, Status, LastPriceInferenceAt, ListingID)
        """, description="create IX_ListingSearchState_PriceInferenceDue", required=False)


def _active_price_inference_where() -> str:
    return """
      AND LOWER(COALESCE(lss.ListingLifecycleStatus, lss.Status, 'active')) = 'active'
      AND (l.CurrentStatus IS NULL OR LOWER(l.CurrentStatus) IN ('active','unknown'))
      AND NULLIF(LTRIM(RTRIM(COALESCE(l.ListingURL, ''))), '') IS NOT NULL
    """


def get_active_listings_for_price_inference(conn, search_id: int, limit: int | None = 10, only_due: bool = False, interval_seconds: int | None = None, listing_external_ids: list[str] | None = None, before_time=None) -> list[dict]:
    ensure_listing_search_state_price_inference_columns(conn)
    top_sql = "" if limit is None else "TOP (?)"
    interval = int(interval_seconds or 0)
    params = [] if limit is None else [max(1, int(limit or 1))]
    params.append(int(search_id))
    due_sql = ""
    if before_time is not None:
        due_sql = "AND (lss.LastPriceInferenceAt IS NULL OR lss.LastPriceInferenceAt < ?)"
        params.append(before_time)
    elif only_due:
        due_sql = "AND (lss.LastPriceInferenceAt IS NULL OR DATEADD(second, ?, lss.LastPriceInferenceAt) <= SYSDATETIME())"
        params.append(interval)
    ids = [str(value).strip() for value in (listing_external_ids or []) if str(value).strip()]
    id_sql = ""
    if ids:
        id_sql = "AND CAST(l.ExternalID AS NVARCHAR(50)) IN (" + ",".join("?" for _ in ids) + ")"
        params.extend(ids)
    cur = conn.cursor()
    cur.execute(f"""
    SELECT {top_sql}
        lss.SearchID AS search_id,
        l.listingID AS db_listing_id,
        CAST(l.ExternalID AS NVARCHAR(50)) AS listing_id,
        CAST(l.ExternalID AS NVARCHAR(50)) AS external_id,
        l.ListingURL AS url,
        COALESCE(p.Address, p.AddressRaw, p.AddressNormalized) AS address,
        l.CurrentPriceDisplay AS price_display,
        l.Price AS displayed_price_value,
        lss.InferredPriceLow AS inferred_price_low,
        lss.InferredPriceHigh AS inferred_price_high,
        lss.InferredPriceMethod AS inferred_price_method,
        lss.InferredPriceSource AS inferred_price_source,
        lss.LastPriceInferenceAt AS last_price_inference_at,
        lss.PriceInferenceStatus AS price_inference_status
    FROM dbo.ListingSearchState lss
    JOIN dbo.Listing l ON l.listingID = lss.ListingID
    LEFT JOIN dbo.Property p ON p.PropertyID = l.PropertyID
    WHERE lss.SearchID=?
    {_active_price_inference_where()}
    {due_sql}
    {id_sql}
    ORDER BY CASE WHEN lss.LastPriceInferenceAt IS NULL THEN 0 ELSE 1 END ASC,
             lss.LastPriceInferenceAt ASC,
             l.listingID ASC
    """, *params)
    cols = [c[0] for c in cur.description]
    return [{cols[i]: row[i] for i in range(len(cols))} for row in cur.fetchall()]


def get_price_inference_history_summary(conn, search_id: int, sample_size: int = 10) -> dict:
    ensure_listing_search_state_price_inference_columns(conn)
    cur = conn.cursor()
    cur.execute(f"""
        SELECT COUNT(1)
        FROM dbo.ListingSearchState lss
        JOIN dbo.Listing l ON l.listingID=lss.ListingID
        WHERE lss.SearchID=?
        {_active_price_inference_where()}
          AND LOWER(COALESCE(lss.PriceInferenceStatus, ''))='completed'
          AND lss.InferredPriceLow IS NOT NULL
          AND lss.InferredPriceHigh IS NOT NULL
        """, int(search_id))
    row = cur.fetchone()
    completed_count = int(row[0] or 0) if row else 0
    safe_sample = max(1, int(sample_size or 10))
    cur.execute(f"""
        SELECT MIN(x.InferredPriceLow)
        FROM (
            SELECT TOP ({safe_sample}) lss.InferredPriceLow
            FROM dbo.ListingSearchState lss
            JOIN dbo.Listing l ON l.listingID=lss.ListingID
            WHERE lss.SearchID=?
            {_active_price_inference_where()}
              AND LOWER(COALESCE(lss.PriceInferenceStatus, ''))='completed'
              AND lss.InferredPriceLow IS NOT NULL
              AND lss.InferredPriceHigh IS NOT NULL
            ORDER BY lss.InferredPriceLow ASC
        ) x
        """, int(search_id))
    low_row = cur.fetchone()
    cur.execute(f"""
        SELECT MAX(x.InferredPriceHigh)
        FROM (
            SELECT TOP ({safe_sample}) lss.InferredPriceHigh
            FROM dbo.ListingSearchState lss
            JOIN dbo.Listing l ON l.listingID=lss.ListingID
            WHERE lss.SearchID=?
            {_active_price_inference_where()}
              AND LOWER(COALESCE(lss.PriceInferenceStatus, ''))='completed'
              AND lss.InferredPriceLow IS NOT NULL
              AND lss.InferredPriceHigh IS NOT NULL
            ORDER BY lss.InferredPriceHigh DESC
        ) x
        """, int(search_id))
    high_row = cur.fetchone()
    return {
        "completed_count": completed_count,
        "low_anchor": low_row[0] if low_row else None,
        "high_anchor": high_row[0] if high_row else None,
        "sample_size": safe_sample,
    }


def get_price_baseline_progress(conn, subscription: dict) -> dict:
    ensure_listing_search_state_price_inference_columns(conn)
    search_id = int(subscription["SearchID"])
    cur = conn.cursor()
    cur.execute(f"""
        SELECT
            COUNT(1) AS TotalCount,
            SUM(CASE WHEN lss.LastPriceInferenceAt IS NOT NULL OR LOWER(COALESCE(lss.PriceInferenceStatus, '')) IN ('completed','skipped','failed','unknown_pending_retry') THEN 1 ELSE 0 END) AS CompletedCount
        FROM dbo.ListingSearchState lss
        JOIN dbo.Listing l ON l.listingID=lss.ListingID
        WHERE lss.SearchID=?
        {_active_price_inference_where()}
        """, search_id)
    row = cur.fetchone()
    total = int(row[0] or 0) if row else 0
    completed = int(row[1] or 0) if row else 0
    return {
        "price_baseline_total_count": total,
        "price_baseline_completed_count": completed,
        "price_baseline_remaining_count": max(0, total - completed),
        "notification_ready_at": subscription.get("NotificationReadyAt"),
    }


def _price_range_materially_changed(old_low, old_high, new_low, new_high) -> bool:
    old_vals = [value for value in (old_low, old_high) if value is not None]
    new_vals = [value for value in (new_low, new_high) if value is not None]
    if not old_vals or not new_vals:
        return False
    try:
        old_mid = sum(Decimal(str(v)) for v in old_vals) / Decimal(len(old_vals))
        new_mid = sum(Decimal(str(v)) for v in new_vals) / Decimal(len(new_vals))
    except Exception:
        return False
    diff = abs(new_mid - old_mid)
    return diff >= Decimal("50000") or (old_mid > 0 and diff / old_mid >= Decimal("0.05"))


def update_listing_price_inference(conn, search_id: int, listing_id: int, low: Any = None, high: Any = None, method: str | None = None, source: str = "module2", status: str = "completed", error: str | None = None, create_event: bool = False) -> None:
    ensure_listing_search_state_price_inference_columns(conn)
    cur = conn.cursor()
    old = _one(cur, "SELECT InferredPriceLow, InferredPriceHigh FROM dbo.ListingSearchState WHERE SearchID=? AND ListingID=?", int(search_id), int(listing_id))
    old_low, old_high = (old[0], old[1]) if old else (None, None)
    cur.execute("""
        UPDATE dbo.ListingSearchState
        SET InferredPriceLow=?, InferredPriceHigh=?, InferredPriceMethod=?, InferredPriceSource=?,
            LastPriceInferenceAt=SYSDATETIME(), PriceInferenceStatus=?, PriceInferenceLastError=?, UpdatedAt=SYSDATETIME()
        WHERE SearchID=? AND ListingID=?
        """, low, high, method, source, status, config.mask_sensitive_text(error or "") if error else None, int(search_id), int(listing_id))
    ext_row = _one(cur, "SELECT CAST(ExternalID AS NVARCHAR(100)) FROM dbo.Listing WHERE listingID=?", int(listing_id))
    if ext_row and ext_row[0] is not None:
        next_retry_at = datetime.now() + timedelta(seconds=int(getattr(config, "PRICE_UNKNOWN_RETRY_INTERVAL_SECONDS", 3600))) if status in {"unknown_pending_retry", "technical_failed"} else None
        upsert_price_inference_state(
            conn,
            str(ext_row[0]),
            int(search_id),
            status if status in {"completed", "unknown_pending_retry", "technical_failed", "skipped_direct_price"} else ("technical_failed" if "failed" in str(status).lower() else "completed"),
            last_error=error,
            next_retry_at=next_retry_at,
            inferred_low=low,
            inferred_high=high,
            method=method,
            increment_attempts=status != "skipped_direct_price",
        )
    if create_event and status == "completed" and _price_range_materially_changed(old_low, old_high, low, high):
        run_id = create_lightweight_scrape_run(conn, int(search_id), source="module2_price_refresh", run_type="change_detection")
        payload = {
            "event_type": "inferred_price_range_changed",
            "field": "inferred_price_range",
            "old_value": {"inferred_price_low": old_low, "inferred_price_high": old_high},
            "new_value": {"inferred_price_low": low, "inferred_price_high": high},
            "inferred_price_method": method,
            "inferred_price_source": source,
            "should_notify": True,
            "reason": "module2_price_refresh",
            "severity": "low",
        }
        eh = _sha(json_dumps_safe({"search_id": int(search_id), "listing_id": int(listing_id), **payload}, sort_keys=True))
        create_listing_event_if_new(conn, int(listing_id), "inferred_price_range_changed", payload, event_hash=eh, search_id=int(search_id), run_id=run_id, suppress_notifications=False)


def mark_price_inference_skipped(conn, search_id: int, listing_id: int, reason: str | None = None, status: str = "skipped_no_range_after_full_sweep") -> None:
    update_listing_price_inference(conn, search_id, listing_id, None, None, "sliding_between_window", "module2", status, reason or status, create_event=False)


def mark_price_inference_unknown_pending_retry(conn, search_id: int, listing_id: int, reason: str | None = None) -> None:
    update_listing_price_inference(
        conn,
        search_id,
        listing_id,
        None,
        None,
        "sliding_between_window",
        "module2",
        "unknown_pending_retry",
        reason or "price_not_inferred_after_sweep",
        create_event=False,
    )


def mark_price_inference_technical_failed(conn, search_id: int, listing_id: int, reason: str | None = None) -> None:
    update_listing_price_inference(
        conn,
        search_id,
        listing_id,
        None,
        None,
        "sliding_between_window",
        "module2",
        "technical_failed",
        reason or "technical_failed",
        create_event=False,
    )


def mark_subscription_price_baseline_started(conn, user_area_id: int) -> None:
    ensure_telegram_bot_tables(conn)
    conn.cursor().execute("UPDATE dbo.UserAreaSubscription SET PriceBaselineStatus='running', PriceBaselineStartedAt=COALESCE(PriceBaselineStartedAt, SYSDATETIME()), NotificationReadyAt=NULL, UpdatedAt=SYSDATETIME() WHERE SearchID=(SELECT SearchID FROM dbo.UserAreaSubscription WHERE UserAreaID=?) AND IsActive=1", int(user_area_id)); conn.commit()


def mark_subscription_price_baseline_completed(conn, user_area_id: int) -> None:
    ensure_telegram_bot_tables(conn)
    conn.cursor().execute("UPDATE dbo.UserAreaSubscription SET PriceBaselineStatus='completed', PriceBaselineCompletedAt=SYSDATETIME(), PriceBaselineLastError=NULL, NotificationReadyAt=COALESCE(NotificationReadyAt, SYSDATETIME()), LastPriceRefreshAt=SYSDATETIME(), UpdatedAt=SYSDATETIME() WHERE SearchID=(SELECT SearchID FROM dbo.UserAreaSubscription WHERE UserAreaID=?) AND IsActive=1", int(user_area_id)); conn.commit()


def mark_subscription_price_baseline_failed(conn, user_area_id: int, error: str | None = None) -> None:
    ensure_telegram_bot_tables(conn)
    conn.cursor().execute("UPDATE dbo.UserAreaSubscription SET PriceBaselineStatus='failed', NotificationReadyAt=NULL, PriceBaselineLastError=?, UpdatedAt=SYSDATETIME() WHERE SearchID=(SELECT SearchID FROM dbo.UserAreaSubscription WHERE UserAreaID=?) AND IsActive=1", config.mask_sensitive_text(error or ""), int(user_area_id)); conn.commit()


def mark_search_price_refreshed(conn, search_id: int) -> None:
    ensure_telegram_bot_tables(conn)
    conn.cursor().execute("UPDATE dbo.UserAreaSubscription SET LastPriceRefreshAt=SYSDATETIME(), UpdatedAt=SYSDATETIME() WHERE SearchID=? AND IsActive=1", int(search_id)); conn.commit()


def mark_search_full_listing_swept(conn, search_id: int) -> None:
    ensure_telegram_bot_tables(conn)
    conn.cursor().execute("UPDATE dbo.UserAreaSubscription SET LastFullListingSweepAt=SYSDATETIME(), UpdatedAt=SYSDATETIME() WHERE SearchID=? AND IsActive=1", int(search_id)); conn.commit()

# Phase 2B SearchID-scoped monitoring helpers

def get_active_user_area_subscriptions_for_search(conn, search_id: int) -> list[dict]:
    ensure_telegram_bot_tables(conn)
    cur = conn.cursor()
    cur.execute("""
        SELECT uas.UserAreaID, uas.TelegramUserID, tu.ChatID, uas.SearchID, uas.SearchURL, uas.AreaLabel,
               uas.Suburb, uas.StateCode, uas.Postcode, uas.IsActive, uas.BaselineStatus,
               uas.BaselineStartedAt, uas.BaselineCompletedAt, uas.NotificationStartAt,
               uas.DetailBaselineStatus, uas.DetailBaselineStartedAt, uas.DetailBaselineCompletedAt,
               uas.DetailBaselineAttemptCount, uas.DetailBaselineLastAttemptAt, uas.DetailBaselineNextRetryAt, uas.DetailBaselineLastError, uas.NotificationReadyAt,
               uas.BaselineSummarySentAt, uas.DetailBaselineStartedSummarySentAt, uas.ReadySummarySentAt,
               uas.BaselineListingsCollected, uas.BaselineNewCount, uas.BaselinePagesChecked, uas.BaselineTotalPagesDetected, uas.BaselineStopReason, uas.BaselineLastError,
               uas.LastLightCheckAt, uas.LastDetailRefreshAt, uas.LastPriceRefreshAt, uas.LastFullListingSweepAt, uas.LastNotificationQueuedAt,
               uas.PriceBaselineStatus, uas.PriceBaselineStartedAt, uas.PriceBaselineCompletedAt, uas.PriceBaselineLastError,
               uas.SubscriptionStatus AS UserAreaSubscriptionStatus, uas.NotifyEnabled AS UserAreaNotifyEnabled, uas.RemovedAt,
               ams.setup_status AS AreaSetupStatus, ams.module1_status AS AreaModule1Status, ams.module3_status AS AreaModule3Status, ams.module2_status AS AreaModule2Status, ams.active_listing_count AS AreaActiveListingCount, ams.last_error AS AreaLastError, ams.ready_at AS AreaReadyAt,
               (SELECT COUNT(1) FROM dbo.ListingSearchState lss WHERE lss.SearchID=uas.SearchID AND LOWER(COALESCE(lss.ListingLifecycleStatus, lss.Status, 'active'))='active') AS LiveActiveListingCount,
               substate.status AS SubscriptionStatus, substate.notify_enabled AS SubscriptionNotifyEnabled,
               uas.CreatedAt, uas.UpdatedAt
        FROM dbo.UserAreaSubscription uas
        JOIN dbo.TelegramUser tu ON tu.TelegramUserID = uas.TelegramUserID
        LEFT JOIN dbo.area_monitoring_state ams ON ams.area_id = uas.SearchID
        LEFT JOIN dbo.user_area_subscription_state substate ON substate.user_id = uas.TelegramUserID AND substate.area_id = uas.SearchID
        WHERE uas.IsActive=1
          AND tu.IsActive=1
          AND uas.SearchID=?
          AND COALESCE(substate.status, uas.SubscriptionStatus, 'active') IN ('active','preparing')
          AND COALESCE(ams.setup_status, 'not_started') <> 'inactive'
        ORDER BY uas.UserAreaID ASC
        """, int(search_id))
    return _rows_to_dicts(cur)


def mark_search_light_checked(conn, search_id: int) -> None:
    ensure_telegram_bot_tables(conn)
    conn.cursor().execute("UPDATE dbo.UserAreaSubscription SET LastLightCheckAt=SYSDATETIME(), UpdatedAt=SYSDATETIME() WHERE SearchID=? AND IsActive=1", int(search_id))
    conn.commit()


def mark_search_detail_refreshed(conn, search_id: int) -> None:
    ensure_telegram_bot_tables(conn)
    conn.cursor().execute("UPDATE dbo.UserAreaSubscription SET LastDetailRefreshAt=SYSDATETIME(), UpdatedAt=SYSDATETIME() WHERE SearchID=? AND IsActive=1", int(search_id))
    conn.commit()
