import csv
import json
import os
import re
import time
import gc
import hashlib
import html
from glob import glob
from datetime import datetime
from typing import Optional, Dict, Any, List, Tuple
from urllib.parse import urlparse
from bs4 import BeautifulSoup

from cloak_browser_helper import By, EC, WebDriverWait, TimeoutException, WebDriverException

from config import AREA_SEARCH_URL
import config
from browser_recovery import BrowserSessionHealth, RecoveryPolicy, is_429_page, is_retryable_navigation_error, log_session_health, recover_browser_for_untrusted_state as recover_browser_after_429, same_session_kpsdk_recheck, safe_realestate_get_with_reset, safe_driver_get, UNTRUSTED_RECOVERY_STATES
from realestate_errors import RealEstateBlockedError
from realestate_page_state import PageState, classify_detail_page, wait_for_detail_page_state
from area_parser import extract_area_display, parse_area_to_sqm

def _verbose_page_state_enabled() -> bool:
    return bool(getattr(config, "SCRAPER_VERBOSE_PAGE_STATE", False) or str(getattr(config, "SCRAPER_LOG_LEVEL", "INFO")).upper() == "DEBUG")


def _should_emit_default_log(message: str) -> bool:
    text = str(message or "")
    noisy_tokens = ("page_state=", "html_length=", "body_text_length=", "session_health ", "same-page settle", "KPSDK same-session recheck")
    if any(token in text for token in noisy_tokens) and not _verbose_page_state_enabled():
        return False
    return True


# -------------------------
# Driver
# -------------------------
def build_driver(profile_dir_override: Optional[str] = None):
    from chrome_options_helper import build_chrome_driver
    return build_chrome_driver(profile_dir_override=profile_dir_override)


def restart_driver(driver):
    from chrome_options_helper import cleanup_chrome_driver
    try:
        driver.quit()
    except Exception:
        pass
    cleanup_chrome_driver(driver)
    time.sleep(0.7)
    return build_driver()


# -------------------------
# GET مقاوم + تشخیص قطع اینترنت
# -------------------------
def is_internet_disconnected(err: Exception) -> bool:
    msg = str(err).lower()
    return any(x in msg for x in [
        "err_internet_disconnected",
        "internet disconnected",
        "err_network_changed",
        "err_connection",
        "net::",
    ])


def get_with_retries(driver, url, tries=2, *, phase: str = "detail", apply_delay: bool = False, log_func=print):
    last_err = None
    for attempt in range(tries):
        try:
            ok, exc = safe_realestate_get_with_reset(
                driver,
                url,
                module_name="Module3",
                phase=phase,
                log_func=log_func,
                apply_delay=apply_delay and attempt == 0,
            )
            try:
                driver._module3_last_navigation = {
                    "url": url,
                    "navigation_failed": not ok,
                    "navigation_error": exc,
                }
            except Exception:
                pass
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
    _driver, ok, err = get_with_retries(driver, url, tries=2, phase="same_url_recheck", apply_delay=False)
    if not ok and err:
        raise err
    return ok


def _module3_current_url(driver, state_result=None) -> str:
    if state_result is not None and getattr(state_result, "current_url", None):
        return str(state_result.current_url or "")
    try:
        return str(getattr(driver, "current_url", "") or "")
    except Exception:
        return ""


def _module3_is_chrome_error_url(value: str | None) -> bool:
    return str(value or "").strip().lower().startswith("chrome-error://chromewebdata")


def _module3_last_navigation(driver) -> dict:
    try:
        value = getattr(driver, "_module3_last_navigation", {}) or {}
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _module3_detail_recovery_reason(driver, detail_state, navigation_info: dict) -> str:
    state = getattr(detail_state, "state", None) or "unknown"
    reason = getattr(detail_state, "reason", None) or state
    if _module3_is_chrome_error_url(_module3_current_url(driver, detail_state)):
        reason = f"chrome_error:{reason}"
    if navigation_info.get("navigation_error") is not None:
        reason = f"navigation_failed:{reason}:{navigation_info.get('navigation_error')}"
    return config.mask_sensitive_text(reason)


def _module3_is_same_page_kpsdk_candidate(driver, requested_url: str, detail_state) -> bool:
    current_url = _module3_current_url(driver, detail_state)
    if _module3_is_chrome_error_url(current_url):
        return False
    current_l = str(current_url or "").lower()
    requested_l = str(requested_url or "").lower()
    if "realestate.com.au" not in current_l:
        return False
    if requested_l and "realestate.com.au" in requested_l:
        current = urlparse(current_l)
        requested = urlparse(requested_l)
        if (current.netloc, current.path) != (requested.netloc, requested.path):
            return False
    state = getattr(detail_state, "state", None)
    network_reason = str(getattr(detail_state, "network_reason", "") or "").lower()
    reason = str(getattr(detail_state, "reason", "") or "").lower()
    is_transient_block = state in {PageState.BLOCKED_KPSDK, PageState.BLOCKED_HTTP_429}
    is_network_429 = "blocked_http_429" in network_reason or "blocked_http_429" in reason
    is_kpsdk = "kpsdk" in network_reason or "kpsdk" in reason
    if not (is_transient_block or is_network_429 or is_kpsdk):
        return False
    html_len = int(getattr(detail_state, "html_length", 0) or 0)
    body_len = int(getattr(detail_state, "body_text_length", 0) or 0)
    return html_len < 5000 or body_len < 250 or is_transient_block


def _module3_same_page_kpsdk_settle(driver, requested_url: str, detail_state, *, log):
    if not _module3_is_same_page_kpsdk_candidate(driver, requested_url, detail_state):
        return detail_state
    log(
        "Module3 transient KPSDK detected; same-page settle start "
        f"state={getattr(detail_state, 'state', None)} current_url={_module3_current_url(driver, detail_state)} "
        f"html_length={getattr(detail_state, 'html_length', 0) or 0} "
        f"body_text_length={getattr(detail_state, 'body_text_length', 0) or 0}"
    )
    settle_seconds = max(0.0, float(getattr(config, "BROWSER_KPSDK_SETTLE_SECONDS", 10)))
    grace_seconds = max(settle_seconds, float(getattr(config, "BROWSER_BLOCK_GRACE_SECONDS", 30)))
    poll_seconds = max(0.05, float(getattr(config, "BROWSER_BLOCK_POLL_SECONDS", 1.0)))
    deadline = time.time() + grace_seconds
    next_sleep = settle_seconds if settle_seconds > 0 else poll_seconds
    previous_html_len = int(getattr(detail_state, "html_length", 0) or 0)
    last = detail_state
    while time.time() <= deadline:
        time.sleep(min(next_sleep, max(0.0, deadline - time.time())))
        current = classify_detail_page(driver, timeout=True)
        log(
            "Module3 same-page settle result state={state} html_length={html_len} body_text_length={body_len} "
            "network_reason={network} current_url={url}".format(
                state=current.state,
                html_len=getattr(current, "html_length", 0) or 0,
                body_len=getattr(current, "body_text_length", 0) or 0,
                network=getattr(current, "network_reason", None),
                url=getattr(current, "current_url", "") or _module3_current_url(driver),
            )
        )
        last = current
        if current.state in {PageState.DETAIL_READY, PageState.DETAIL_REMOVED, PageState.DETAIL_NOT_FOUND, PageState.DETAIL_SOLD}:
            log("Module3 DOM-first detail ready after transient KPSDK; ignoring historical 429")
            return current
        if _module3_is_chrome_error_url(_module3_current_url(driver, current)):
            return current
        html_len = int(getattr(current, "html_length", 0) or 0)
        title = str(getattr(current, "title", "") or "").lower()
        still_loading_rea = "real estate" in title or "realestate.com.au" in title or html_len > previous_html_len
        if not still_loading_rea:
            break
        previous_html_len = max(previous_html_len, html_len)
        next_sleep = poll_seconds
    return last


def _module3_detail_state_is_recoverable(driver, detail_state, navigation_info: dict) -> bool:
    if _module3_is_chrome_error_url(_module3_current_url(driver, detail_state)):
        return True
    state = getattr(detail_state, "state", None)
    if state in UNTRUSTED_RECOVERY_STATES:
        return True
    return bool(navigation_info.get("navigation_failed") and state not in {PageState.DETAIL_READY, PageState.DETAIL_REMOVED, PageState.DETAIL_NOT_FOUND, PageState.DETAIL_SOLD})


def _module3_load_detail_page(driver, url: str, profile_dir_current: str, rotations_used: int, wait_timeout: int, log, *, apply_delay: bool = False):
    """Load one detail URL with DOM-first classification and bounded profile recovery."""
    retry_after = int(getattr(config, "REA_RATE_LIMIT_BACKOFF_SECONDS", 21600))
    max_rotations = min(config.BROWSER_MAX_PROFILE_ROTATIONS_PER_RUN, config.MODULE3_MAX_PROFILE_ROTATIONS_PER_RUN)
    max_attempts = max(1, int(getattr(config, "MODULE3_RETRY_SAME_LISTING_AFTER_429", 1)) + max_rotations + int(getattr(config, "BROWSER_SAME_URL_MAX_RETRIES", 2)) + 1)
    health = BrowserSessionHealth(module_name="Module3")
    policy = RecoveryPolicy()
    last_state = None
    last_error = None
    for attempt in range(1, max_attempts + 1):
        driver, ok, err = get_with_retries(driver, url, tries=1, phase="detail", apply_delay=apply_delay and attempt == 1, log_func=log)
        navigation_info = _module3_last_navigation(driver)
        health.record_navigation(url, ok, err or navigation_info.get("navigation_error"), _module3_current_url(driver))
        last_error = err or navigation_info.get("navigation_error")
        if ok:
            try:
                WebDriverWait(driver, min(5, wait_timeout)).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "body"))
                )
            except Exception:
                pass
            time.sleep(0.35)
            detail_state = wait_for_detail_page_state(driver, timeout=wait_timeout)
        else:
            detail_state = classify_detail_page(driver, timeout=True)
        last_state = detail_state
        health.record_page_state(detail_state)
        if _verbose_page_state_enabled():
            log(
                "Module3 Detail page_state={state} network_reason={network} block_reason={reason} current_url={url} "
                "html_length={html_len} body_text_length={body_len} attempt={attempt}".format(
                    state=detail_state.state,
                    network=detail_state.network_reason,
                    reason=detail_state.reason,
                    url=detail_state.current_url,
                    html_len=detail_state.html_length,
                    body_len=detail_state.body_text_length,
                    attempt=attempt,
                )
            )
        detail_state = _module3_same_page_kpsdk_settle(driver, url, detail_state, log=log)
        detail_state, _ = same_session_kpsdk_recheck(
            driver=driver,
            url=url,
            wait_func=wait_for_detail_page_state,
            safe_get_func=_same_driver_get,
            log_func=log,
            module_name="Module3",
            timeout=wait_timeout,
            min_cards=None,
            initial_result=detail_state,
        )
        last_state = detail_state
        health.record_page_state(detail_state)
        current_url = _module3_current_url(driver, detail_state)
        trusted_detail_url = (not _module3_is_chrome_error_url(current_url)) and "realestate.com.au" in str(current_url).lower()
        if detail_state.state in {PageState.DETAIL_READY, PageState.DETAIL_REMOVED, PageState.DETAIL_NOT_FOUND, PageState.DETAIL_SOLD} and trusted_detail_url:
            log_session_health(health, url_type="detail", page_state=detail_state.state, action="success", log_func=log)
            return driver, profile_dir_current, rotations_used, detail_state, True, None
        if not (config.BROWSER_RECOVERY_ON_429 and _module3_detail_state_is_recoverable(driver, detail_state, navigation_info)):
            return driver, profile_dir_current, rotations_used, detail_state, ok, last_error
        reason = _module3_detail_recovery_reason(driver, detail_state, navigation_info)
        should_same_url_retry = _module3_is_chrome_error_url(_module3_current_url(driver, detail_state)) or bool(navigation_info.get("navigation_failed"))
        if should_same_url_retry and policy.should_retry_same_profile(health):
            health.record_same_url_retry(reason)
            log_session_health(health, url_type="detail", page_state=detail_state.state, action="retry_same_profile", log_func=log)
            continue
        if not policy.should_rotate(health, explicit_trusted_block=detail_state.state == PageState.BLOCKED_ACCESS_DENIED):
            log_session_health(health, url_type="detail", page_state=detail_state.state, action="retry_wait", log_func=log)
            return driver, profile_dir_current, rotations_used, detail_state, False, RealEstateBlockedError(reason, retry_after_seconds=retry_after)
        log_session_health(health, url_type="detail", page_state=detail_state.state, action="rotate_profile", log_func=log)
        log(f"Module3 untrusted detail state={detail_state.state} current_url={_module3_current_url(driver, detail_state)}. Recovering profile/session reason={reason}.")
        driver, rotations_used, profile_dir_current, recovery_status = recover_browser_after_429(
            driver=driver,
            current_profile_dir=profile_dir_current,
            build_driver_func=build_driver,
            rotations_used=rotations_used,
            max_rotations=max_rotations,
            reason=reason,
            log_func=log,
        )
        if recovery_status != "recovered":
            return driver, profile_dir_current, rotations_used, detail_state, False, RealEstateBlockedError(reason, retry_after_seconds=retry_after)
        health.record_rotation(reason)
    reason = _module3_detail_recovery_reason(driver, last_state, _module3_last_navigation(driver)) if last_state is not None else config.mask_sensitive_text(last_error or "module3_detail_retry_limit")
    return driver, profile_dir_current, rotations_used, last_state, False, RealEstateBlockedError(reason, retry_after_seconds=retry_after)


def _module3_retryable_result(reason: Any) -> dict:
    retry_after = int(getattr(config, "REA_RATE_LIMIT_BACKOFF_SECONDS", 21600))
    return {
        "status": "retry_wait_browser_recovery",
        "reason": config.mask_sensitive_text(reason or "module3_retryable_browser_failure"),
        "retry_after_seconds": retry_after,
    }


# -------------------------
# IO helpers
# -------------------------
def read_rows(input_path: str) -> List[Dict[str, Any]]:
    ext = os.path.splitext(input_path.lower())[1]
    if ext == ".json":
        with open(input_path, "r", encoding="utf-8") as f:
            return json.load(f)
    if ext == ".csv":
        with open(input_path, "r", encoding="utf-8-sig") as f:
            return list(csv.DictReader(f))
    raise ValueError("Input must be .csv or .json")


def _csv_safe(v: Any) -> Any:
    """برای CSV: list/dict را JSON-string می‌کنیم."""
    if isinstance(v, (list, dict)):
        try:
            return json.dumps(v, ensure_ascii=False)
        except Exception:
            return str(v)
    return v


def write_outputs(rows: List[Dict[str, Any]], out_dir="output") -> Tuple[str, str]:
    def log(msg: str) -> None:
        if not _should_emit_default_log(msg):
            return
        print(msg)
        if on_log:
            try:
                on_log(msg)
            except Exception:
                pass

    os.makedirs(out_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    out_csv = os.path.join(out_dir, f"realestate_properties_full_{ts}.csv")
    out_json = os.path.join(out_dir, f"realestate_properties_full_{ts}.json")

    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)

    keys = set()
    for r in rows:
        keys.update(r.keys())
    fieldnames = sorted(keys)

    with open(out_csv, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow({k: _csv_safe(r.get(k, "")) for k in fieldnames})

    return out_csv, out_json


def find_latest_module2_output(out_dir="output") -> str:
    patterns = [
        os.path.join(out_dir, "realestate_properties_with_prices_*.json"),
        os.path.join(out_dir, "realestate_properties_with_prices_*.csv"),
    ]
    candidates = []
    for p in patterns:
        candidates.extend(glob(p))
    if not candidates:
        raise FileNotFoundError(f"No module2 outputs found in: {os.path.abspath(out_dir)}")
    return max(candidates, key=os.path.getmtime)


# -------------------------
# Checkpoint / Resume
# -------------------------
def slugify(s: str) -> str:
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    return s.strip("_") or "checkpoint"


def extract_location_slug_from_search_url(search_url: str) -> str:
    u = urlparse(search_url)
    m = re.search(r"/buy/in-(.+?)/list-\d+", u.path)
    if m:
        return m.group(1)
    return "area"


def checkpoint_path(out_dir: str, area_search_url: str) -> str:
    loc = extract_location_slug_from_search_url(area_search_url)
    h = hashlib.md5(area_search_url.encode("utf-8")).hexdigest()[:8]
    return os.path.join(out_dir, f"module3_details_checkpoint_{slugify(loc)}_{h}.json")


def save_checkpoint(path: str, data: dict, retries: int = 10, delay: float = 0.2):
    """
    ✅ نسخه مقاوم برای ویندوز/آنتی‌ویروس:
    - tmp write + flush + fsync
    - os.replace با retry
    - اگر نشد: backup timestamp
    """
    tmp = path + ".tmp"
    data["saved_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 1) write tmp + fsync
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        try:
            os.fsync(f.fileno())
        except Exception:
            pass

    # 2) atomic replace with retry (WinError 5)
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

    # 3) fallback: backup
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = f"{path}.backup_{ts}"
    try:
        with open(backup_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

    try:
        if os.path.exists(tmp):
            os.remove(tmp)
    except Exception:
        pass

    raise last_err if last_err else PermissionError(f"Could not replace checkpoint file: {path}")


def load_checkpoint(path: str) -> Optional[dict]:
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


# -------------------------
# DOM helpers
# -------------------------
def first_text(driver_or_el, selectors: List[str]) -> Optional[str]:
    for sel in selectors:
        try:
            el = driver_or_el.find_element(By.CSS_SELECTOR, sel)
            t = (el.text or "").strip()
            if t:
                return t
        except Exception:
            continue
    return None


def parse_id_from_profile_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    m = re.search(r"-(\d+)(?:[/?].*)?$", url)
    return m.group(1) if m else None


def parse_agency_code_from_url(url: Optional[str]) -> Optional[str]:
    """
    مثال: /agency/montano-group-leichhardt-XLJLEI -> XLJLEI
    """
    if not url:
        return None
    try:
        path = urlparse(url).path
        m = re.search(r"/agency/[^/]+-([A-Za-z0-9]+)$", path)
        if m:
            return m.group(1)
    except Exception:
        pass
    return None


def get_attr_any(el, attrs: List[str]) -> Optional[str]:
    for attr in attrs:
        v = (el.get(attr) or "").strip()
        if v:
            return v
    return None


def normalize_text(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    out = html.unescape(text).replace("\r", "\n")
    out = re.sub(r"[ \t]+", " ", out)
    out = re.sub(r"\n\s*\n+", "\n", out).strip()
    return out or None


def is_bad_description(text: Optional[str]) -> bool:
    if not text:
        return True
    s = text.lower()
    bad_parts = [
        "these properties from",
        "shown based on the property type",
        "distance to this listing",
        "similar properties",
        "recommended",
        "nearby properties",
    ]
    return any(p in s for p in bad_parts)


def extract_meta_content_from_soup(soup: BeautifulSoup, selector: str) -> Optional[str]:
    el = soup.select_one(selector)
    return normalize_text(el.get("content")) if el else None


def extract_price_from_meta_description(meta_desc: Optional[str]) -> Optional[str]:
    if not meta_desc:
        return None
    m = re.search(r"(Auction\s*-\s*Contact Agent|Contact Agent|Call for price|Price on request|Expressions of Interest|EOI|Offers invited|Offers|For Sale|Under offer)", meta_desc, flags=re.I)
    return m.group(1).strip() if m else None


def _set_detail_price_fields(out: Dict[str, Any], price_text: str | None) -> None:
    price = normalize_text(price_text)
    if not price:
        return
    out["detail_price_display"] = price
    out["AdPriceDisplay"] = price
    out["ad_price_display"] = price
    out["Price"] = price
    out["price"] = price
    out["PriceSource"] = "ad_price"


STRONG_SOLD_PATTERNS = (
    r"\bSold\s+prior\s+to\s+auction\b",
    r"\bSold\s+at\s+auction\b",
    r"\bSold\s+on\s+\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}\b",
    r"\bSold\s+for\s+\$?\s*[0-9][0-9,]*(?:\.[0-9]+)?\b",
)


def _extract_sold_evidence(text: str | None) -> str | None:
    """Return strong listing-level sold evidence only.

    This intentionally does not treat a generic occurrence of "sold" as a
    sold listing. REA detail pages can contain unrelated scripts, recommendations,
    ad copy (for example "must be sold"), or historical text with that word.
    """
    if not text:
        return None
    normalized = normalize_text(text) or ""
    for pattern in STRONG_SOLD_PATTERNS:
        match = re.search(pattern, normalized, flags=re.I)
        if match:
            return normalize_text(match.group(0))
    if re.fullmatch(r"sold", normalized, flags=re.I):
        return "Sold"
    return None


def _contains_weak_sold_text(text: str | None) -> bool:
    return bool(text and re.search(r"\bsold\b", str(text), flags=re.I))


def _iter_json_values(value: Any, path: tuple[str, ...] = ()):
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _iter_json_values(item, path + (str(key),))
    elif isinstance(value, list):
        for idx, item in enumerate(value):
            yield from _iter_json_values(item, path + (str(idx),))
    else:
        yield path, value


def _extract_structured_sold_evidence(soup: BeautifulSoup) -> str | None:
    status_keys = {
        "status", "listingstatus", "listing_status", "propertystatus",
        "property_status", "adstatus", "ad_status", "lifecyclestatus",
        "lifecycle_status", "currentstatus", "current_status",
    }
    for script in soup.select('script[type="application/ld+json"], script[type="application/json"]'):
        text = script.string or script.get_text()
        if not text or "sold" not in text.lower():
            continue
        try:
            payload = json.loads(text)
        except Exception:
            continue
        for path, value in _iter_json_values(payload):
            key = path[-1].lower() if path else ""
            key_norm = re.sub(r"[^a-z0-9_]", "", key)
            if key_norm in status_keys and isinstance(value, str) and value.strip().lower() == "sold":
                return f"structured status sold ({'.'.join(path)})"
    return None


def _extract_primary_sold_evidence_from_soup(soup: BeautifulSoup) -> tuple[str | None, bool]:
    structured = _extract_structured_sold_evidence(soup)
    if structured:
        return structured, True

    primary_selectors = (
        "main [data-testid*='status' i]",
        "main [data-testid*='badge' i]",
        "main [class*='status' i]",
        "main [class*='badge' i]",
        "main [class*='label' i]",
        "header [data-testid*='status' i]",
        "header [class*='status' i]",
        "header [class*='badge' i]",
        "[data-testid='listing-details__summary-title']",
        "[data-testid='property-price']",
        ".property-info__price",
        ".property-price",
        "h1",
    )
    seen_text: set[str] = set()
    weak = False
    for selector in primary_selectors:
        for el in soup.select(selector):
            text = normalize_text(el.get_text(" ", strip=True))
            if not text or text in seen_text:
                continue
            seen_text.add(text)
            evidence = _extract_sold_evidence(text)
            if evidence:
                return evidence, True
            weak = weak or _contains_weak_sold_text(text)

    full_text = soup.get_text(" ", strip=True)
    return None, bool(weak or _contains_weak_sold_text(full_text) or _contains_weak_sold_text(str(soup)))


def _apply_sold_status_evidence(out: Dict[str, Any], evidence: str | None, weak: bool = False) -> None:
    if evidence:
        out["status"] = "sold"
        out["ListingLifecycleStatus"] = "sold"
        out["StatusReason"] = "sold_evidence"
        out["StatusEvidence"] = evidence
        out["SoldEvidenceStrength"] = "strong"
    elif weak:
        out["SoldEvidenceStrength"] = "weak"
        out["StatusReason"] = "weak_sold_evidence_ignored"


def _int_from_text(text: str | None) -> Optional[int]:
    if not text:
        return None
    match = re.search(r"\d+", str(text).replace(",", ""))
    return int(match.group(0)) if match else None


def _set_size_field(out: Dict[str, Any], prefix: str, text: str | None) -> None:
    display = extract_area_display(text)
    sqm = parse_area_to_sqm(text)
    if not display or sqm is None:
        return
    pascal = {
        "land_size": "LandSize",
        "building_size": "BuildingSize",
        "floor_area": "FloorArea",
    }[prefix]
    out[f"{prefix}_display"] = display
    out[f"{prefix}_sqm"] = sqm
    out[f"{pascal}Display"] = display
    out[f"{pascal}Sqm"] = sqm


def _extract_feature_sizes_from_soup(soup: BeautifulSoup) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    labels = []
    for li in soup.select("li[aria-label]"):
        label = normalize_text(li.get("aria-label")) or ""
        visible = normalize_text(li.get_text(" ", strip=True))
        labels.append((label.lower(), visible or label))
    for ul in soup.select("ul[aria-label]"):
        label = normalize_text(ul.get("aria-label")) or ""
        labels.append((label.lower(), label))

    for label_lower, text in labels:
        if "land size" in label_lower:
            _set_size_field(out, "land_size", text)
        elif "building size" in label_lower:
            _set_size_field(out, "building_size", text)
        elif any(key in label_lower for key in ("floor area", "internal area", "living area")):
            _set_size_field(out, "floor_area", text)
    return out


def _extract_primary_features_from_soup(soup: BeautifulSoup) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    address = soup.select_one("h1.property-info-address, h1[data-testid='listing-details__button-copy-wrapper'], h1")
    if address:
        text = normalize_text(address.get_text(" ", strip=True))
        if text:
            out["address"] = text

    feature_root = soup.select_one("ul.property-info__primary-features") or soup.select_one("ul[aria-label*='bedroom' i], ul[aria-label*='land size' i], ul[aria-label*='building size' i]")
    if feature_root:
        for li in feature_root.select("li[aria-label]"):
            label = (li.get("aria-label") or "").lower()
            value = normalize_text(li.get_text(" ", strip=True)) or li.get("aria-label")
            if "bedroom" in label:
                out["bedrooms"] = _int_from_text(value)
            elif "bathroom" in label:
                out["bathrooms"] = _int_from_text(value)
            elif "car space" in label or "parking" in label:
                out["parking"] = _int_from_text(value)
        direct_p = [p for p in feature_root.find_all("p", recursive=False)]
        for p in reversed(direct_p):
            text = normalize_text(p.get_text(" ", strip=True))
            if text and not parse_area_to_sqm(text) and not re.fullmatch(r"\d+", text):
                out["property_type"] = text
                break
        if not out.get("property_type"):
            label = feature_root.get("aria-label") or ""
            match = re.match(r"\s*([A-Za-z][A-Za-z /-]+?)\s+with\b", label)
            if match:
                out["property_type"] = match.group(1).strip()
    out.update(_extract_feature_sizes_from_soup(soup))
    return {k: v for k, v in out.items() if v not in (None, "", [], {})}


def _strip_tags(text: str | None) -> str | None:
    if not text:
        return None
    return normalize_text(re.sub(r"<[^>]+>", " ", text))


def _extract_detail_data_from_html_regex(html_text: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    h1 = re.search(r'<h1[^>]*class=["\'][^"\']*property-info-address[^"\']*["\'][^>]*>(.*?)</h1>', html_text or "", flags=re.I | re.S)
    if h1:
        out["address"] = _strip_tags(h1.group(1))
    price = re.search(r'<span[^>]*class=["\'][^"\']*property-price[^"\']*["\'][^>]*>(.*?)</span>', html_text or "", flags=re.I | re.S)
    if price:
        _set_detail_price_fields(out, _strip_tags(price.group(1)))
    price_text = _strip_tags(price.group(1)) if price else None
    h1_text = _strip_tags(h1.group(1)) if h1 else None
    sold_evidence = _extract_sold_evidence(price_text) or _extract_sold_evidence(h1_text)
    _apply_sold_status_evidence(out, sold_evidence, weak=bool(_contains_weak_sold_text(html_text) and not sold_evidence))
    ul = re.search(r'<ul[^>]*property-info__primary-features[^>]*aria-label=["\']([^"\']+)["\'][^>]*>(.*?)</ul>', html_text or "", flags=re.I | re.S)
    pairs: list[tuple[str, str]] = []
    if ul:
        aria = html.unescape(ul.group(1))
        body = ul.group(2)
        pairs.append((aria.lower(), aria))
        type_match = re.search(r"<p[^>]*>\s*([^<]*[A-Za-z][^<]*)\s*</p>\s*$", body, flags=re.I | re.S)
        if type_match:
            out["property_type"] = normalize_text(type_match.group(1))
        if not out.get("property_type"):
            m = re.match(r"\s*([A-Za-z][A-Za-z /-]+?)\s+with\b", aria)
            if m:
                out["property_type"] = m.group(1).strip()
        for li in re.finditer(r'<li[^>]*aria-label=["\']([^"\']+)["\'][^>]*>(.*?)</li>', body, flags=re.I | re.S):
            label = html.unescape(li.group(1))
            value = _strip_tags(li.group(2)) or label
            label_lower = label.lower()
            pairs.append((label_lower, value))
            if "bedroom" in label_lower:
                out["bedrooms"] = _int_from_text(value)
            elif "bathroom" in label_lower:
                out["bathrooms"] = _int_from_text(value)
            elif "car space" in label_lower or "parking" in label_lower:
                out["parking"] = _int_from_text(value)
    for label_lower, text in pairs:
        if "land size" in label_lower:
            _set_size_field(out, "land_size", text)
        elif "building size" in label_lower:
            _set_size_field(out, "building_size", text)
        elif any(key in label_lower for key in ("floor area", "internal area", "living area")):
            _set_size_field(out, "floor_area", text)
    return {k: v for k, v in out.items() if v not in (None, "", [], {})}


def extract_detail_data_from_html(html_text: str) -> Dict[str, Any]:
    soup = BeautifulSoup(html_text or "", "html.parser")
    if soup is None:
        return _extract_detail_data_from_html_regex(html_text or "")
    out: Dict[str, Any] = _extract_primary_features_from_soup(soup)
    method = []
    if out:
        method.append("dom_primary_features")

    og_desc = extract_meta_content_from_soup(soup, 'meta[property="og:description"]')
    meta_desc = extract_meta_content_from_soup(soup, 'meta[name="description"]')
    desc = og_desc if og_desc and not is_bad_description(og_desc) else None
    if desc:
        out["description"] = desc
        method.append("meta_og_description")
    elif meta_desc and not is_bad_description(meta_desc):
        out["description"] = meta_desc
        method.append("meta_description")
    else:
        for sel in DESCRIPTION_SELECTORS[:4]:
            el = soup.select_one(sel)
            if el:
                txt = normalize_text(el.get_text(" ", strip=True))
                if txt and not is_bad_description(txt):
                    out["description"] = txt
                    method.append("dom_description")
                    break

    price_dom = None
    for sel in PRICE_DETAIL_SELECTORS:
        el = soup.select_one(sel)
        if el:
            txt = normalize_text(el.get_text(" ", strip=True))
            if txt:
                price_dom = txt
                break
    _set_detail_price_fields(out, price_dom or extract_price_from_meta_description(meta_desc))
    sold_evidence, weak_sold_evidence = _extract_primary_sold_evidence_from_soup(soup)
    _apply_sold_status_evidence(out, sold_evidence, weak=weak_sold_evidence)

    agents = []
    seen = set()
    for a in soup.select('a[href*="/agent/"], a[data-savepage-href*="/agent/"]'):
        href = get_attr_any(a, ["href", "data-savepage-href"])
        if not href:
            continue
        aid = parse_id_from_profile_url(href)
        key = aid or href
        if key in seen:
            continue
        seen.add(key)
        name = normalize_text(a.get_text(" ", strip=True))
        if not name:
            img = a.find("img")
            alt = normalize_text((img.get("alt") if img else None))
            if alt:
                name = re.sub(r"^Image\.\s*Photo of\s*", "", alt, flags=re.I).strip()
        parent = a.parent if a.parent else soup
        tel = parent.select_one('a[href^="tel:"], a[data-savepage-href^="tel:"]')
        sms = parent.select_one('a[href^="sms:"], a[data-savepage-href^="sms:"]')
        phone = None
        if tel:
            phone = re.sub(r"\D", "", get_attr_any(tel, ["href", "data-savepage-href"]).replace("tel:", ""))
        elif sms:
            sms_href = get_attr_any(sms, ["href", "data-savepage-href"])
            m = re.search(r"sms:([0-9+ ]+)", sms_href or "", flags=re.I)
            if m:
                phone = re.sub(r"\D", "", m.group(1))
        phone_masked = None
        reveal = parent.select_one('button[title*="reveal phone" i]')
        if reveal:
            phone_masked = normalize_text(reveal.get_text(" ", strip=True))
        rating = None
        reviews = None
        t = normalize_text(parent.get_text(" ", strip=True)) or ""
        mr = re.search(r"\b([0-5](?:\.\d)?)\b", t)
        mv = re.search(r"\b(\d+\s+reviews?)\b", t, flags=re.I)
        if mr:
            rating = mr.group(1)
        if mv:
            reviews = mv.group(1)
        agents.append({"name": name, "agent_id": aid, "profile_url": href, "phone": phone, "phone_masked": phone_masked, "rating": rating, "reviews": reviews})
    if agents:
        out["agents"] = agents
        method.append("dom_agent_links")
        for i, ag in enumerate(agents[:3], start=1):
            out[f"agent_{i}_name"] = ag.get("name")
            out[f"agent_{i}_id"] = ag.get("agent_id")
            out[f"agent_{i}_profile_url"] = ag.get("profile_url")
            out[f"agent_{i}_phone"] = ag.get("phone")

    agency = None
    for a in soup.select('a[href*="/agency/"], a[data-savepage-href*="/agency/"]'):
        href = get_attr_any(a, ["href", "data-savepage-href"])
        if href:
            agency = a
            out["agency_profile_url"] = href
            out["agency_code"] = parse_agency_code_from_url(href)
            out["agency_name"] = normalize_text(a.get_text(" ", strip=True)) or normalize_text((a.find("img").get("alt") if a.find("img") else None))
            break
    if agency:
        method.append("dom_contact_panel")

    if method:
        out["detail_extraction_method"] = ",".join(method)
    return {k: v for k, v in out.items() if v not in (None, "", [])}


# -------------------------
# Wait for real "detail page ready"
# -------------------------
DETAIL_READY_SELECTORS = [
    "div.contact-agent-panel",
    "ul.agent-info",
    '[data-testid="listing-description"]',
    '[data-testid="property-description"]',
    '[data-testid*="listing-price"]',
    '[data-testid="property-price"]',
    "h1",
]

def wait_for_detail_ready(driver, timeout=25):
    """
    تا وقتی یکی از عناصر کلیدی صفحه جزئیات ظاهر نشود، جلو نمی‌رود.
    """
    def _cond(d):
        try:
            rs = d.execute_script("return document.readyState")
            if rs not in ("interactive", "complete"):
                return False
        except Exception:
            pass

        # اگر __NEXT_DATA__ آمد، حداقل دیتا آمده
        try:
            if d.find_elements(By.CSS_SELECTOR, "script#__NEXT_DATA__"):
                return True
        except Exception:
            pass

        for sel in DETAIL_READY_SELECTORS:
            try:
                els = d.find_elements(By.CSS_SELECTOR, sel)
                for el in els:
                    if el.is_displayed():
                        return True
            except Exception:
                continue

        return False

    return WebDriverWait(driver, timeout, poll_frequency=0.3).until(_cond)


# -------------------------
# Extractors
# -------------------------
DESCRIPTION_SELECTORS = [
    '[data-testid="listing-description"]',
    '[data-testid="property-description"]',
    '[data-testid="description"]',
    'section[class*="description"]',
    'div[class*="description"]',
    'div[class*="PropertyDescription"]',
    'div[class*="property-description"]',
]

PRICE_DETAIL_SELECTORS = [
    '[data-testid*="listing-price"]',
    '[data-testid="property-price"]',
    'span[class*="price"]',
    'p[class*="price"]',
]


def extract_agents_from_contact_panel(driver) -> List[Dict[str, Any]]:
    agents: List[Dict[str, Any]] = []
    try:
        panel = driver.find_element(By.CSS_SELECTOR, "div.contact-agent-panel")
    except Exception:
        return agents

    try:
        items = panel.find_elements(By.CSS_SELECTOR, "ul.agent-info li.agent-info__agent")
    except Exception:
        items = []

    for li in items:
        a_name = None
        a_url = None
        a_id = None
        phone_tel = None
        phone_masked = None
        rating = None
        reviews = None

        try:
            name_el = li.find_element(By.CSS_SELECTOR, "a.agent-info__name")
            a_name = (name_el.text or "").strip() or None
            a_url = (name_el.get_attribute("href") or "").strip() or None
        except Exception:
            pass

        if not a_url:
            try:
                link_el = li.find_element(By.CSS_SELECTOR, "a.agent-info__link")
                a_url = (link_el.get_attribute("href") or "").strip() or None
            except Exception:
                pass

        a_id = parse_id_from_profile_url(a_url)

        try:
            tel_el = li.find_element(By.CSS_SELECTOR, 'a.phone__link[href^="tel:"]')
            href = (tel_el.get_attribute("href") or "").strip()
            if href.startswith("tel:"):
                phone_tel = href.replace("tel:", "").strip()
        except Exception:
            pass

        try:
            m_el = li.find_element(By.CSS_SELECTOR, "button.phone__reveal span.phone__reveal-text")
            phone_masked = (m_el.text or "").strip() or None
        except Exception:
            pass

        try:
            rc = li.find_element(By.CSS_SELECTOR, '[data-testid="agent-rating-container"]')
            try:
                rating_el = rc.find_element(By.CSS_SELECTOR, 'span[class*="AvgRatingText"]')
                rating = (rating_el.text or "").strip() or None
            except Exception:
                pass
            try:
                rev_el = rc.find_element(By.CSS_SELECTOR, 'span[class*="ReviewsText"]')
                reviews = (rev_el.text or "").strip() or None
            except Exception:
                pass
        except Exception:
            pass

        if a_name or a_url:
            agents.append({
                "name": a_name,
                "agent_id": a_id,
                "profile_url": a_url,
                "phone": phone_tel,
                "phone_masked": phone_masked,
                "rating": rating,
                "reviews": reviews,
            })

    return agents


def extract_agency_from_contact_panel(driver) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    try:
        panel = driver.find_element(By.CSS_SELECTOR, "div.contact-agent-panel")
    except Exception:
        return out

    try:
        a = panel.find_element(By.CSS_SELECTOR, "a.sidebar-traffic-driver__name")
        out["agency_name"] = (a.text or "").strip() or None
        out["agency_profile_url"] = (a.get_attribute("href") or "").strip() or None
        out["agency_code"] = parse_agency_code_from_url(out["agency_profile_url"])
    except Exception:
        pass

    try:
        d = panel.find_element(By.CSS_SELECTOR, "div.sidebar-traffic-driver__detail-info")
        out["agency_address"] = (d.text or "").strip() or None
    except Exception:
        pass

    return out



DETAIL_REFRESH_STATUS_FIELDS = ("detail_refresh_success", "detail_refresh_error", "detail_extraction_quality")
DETAIL_RELIABILITY_FIELDS = (
    "detail_agents_reliable",
    "detail_agency_reliable",
    "detail_price_reliable",
    "detail_description_reliable",
    "detail_inspection_reliable",
    "detail_auction_reliable",
    "detail_status_reliable",
    "agents_explicitly_absent",
    "agency_explicitly_absent",
    "detail_reliable_fields",
)


def _has_meaningful_detail_data(data: Dict[str, Any]) -> bool:
    return any(k != "detail_error" and v not in (None, "", [], {}) for k, v in (data or {}).items())


def _detail_quality(data: Dict[str, Any], wait_ready: bool = True) -> str:
    if not _has_meaningful_detail_data(data):
        return "failed"
    # If the page never reached the known detail-ready selectors, keep the
    # extraction as partial even when some fields were salvaged from HTML.
    if not wait_ready:
        return "partial"
    return "ok"


def _set_detail_reliability(row: Dict[str, Any], data: Dict[str, Any], quality: str) -> None:
    reliable_fields: list[str] = []
    agents_reliable = bool((data or {}).get("agents"))
    agency_reliable = any((data or {}).get(k) not in (None, "", [], {}) for k in ("agency_name", "agency_code", "agency_profile_url"))
    price_reliable = any((data or {}).get(k) not in (None, "", [], {}) for k in ("detail_price_display", "price_display", "price_low", "price_high"))
    description_reliable = (data or {}).get("description") not in (None, "", [], {})
    inspection_reliable = any((data or {}).get(k) not in (None, "", [], {}) for k in ("inspection_short", "inspection_long"))
    auction_reliable = any((data or {}).get(k) not in (None, "", [], {}) for k in ("auction_label", "auction_time", "auction_date", "auction_result"))
    status_reliable = any((data or {}).get(k) not in (None, "", [], {}) for k in ("status", "current_status"))
    for field, ok in (
        ("agents", agents_reliable),
        ("agency", agency_reliable),
        ("price", price_reliable),
        ("description", description_reliable),
        ("inspection", inspection_reliable),
        ("auction", auction_reliable),
        ("status", status_reliable),
    ):
        if quality != "failed" and ok:
            reliable_fields.append(field)
    row["detail_agents_reliable"] = quality != "failed" and agents_reliable
    row["detail_agency_reliable"] = quality != "failed" and agency_reliable
    row["detail_price_reliable"] = quality != "failed" and price_reliable
    row["detail_description_reliable"] = quality != "failed" and description_reliable
    row["detail_inspection_reliable"] = quality != "failed" and inspection_reliable
    row["detail_auction_reliable"] = quality != "failed" and auction_reliable
    row["detail_status_reliable"] = quality != "failed" and status_reliable
    row["agents_explicitly_absent"] = False
    row["agency_explicitly_absent"] = False
    row["detail_reliable_fields"] = reliable_fields


def _mark_detail_failure(row: Dict[str, Any], reason: Any) -> Dict[str, Any]:
    row["detail_error"] = str(reason or "detail_refresh_failed")
    row["detail_refresh_success"] = False
    row["detail_refresh_error"] = row["detail_error"]
    row["detail_extraction_quality"] = "failed"
    _set_detail_reliability(row, {}, "failed")
    return row


def _mark_detail_lifecycle_state(row: Dict[str, Any], state_result) -> Dict[str, Any]:
    if state_result.state == PageState.DETAIL_SOLD:
        status = "sold"
    elif state_result.state == PageState.DETAIL_NOT_FOUND:
        status = "not_found"
    else:
        status = "removed"
    evidence = state_result.reason or state_result.state
    row["status"] = status
    row["current_status"] = status
    row["ListingLifecycleStatus"] = status
    row["StatusReason"] = state_result.state
    row["StatusEvidence"] = evidence
    row["detail_refresh_success"] = True
    row["detail_refresh_error"] = None
    row["detail_extraction_quality"] = "ok"
    _set_detail_reliability(row, {}, "ok")
    row.pop("detail_error", None)
    return row


def _merge_extracted_detail(row: Dict[str, Any], data: Dict[str, Any], quality: str, only_if_missing: bool = False) -> Dict[str, Any]:
    row["detail_refresh_success"] = quality != "failed"
    row["detail_refresh_error"] = None if quality != "failed" else str((data or {}).get("detail_error") or "detail_refresh_failed")
    row["detail_extraction_quality"] = quality
    _set_detail_reliability(row, data or {}, quality)
    if quality == "failed":
        row["detail_error"] = row["detail_refresh_error"]
        return row
    for k, v in (data or {}).items():
        if k == "detail_error" or k in DETAIL_REFRESH_STATUS_FIELDS or k in DETAIL_RELIABILITY_FIELDS:
            continue
        if v in (None, "", [], {}):
            # Missing extraction is not evidence of removal; preserve candidate state.
            continue
        if only_if_missing:
            existing = row.get(k)
            if existing in (None, "", [], {}):
                row[k] = v
        else:
            row[k] = v
    row.pop("detail_error", None)
    return row

def extract_detail_data(driver) -> Dict[str, Any]:
    html_text = driver.page_source or ""
    out = extract_detail_data_from_html(html_text)
    if out.get("agents"):
        out.setdefault("agent_name", out["agents"][0].get("name"))
        out.setdefault("agent_id", out["agents"][0].get("agent_id"))
        out.setdefault("agent_profile_url", out["agents"][0].get("profile_url"))
    return out


# -------------------------
# Runner
# -------------------------
def module3_run(
    area_search_url: str,
    input_file: Optional[str] = None,
    out_dir: str = "output",
    only_if_missing: bool = True,
    wait_timeout: int = 25,
    sleep_between: float | None = None,
    empty_retry: int = 1,
    cancel_token=None,
    on_progress=None,
    on_log=None,
):
    module3_run.last_result = {"status": "running"}
    success_count = 0
    session_failure_count = 0
    os.makedirs(out_dir, exist_ok=True)

    def log(msg: str) -> None:
        if not _should_emit_default_log(msg):
            return
        print(msg)
        if on_log:
            try:
                on_log(msg)
            except Exception:
                pass

    if not input_file:
        input_file = find_latest_module2_output(out_dir)

    input_file = os.path.abspath(input_file)
    log(f"📥 Module3 Input: {input_file}")

    rows = read_rows(input_file)

    ck_path = checkpoint_path(out_dir, area_search_url)
    ck = load_checkpoint(ck_path)

    if not ck:
        ck = {
            "version": 1,
            "area_search_url": area_search_url,
            "input_file": input_file,
            "done_listing_ids": [],
            "last_index": -1,
        }
        save_checkpoint(ck_path, ck)

    done = set(ck.get("done_listing_ids", []))
    start_from = int(ck.get("last_index", -1)) + 1
    log(f"Resume: done={len(done)} start_from_index={start_from}")

    driver = None
    consecutive_get_failures = 0
    profile_dir_current = config.get_effective_browser_profile_dir("module3")
    rotations_used = 0

    try:
        driver = build_driver(profile_dir_override=profile_dir_current)
        attempted_detail_navigations = 0

        for idx in range(start_from, len(rows)):
            if getattr(cancel_token, "is_set", lambda: False)():
                log("Cancel requested in module3.")
                return None, None

            if on_progress:
                try:
                    on_progress("module3_progress", {"i": idx + 1, "n": len(rows)})
                except Exception:
                    pass
            r = rows[idx]
            lid = (r.get("listing_id") or "").strip()
            url = (r.get("url") or "").strip()

            if not lid or not url or url.upper() == "N/A":
                ck["last_index"] = idx
                save_checkpoint(ck_path, ck)
                continue

            if lid in done:
                ck["last_index"] = idx
                save_checkpoint(ck_path, ck)
                continue

            # اگر فقط برای missing اجرا می‌کنی و قبلاً description داریم
            if only_if_missing and (r.get("description") or "").strip():
                done.add(lid)
                ck["done_listing_ids"] = list(done)
                ck["last_index"] = idx
                save_checkpoint(ck_path, ck)
                continue

            log(f"\n🔎 Detail {idx+1}/{len(rows)} | listing_id={lid}")

            listing_429_retries = 0
            driver, profile_dir_current, rotations_used, detail_state, ok, err = _module3_load_detail_page(
                driver,
                url,
                profile_dir_current,
                rotations_used,
                wait_timeout,
                log,
                apply_delay=attempted_detail_navigations > 0,
            )
            attempted_detail_navigations += 1
            if not ok:
                consecutive_get_failures += 1
                if isinstance(err, RealEstateBlockedError) or is_retryable_navigation_error(err):
                    session_failure_count += 1
                    module3_run.last_result = _module3_retryable_result(err)
                    module3_run.last_result["success_count"] = success_count
                    module3_run.last_result["session_failure_count"] = session_failure_count
                    save_checkpoint(ck_path, ck)
                    log("Retryable Module3 browser/navigation interruption. Checkpoint saved without advancing this listing; job should retry_wait.")
                    return None, None
                ck["last_index"] = idx
                save_checkpoint(ck_path, ck)
                log("   -> GET failed/timeout (renderer).")
                if consecutive_get_failures >= 2:
                    log("   -> Restarting driver ...")
                    driver = restart_driver(driver)
                    consecutive_get_failures = 0
                continue
            if detail_state.state in {PageState.DETAIL_REMOVED, PageState.DETAIL_NOT_FOUND, PageState.DETAIL_SOLD}:
                _mark_detail_lifecycle_state(r, detail_state)
                success_count += 1
                done.add(lid)
                ck["done_listing_ids"] = list(done)
                ck["last_index"] = idx
                save_checkpoint(ck_path, ck)
                continue
            if detail_state.state in {PageState.RENDER_TIMEOUT, PageState.BLANK_RENDER, PageState.UNKNOWN}:
                try:
                    driver.refresh()
                    detail_state = wait_for_detail_page_state(driver, timeout=wait_timeout)
                except Exception:
                    pass
                if detail_state.state in {PageState.RENDER_TIMEOUT, PageState.BLANK_RENDER, PageState.UNKNOWN}:
                    r["detail_error"] = "detail_render_timeout" if detail_state.state == PageState.RENDER_TIMEOUT else f"detail_{detail_state.state}"
                    done.add(lid)
                    ck["done_listing_ids"] = list(done)
                    ck["last_index"] = idx
                    save_checkpoint(ck_path, ck)
                    continue
            while config.BROWSER_RECOVERY_ON_429 and is_429_page(driver):
                write_outputs(rows, out_dir=out_dir)
                ck["last_index"] = idx
                save_checkpoint(ck_path, ck)
                driver, rotations_used, profile_dir_current, recovery_status = recover_browser_after_429(
                    driver=driver,
                    current_profile_dir=profile_dir_current,
                    build_driver_func=build_driver,
                    rotations_used=rotations_used,
                    max_rotations=min(config.BROWSER_MAX_PROFILE_ROTATIONS_PER_RUN, config.MODULE3_MAX_PROFILE_ROTATIONS_PER_RUN),
                    reason=detail_state.state,
                    log_func=log,
                )
                if recovery_status != "recovered":
                    if config.MODULE3_STOP_ON_429_ROTATION_LIMIT:
                        return None, None
                    r["detail_error"] = "blocked_after_retries"
                    break
                listing_429_retries += 1
                if listing_429_retries > config.MODULE3_RETRY_SAME_LISTING_AFTER_429:
                    if config.MODULE3_STOP_ON_429_ROTATION_LIMIT:
                        return None, None
                    r["detail_error"] = "blocked_after_retries"
                    break
                driver, ok, err = get_with_retries(driver, url, tries=2, phase="blocked_retry", apply_delay=False, log_func=log)
                if not ok:
                    break
            if r.get("detail_error") == "blocked_after_retries":
                done.add(lid)
                ck["done_listing_ids"] = list(done)
                ck["last_index"] = idx
                save_checkpoint(ck_path, ck)
                continue

            consecutive_get_failures = 0

            # صبر واقعی برای آماده شدن صفحه
            try:
                wait_for_detail_ready(driver, timeout=wait_timeout)
            except TimeoutException:
                log("   -> Detail not ready (timeout). Will retry once.")
                # یک بار رفرش
                try:
                    driver.refresh()
                    wait_for_detail_ready(driver, timeout=wait_timeout)
                except Exception:
                    pass

            # استخراج
            data = {}
            try:
                data = extract_detail_data(driver)
            except Exception as e:
                data = {"detail_error": str(e)}

            # اگر خالی بود، retry
            if (not data) or all((v is None or v == "" or v == []) for v in data.values()):
                if empty_retry > 0:
                    try:
                        log("   -> Empty extract. Refresh + retry...")
                        driver.refresh()
                        wait_for_detail_ready(driver, timeout=wait_timeout)
                        data = extract_detail_data(driver)
                    except Exception as e:
                        data = {"detail_error": f"detail_parse_empty_after_retry: {e}"}
                else:
                    data = {"detail_error": "detail_parse_empty"}

            if _module3_is_chrome_error_url(_module3_current_url(driver)):
                session_failure_count += 1
                module3_run.last_result = _module3_retryable_result("chrome_error_after_detail_load")
                module3_run.last_result["success_count"] = success_count
                module3_run.last_result["session_failure_count"] = session_failure_count
                log("Module3 chrome-error before extraction. Checkpoint unchanged for this listing; job should retry_wait.")
                return None, None

            # merge into row
            r["detail_scraped_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for k, v in data.items():
                if only_if_missing:
                    existing = r.get(k)
                    if existing in (None, "", [], {}):
                        r[k] = v
                else:
                    r[k] = v

            if data and not data.get("detail_error"):
                success_count += 1

            # done + checkpoint
            done.add(lid)
            ck["done_listing_ids"] = list(done)
            ck["last_index"] = idx
            save_checkpoint(ck_path, ck)


        # خروجی FULL
        out_csv, out_json = write_outputs(rows, out_dir=out_dir)
        if success_count <= 0 and rows:
            module3_run.last_result = _module3_retryable_result("module3_no_successful_detail_extractions")
            module3_run.last_result["success_count"] = success_count
            module3_run.last_result["session_failure_count"] = session_failure_count
            log("Module3 produced no successful detail extractions; refusing FULL success output and requesting retry_wait.")
            return None, None
        module3_run.last_result = {"status": "completed", "rows": len(rows), "output_json": out_json, "success_count": success_count, "session_failure_count": session_failure_count}
        log("\nModule3 done. FULL files saved:")
        log(f" - {out_csv}")
        log(f" - {out_json}")

        # done checkpoint rename (با retry ساده)
        try:
            for _ in range(10):
                try:
                    os.replace(ck_path, ck_path + ".done")
                    break
                except PermissionError:
                    time.sleep(0.2)
        except Exception:
            pass

        return out_csv, out_json

    finally:
        if driver:
            from chrome_options_helper import cleanup_chrome_driver
            try:
                driver.quit()
            except Exception:
                pass
            cleanup_chrome_driver(driver)
        driver = None
        gc.collect()


if __name__ == "__main__":
    module3_run(
        area_search_url=AREA_SEARCH_URL,
        input_file=None,
        out_dir="output",
        only_if_missing=True,
        wait_timeout=25,
        sleep_between=None,
        empty_retry=1,
    )

def enrich_detail_rows(
    rows: list[dict],
    output_dir: str | None = None,
    wait_timeout: int | None = None,
    sleep_between: float | None = None,
    empty_retry: int = 1,
    on_log=None,
) -> list[dict]:
    out_dir = output_dir or "output"
    wait_timeout = int(wait_timeout or config.MODULE3_WAIT_TIMEOUT)
    sleep_between = float(sleep_between if sleep_between is not None else config.MODULE3_SLEEP_BETWEEN)

    def log(msg: str) -> None:
        if not _should_emit_default_log(msg):
            return
        if on_log:
            try:
                on_log(msg)
            except Exception:
                pass

    profile_dir_current = config.get_effective_browser_profile_dir("module3")
    driver = build_driver(profile_dir_override=profile_dir_current)
    rotations_used = 0
    enriched: list[dict] = []
    try:
        for row in rows:
            merged = dict(row)
            original_external_id = merged.get("external_id") or merged.get("listing_id")
            original_db_listing_id = merged.get("db_listing_id") or merged.get("internal_listing_id")
            url = (merged.get("url") or merged.get("listing_url") or merged.get("ListingURL") or "").strip()
            if not url:
                _mark_detail_failure(merged, "missing_url")
                enriched.append(merged)
                continue
            driver, profile_dir_current, rotations_used, detail_state, ok, err = _module3_load_detail_page(
                driver,
                url,
                profile_dir_current,
                rotations_used,
                wait_timeout,
                log,
            )
            if not ok:
                if isinstance(err, RealEstateBlockedError):
                    raise err
                _mark_detail_failure(merged, "get_failed")
                enriched.append(merged)
                continue
            if detail_state.state in {PageState.DETAIL_REMOVED, PageState.DETAIL_NOT_FOUND, PageState.DETAIL_SOLD}:
                _mark_detail_lifecycle_state(merged, detail_state)
                enriched.append(merged)
                time.sleep(max(0.0, sleep_between))
                continue
            if detail_state.state in {PageState.RENDER_TIMEOUT, PageState.BLANK_RENDER, PageState.UNKNOWN}:
                try:
                    driver.refresh()
                    detail_state = wait_for_detail_page_state(driver, timeout=wait_timeout)
                except Exception:
                    pass
                if detail_state.state in {PageState.RENDER_TIMEOUT, PageState.BLANK_RENDER, PageState.UNKNOWN}:
                    reason = "detail_render_timeout" if detail_state.state == PageState.RENDER_TIMEOUT else f"detail_{detail_state.state}"
                    _mark_detail_failure(merged, reason)
                    enriched.append(merged)
                    continue
            listing_429_retries = 0
            while config.BROWSER_RECOVERY_ON_429 and is_429_page(driver):
                driver, rotations_used, profile_dir_current, recovery_status = recover_browser_after_429(
                    driver=driver,
                    current_profile_dir=profile_dir_current,
                    build_driver_func=build_driver,
                    rotations_used=rotations_used,
                    max_rotations=min(config.BROWSER_MAX_PROFILE_ROTATIONS_PER_RUN, config.MODULE3_MAX_PROFILE_ROTATIONS_PER_RUN),
                    reason=detail_state.state,
                    log_func=log,
                )
                if recovery_status != "recovered":
                    _mark_detail_failure(merged, "blocked_after_retries")
                    break
                listing_429_retries += 1
                if listing_429_retries > config.MODULE3_RETRY_SAME_LISTING_AFTER_429:
                    _mark_detail_failure(merged, "blocked_after_retries")
                    break
                driver, ok, _ = get_with_retries(driver, url, tries=2)
                if not ok:
                    _mark_detail_failure(merged, "get_failed_after_recover")
                    break
            if merged.get("detail_error"):
                enriched.append(merged)
                continue
            wait_ready = True
            try:
                wait_for_detail_ready(driver, timeout=wait_timeout)
            except Exception:
                wait_ready = False
            try:
                data = extract_detail_data(driver)
            except Exception as e:
                data = {"detail_error": f"extract_detail_data_failed: {e}"}
            if (not data or all((v is None or v == "" or v == []) for v in data.values())) and empty_retry > 0:
                try:
                    driver.refresh()
                    try:
                        wait_for_detail_ready(driver, timeout=wait_timeout)
                    except Exception:
                        wait_ready = False
                    data = extract_detail_data(driver)
                except Exception as e:
                    data = {"detail_error": f"detail_parse_empty_after_retry: {e}"}
            quality = _detail_quality(data or {}, wait_ready=wait_ready)
            _merge_extracted_detail(merged, data or {}, quality, only_if_missing=False)
            if original_external_id is not None:
                merged["external_id"] = str(original_external_id)
                merged["listing_id"] = str(original_external_id)
            if original_db_listing_id is not None:
                merged["db_listing_id"] = original_db_listing_id
            enriched.append(merged)
            time.sleep(max(0.0, sleep_between))
        return enriched
    finally:
        try:
            write_outputs(enriched, out_dir=out_dir)
        except Exception:
            pass
        try:
            driver.quit()
        except Exception:
            pass
