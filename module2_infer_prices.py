import csv
import json
import os
import re
import time
import random
import gc
import hashlib
import uuid
from glob import glob
from datetime import datetime
from urllib.parse import urlparse, urlunparse, urlencode, parse_qsl

from cloak_browser_helper import By, EC, WebDriverWait, TimeoutException, WebDriverException

from config import AREA_SEARCH_URL
import config
from json_safe import json_safe
from chrome_options_helper import build_chrome_driver, cleanup_chrome_driver
from module2_price_utils import price_needs_inference as _price_needs_inference_impl
from browser_recovery import BrowserSessionHealth, RecoveryPolicy, is_429_page, log_session_health, recover_browser_for_untrusted_state as recover_browser_after_429, same_session_kpsdk_recheck, safe_driver_get
from realestate_page_state import PageState, wait_for_search_page_state


def price_needs_inference(price_text: str | None) -> bool:
    return _price_needs_inference_impl(price_text)


# =========================
# Selectors
# =========================
CARD_SELECTORS = [
    'article[data-testid="ResidentialCard"]',
    '[data-testid="property-card"]',
    "article.residential-card",
    "article[data-testid]",
]

PAGINATION_CONTAINERS = [
    'nav[aria-label*="Pagination"]',
    'nav[aria-label*="pagination"]',
    '[data-testid*="pagination"]',
    '[data-testid*="paginator"]',
]

NEXT_SELECTORS = [
    'nav[aria-label*="Pagination"] a[aria-label*="Next"]',
    'nav[aria-label*="pagination"] a[aria-label*="Next"]',
    '[data-testid*="paginator"] a[aria-label*="Next"]',
    '[data-testid*="pagination"] a[aria-label*="Next"]',
    'a[aria-label*="Next"]',
    'button[aria-label*="Next"]',
    '[data-testid*="paginator-next"]',
    '[data-testid*="next"]',
    'a[rel="next"]',  # فقط اگر داخل body و visible باشد
]

NO_RESULTS_SELECTORS = [
    '[data-testid*="no-results"]',
    '[data-testid*="noResult"]',
    '[data-testid*="empty"]',
    'div[class*="no-results"]',
    'section[class*="no-results"]',
]


# =========================
# Driver
# =========================
def build_driver(profile_dir_override: str | None = None):
    return build_chrome_driver(profile_dir_override=profile_dir_override)


def restart_driver(driver):
    try:
        driver.quit()
    except Exception:
        pass
    cleanup_chrome_driver(driver)
    time.sleep(0.7)
    return build_driver()




# =========================
# GET resilient + internet detect
# =========================
def is_internet_disconnected(err: Exception) -> bool:
    msg = str(err).lower()
    return any(x in msg for x in [
        "err_internet_disconnected",
        "internet disconnected",
        "err_network_changed",
        "err_connection",
        "net::",
    ])


def get_with_retries(driver, url, tries=2):
    last_err = None
    for _ in range(tries):
        try:
            ok, exc = safe_driver_get(driver, url)
            if ok:
                return driver, True, None
            last_err = exc
            if is_internet_disconnected(exc):
                return driver, False, exc
            time.sleep(0.6)
            continue
        except TimeoutException as e:
            last_err = e
            try:
                driver.execute_script("window.stop();")
            except Exception:
                pass
            time.sleep(0.6)
        except WebDriverException as e:
            last_err = e
            if is_internet_disconnected(e):
                return driver, False, e
            time.sleep(0.6)

    return driver, False, last_err


def _same_driver_get(driver, url: str):
    _driver, ok, err = get_with_retries(driver, url, tries=2)
    if not ok and err:
        raise err
    return ok


# =========================
# IO
# =========================
def read_rows(input_path: str) -> list[dict]:
    ext = os.path.splitext(input_path.lower())[1]
    if ext == ".json":
        with open(input_path, "r", encoding="utf-8") as f:
            return json.load(f)
    if ext == ".csv":
        with open(input_path, "r", encoding="utf-8-sig") as f:
            return list(csv.DictReader(f))
    raise ValueError("Input must be .csv or .json")


def write_outputs(rows: list[dict], out_dir="output") -> tuple[str, str]:
    os.makedirs(out_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    out_csv = os.path.join(out_dir, f"realestate_properties_with_prices_{ts}.csv")
    out_json = os.path.join(out_dir, f"realestate_properties_with_prices_{ts}.json")

    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(json_safe(rows), f, ensure_ascii=False, indent=2)

    keys = set()
    for r in rows:
        keys.update(r.keys())
    fieldnames = sorted(keys)

    with open(out_csv, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow({k: json_safe(r.get(k, "")) for k in fieldnames})

    return out_csv, out_json


def find_latest_module1_output(out_dir="output") -> str:
    patterns = [
        os.path.join(out_dir, "realestate_properties_*.json"),
        os.path.join(out_dir, "realestate_properties_*.csv"),
    ]
    candidates = []
    for p in patterns:
        candidates.extend(glob(p))
    if not candidates:
        raise FileNotFoundError(f"No module1 outputs found in: {os.path.abspath(out_dir)}")
    return max(candidates, key=os.path.getmtime)


# =========================
# URL build between + keep query
# =========================
def extract_location_slug(base_list_url: str) -> str:
    u = urlparse(base_list_url)
    path = u.path

    m = re.search(r"/buy/in-(.+?)/list-\d+", path)
    if m:
        return m.group(1)

    m = re.search(r"/buy/between-\d+-\d+-in-(.+?)/list-\d+", path)
    if m:
        return m.group(1)

    raise ValueError("Could not parse location from base_list_url")


def build_between_url(base_list_url: str, low: int, high: int, page: int = 1) -> str:
    u = urlparse(base_list_url)
    loc = extract_location_slug(base_list_url)

    new_path = f"/buy/between-{low}-{high}-in-{loc}/list-{page}"

    q = dict(parse_qsl(u.query))
    q.setdefault("source", "refinement")
    query = urlencode(q)

    return urlunparse((u.scheme, u.netloc, new_path, "", query, ""))


def _parse_list_page(url: str) -> int | None:
    m = re.search(r"/list-(\d+)", url or "")
    return int(m.group(1)) if m else None


# =========================
# Cards / listing_id extraction
# =========================
def parse_listing_id_from_url(url: str | None) -> str | None:
    if not url:
        return None
    m = re.search(r"-(\d+)(?:[/?].*)?$", url)
    return m.group(1) if m else None


def _get_cards(driver) -> list:
    for sel in CARD_SELECTORS:
        try:
            els = driver.find_elements(By.CSS_SELECTOR, sel)
            if els:
                return els
        except Exception:
            continue
    return []


def extract_listing_ids_from_cards(cards) -> set[str]:
    ids = set()
    for card in cards:
        try:
            links = card.find_elements(By.CSS_SELECTOR, 'a[href*="/property-"], a[href*="/sold/property-"]')
            for a in links:
                href = a.get_attribute("href")
                lid = parse_listing_id_from_url(href)
                if lid:
                    ids.add(lid)
                    break
        except Exception:
            continue
    return ids


# =========================
# Pagination: get max pages (FIX)
# =========================
def get_max_pages_from_pagination(driver) -> int | None:
    """
    تلاش می‌کند max page واقعی را از pagination به دست آورد.
    اگر پیدا نکرد، None برمی‌گرداند.
    """
    texts = []
    # container text
    for sel in PAGINATION_CONTAINERS:
        try:
            for nav in driver.find_elements(By.CSS_SELECTOR, sel):
                if nav.is_displayed():
                    t = (nav.text or "").strip()
                    if t:
                        texts.append(t)
        except Exception:
            pass

    # pattern: "Page 1 of 1" / "1 of 7"
    joined = " | ".join(texts)
    m = re.search(r"\bof\s+(\d+)\b", joined, flags=re.IGNORECASE)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            pass

    # شماره صفحات از لینک‌ها/دکمه‌ها
    max_num = None
    try:
        # لینک/دکمه‌هایی که متن عددی دارند
        candidates = driver.find_elements(By.CSS_SELECTOR, 'a, button')
        for el in candidates:
            try:
                if not el.is_displayed():
                    continue
                txt = (el.text or "").strip()
                if txt.isdigit():
                    n = int(txt)
                    if max_num is None or n > max_num:
                        max_num = n
            except Exception:
                continue
    except Exception:
        pass

    return max_num


# =========================
# No-results detection (FIX)
# =========================
def has_no_results(driver) -> bool:
    # اگر کارت داریم => no-results نیست
    if _get_cards(driver):
        return False

    for sel in NO_RESULTS_SELECTORS:
        try:
            for el in driver.find_elements(By.CSS_SELECTOR, sel):
                if el.is_displayed():
                    txt = (el.text or "").strip().lower()
                    if (not txt) or ("couldn't find" in txt) or ("matches your search" in txt) or ("no results" in txt):
                        return True
        except Exception:
            continue

    # fallback متن (فقط وقتی کارت 0 است)
    try:
        el = driver.find_element(
            By.XPATH,
            "//*[contains(., \"We couldn't find anything\") or contains(., \"matches your search\") or contains(., \"No results\")]"
        )
        if el and el.is_displayed():
            return True
    except Exception:
        pass

    return False


def wait_for_cards_or_no_results(driver, timeout=25, min_cards=1):
    """
    صبر می‌کند تا یا کارت‌ها بیایند یا no-results «پایدار» شود.
    خروجی: ("cards", cards) یا ("no_results", [])
    """
    state = {"no_seen_at": None}

    def _cond(d):
        cards = _get_cards(d)
        if len(cards) >= min_cards:
            state["no_seen_at"] = None
            return ("cards", cards)

        if has_no_results(d):
            if state["no_seen_at"] is None:
                state["no_seen_at"] = time.time()
                return False
            if time.time() - state["no_seen_at"] >= 1.0:
                return ("no_results", [])
            return False

        state["no_seen_at"] = None
        return False

    return WebDriverWait(driver, timeout, poll_frequency=0.3).until(_cond)


# =========================
# Next detection (FIX)
# =========================
def detect_next(driver, current_page: int | None = None) -> bool:
    """
    فقط Next های داخل body که visible و فعال‌اند را قبول می‌کند.
    اگر current_page داده شود، href هم باید به list-(current_page+1) اشاره کند.
    """
    candidates = []
    for sel in NEXT_SELECTORS:
        try:
            candidates.extend(driver.find_elements(By.CSS_SELECTOR, sel))
        except Exception:
            pass

    for el in candidates:
        try:
            if not el.is_displayed():
                continue
            aria_disabled = (el.get_attribute("aria-disabled") or "").lower() == "true"
            disabled_attr = el.get_attribute("disabled") is not None
            cls = (el.get_attribute("class") or "").lower()
            if aria_disabled or disabled_attr or ("disabled" in cls):
                continue
            if hasattr(el, "is_enabled") and not el.is_enabled():
                continue

            # اگر لینک است و href دارد، sanity check
            href = (el.get_attribute("href") or "").strip()
            if current_page and href:
                target = _parse_list_page(href)
                if target is not None and target <= current_page:
                    continue

            return True
        except Exception:
            continue

    return False


def _is_disabled_pagination_control(el) -> bool:
    aria_disabled = (el.get_attribute("aria-disabled") or "").lower() == "true"
    disabled_attr = el.get_attribute("disabled") is not None
    cls = (el.get_attribute("class") or "").lower()
    href = (el.get_attribute("href") or "").strip().lower()
    href_disabled = (not href) or href == "javascript:void(0)"
    return aria_disabled or disabled_attr or ("disabled" in cls) or href_disabled


def has_next_results_page(driver, current_page: int) -> bool:
    selectors = [
        'a[rel="next"]',
        'a[aria-label*="Next"]',
        'button[aria-label*="Next"]',
    ]
    for sel in selectors:
        try:
            for el in driver.find_elements(By.CSS_SELECTOR, sel):
                if not el.is_displayed() or _is_disabled_pagination_control(el):
                    continue
                if hasattr(el, "is_enabled") and not el.is_enabled():
                    continue
                return True
        except Exception:
            continue

    next_page = current_page + 1
    try:
        candidates = driver.find_elements(By.CSS_SELECTOR, f'a[href*="/list-{next_page}"]')
        for el in candidates:
            if not el.is_displayed() or _is_disabled_pagination_control(el):
                continue
            return True
    except Exception:
        pass
    return False


 


# =========================
# Price parse for smart-start
# =========================
def parse_any_price_number(price_text: str) -> int | None:
    if not price_text:
        return None
    from db_layer import parse_price_bounds_from_text

    low, _high = parse_price_bounds_from_text(price_text)
    return low

def guess_start_low_from_rows(rows: list[dict], window_width: int, step: int) -> int:
    mins = []
    for r in rows:
        n = parse_any_price_number((r.get("price") or "").strip())
        if n:
            mins.append(n)
    if not mins:
        return 0
    m = min(mins)
    start = max(0, m - window_width)
    start = (start // step) * step
    return start


def format_range_price(low: int, high: int) -> str:
    return f"${low:,}-${high:,}"


# =========================
# Checkpoint / Resume
# =========================
def slugify(s: str) -> str:
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    return s.strip("_") or "checkpoint"


def checkpoint_path(out_dir: str, base_list_url: str) -> str:
    loc = extract_location_slug(base_list_url)
    h = hashlib.md5(base_list_url.encode("utf-8")).hexdigest()[:8]
    return os.path.join(out_dir, f"module2_price_checkpoint_{slugify(loc)}_{h}.json")


def save_checkpoint(path: str, data: dict, retries: int = 10, delay: float = 0.2):
    tmp = path + ".tmp"
    data["saved_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(json_safe(data), f, ensure_ascii=False, indent=2)
        f.flush()
        try:
            os.fsync(f.fileno())
        except Exception:
            pass

    last_err = None
    for _ in range(retries):
        try:
            os.replace(tmp, path)
            return
        except PermissionError as e:
            last_err = e
            time.sleep(delay)
        except OSError as e:
            last_err = e
            time.sleep(delay)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = f"{path}.backup_{ts}"
    try:
        with open(backup_path, "w", encoding="utf-8") as f:
            json.dump(json_safe(data), f, ensure_ascii=False, indent=2)
    except Exception:
        pass

    try:
        if os.path.exists(tmp):
            os.remove(tmp)
    except Exception:
        pass

    raise last_err if last_err else PermissionError(f"Could not replace checkpoint file: {path}")


def load_checkpoint(path: str) -> dict | None:
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def checkpoint_is_compatible(ck: dict, base_list_url: str, input_file: str, params: dict, target_ids: set[str]) -> bool:
    try:
        if ck.get("base_list_url") != base_list_url:
            return False
        # Compatibility intentionally ignores the temporary input_file path.
        # Price-baseline runs create fresh temp JSON files; the stable identity is
        # the SearchID/targets/sweep plan stored in params plus target_ids.
        if ck.get("params", {}) != params:
            return False
        ck_targets = ck.get("target_ids", ck.get("missing_ids", []))
        return set(ck_targets) == set(target_ids)
    except Exception:
        return False


MIN_SMART_PRICE_HISTORY_COUNT = int(getattr(config, "MIN_SMART_PRICE_HISTORY_COUNT", 10))
SWEEP_MODES = {"setup_full_sweep", "smart_refresh", "smart_retry_expanded", "fallback_full_for_missing"}
STEP_PROFILE = "<1800000:50000,1800000-6000000:100000,>6000000:200000"


def step_for_price(price: int | float | None) -> int:
    value = int(price or 0)
    if value < 1_800_000:
        return 50_000
    if value <= 6_000_000:
        return 100_000
    return 200_000


def generate_full_sweep_windows(window_width: int, max_high: int, start_low: int | None = None) -> list[tuple[int, int, int]]:
    windows: list[tuple[int, int, int]] = []
    low = int(config.MODULE2_MIN_LOW if start_low is None else start_low)
    max_high = int(max_high)
    while low <= max_high:
        current_step = step_for_price(low)
        windows.append((low, low + int(window_width), current_step))
        low += current_step
    return windows


def smart_sweep_bounds(low_anchor: int | float | None, high_anchor: int | float | None, expansion_steps: int, max_high: int, min_low: int | None = None) -> tuple[int, int]:
    min_low = int(config.MODULE2_MIN_LOW if min_low is None else min_low)
    if low_anchor is None or high_anchor is None:
        return min_low, int(max_high)
    low_anchor_int = int(low_anchor)
    high_anchor_int = int(high_anchor)
    start_low = max(min_low, low_anchor_int - int(expansion_steps) * step_for_price(low_anchor_int))
    end_high = min(int(max_high), high_anchor_int + int(expansion_steps) * step_for_price(high_anchor_int))
    return start_low, end_high


def generate_smart_sweep_windows(low_anchor: int | float | None, high_anchor: int | float | None, window_width: int, max_high: int, expansion_steps: int, min_low: int | None = None) -> tuple[list[tuple[int, int, int]], int, int]:
    start_low, end_high = smart_sweep_bounds(low_anchor, high_anchor, expansion_steps, max_high, min_low=min_low)
    windows: list[tuple[int, int, int]] = []
    low = start_low
    while low <= end_high:
        current_step = step_for_price(low)
        high = low + int(window_width)
        if high >= start_low:
            windows.append((low, high, current_step))
        low += current_step
    return windows, start_low, end_high


def build_sweep_plan(sweep_mode: str, window_width: int, max_high: int, low_anchor: int | float | None = None, high_anchor: int | float | None = None, min_low: int | None = None) -> dict:
    min_low = int(config.MODULE2_MIN_LOW if min_low is None else min_low)
    mode = sweep_mode if sweep_mode in SWEEP_MODES else "setup_full_sweep"
    if mode in {"setup_full_sweep", "fallback_full_for_missing"}:
        windows = generate_full_sweep_windows(window_width, max_high, start_low=min_low)
        return {
            "sweep_mode": mode,
            "low_anchor": low_anchor,
            "high_anchor": high_anchor,
            "min_low": min_low,
            "max_high": int(max_high),
            "start_low": min_low,
            "end_high": int(max_high),
            "step_profile": STEP_PROFILE,
            "windows": windows,
        }
    expansion = 10 if mode == "smart_retry_expanded" else 4
    windows, start_low, end_high = generate_smart_sweep_windows(low_anchor, high_anchor, window_width, max_high, expansion, min_low=min_low)
    return {
        "sweep_mode": mode,
        "low_anchor": low_anchor,
        "high_anchor": high_anchor,
        "min_low": min_low,
        "max_high": int(max_high),
        "start_low": start_low,
        "end_high": end_high,
        "step_profile": STEP_PROFILE,
        "windows": windows,
    }

# =========================
# Inference (FIXED)
# =========================
def infer_prices_window_based_with_checkpoint(
    driver,
    base_list_url: str,
    target_ids: set[str],
    window_width: int,
    step: int,
    start_low: int,
    max_high: int,
    max_pages_per_window: int,
    wait_timeout: int,
    ck_path: str,
    ck: dict,
    cancel_token=None,
    log_func=print,
    on_progress=None,
    sweep_windows: list[tuple[int, int, int]] | None = None,
    max_windows_per_run: int | None = None,
    test_limit_mode: bool = False,
):
    inferred = ck.get("inferred_map", ck.get("inferred", {})) or {}
    remaining = set(target_ids) - set(inferred.keys())
    if sweep_windows is None:
        sweep_windows = []
        high = int(start_low + window_width)
        while high <= int(max_high):
            low = high - int(window_width)
            sweep_windows.append((low, high, int(step)))
            high += int(step)
    window_cursor = int(ck.get("next_window_index", ck.get("window_idx", 0)))
    window_idx = int(ck.get("window_idx", ck.get("last_successful_window_idx", 0)))
    consecutive_get_failures = int(ck.get("consecutive_get_failures", 0))
    consecutive_timeout_windows = int(ck.get("consecutive_timeout_windows", 0))
    profile_generation = int(ck.get("profile_generation", 0))
    profile_rotations = int(ck.get("profile_rotations", 0))
    session_health = BrowserSessionHealth(module_name="Module2")
    session_health.rotations_count = profile_rotations
    recovery_policy = RecoveryPolicy(
        goto_failure_threshold=max(
            int(getattr(config, "BROWSER_CONSECUTIVE_GOTO_FAILURE_ROTATION_THRESHOLD", 3)),
            int(getattr(config, "MODULE2_MIN_WINDOWS_BEFORE_SESSION_RECOVERY", 5)),
        ),
        chrome_error_threshold=max(
            int(getattr(config, "BROWSER_CONSECUTIVE_CHROME_ERROR_ROTATION_THRESHOLD", 3)),
            int(getattr(config, "MODULE2_MIN_WINDOWS_BEFORE_SESSION_RECOVERY", 5)),
        ),
        zero_success_hard_failure_threshold=max(
            int(getattr(config, "BROWSER_ZERO_SUCCESS_HARD_FAILURE_THRESHOLD", 3)),
            int(getattr(config, "MODULE2_MIN_WINDOWS_BEFORE_SESSION_RECOVERY", 5)),
        ),
        min_attempted_urls_before_rotation=int(getattr(config, "MODULE2_MIN_WINDOWS_BEFORE_SESSION_RECOVERY", 5)),
    )

    effective_max_windows = int(config.MODULE2_MAX_WINDOWS_PER_RUN if max_windows_per_run is None else max_windows_per_run)
    checked_this_run = 0
    session_failure_windows = int(ck.get("session_failure_windows", 0) or 0)
    log_func(f"Resume: inferred={len(inferred)} remaining={len(remaining)} window_cursor={window_cursor} window_idx={window_idx}")

    while window_cursor < len(sweep_windows) and remaining and (effective_max_windows <= 0 or checked_this_run < effective_max_windows):
        if getattr(cancel_token, "is_set", lambda: False)():
            log_func("Cancel requested in module2.")
            return inferred, driver, "cancelled"
        low, high, current_step = sweep_windows[window_cursor]
        window_cursor += 1
        checked_this_run += 1
        window_idx += 1

        ck["inferred_map"] = inferred
        ck["remaining_ids"] = sorted(remaining)
        ck["window_idx"] = window_idx
        ck["next_window_index"] = window_cursor
        ck["next_low"] = high
        ck["current_window_low"] = low
        ck["current_window_high"] = high
        ck["profile_generation"] = profile_generation
        ck["current_profile_dir"] = ck.get("current_profile_dir") or config.get_effective_browser_profile_dir("module2")
        ck["profile_rotations"] = profile_rotations
        ck["updated_at"] = datetime.now().isoformat()
        ck["consecutive_get_failures"] = consecutive_get_failures
        ck["consecutive_timeout_windows"] = consecutive_timeout_windows
        ck["session_failure_windows"] = session_failure_windows
        save_checkpoint(ck_path, ck)

        prev_url = None
        window_seen_ids = set()
        window_timed_out = False
        detected_max_pages = None  # از pagination استخراج می‌کنیم

        for page in range(1, max_pages_per_window + 1):
            if getattr(cancel_token, "is_set", lambda: False)():
                log_func("Cancel requested in module2 page loop.")
                return inferred, driver, "cancelled"
            if detected_max_pages is not None and page > detected_max_pages:
                break

            url = build_between_url(base_list_url, low, high, page=page)
            log_func(f"Window {window_idx}: between-{low}-{high} | page {page} | remaining={len(remaining)}")
            if on_progress:
                on_progress(
                    "module2_progress",
                    {"window": window_idx, "page": page, "remaining": len(remaining), "range_low": low, "range_high": high},
                )

            driver, ok, err = get_with_retries(driver, url, tries=2)
            session_health.record_navigation(url, ok, err, str(getattr(driver, "current_url", "") or ""))
            if not ok:
                consecutive_get_failures += 1

                if err and is_internet_disconnected(err):
                    ck["inferred"] = inferred
                    ck["window_idx"] = window_idx
                    ck["next_high"] = high
                    ck["consecutive_get_failures"] = consecutive_get_failures
                    ck["stopped_reason"] = "retry_wait_network_interrupted"
                    ck["browser_recovery_action"] = ck.get("browser_recovery_action")
                    save_checkpoint(ck_path, ck)
                    log_func("Network interrupted. Checkpoint saved; rerun to resume.")
                    return inferred, driver, "retry_wait_network_interrupted"

                log_func("   -> GET failed/timeout (renderer).")
                log_session_health(session_health, url_type="price_window", page_state="navigation_failed", action="retry_same_profile", log_func=log_func)
                if recovery_policy.should_retry_same_profile(session_health):
                    session_health.record_same_url_retry(err or "module2_navigation_failed")
                    continue
                if recovery_policy.should_rotate(session_health):
                    log_func("   -> Repeated GET failures. Triggering shared browser/profile recovery ...")
                    try:
                        driver, new_rotations, profile_dir, recovery_status = recover_browser_after_429(
                            driver=driver,
                            current_profile_dir=ck.get("current_profile_dir") or config.get_effective_browser_profile_dir("module2"),
                            build_driver_func=build_driver,
                            rotations_used=int(ck.get("profile_rotations", 0)),
                            max_rotations=config.MODULE2_MAX_PROFILE_ROTATIONS_PER_RUN,
                            reason="module2_navigation_or_page_state",
                            log_func=log_func,
                        )
                        ck["profile_rotations"] = new_rotations
                        ck["current_profile_dir"] = profile_dir
                        ck["browser_recovery_action"] = recovery_status
                        save_checkpoint(ck_path, ck)
                        if recovery_status != "recovered":
                            return inferred, driver, "interrupted_checkpoint_saved"
                    except Exception as recovery_exc:
                        ck["browser_recovery_action"] = f"failed:{recovery_exc}"
                        save_checkpoint(ck_path, ck)
                        return inferred, driver, "interrupted_checkpoint_saved"
                    consecutive_get_failures = 0
                    session_health.record_rotation("module2_navigation_or_page_state")
                else:
                    ck["stopped_reason"] = "retry_wait_browser_recovery"
                    ck["browser_recovery_action"] = "retry_wait"
                    save_checkpoint(ck_path, ck)
                    return inferred, driver, "retry_wait_browser_recovery"
                continue

            consecutive_get_failures = 0

            # ✅ اگر ریدایرکت شد (list-2 خواستی ولی برگشت list-1) قطع paging
            actual_page = _parse_list_page(driver.current_url or "")
            if actual_page is not None and page > 1 and actual_page != page:
                log_func(f"   -> Redirected to list-{actual_page} while requesting list-{page}. No more pages.")
                break

            # ✅ URL تکراری => پایان paging
            if prev_url and (driver.current_url == prev_url):
                log_func("   -> Same URL as previous page. Stop paging this window.")
                break
            prev_url = driver.current_url

            try:
                WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.CSS_SELECTOR, "body")))
            except Exception:
                pass
            state_result, cards = wait_for_search_page_state(driver, timeout=wait_timeout, min_cards=1)
            log_func(
                "   Module2 page_state={state} cards_found={cards} network_reason={network} block_reason={reason} "
                "no_results_detected={no_results} current_url={url} html_length={html_len} body_text_length={body_len}".format(
                    state=state_result.state,
                    cards=state_result.cards_count,
                    network=state_result.network_reason,
                    reason=state_result.reason,
                    no_results=state_result.is_no_results,
                    url=state_result.current_url,
                    html_len=state_result.html_length,
                    body_len=state_result.body_text_length,
                )
            )
            state_result, cards = same_session_kpsdk_recheck(
                driver=driver,
                url=url,
                wait_func=wait_for_search_page_state,
                safe_get_func=_same_driver_get,
                log_func=log_func,
                module_name="Module2",
                timeout=wait_timeout,
                min_cards=1,
                initial_result=state_result,
                initial_payload=cards,
            )
            session_health.record_page_state(state_result)
            trusted_window = state_result.state in {PageState.LISTINGS, PageState.NO_RESULTS}
            log_func(f"   Module2 trusted_window={trusted_window} state={state_result.state}")
            if trusted_window:
                log_session_health(session_health, url_type="price_window", page_state=state_result.state, action="success", log_func=log_func)
            if state_result.is_blocked or state_result.state == PageState.CHROME_ERROR:
                retry_page1_after_rotate = page == 1 and int(ck.get("last_429_page1_window_idx", -1)) != window_idx
                if page == 1:
                    ck["last_429_page1_window_idx"] = window_idx
                ck["stopped_reason"] = state_result.state
                ck["updated_at"] = datetime.now().isoformat()
                ck["last_successful_url"] = driver.current_url
                ck["last_successful_window_idx"] = window_idx
                ck["last_successful_page"] = max(0, page - 1)
                ck["inferred_map"] = inferred
                ck["remaining_ids"] = sorted(remaining)
                ck["page_state"] = state_result.state
                ck["network_reason"] = state_result.network_reason
                ck["trusted_window"] = False
                save_checkpoint(ck_path, ck)
                action = "retry_same_profile"
                should_same_url_retry = state_result.state == PageState.CHROME_ERROR
                if should_same_url_retry and recovery_policy.should_retry_same_profile(session_health):
                    session_health.record_same_url_retry(state_result.reason or state_result.state)
                    log_session_health(session_health, url_type="price_window", page_state=state_result.state, action=action, log_func=log_func)
                    driver, ok_retry, err_retry = get_with_retries(driver, url, tries=1)
                    session_health.record_navigation(url, ok_retry, err_retry, str(getattr(driver, "current_url", "") or ""))
                    if ok_retry:
                        try:
                            WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.CSS_SELECTOR, "body")))
                        except Exception:
                            pass
                        state_result, cards = wait_for_search_page_state(driver, timeout=wait_timeout, min_cards=1)
                        session_health.record_page_state(state_result)
                        if state_result.state in {PageState.LISTINGS, PageState.NO_RESULTS}:
                            log_session_health(session_health, url_type="price_window", page_state=state_result.state, action="success", log_func=log_func)
                            trusted_window = True
                        else:
                            trusted_window = False
                    else:
                        trusted_window = False
                    if not trusted_window:
                        window_timed_out = True
                        break
                if trusted_window:
                    pass
                elif not recovery_policy.should_rotate(session_health):
                    log_session_health(session_health, url_type="price_window", page_state=state_result.state, action="retry_wait", log_func=log_func)
                    window_timed_out = True
                    session_failure_windows += 1
                    ck["session_failure_windows"] = session_failure_windows
                    break
                else:
                    log_session_health(session_health, url_type="price_window", page_state=state_result.state, action="retry_wait", log_func=log_func)
                    return inferred, driver, "retry_wait_browser_recovery"
            if state_result.state == PageState.NO_RESULTS:
                log_func("   -> No results page. Skip remaining pages of this window.")
                break
            if state_result.state != PageState.LISTINGS:
                ck["stopped_reason"] = state_result.state if state_result.state != PageState.UNKNOWN else "render_timeout"
                ck["page_state"] = state_result.state
                ck["network_reason"] = state_result.network_reason
                ck["trusted_window"] = False
                ck["updated_at"] = datetime.now().isoformat()
                save_checkpoint(ck_path, ck)
                if page == 1:
                    log_func("   -> Render timeout/no usable search content on page 1. Saving checkpoint for retry.")
                    window_timed_out = True
                    return inferred, driver, ck["stopped_reason"]
                log_func("   -> Render timeout/no usable search content on page > 1. Stop paging this window.")
                break

            # ✅ روی page1 تعداد صفحات را استخراج کن تا page اضافی نزنیم
            if page == 1:
                detected = get_max_pages_from_pagination(driver)
                if detected is not None:
                    detected_max_pages = max(1, min(detected, max_pages_per_window))
                    # print(f"   ↳ Detected max pages: {detected_max_pages}")

            # ✅ اینجا Timeout نباید کرش کند
            try:
                status, payload = wait_for_cards_or_no_results(driver, timeout=wait_timeout, min_cards=1)
            except TimeoutException:
                # اگر page1 تایم‌اوت شد، این window را skip کن
                if page == 1:
                    log_func("   -> Timeout waiting for cards/no-results on page 1. Skipping this window.")
                    window_timed_out = True
                    break
                # اگر page>1 تایم‌اوت شد، paging را قطع کن
                log_func("   -> Timeout on page > 1. Stop paging this window.")
                break

            if status == "no_results":
                log_func("   -> No results page. Skip remaining pages of this window.")
                break

            cards = payload
            ck["last_successful_url"] = driver.current_url
            ck["last_successful_window_idx"] = window_idx
            ck["last_successful_page"] = page
            ids_in_page = extract_listing_ids_from_cards(cards)

            # ✅ اگر صفحه جدید هیچ listing جدیدی نداشت => ادامه نده
            new_ids = ids_in_page - window_seen_ids
            if page > 1 and not new_ids:
                log_func("   -> No new listings on this page. Stop paging this window.")
                break
            window_seen_ids.update(ids_in_page)

            hit = ids_in_page.intersection(remaining)
            if hit:
                for lid in hit:
                    inferred[lid] = {
                        "low": low,
                        "high": high,
                        "window_low": low,
                        "window_high": high,
                        "found_at_page": page,
                    }
                remaining.difference_update(hit)

                ck["inferred_map"] = inferred
                ck["remaining_ids"] = sorted(remaining)
                ck["window_idx"] = window_idx
                ck["next_window_index"] = window_cursor
                ck["next_low"] = high
                ck["current_window_low"] = low
                ck["current_window_high"] = high
                ck["updated_at"] = datetime.now().isoformat()
                ck["consecutive_get_failures"] = consecutive_get_failures
                save_checkpoint(ck_path, ck)
            else:
                ck["inferred_map"] = inferred
                ck["remaining_ids"] = sorted(remaining)
                ck["updated_at"] = datetime.now().isoformat()
                save_checkpoint(ck_path, ck)

            # ✅ اگر pagination می‌گوید فقط ۱ صفحه است، همینجا قطع
            if detected_max_pages is not None and page >= detected_max_pages:
                break

            # ✅ next واقعی نیست => stop
            if len(cards) < 20 and not has_next_results_page(driver, page):
                log_func("No next page detected. Stop paging this window.")
                break
            if page < max_pages_per_window and not has_next_results_page(driver, page):
                log_func("No next page detected. Stop paging this window.")
                break
            time.sleep(random.uniform(config.MODULE2_SLEEP_BETWEEN_PAGES_MIN, config.MODULE2_SLEEP_BETWEEN_PAGES_MAX))

        log_func(f"Remaining after window: {len(remaining)}")
        if window_timed_out:
            consecutive_timeout_windows += 1
        else:
            consecutive_timeout_windows = 0
        if consecutive_timeout_windows >= config.MODULE2_MAX_CONSECUTIVE_TIMEOUT_WINDOWS:
            ck["stopped_reason"] = "timeout_limit"
            ck["updated_at"] = datetime.now().isoformat()
            ck["inferred_map"] = inferred
            ck["remaining_ids"] = sorted(remaining)
            save_checkpoint(ck_path, ck)
            return inferred, driver, "timeout_limit"

        ck["inferred_map"] = inferred
        ck["remaining_ids"] = sorted(remaining)
        ck["window_idx"] = window_idx
        ck["next_window_index"] = window_cursor
        ck["next_low"] = high
        ck["updated_at"] = datetime.now().isoformat()
        ck["consecutive_get_failures"] = consecutive_get_failures
        ck["consecutive_timeout_windows"] = consecutive_timeout_windows
        ck["session_failure_windows"] = session_failure_windows
        save_checkpoint(ck_path, ck)
        time.sleep(random.uniform(config.MODULE2_SLEEP_BETWEEN_WINDOWS_MIN, config.MODULE2_SLEEP_BETWEEN_WINDOWS_MAX))

    if session_failure_windows and checked_this_run > 0 and session_failure_windows >= checked_this_run and not inferred:
        ck["stopped_reason"] = "retry_wait_browser_recovery"
        ck["updated_at"] = datetime.now().isoformat()
        ck["inferred_map"] = inferred
        ck["remaining_ids"] = sorted(remaining)
        ck["session_failure_windows"] = session_failure_windows
        save_checkpoint(ck_path, ck)
        return inferred, driver, "retry_wait_browser_recovery"

    if test_limit_mode and remaining and effective_max_windows > 0 and checked_this_run >= effective_max_windows and window_cursor < len(sweep_windows):
        ck["stopped_reason"] = "max_windows_test_limit"
        ck["updated_at"] = datetime.now().isoformat()
        ck["inferred_map"] = inferred
        ck["remaining_ids"] = sorted(remaining)
        ck["consecutive_get_failures"] = consecutive_get_failures
        ck["consecutive_timeout_windows"] = consecutive_timeout_windows
        ck["session_failure_windows"] = session_failure_windows
        save_checkpoint(ck_path, ck)
        return inferred, driver, "max_windows_test_limit"

    return inferred, driver, "done"


# =========================
# Runner
# =========================
def _missing_listing_debug(rows: list[dict], missing_ids: set[str] | list[str]) -> dict:
    wanted = {str(value).strip() for value in missing_ids if str(value).strip()}
    missing_rows = []
    for row in rows:
        lid = str(row.get("listing_id") or row.get("external_id") or row.get("ExternalID") or "").strip()
        if lid in wanted:
            missing_rows.append(row)

    def unique_values(*keys: str) -> list[str]:
        values = []
        seen = set()
        for row in missing_rows:
            for key in keys:
                value = str(row.get(key) or "").strip()
                if value and value not in seen:
                    seen.add(value)
                    values.append(value)
                    break
        return values

    return {
        "missing_external_ids": unique_values("external_id", "ExternalID", "listing_id"),
        "missing_current_price_display": unique_values("price", "price_display", "CurrentPriceDisplay", "current_price_display"),
        "missing_listing_urls": unique_values("url", "listing_url", "ListingURL"),
        "missing_addresses": unique_values("address", "Address", "PropertyAddress", "property_address"),
    }


def _target_listing_debug(rows: list[dict], target_ids: set[str] | list[str]) -> dict:
    debug = _missing_listing_debug(rows, target_ids)
    return {
        "target_external_ids": debug.get("missing_external_ids", []),
        "target_current_price_display": debug.get("missing_current_price_display", []),
        "target_listing_urls": debug.get("missing_listing_urls", []),
        "target_addresses": debug.get("missing_addresses", []),
        **debug,
    }


def _looks_like_chrome_profile_lock(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(
        needle in text
        for needle in (
            "user data directory is already in use",
            "profile appears to be in use",
            "access is denied",
            "permission denied",
            "cannot create default profile directory",
            "failed to create data directory",
        )
    )


def _module2_job_profile_dir(out_dir: str, checkpoint_search_id: int | None) -> str:
    search_label = f"search_{int(checkpoint_search_id)}" if checkpoint_search_id is not None else "manual"
    root = os.path.join(out_dir, "module2_chrome_profiles")
    os.makedirs(root, exist_ok=True)
    return os.path.join(root, f"{search_label}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}")


def module2_run(
    base_list_url: str,
    input_file: str | None = None,
    out_dir: str = "output",
    window_width: int = 200_000,
    step: int = 50_000,
    max_high: int | None = None,
    max_pages_per_window: int = 5,
    only_overwrite_na: bool = True,
    smart_start: bool = True,
    sweep_mode: str = "smart_refresh",
    low_anchor: int | float | None = None,
    high_anchor: int | float | None = None,
    checkpoint_search_id: int | None = None,
    target_mode: str = "missing_only",
    target_listing_ids: set[str] | list[str] | tuple[str, ...] | None = None,
    preserve_existing_price_display: bool = True,
    cancel_token=None,
    on_log=None,
    on_progress=None,
    test_max_windows: int | None = None,
    checkpoint_path_override: str | None = None,
    resume_checkpoint: bool = True,
):
    def log(msg: str) -> None:
        print(msg)
        if on_log:
            try:
                on_log(msg)
            except Exception:
                pass

    module2_run.last_result = {}

    min_low = int(config.MODULE2_MIN_LOW)
    if max_high is None:
        max_high = config.MODULE2_MAX_HIGH
    else:
        max_high = int(max_high)

    os.makedirs(out_dir, exist_ok=True)

    if not input_file:
        input_file = find_latest_module1_output(out_dir)

    input_file = os.path.abspath(input_file)
    log(f"📥 Input: {input_file}")
    log(f"📁 CWD: {os.getcwd()}")

    rows = read_rows(input_file)

    normalized_target_mode = str(target_mode or "missing_only").strip().lower()
    if normalized_target_mode not in {"missing_only", "all"}:
        raise ValueError("target_mode must be 'missing_only' or 'all'")

    row_ids = {
        str(r.get("listing_id") or r.get("external_id") or r.get("ExternalID") or "").strip()
        for r in rows
        if str(r.get("listing_id") or r.get("external_id") or r.get("ExternalID") or "").strip()
    }
    if target_listing_ids is not None:
        target_ids = {str(value).strip() for value in target_listing_ids if str(value).strip()}
    elif normalized_target_mode == "all":
        target_ids = set(row_ids)
    else:
        target_ids = set()
        for r in rows:
            lid = str(r.get("listing_id") or r.get("external_id") or r.get("ExternalID") or "").strip()
            price = str(r.get("price") or r.get("price_display") or r.get("CurrentPriceDisplay") or "").strip()
            if lid and price_needs_inference(price):
                target_ids.add(lid)

    log(f"Rows: {len(rows)} | Target price inference count: {len(target_ids)} | target_mode={normalized_target_mode}")

    if not target_ids:
        out_csv, out_json = write_outputs(rows, out_dir=out_dir)
        log("ℹ️ No target rows due for price inference. Saved copy anyway:")
        log(f" - {out_csv}")
        log(f" - {out_json}")
        if on_progress:
            on_progress("module2_skipped", {"reason": "no_due_targets", "rows": len(rows), "target_mode": normalized_target_mode})
        module2_run.last_result = json_safe({
            "status": "skipped_no_targets",
            "target_mode": normalized_target_mode,
            "target_count": 0,
            "rows": len(rows),
        })
        return out_csv, out_json

    plan = build_sweep_plan(sweep_mode, window_width, max_high, low_anchor=low_anchor, high_anchor=high_anchor, min_low=min_low)
    start_low = int(plan["start_low"])
    end_high = int(plan["end_high"])
    sweep_windows = plan["windows"]
    sorted_target_listing_ids = sorted(target_ids)
    window_plan_hash = hashlib.sha256(json.dumps(json_safe(sweep_windows), sort_keys=True).encode("utf-8")).hexdigest()
    log(f"🚀 Sweep mode: {plan['sweep_mode']} start_low={start_low} end_high={end_high} windows={len(sweep_windows)}")

    params = {
        "window_width": window_width,
        "step": step,
        "min_low": min_low,
        "max_high": max_high,
        "max_pages_per_window": max_pages_per_window,
        "wait_timeout": 25,
        "sweep_mode": plan["sweep_mode"],
        "low_anchor": low_anchor,
        "high_anchor": high_anchor,
        "target_mode": normalized_target_mode,
        "start_low": start_low,
        "end_high": end_high,
        "step_profile": plan["step_profile"],
        "search_id": checkpoint_search_id,
        "target_listing_ids": sorted_target_listing_ids,
        "window_plan_hash": window_plan_hash,
        "module2_max_high": max_high,
    }

    ck_path = checkpoint_path_override or checkpoint_path(out_dir, base_list_url)
    ck = load_checkpoint(ck_path) if resume_checkpoint else None

    if ck and not checkpoint_is_compatible(ck, base_list_url, input_file, params, target_ids):
        log("Checkpoint exists but is not compatible with current input/parameters. Starting fresh.")
        ck = None

    if not ck:
        ck = {
            "version": 1,
            "base_list_url": base_list_url,
            "input_file": input_file,
            "checkpoint_identity": params,
            "params": params,
            "target_ids": sorted_target_listing_ids,
            "missing_ids": sorted_target_listing_ids,
            "inferred_map": {},
            "remaining_ids": sorted_target_listing_ids,
            "window_idx": 0,
            "next_low": sweep_windows[0][1] if sweep_windows else start_low + window_width,
            "next_window_index": 0,
            "consecutive_get_failures": 0,
            "consecutive_timeout_windows": 0,
            "profile_generation": 0,
            "current_profile_dir": config.get_effective_browser_profile_dir("module2"),
            "profile_rotations": 0,
            "browser_recovery_action": None,
            "stopped_reason": "",
        }
        save_checkpoint(ck_path, ck)

    checkpoint_resumed = bool(ck and int(ck.get("window_idx", 0) or 0) > 0)
    driver = None
    try:
        profile_dir_current = ck.get("current_profile_dir") or config.get_effective_browser_profile_dir("module2")
        try:
            driver = build_driver(profile_dir_override=profile_dir_current)
        except Exception as driver_exc:
            if _looks_like_chrome_profile_lock(driver_exc):
                fallback_profile_dir = _module2_job_profile_dir(out_dir, checkpoint_search_id)
                log(f"Chrome profile unavailable; retrying once with isolated profile: {fallback_profile_dir}")
                ck["browser_recovery_action"] = f"profile_lock_retry:{driver_exc}"
                ck["current_profile_dir"] = fallback_profile_dir
                save_checkpoint(ck_path, ck)
                try:
                    driver = build_driver(profile_dir_override=fallback_profile_dir)
                    profile_dir_current = fallback_profile_dir
                except Exception as retry_exc:
                    ck["stopped_reason"] = "interrupted_checkpoint_saved"
                    ck["browser_recovery_action"] = f"profile_lock_retry_failed:{retry_exc}"
                    save_checkpoint(ck_path, ck)
                    module2_run.last_result = json_safe({
                        "sweep_mode": plan["sweep_mode"],
                        "min_low": min_low,
                        "max_high": max_high,
                        "start_low": start_low,
                        "end_high": end_high,
                        "step_profile": plan["step_profile"],
                        "status": "interrupted_checkpoint_saved",
                        "target_mode": normalized_target_mode,
                        "target_count": len(target_ids),
                        "remaining_count": len(target_ids),
                        "missing_listing_ids": sorted(target_ids),
                        "target_listing_ids": sorted(target_ids),
                        "checkpoint_resumed": checkpoint_resumed,
                        "browser_profile_used": fallback_profile_dir,
                        "browser_recovery_action": ck.get("browser_recovery_action"),
                        **_target_listing_debug(rows, target_ids),
                    })
                    return None, None
            else:
                ck["stopped_reason"] = "interrupted_checkpoint_saved"
                ck["browser_recovery_action"] = f"initial_driver_failed:{driver_exc}"
                save_checkpoint(ck_path, ck)
                module2_run.last_result = json_safe({
                    "sweep_mode": plan["sweep_mode"],
                    "min_low": min_low,
                    "max_high": max_high,
                    "start_low": start_low,
                    "end_high": end_high,
                    "step_profile": plan["step_profile"],
                    "status": "interrupted_checkpoint_saved",
                    "target_mode": normalized_target_mode,
                    "target_count": len(target_ids),
                    "remaining_count": len(target_ids),
                    "missing_listing_ids": sorted(target_ids),
                    "target_listing_ids": sorted(target_ids),
                    "checkpoint_resumed": checkpoint_resumed,
                    "browser_profile_used": profile_dir_current,
                    "browser_recovery_action": ck.get("browser_recovery_action"),
                    **_target_listing_debug(rows, target_ids),
                })
                return None, None

        while True:
            inferred, driver, status = infer_prices_window_based_with_checkpoint(
                driver=driver,
                base_list_url=base_list_url,
                target_ids=target_ids,
                window_width=window_width,
                step=step,
                start_low=start_low,
                max_high=max_high,
                max_pages_per_window=max_pages_per_window,
                wait_timeout=25,
                ck_path=ck_path,
                ck=ck,
                cancel_token=cancel_token,
                log_func=log,
                on_progress=on_progress,
                sweep_windows=sweep_windows,
                max_windows_per_run=int(test_max_windows) if test_max_windows is not None else (0 if plan["sweep_mode"] in {"setup_full_sweep", "fallback_full_for_missing"} else None),
                test_limit_mode=test_max_windows is not None,
            )
            if not str(status).startswith("429"):
                break
            log("HTTP 429 detected. Saving checkpoint, rotating profile, and resuming safely.")
            write_outputs(rows, out_dir=out_dir)
            try:
                driver.quit()
            except Exception:
                pass
            cleanup_chrome_driver(driver)
            driver = None
            rotations = int(ck.get("profile_rotations", 0))
            if (not config.MODULE2_ROTATE_PROFILE_ON_429):
                ck["stopped_reason"] = "interrupted_checkpoint_saved"
                save_checkpoint(ck_path, ck)
                module2_run.last_result = json_safe({"sweep_mode": plan["sweep_mode"], "min_low": min_low, "max_high": max_high, "start_low": start_low, "end_high": end_high, "step_profile": plan["step_profile"], "status": "429_retry_wait", "target_mode": normalized_target_mode, "target_count": len(target_ids), "remaining_count": len(target_ids), "missing_listing_ids": sorted(target_ids), "target_listing_ids": sorted(target_ids), "checkpoint_resumed": checkpoint_resumed, "browser_profile_used": ck.get("current_profile_dir"), "browser_recovery_action": "429_rotation_disabled", **_target_listing_debug(rows, target_ids)})
                return None, None
            driver, new_rotations, profile_dir, recovery_status = recover_browser_after_429(
                driver=driver,
                current_profile_dir=ck.get("current_profile_dir") or config.get_effective_browser_profile_dir("module2"),
                build_driver_func=build_driver,
                rotations_used=rotations,
                max_rotations=config.MODULE2_MAX_PROFILE_ROTATIONS_PER_RUN,
                reason="module2_blocked_page_state",
                log_func=log,
            )
            if recovery_status != "recovered":
                ck["stopped_reason"] = "interrupted_checkpoint_saved"
                save_checkpoint(ck_path, ck)
                write_outputs(rows, out_dir=out_dir)
                module2_run.last_result = json_safe({"sweep_mode": plan["sweep_mode"], "min_low": min_low, "max_high": max_high, "start_low": start_low, "end_high": end_high, "step_profile": plan["step_profile"], "status": "429_retry_wait", "target_mode": normalized_target_mode, "target_count": len(target_ids), "remaining_count": len(target_ids), "missing_listing_ids": sorted(target_ids), "target_listing_ids": sorted(target_ids), "checkpoint_resumed": checkpoint_resumed, "browser_profile_used": ck.get("current_profile_dir"), "browser_recovery_action": recovery_status, **_target_listing_debug(rows, target_ids)})
                return None, None
            ck["profile_rotations"] = new_rotations
            ck["profile_generation"] = int(ck.get("profile_generation", 0)) + 1
            ck["current_profile_dir"] = profile_dir
            save_checkpoint(ck_path, ck)
            if status == "429":
                ck["stopped_reason"] = "interrupted_checkpoint_saved"
                save_checkpoint(ck_path, ck)
                module2_run.last_result = json_safe({"sweep_mode": plan["sweep_mode"], "min_low": min_low, "max_high": max_high, "start_low": start_low, "end_high": end_high, "step_profile": plan["step_profile"], "status": "429_retry_wait", "target_mode": normalized_target_mode, "target_count": len(target_ids), "remaining_count": len(target_ids), "missing_listing_ids": sorted(target_ids), "target_listing_ids": sorted(target_ids), "checkpoint_resumed": checkpoint_resumed, "browser_profile_used": ck.get("current_profile_dir"), "browser_recovery_action": "429", **_target_listing_debug(rows, target_ids)})
                return None, None
        remaining_after = sorted(set(target_ids) - set(inferred.keys()))
        windows_checked = int(ck.get("window_idx", 0))
        stopped_early = not remaining_after
        skipped_reason = "skipped_no_range_after_full_sweep" if remaining_after and plan["sweep_mode"] in {"setup_full_sweep", "fallback_full_for_missing"} and int(ck.get("next_window_index", 0)) >= len(sweep_windows) else None
        module2_run.last_result = json_safe({
            "sweep_mode": plan["sweep_mode"],
            "min_low": min_low,
            "max_high": max_high,
            "low_anchor": low_anchor,
            "high_anchor": high_anchor,
            "start_low": start_low,
            "end_high": end_high,
            "step_profile": plan["step_profile"],
            "windows_checked": windows_checked,
            "checkpoint_path": ck_path,
            "target_mode": normalized_target_mode,
            "target_count": len(target_ids),
            "remaining_count": len(remaining_after),
            "missing_listing_ids": remaining_after,
            "target_listing_ids": sorted(target_ids),
            "skipped_reason": skipped_reason,
            "used_fallback_full_sweep": plan["sweep_mode"] == "fallback_full_for_missing",
            "stopped_early_all_targets_found": stopped_early,
            "status": status,
            "checkpoint_resumed": checkpoint_resumed,
            "stopped_reason": ck.get("stopped_reason"),
            "browser_profile_used": ck.get("current_profile_dir"),
            "browser_recovery_action": ck.get("browser_recovery_action"),
            **_target_listing_debug(rows, remaining_after),
        })
        log(f"Inferred so far: {len(inferred)} / {len(target_ids)} | remaining={len(remaining_after)}")

        if status == "max_windows_test_limit":
            module2_run.last_result["status"] = "partial_test_limit"
            module2_run.last_result["stopped_reason"] = "max_windows_test_limit"
        elif status in {"retry_wait_network_interrupted", "retry_wait_browser_recovery", "interrupted_checkpoint_saved", "timeout_limit", "render_timeout", "blank_render", "unknown", "chrome_error"}:
            module2_run.last_result["status"] = "interrupted_checkpoint_saved" if status == "timeout_limit" else status
            module2_run.last_result["skipped_reason"] = None
            return None, None

        for r in rows:
            lid = str(r.get("listing_id") or r.get("external_id") or r.get("ExternalID") or "").strip()
            if not lid or lid not in inferred:
                continue

            info = inferred[lid]
            low = info["low"]
            high = info["high"]

            r["price_original"] = r.get("price")
            r["price_inferred_display"] = format_range_price(low, high)
            if not preserve_existing_price_display and price_needs_inference(r.get("price")):
                r["price"] = r["price_inferred_display"]

            r["price_inferred_low"] = low
            r["price_inferred_high"] = high
            r["price_inferred_method"] = "sliding_between_window"
            r["price_inferred_window_low"] = info["window_low"]
            r["price_inferred_window_high"] = info["window_high"]
            r["price_inferred_found_at_page"] = info["found_at_page"]
            r["InferredPriceLow"] = low
            r["InferredPriceHigh"] = high
            r["InferredPriceRange"] = r["price_inferred_display"]
            r["PriceInferenceStatus"] = "completed"
            r["PriceInferenceLastError"] = None
            r["PriceInferenceLastAttemptAt"] = datetime.now().isoformat()

        out_csv, out_json = write_outputs(rows, out_dir=out_dir)
        log("\nSaved:")
        log(f" - {out_csv}")
        log(f" - {out_json}")

        try:
            os.replace(ck_path, ck_path + ".done")
        except Exception:
            pass

        return out_csv, out_json

    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
            cleanup_chrome_driver(driver)
        driver = None
        gc.collect()


module2_run.last_result = {}

if __name__ == "__main__":
    BASE_LIST_URL = AREA_SEARCH_URL
    INPUT_FILE = None

    module2_run(
        base_list_url=BASE_LIST_URL,
        input_file=INPUT_FILE,
        out_dir="output",
        window_width=200_000,
        step=50_000,
        max_high=config.MODULE2_MAX_HIGH,
        max_pages_per_window=5,
        only_overwrite_na=True,
        smart_start=True,
    )
