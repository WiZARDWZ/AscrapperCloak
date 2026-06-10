import json
import os
import platform
import re
import shutil
from importlib.util import find_spec

if find_spec("dotenv") is not None:
    from dotenv import load_dotenv

    load_dotenv()

# config.py
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
AREA_SEARCH_URL = "https://www.realestate.com.au/buy/in-petersham,+nsw+2049/list-1?activeSort=list-date"


def _env_first(*names: str, default: str = "") -> str:
    for name in names:
        value = os.getenv(name)
        if value is not None:
            return value
    return default


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    text = value.strip().lower()
    if text in {"", "0", "false", "no", "n", "off"}:
        return False
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    return default


def _optional_int_env(name: str, default: int | None = None) -> int | None:
    value = os.getenv(name)
    if value is None or str(value).strip() == "":
        return default
    return int(value)


def _optional_str_env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _json_object_env(name: str) -> dict | None:
    value = os.getenv(name)
    if value is None or not str(value).strip():
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name} must be valid JSON object: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{name} must be a JSON object")
    return parsed


def _viewport_env(name: str, default_width: int, default_height: int) -> tuple[int, int]:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default_width, default_height
    match = re.match(r"^\s*(\d+)\s*[x,]\s*(\d+)\s*$", raw)
    if not match:
        raise ValueError(f"{name} must be WIDTHxHEIGHT, e.g. 1365x768")
    return int(match.group(1)), int(match.group(2))


def _windows_default_chrome_path() -> str:
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return candidates[0] if os.name == "nt" else ""


# Core paths/settings
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
DB_PATH = os.getenv("DB_PATH", "realestate.db")
# SQL Server connection settings
DB_DRIVER = _env_first("DB_DRIVER", "SQLSERVER_DRIVER", default="ODBC Driver 18 for SQL Server")
DB_HOST = _env_first("DB_HOST", "SQLSERVER_HOST", "SQLSERVER_SERVER", default="localhost")
DB_PORT = _env_first("DB_PORT", "SQLSERVER_PORT", default="")
DB_NAME = _env_first("DB_NAME", "SQLSERVER_DATABASE", default="AScrapper")
DB_USER = _env_first("DB_USER", "SQLSERVER_USERNAME", default="")
DB_PASSWORD = _env_first("DB_PASSWORD", "SQLSERVER_PASSWORD", default="")
DB_ENCRYPT = _env_first("DB_ENCRYPT", "SQLSERVER_ENCRYPT", default="yes")
DB_TRUST_SERVER_CERTIFICATE = _env_first("DB_TRUST_SERVER_CERTIFICATE", "SQLSERVER_TRUST_SERVER_CERTIFICATE", default="yes")
DB_TIMEOUT = int(_env_first("DB_TIMEOUT", "SQLSERVER_TIMEOUT", default="30"))
DB_TRUSTED_CONNECTION = _env_first(
    "DB_TRUSTED_CONNECTION",
    "SQLSERVER_TRUSTED_CONNECTION",
    default="yes" if os.name == "nt" and not DB_USER else "no",
)

# Backward-compatible names used by the current codebase.
SQLSERVER_DRIVER = DB_DRIVER
SQLSERVER_SERVER = DB_HOST
SQLSERVER_DATABASE = DB_NAME
SQLSERVER_USERNAME = DB_USER
SQLSERVER_PASSWORD = DB_PASSWORD
SQLSERVER_TRUSTED_CONNECTION = DB_TRUSTED_CONNECTION
SQLSERVER_ENCRYPT = DB_ENCRYPT
SQLSERVER_TRUST_SERVER_CERTIFICATE = DB_TRUST_SERVER_CERTIFICATE
AUTO_APPROVE_TELEGRAM_USERS = os.getenv("AUTO_APPROVE_TELEGRAM_USERS", "true").lower() == "true"
OUTPUT_DIR = os.getenv("OUTPUT_DIR", "output")
LOG_DIR = os.getenv("LOG_DIR", "logs")
RUNTIME_DIR = os.getenv("RUNTIME_DIR", "runtime")
EXCEL_EXPORT_MODE = os.getenv("EXCEL_EXPORT_MODE", "normal").lower()
if EXCEL_EXPORT_MODE not in {"normal", "debug"}:
    EXCEL_EXPORT_MODE = "normal"

# User/watchlist limits
MAX_AREAS_PER_USER = 3
DEFAULT_POLL_INTERVAL_MINUTES = 60

# Pipeline controls
MAX_PAGES_MODULE1 = None
PIPELINE_TIMEOUT = 25


def get_profile_settings(profile: str) -> dict:
    normalized = (profile or "normal").strip().lower()
    if normalized not in {"normal"}:
        normalized = "normal"

    # TODO: Low Mode is intentionally disabled for now. Re-enable in a later performance profile refactor.
    if False and normalized == "low":
        return {
            "LIGHT_CHECK_INTERVAL_SECONDS": 600,
            "LIGHT_CHECK_PAGES": 1,
            "WORKER_TICK_SECONDS": 20,
            "MAX_CONCURRENT_FULL_RUNS": 1,
            "MAX_CONCURRENT_ENRICH": 1,
            "CHROME_WINDOW_SIZE": "1365,768",
            "IMAGES_DISABLED": True,
            "EXTRA_CHROME_LIGHT_FLAGS": True,
            # Optional tighter cap for weak machines.
            "MAX_PAGES_MODULE1": 2,
            "USE_XVFB": False,
        }

    return {
        "LIGHT_CHECK_INTERVAL_SECONDS": 180,
        "LIGHT_CHECK_PAGES": 2,
        "WORKER_TICK_SECONDS": 10,
        "MAX_CONCURRENT_FULL_RUNS": 1,
        "MAX_CONCURRENT_ENRICH": 1,
        "CHROME_WINDOW_SIZE": "1920,1080",
        "IMAGES_DISABLED": True,
        "EXTRA_CHROME_LIGHT_FLAGS": False,
        "MAX_PAGES_MODULE1": None,
        # for Linux runs with visible browser (HEADLESS=0), Xvfb should be used by runner/deploy scripts
        "USE_XVFB": (os.name != "nt") and (os.getenv("HEADLESS", "0") == "0"),
    }


PERF_PROFILE = os.getenv("PERF_PROFILE", "normal").lower()
if PERF_PROFILE not in {"normal"}:
    PERF_PROFILE = "normal"

PROFILE_SETTINGS = get_profile_settings(PERF_PROFILE)

MODULE2_WINDOW_WIDTH = 200_000
MODULE2_STEP = 50_000
MODULE2_MIN_LOW = int(os.getenv("MODULE2_MIN_LOW", "0"))
MODULE2_MAX_HIGH = int(os.getenv("MODULE2_MAX_HIGH", "20000000"))
MODULE2_MAX_PAGES_PER_WINDOW = 5
MODULE2_ROTATE_PROFILE_ON_429 = True
MODULE2_MAX_PROFILE_ROTATIONS_PER_RUN = 2
MODULE2_COOLDOWN_ON_429_SECONDS = 60
MODULE2_PROFILE_BASE_DIR = os.getenv("MODULE2_PROFILE_BASE_DIR", "").strip() or None
MODULE2_PROFILE_BACKUP_PREFIX = "rea_profile_429_backup_"
MODULE1_INTER_PAGE_DELAY_SECONDS = float(os.getenv("MODULE1_INTER_PAGE_DELAY_SECONDS", "8"))
MODULE1_INTER_PAGE_DELAY_JITTER_SECONDS = float(os.getenv("MODULE1_INTER_PAGE_DELAY_JITTER_SECONDS", "4"))
MODULE1_CHROME_ERROR_RETRY_DELAY_SECONDS = float(os.getenv("MODULE1_CHROME_ERROR_RETRY_DELAY_SECONDS", "3"))
MODULE1_CHROME_ERROR_NAV_RESET = _bool_env("MODULE1_CHROME_ERROR_NAV_RESET", True)
MODULE2_INTER_PAGE_DELAY_SECONDS = float(os.getenv("MODULE2_INTER_PAGE_DELAY_SECONDS", "8"))
MODULE2_INTER_PAGE_DELAY_JITTER_SECONDS = float(os.getenv("MODULE2_INTER_PAGE_DELAY_JITTER_SECONDS", "4"))
MODULE2_INTER_WINDOW_DELAY_SECONDS = float(os.getenv("MODULE2_INTER_WINDOW_DELAY_SECONDS", "10"))
MODULE2_INTER_WINDOW_DELAY_JITTER_SECONDS = float(os.getenv("MODULE2_INTER_WINDOW_DELAY_JITTER_SECONDS", "5"))
MODULE2_CHROME_ERROR_RETRY_DELAY_SECONDS = float(os.getenv("MODULE2_CHROME_ERROR_RETRY_DELAY_SECONDS", "3"))
MODULE2_CHROME_ERROR_NAV_RESET = _bool_env("MODULE2_CHROME_ERROR_NAV_RESET", True)
MODULE2_SLEEP_BETWEEN_WINDOWS_MIN = float(os.getenv("MODULE2_SLEEP_BETWEEN_WINDOWS_MIN", str(MODULE2_INTER_WINDOW_DELAY_SECONDS)))
MODULE2_SLEEP_BETWEEN_WINDOWS_MAX = float(os.getenv("MODULE2_SLEEP_BETWEEN_WINDOWS_MAX", str(MODULE2_INTER_WINDOW_DELAY_SECONDS + MODULE2_INTER_WINDOW_DELAY_JITTER_SECONDS)))
MODULE2_SLEEP_BETWEEN_PAGES_MIN = float(os.getenv("MODULE2_SLEEP_BETWEEN_PAGES_MIN", str(MODULE2_INTER_PAGE_DELAY_SECONDS)))
MODULE2_SLEEP_BETWEEN_PAGES_MAX = float(os.getenv("MODULE2_SLEEP_BETWEEN_PAGES_MAX", str(MODULE2_INTER_PAGE_DELAY_SECONDS + MODULE2_INTER_PAGE_DELAY_JITTER_SECONDS)))
MODULE3_INTER_DETAIL_DELAY_SECONDS = float(os.getenv("MODULE3_INTER_DETAIL_DELAY_SECONDS", "8"))
MODULE3_INTER_DETAIL_DELAY_JITTER_SECONDS = float(os.getenv("MODULE3_INTER_DETAIL_DELAY_JITTER_SECONDS", "4"))
MODULE3_CHROME_ERROR_RETRY_DELAY_SECONDS = float(os.getenv("MODULE3_CHROME_ERROR_RETRY_DELAY_SECONDS", "3"))
MODULE3_CHROME_ERROR_NAV_RESET = _bool_env("MODULE3_CHROME_ERROR_NAV_RESET", True)
MODULE2_MAX_CONSECUTIVE_TIMEOUT_WINDOWS = 3
MODULE2_MAX_WINDOWS_PER_RUN = 12
PRICE_INFERENCE_ENABLED = os.getenv("PRICE_INFERENCE_ENABLED", "true").lower() == "true"
SETUP_PRICE_BASELINE_BATCH_SIZE = int(os.getenv("SETUP_PRICE_BASELINE_BATCH_SIZE", "10"))
PRICE_REFRESH_BATCH_SIZE = int(os.getenv("PRICE_REFRESH_BATCH_SIZE", "10"))
PRICE_UNKNOWN_RETRY_INTERVAL_SECONDS = int(os.getenv("PRICE_UNKNOWN_RETRY_INTERVAL_SECONDS", "3600"))
MIN_SMART_PRICE_HISTORY_COUNT = int(os.getenv("MIN_SMART_PRICE_HISTORY_COUNT", "10"))
SCHEDULE_TIMEZONE = os.getenv("SCHEDULE_TIMEZONE", "Australia/Sydney")
PRICE_REFRESH_TIMES = os.getenv("PRICE_REFRESH_TIMES", "00:00,12:00")
DAILY_FULL_LISTING_SWEEP_TIME = os.getenv("DAILY_FULL_LISTING_SWEEP_TIME", "04:00")
PROCESS_NEW_LISTING_BATCH_SIZE = int(os.getenv("PROCESS_NEW_LISTING_BATCH_SIZE", "5"))
MODULE1_MAX_PROFILE_ROTATIONS_PER_RUN = 2
MODULE1_RETRY_SAME_PAGE_AFTER_429 = 1
MODULE3_MAX_PROFILE_ROTATIONS_PER_RUN = 2
MODULE3_RETRY_SAME_LISTING_AFTER_429 = 1
MODULE3_STOP_ON_429_ROTATION_LIMIT = False

BROWSER_RECOVERY_ON_429 = True
BROWSER_PROFILE_BASE_DIR = "rea_profile"
BROWSER_PROFILE_BACKUP_PREFIX = "rea_profile_429_backup_"
BROWSER_PROFILE_GENERATED_PREFIX = "rea_profile_gen_"
BROWSER_MAX_PROFILE_ROTATIONS_PER_RUN = 2
BROWSER_COOLDOWN_ON_429_SECONDS = 60
BROWSER_USE_RUNTIME_PROFILE_STATE = _bool_env("BROWSER_USE_RUNTIME_PROFILE_STATE", True)
BROWSER_PROFILE_STATE_PATH = "output/browser_profile_state.json"
BROWSER_KILL_CHROME_ON_RECOVERY = False
REA_RATE_LIMIT_BACKOFF_SECONDS = int(os.getenv("REA_RATE_LIMIT_BACKOFF_SECONDS", "21600"))
BROWSER_BLOCK_GRACE_SECONDS = float(os.getenv("BROWSER_BLOCK_GRACE_SECONDS", "30"))
BROWSER_BLOCK_POLL_SECONDS = float(os.getenv("BROWSER_BLOCK_POLL_SECONDS", "1.0"))
BROWSER_NO_RESULTS_STABLE_SECONDS = float(os.getenv("BROWSER_NO_RESULTS_STABLE_SECONDS", "1.0"))
BROWSER_KPSDK_SAME_SESSION_RECHECKS = int(os.getenv("BROWSER_KPSDK_SAME_SESSION_RECHECKS", "2"))
BROWSER_KPSDK_SETTLE_SECONDS = float(os.getenv("BROWSER_KPSDK_SETTLE_SECONDS", "10"))
BROWSER_PAGE_STATE_DEBUG = _bool_env("BROWSER_PAGE_STATE_DEBUG", True)
BROWSER_SAME_URL_MAX_RETRIES = int(os.getenv("BROWSER_SAME_URL_MAX_RETRIES", "1"))
BROWSER_CONSECUTIVE_GOTO_FAILURE_ROTATION_THRESHOLD = int(os.getenv("BROWSER_CONSECUTIVE_GOTO_FAILURE_ROTATION_THRESHOLD", "3"))
BROWSER_CONSECUTIVE_CHROME_ERROR_ROTATION_THRESHOLD = int(os.getenv("BROWSER_CONSECUTIVE_CHROME_ERROR_ROTATION_THRESHOLD", "3"))
BROWSER_ZERO_SUCCESS_HARD_FAILURE_THRESHOLD = int(os.getenv("BROWSER_ZERO_SUCCESS_HARD_FAILURE_THRESHOLD", "3"))
MODULE2_MIN_WINDOWS_BEFORE_SESSION_RECOVERY = int(os.getenv("MODULE2_MIN_WINDOWS_BEFORE_SESSION_RECOVERY", "5"))

MODULE3_SLEEP_BETWEEN = float(os.getenv("MODULE3_SLEEP_BETWEEN", "0"))
MODULE3_WAIT_TIMEOUT = 25
MODULE3_EMPTY_RETRY = 1

# Bot scheduler/concurrency
BOT_SCHEDULER_TICK_SECONDS = 60
LIGHT_CHECK_PAGES = PROFILE_SETTINGS["LIGHT_CHECK_PAGES"]
FULL_RUN_CONCURRENCY = PROFILE_SETTINGS["MAX_CONCURRENT_FULL_RUNS"]


# Data-driven queue settings

DEFAULT_LIGHT_CHECK_PAGES = LIGHT_CHECK_PAGES
LIGHT_CHECK_INTERVAL_SECONDS = 1800
FULL_WORKER_INTERVAL_SECONDS = PROFILE_SETTINGS["WORKER_TICK_SECONDS"]
MAX_CONCURRENT_ENRICH_JOBS = PROFILE_SETTINGS["MAX_CONCURRENT_ENRICH"]
MAX_PAGES_MODULE1 = PROFILE_SETTINGS["MAX_PAGES_MODULE1"] if PROFILE_SETTINGS["MAX_PAGES_MODULE1"] is not None else MAX_PAGES_MODULE1
CHROME_WINDOW_SIZE = PROFILE_SETTINGS["CHROME_WINDOW_SIZE"]
IMAGES_DISABLED = PROFILE_SETTINGS["IMAGES_DISABLED"]
BROWSER_ENGINE = os.getenv("BROWSER_ENGINE", "cloak").strip().lower() or "cloak"
_MODULE1_PAGINATION_NAV_MODE_DEFAULT = "click_next" if BROWSER_ENGINE == "cloak" else "direct_url"
MODULE1_PAGINATION_NAV_MODE = os.getenv("MODULE1_PAGINATION_NAV_MODE", _MODULE1_PAGINATION_NAV_MODE_DEFAULT).strip().lower() or _MODULE1_PAGINATION_NAV_MODE_DEFAULT
if MODULE1_PAGINATION_NAV_MODE not in {"click_next", "direct_url", "fresh_context_per_page"}:
    MODULE1_PAGINATION_NAV_MODE = _MODULE1_PAGINATION_NAV_MODE_DEFAULT
_MODULE2_PAGINATION_NAV_MODE_DEFAULT = "click_next" if BROWSER_ENGINE == "cloak" else "direct_url"
MODULE2_PAGINATION_NAV_MODE = os.getenv("MODULE2_PAGINATION_NAV_MODE", _MODULE2_PAGINATION_NAV_MODE_DEFAULT).strip().lower() or _MODULE2_PAGINATION_NAV_MODE_DEFAULT
if MODULE2_PAGINATION_NAV_MODE not in {"click_next", "direct_url", "fresh_context_per_page"}:
    MODULE2_PAGINATION_NAV_MODE = _MODULE2_PAGINATION_NAV_MODE_DEFAULT
_MODULE2_WINDOW_NAV_MODE_DEFAULT = "fresh_context_on_failure" if BROWSER_ENGINE == "cloak" else "direct_url"
MODULE2_WINDOW_NAV_MODE = os.getenv("MODULE2_WINDOW_NAV_MODE", _MODULE2_WINDOW_NAV_MODE_DEFAULT).strip().lower() or _MODULE2_WINDOW_NAV_MODE_DEFAULT
if MODULE2_WINDOW_NAV_MODE not in {"direct_url", "fresh_context_on_failure", "fresh_context_per_window"}:
    MODULE2_WINDOW_NAV_MODE = _MODULE2_WINDOW_NAV_MODE_DEFAULT
CLOAK_PROFILE_DIR = os.getenv("CLOAK_PROFILE_DIR", os.getenv("CHROME_PROFILE_DIR", "rea_profile"))
CLOAK_HEADLESS = _bool_env("CLOAK_HEADLESS", _bool_env("HEADLESS", False))
CLOAK_PROXY = _optional_str_env("CLOAK_PROXY")
CLOAK_GEOIP = _bool_env("CLOAK_GEOIP", False)
CLOAK_HUMANIZE = _bool_env("CLOAK_HUMANIZE", BROWSER_ENGINE == "cloak" and not CLOAK_HEADLESS)
CLOAK_HUMAN_PRESET = _optional_str_env("CLOAK_HUMAN_PRESET")
CLOAK_HUMAN_CONFIG = _json_object_env("CLOAK_HUMAN_CONFIG_JSON")
CLOAK_FINGERPRINT_SEED = _optional_str_env("CLOAK_FINGERPRINT_SEED")
CLOAK_FINGERPRINT_PLATFORM = os.getenv("CLOAK_FINGERPRINT_PLATFORM", "windows")
CLOAK_FINGERPRINT_STORAGE_QUOTA = _optional_str_env("CLOAK_FINGERPRINT_STORAGE_QUOTA")
_CLOAK_VIEWPORT_DEFAULT = _viewport_env("CLOAK_VIEWPORT", 1365, 768)
CLOAK_VIEWPORT_WIDTH = int(os.getenv("CLOAK_VIEWPORT_WIDTH", str(_CLOAK_VIEWPORT_DEFAULT[0])))
CLOAK_VIEWPORT_HEIGHT = int(os.getenv("CLOAK_VIEWPORT_HEIGHT", str(_CLOAK_VIEWPORT_DEFAULT[1])))
CLOAK_LOCALE = os.getenv("CLOAK_LOCALE", "en-AU")
CLOAK_TIMEZONE = os.getenv("CLOAK_TIMEZONE", "Australia/Sydney")
_CLOAK_HTTP2_MODE_RAW = os.getenv("CLOAK_HTTP2_MODE", "").strip().lower()
if not _CLOAK_HTTP2_MODE_RAW:
    _CLOAK_HTTP2_MODE_RAW = "disable" if _bool_env("CLOAK_DISABLE_HTTP2", False) else "default"
if _CLOAK_HTTP2_MODE_RAW not in {"default", "disable", "warmup_only"}:
    raise ValueError("CLOAK_HTTP2_MODE must be one of: default, disable, warmup_only")
CLOAK_HTTP2_MODE = _CLOAK_HTTP2_MODE_RAW
CLOAK_DISABLE_HTTP2 = CLOAK_HTTP2_MODE == "disable"
CLOAK_USE_PERSISTENT_CONTEXT = _bool_env("CLOAK_USE_PERSISTENT_CONTEXT", True)
CLOAK_CONTEXT_REUSE_MODE = os.getenv("CLOAK_CONTEXT_REUSE_MODE", "per_driver")
_DEFAULT_BLOCK_RESOURCES = BROWSER_ENGINE != "cloak"
LOW_BANDWIDTH_MODE = _bool_env("LOW_BANDWIDTH_MODE", _DEFAULT_BLOCK_RESOURCES)
BLOCK_HEAVY_RESOURCES = _bool_env("BLOCK_HEAVY_RESOURCES", _DEFAULT_BLOCK_RESOURCES)
ULTRA_LOW_BANDWIDTH = _bool_env("ULTRA_LOW_BANDWIDTH", _DEFAULT_BLOCK_RESOURCES)
BLOCK_TRACKERS = _bool_env("BLOCK_TRACKERS", _DEFAULT_BLOCK_RESOURCES)
BLOCK_IMAGES = _bool_env("BLOCK_IMAGES", _DEFAULT_BLOCK_RESOURCES)
BLOCK_MEDIA = _bool_env("BLOCK_MEDIA", _DEFAULT_BLOCK_RESOURCES)
BLOCK_FONTS = _bool_env("BLOCK_FONTS", _DEFAULT_BLOCK_RESOURCES)
BLOCK_MAPS = _bool_env("BLOCK_MAPS", _DEFAULT_BLOCK_RESOURCES)
BLOCK_ADS = _bool_env("BLOCK_ADS", _DEFAULT_BLOCK_RESOURCES)
BLOCK_ANALYTICS = _bool_env("BLOCK_ANALYTICS", _DEFAULT_BLOCK_RESOURCES)
BLOCK_CSS = _bool_env("BLOCK_CSS", False)
BLOCK_JS = _bool_env("BLOCK_JS", False)
NETWORK_DEBUG = _bool_env("NETWORK_DEBUG", True)
NETWORK_DEBUG_TOP_N = int(os.getenv("NETWORK_DEBUG_TOP_N", "30"))
USE_TEMP_CHROME_PROFILE = _bool_env("USE_TEMP_CHROME_PROFILE", False)
USE_PERSISTENT_CHROME_PROFILE = _bool_env("USE_PERSISTENT_CHROME_PROFILE", True)
CHROME_PROFILE_DIR = os.getenv("CHROME_PROFILE_DIR", "rea_profile")
CHROME_PROFILE_DIRECTORY = os.getenv("CHROME_PROFILE_DIRECTORY", "Default")
CLEAR_CHROME_CACHE_ON_START = False
CLEAR_CHROME_CACHE_ON_EXIT = False
RESET_CHROME_PROFILE_EACH_RUN = False
NETWORK_THROTTLE_ENABLED = False
NETWORK_THROTTLE_DOWNLOAD_KBPS = 300
NETWORK_THROTTLE_UPLOAD_KBPS = 100


def get_effective_browser_profile_dir(module_name: str | None = None, explicit_override: str | None = None) -> str:
    """Resolve the configured persistent browser profile before runtime state overrides."""
    if explicit_override and str(explicit_override).strip():
        return str(explicit_override).strip()
    normalized_module = str(module_name or "").strip().lower()
    if normalized_module == "module2" and MODULE2_PROFILE_BASE_DIR:
        return MODULE2_PROFILE_BASE_DIR
    if BROWSER_ENGINE == "cloak":
        return CLOAK_PROFILE_DIR or CHROME_PROFILE_DIR or BROWSER_PROFILE_BASE_DIR
    return CHROME_PROFILE_DIR or BROWSER_PROFILE_BASE_DIR


CHROME_BINARY_PATH = _env_first(
    "CHROME_BINARY_PATH",
    "GOOGLE_CHROME_BIN",
    default=_windows_default_chrome_path(),
)
CHROME_VERSION_MAIN = _optional_int_env("CHROME_VERSION_MAIN", 148 if os.name == "nt" else None)
EXTRA_CHROME_LIGHT_FLAGS = PROFILE_SETTINGS["EXTRA_CHROME_LIGHT_FLAGS"]
# TODO: Low Mode is intentionally disabled for now. Re-enable in a later performance profile refactor.
USE_XVFB = PROFILE_SETTINGS["USE_XVFB"]
ENRICH_JOB_MAX_ATTEMPTS = 5
ENRICH_JOB_BACKOFF_SECONDS = [30, 120, 300, 900, 1800]
JOB_LOCK_TIMEOUT_SECONDS = 600
JOB_STALE_TIMEOUT_MINUTES_DEFAULT = int(os.getenv("JOB_STALE_TIMEOUT_MINUTES_DEFAULT", "120"))
JOB_STALE_TIMEOUT_MINUTES_LIGHT_CHECK = int(os.getenv("JOB_STALE_TIMEOUT_MINUTES_LIGHT_CHECK", "45"))
JOB_STALE_TIMEOUT_MINUTES_PROCESS_NEW_LISTING = int(os.getenv("JOB_STALE_TIMEOUT_MINUTES_PROCESS_NEW_LISTING", "90"))
JOB_STALE_TIMEOUT_MINUTES_DETAIL_REFRESH = int(os.getenv("JOB_STALE_TIMEOUT_MINUTES_DETAIL_REFRESH", "180"))
JOB_STALE_TIMEOUT_MINUTES_MODULE2_REFRESH = int(os.getenv("JOB_STALE_TIMEOUT_MINUTES_MODULE2_REFRESH", "300"))
JOB_STALE_TIMEOUT_MINUTES_MODULE1_SWEEP = int(os.getenv("JOB_STALE_TIMEOUT_MINUTES_MODULE1_SWEEP", "120"))
JOB_STALE_TIMEOUT_MINUTES_BASELINE_SETUP = int(os.getenv("JOB_STALE_TIMEOUT_MINUTES_BASELINE_SETUP", "480"))
JOB_STALE_TIMEOUT_MINUTES_PRICE_RETRY_UNKNOWNS = int(os.getenv("JOB_STALE_TIMEOUT_MINUTES_PRICE_RETRY_UNKNOWNS", "180"))
JOB_STALE_TIMEOUT_MINUTES_LISTING_STATUS_RECHECK = int(os.getenv("JOB_STALE_TIMEOUT_MINUTES_LISTING_STATUS_RECHECK", "45"))
AREA_URL_FORCE_SORT = True


LIGHT_CHECK_DEFAULT_MAX_PAGES = 3
LIGHT_CHECK_HARD_MAX_PAGES = 5

DETAIL_REFRESH_DEFAULT_LIMIT = 10
DETAIL_REFRESH_HARD_LIMIT = 50
DETAIL_REFRESH_STALE_HOURS = 24
DETAIL_REFRESH_TIMEOUT = 25
DETAIL_REFRESH_SLEEP_BETWEEN = 0.5
DETAIL_REFRESH_ONLY_ACTIVE = True

# Phase 5 Telegram subscriptions and monitoring scheduler
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN)
MAX_AREAS_PER_USER = int(os.getenv("MAX_AREAS_PER_USER", str(MAX_AREAS_PER_USER)))
LIGHT_CHECK_INTERVAL_MINUTES = int(os.getenv("LIGHT_CHECK_INTERVAL_MINUTES", "60"))
DETAIL_REFRESH_INTERVAL_HOURS = int(os.getenv("DETAIL_REFRESH_INTERVAL_HOURS", "6"))
NOTIFICATION_QUEUE_INTERVAL_MINUTES = int(os.getenv("NOTIFICATION_QUEUE_INTERVAL_MINUTES", "5"))
TELEGRAM_SEND_INTERVAL_SECONDS = int(os.getenv("TELEGRAM_SEND_INTERVAL_SECONDS", "15"))
DETAIL_REFRESH_BATCH_LIMIT = int(os.getenv("DETAIL_REFRESH_BATCH_LIMIT", "10"))
DETAIL_BASELINE_MAX_ATTEMPTS = int(os.getenv("DETAIL_BASELINE_MAX_ATTEMPTS", "5"))
DETAIL_BASELINE_RETRY_BACKOFF_SECONDS = [300, 900, 1800, 3600, 7200]

def _optional_positive_int_env(name: str, default: str) -> int | None:
    value = os.getenv(name, default).strip().lower()
    if value in {"none", "0"}:
        return None
    parsed = int(value)
    if parsed < 0:
        raise ValueError(f"{name} must be positive, 0, or none")
    return parsed


INITIAL_BASELINE_MAX_PAGES = _optional_positive_int_env("INITIAL_BASELINE_MAX_PAGES", "50")
# Backward-compatible alias. New baseline code uses INITIAL_BASELINE_MAX_PAGES explicitly.
BASELINE_MAX_PAGES = INITIAL_BASELINE_MAX_PAGES
MONITORING_TICK_SLEEP_SECONDS = int(os.getenv("MONITORING_TICK_SLEEP_SECONDS", "60"))

TELEGRAM_SENDER_MAX_PER_TICK = int(os.getenv("TELEGRAM_SENDER_MAX_PER_TICK", "10"))
NOTIFY_ON_SIZE_CHANGED = os.getenv("NOTIFY_ON_SIZE_CHANGED", "true").lower() == "true"
NOTIFY_ON_SIZE_DISCOVERED = os.getenv("NOTIFY_ON_SIZE_DISCOVERED", "true").lower() == "true"
NOTIFY_ON_FIELD_DISCOVERED = os.getenv("NOTIFY_ON_FIELD_DISCOVERED", "true").lower() == "true"

# Phase 2C single-runtime settings for telegram_bot.py.
SCHEDULER_LOOP_SECONDS = int(os.getenv("SCHEDULER_LOOP_SECONDS", "60"))
WORKER_IDLE_SLEEP_SECONDS = int(os.getenv("WORKER_IDLE_SLEEP_SECONDS", "10"))
WORKER_ERROR_SLEEP_SECONDS = int(os.getenv("WORKER_ERROR_SLEEP_SECONDS", "30"))
HEARTBEAT_SECONDS = int(os.getenv("HEARTBEAT_SECONDS", "60"))
ADMIN_TELEGRAM_IDS = os.getenv("ADMIN_TELEGRAM_IDS", "111694049")


def parse_admin_telegram_ids(value: str | None = None) -> set[str]:
    raw = ADMIN_TELEGRAM_IDS if value is None else value
    return {item.strip() for item in str(raw or "").split(",") if item.strip()}


CREATE_AUDIT_EVENTS_DURING_INITIAL_BASELINE = os.getenv("CREATE_AUDIT_EVENTS_DURING_INITIAL_BASELINE", "false").lower() == "true"


def mask_sensitive_text(value) -> str:
    text = str(value or "")
    if TELEGRAM_BOT_TOKEN:
        text = text.replace(TELEGRAM_BOT_TOKEN, "***REDACTED_TELEGRAM_TOKEN***")
    if DB_PASSWORD:
        text = text.replace(DB_PASSWORD, "***REDACTED_DB_PASSWORD***")
    return text


def is_production() -> bool:
    return _bool_env("PRODUCTION", False) or os.getenv("APP_ENV", "").strip().lower() in {"prod", "production"}


def db_uses_trusted_connection() -> bool:
    return str(DB_TRUSTED_CONNECTION or "").strip().lower() in {"yes", "true", "1"}


def db_server_for_odbc() -> str:
    host = (DB_HOST or "localhost").strip()
    port = str(DB_PORT or "").strip()
    if port and "," not in host:
        return f"{host},{port}"
    return host


def build_sqlserver_connection_string(include_password: bool = True) -> str:
    parts = [
        f"DRIVER={{{DB_DRIVER}}}",
        f"SERVER={db_server_for_odbc()}",
        f"DATABASE={DB_NAME}",
        f"Encrypt={DB_ENCRYPT}",
        f"TrustServerCertificate={DB_TRUST_SERVER_CERTIFICATE}",
        f"Connection Timeout={DB_TIMEOUT}",
    ]
    if db_uses_trusted_connection():
        parts.append("Trusted_Connection=yes")
    else:
        parts.append(f"UID={DB_USER}")
        parts.append(f"PWD={DB_PASSWORD if include_password else '***'}")
    return ";".join(parts) + ";"


def effective_chrome_binary() -> str:
    if CHROME_BINARY_PATH:
        found = shutil.which(CHROME_BINARY_PATH)
        return found or CHROME_BINARY_PATH
    for command in ("google-chrome", "google-chrome-stable", "chromium-browser", "chromium"):
        found = shutil.which(command)
        if found:
            return found
    return ""


def safe_runtime_summary() -> dict:
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "db_host": DB_HOST,
        "db_port": DB_PORT or "(default)",
        "db_name": DB_NAME,
        "db_driver": DB_DRIVER,
        "db_encrypt": DB_ENCRYPT,
        "db_trust_server_certificate": DB_TRUST_SERVER_CERTIFICATE,
        "db_trusted_connection": DB_TRUSTED_CONNECTION,
        "headless": os.getenv("HEADLESS", "0"),
        "perf_profile": PERF_PROFILE,
        "output_dir": OUTPUT_DIR,
        "log_dir": LOG_DIR,
        "browser_engine": BROWSER_ENGINE,
        "cloak_profile_dir": CLOAK_PROFILE_DIR,
        "cloak_viewport": f"{CLOAK_VIEWPORT_WIDTH}x{CLOAK_VIEWPORT_HEIGHT}",
        "cloak_locale": CLOAK_LOCALE,
        "cloak_timezone": CLOAK_TIMEZONE,
        "cloak_disable_http2": str(CLOAK_DISABLE_HTTP2).lower(),
    }


def validate_runtime_config(require_token: bool = False, require_db: bool = False) -> list[str]:
    missing: list[str] = []
    if require_token and not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if require_db:
        for name, value in {
            "DB_HOST": DB_HOST,
            "DB_NAME": DB_NAME,
            "DB_DRIVER": DB_DRIVER,
        }.items():
            if not str(value or "").strip():
                missing.append(name)
        if not db_uses_trusted_connection():
            if not DB_USER:
                missing.append("DB_USER")
            if not DB_PASSWORD:
                missing.append("DB_PASSWORD")
    return missing

LIGHT_CHECK_INTERVAL_SECONDS = 30
DEFAULT_POLL_INTERVAL_MINUTES = 5

# Phase 2B priority job queue settings
NEW_LISTING_CHECK_INTERVAL_SECONDS = int(os.getenv("NEW_LISTING_CHECK_INTERVAL_SECONDS", "1800"))
DETAIL_REFRESH_INTERVAL_SECONDS = int(os.getenv("DETAIL_REFRESH_INTERVAL_SECONDS", "3600"))
DETAIL_REFRESH_BATCH_SIZE = int(os.getenv("DETAIL_REFRESH_BATCH_SIZE", "35"))
SETUP_DETAIL_BATCH_SIZE = int(os.getenv("SETUP_DETAIL_BATCH_SIZE", str(DETAIL_REFRESH_BATCH_LIMIT if 'DETAIL_REFRESH_BATCH_LIMIT' in globals() else 10)))
WORKER_SLEEP_SECONDS = int(os.getenv("WORKER_SLEEP_SECONDS", "10"))
