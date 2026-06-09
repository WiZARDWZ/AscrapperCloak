import json
import random
import os
import shutil
import time
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime
from html import unescape

import config
from realestate_errors import RealEstateBlockedError, RealEstateRateLimitedError
from realestate_page_state import (
    PageState,
    classify_current_page,
    get_html_block_reason,
    is_chrome_error_url,
)


@dataclass
class BrowserSessionHealth:
    module_name: str
    requested_url: str = ""
    current_url: str = ""
    consecutive_goto_failures: int = 0
    consecutive_chrome_error_pages: int = 0
    successful_page_loads_since_rotation: int = 0
    attempted_urls_since_rotation: int = 0
    recovery_attempts_for_current_url: int = 0
    rotations_count: int = 0
    last_recovery_reason: str = ""
    last_page_state: str = ""

    def begin_url(self, requested_url: str) -> None:
        if requested_url != self.requested_url:
            self.requested_url = requested_url
            self.recovery_attempts_for_current_url = 0
        self.attempted_urls_since_rotation += 1

    def record_navigation(self, requested_url: str, ok: bool, exc: Exception | None = None, current_url: str = "") -> None:
        self.begin_url(requested_url)
        self.current_url = current_url or self.current_url
        if ok:
            self.consecutive_goto_failures = 0
        else:
            self.consecutive_goto_failures += 1
            self.last_recovery_reason = config.mask_sensitive_text(exc or "navigation_failed")

    def record_page_state(self, state_result) -> None:
        self.current_url = str(getattr(state_result, "current_url", "") or self.current_url)
        self.last_page_state = str(getattr(state_result, "state", "") or "")
        if is_chrome_error_url(self.current_url) or self.last_page_state == PageState.CHROME_ERROR:
            self.consecutive_chrome_error_pages += 1
        elif self.last_page_state in TRUSTED_RECOVERY_STATES:
            self.consecutive_chrome_error_pages = 0
            self.consecutive_goto_failures = 0
            self.successful_page_loads_since_rotation += 1

    def record_same_url_retry(self, reason: str) -> None:
        self.recovery_attempts_for_current_url += 1
        self.last_recovery_reason = config.mask_sensitive_text(reason)

    def record_rotation(self, reason: str) -> None:
        self.rotations_count += 1
        self.consecutive_goto_failures = 0
        self.consecutive_chrome_error_pages = 0
        self.successful_page_loads_since_rotation = 0
        self.attempted_urls_since_rotation = 0
        self.recovery_attempts_for_current_url = 0
        self.last_recovery_reason = config.mask_sensitive_text(reason)


class RecoveryPolicy:
    def __init__(
        self,
        *,
        same_url_max_retries: int | None = None,
        goto_failure_threshold: int | None = None,
        chrome_error_threshold: int | None = None,
        zero_success_hard_failure_threshold: int | None = None,
        min_attempted_urls_before_rotation: int = 1,
    ) -> None:
        self.same_url_max_retries = max(0, int(same_url_max_retries if same_url_max_retries is not None else getattr(config, "BROWSER_SAME_URL_MAX_RETRIES", 2)))
        self.goto_failure_threshold = max(1, int(goto_failure_threshold if goto_failure_threshold is not None else getattr(config, "BROWSER_CONSECUTIVE_GOTO_FAILURE_ROTATION_THRESHOLD", 3)))
        self.chrome_error_threshold = max(1, int(chrome_error_threshold if chrome_error_threshold is not None else getattr(config, "BROWSER_CONSECUTIVE_CHROME_ERROR_ROTATION_THRESHOLD", 3)))
        self.zero_success_hard_failure_threshold = max(1, int(zero_success_hard_failure_threshold if zero_success_hard_failure_threshold is not None else getattr(config, "BROWSER_ZERO_SUCCESS_HARD_FAILURE_THRESHOLD", 3)))
        self.min_attempted_urls_before_rotation = max(1, int(min_attempted_urls_before_rotation or 1))

    def should_retry_same_profile(self, health: BrowserSessionHealth) -> bool:
        return health.recovery_attempts_for_current_url < self.same_url_max_retries

    def should_rotate(self, health: BrowserSessionHealth, *, explicit_trusted_block: bool = False) -> bool:
        if explicit_trusted_block:
            return True
        if health.attempted_urls_since_rotation < self.min_attempted_urls_before_rotation:
            if health.successful_page_loads_since_rotation > 0:
                return False
            hard_failures = max(health.consecutive_goto_failures, health.consecutive_chrome_error_pages)
            return hard_failures >= self.zero_success_hard_failure_threshold
        if health.consecutive_chrome_error_pages >= self.chrome_error_threshold:
            return True
        if health.consecutive_goto_failures >= self.goto_failure_threshold:
            return True
        if health.successful_page_loads_since_rotation == 0:
            hard_failures = max(health.consecutive_goto_failures, health.consecutive_chrome_error_pages)
            return hard_failures >= self.zero_success_hard_failure_threshold
        return False


def log_session_health(health: BrowserSessionHealth, *, url_type: str, page_state: str, action: str, log_func=print) -> None:
    if not log_func:
        return
    log_func(
        "session_health module={module} url_type={url_type} requested_url={requested} current_url={current} "
        "page_state={state} same_url_retry_count={same_retries} consecutive_chrome_errors={chrome_errors} "
        "successful_page_loads_since_rotation={successes} attempted_urls_since_rotation={attempted} "
        "consecutive_goto_failures={goto_failures} rotations_count={rotations} action={action}".format(
            module=health.module_name,
            url_type=url_type,
            requested=config.mask_sensitive_text(health.requested_url)[:240],
            current=config.mask_sensitive_text(health.current_url)[:240],
            state=page_state,
            same_retries=health.recovery_attempts_for_current_url,
            chrome_errors=health.consecutive_chrome_error_pages,
            successes=health.successful_page_loads_since_rotation,
            attempted=health.attempted_urls_since_rotation,
            goto_failures=health.consecutive_goto_failures,
            rotations=health.rotations_count,
            action=action,
        )
    )


def _looks_like_windows_path(path: str) -> bool:
    text = str(path or "").strip()
    return bool(re.match(r"^[A-Za-z]:[\\/]", text) or "\\Users\\" in text or "\\PycharmProjects\\" in text)


def _sanitize_profile_dir_for_platform(profile_dir: str | None) -> str:
    text = str(profile_dir or "").strip()
    if os.name != "nt" and _looks_like_windows_path(text):
        return config.BROWSER_PROFILE_BASE_DIR
    return text or config.BROWSER_PROFILE_BASE_DIR


def is_429_html(title: str, html: str) -> bool:
    body_text = _extract_visible_text_from_html(html)
    if _html_has_normal_realestate_content(html, body_text) or _html_has_no_results(html, body_text):
        return False
    return get_html_block_reason(title, html) is not None


def is_429_page(driver) -> bool:
    state = classify_current_page(driver)
    if state.is_usable or state.is_no_results:
        return False
    if state.is_blocked:
        preview = ""
        try:
            preview = re.sub(r"\s+", " ", (driver.page_source or ""))[:220]
        except Exception:
            preview = ""
        print(
            "[recovery] block confirmed | page_state={state} | cards_found={cards} | network_reason={network} "
            "| title={title} | url={url} | html_preview={preview}".format(
                state=state.state,
                cards=state.cards_count,
                network=state.network_reason,
                title=state.title[:120],
                url=state.current_url[:200],
                preview=preview,
            )
        )
        return True
    return False


KPSDK_RECHECK_BLOCK_STATES = {
    PageState.BLOCKED_KPSDK,
    PageState.BLOCKED_HTTP_429,
    PageState.BLOCKED_ACCESS_DENIED,
}

KPSDK_RECHECK_USABLE_STATES = {
    PageState.LISTINGS,
    PageState.NO_RESULTS,
    PageState.DETAIL_READY,
    PageState.DETAIL_REMOVED,
    PageState.DETAIL_SOLD,
    PageState.DETAIL_NOT_FOUND,
}


def _unpack_wait_result(wait_result):
    if isinstance(wait_result, tuple):
        state_result = wait_result[0] if wait_result else None
        payload = wait_result[1] if len(wait_result) > 1 else None
        return state_result, payload
    return wait_result, None


def _call_wait_func(wait_func, driver, timeout, min_cards):
    if min_cards is None:
        return wait_func(driver, timeout=timeout)
    try:
        return wait_func(driver, timeout=timeout, min_cards=min_cards)
    except TypeError:
        return wait_func(driver, timeout=timeout)


def same_session_kpsdk_recheck(
    driver,
    url,
    wait_func,
    safe_get_func,
    log_func=print,
    module_name: str = "Module",
    timeout: int | float = 25,
    min_cards: int | None = 1,
    initial_result=None,
    initial_payload=None,
):
    """Let a KPSDK shell settle in the same browser profile before rotating."""
    state_result = initial_result
    payload = initial_payload
    if state_result is None:
        state_result, payload = _unpack_wait_result(_call_wait_func(wait_func, driver, timeout, min_cards))
    if getattr(state_result, "state", None) != PageState.BLOCKED_KPSDK:
        return state_result, payload

    rechecks = max(0, int(getattr(config, "BROWSER_KPSDK_SAME_SESSION_RECHECKS", 2)))
    settle_seconds = max(0.0, float(getattr(config, "BROWSER_KPSDK_SETTLE_SECONDS", 10)))

    for attempt in range(1, rechecks + 1):
        if settle_seconds:
            time.sleep(settle_seconds)
        safe_get_func(driver, url)
        state_result, payload = _unpack_wait_result(_call_wait_func(wait_func, driver, timeout, min_cards))
        if log_func:
            log_func(
                "{module} KPSDK same-session recheck attempt={attempt} state={state} cards_found={cards} "
                "html_length={html_len} body_text_length={body_len}".format(
                    module=module_name,
                    attempt=attempt,
                    state=getattr(state_result, "state", None),
                    cards=getattr(state_result, "cards_count", 0),
                    html_len=getattr(state_result, "html_length", 0),
                    body_len=getattr(state_result, "body_text_length", 0),
                )
            )
        if getattr(state_result, "state", None) in KPSDK_RECHECK_USABLE_STATES:
            return state_result, payload
        if getattr(state_result, "state", None) not in KPSDK_RECHECK_BLOCK_STATES:
            return state_result, payload

    return state_result, payload


RETRYABLE_NAVIGATION_ERROR_MARKERS = (
    "net::err_http_response_code_failure",
    "net::err_http2_protocol_error",
    "net::err_connection_reset",
    "net::err_network_changed",
    "net::err_timed_out",
    "navigation timeout",
    "timeout",
    "page.goto",
    "navigation",
    "interrupted",
)

UNTRUSTED_RECOVERY_STATES = {
    PageState.BLOCKED_KPSDK,
    PageState.BLOCKED_HTTP_429,
    PageState.BLOCKED_ACCESS_DENIED,
    PageState.BLANK_RENDER,
    PageState.RENDER_TIMEOUT,
    PageState.UNKNOWN,
    PageState.CHROME_ERROR,
}

TRUSTED_RECOVERY_STATES = {
    PageState.LISTINGS,
    PageState.DETAIL_READY,
    PageState.NO_RESULTS,
    PageState.DETAIL_REMOVED,
    PageState.DETAIL_SOLD,
    PageState.DETAIL_NOT_FOUND,
}


def is_retryable_navigation_error(exc: Exception | str | None) -> bool:
    text = str(exc or "").lower()
    return any(marker in text for marker in RETRYABLE_NAVIGATION_ERROR_MARKERS)


def stop_page_loading(driver) -> None:
    try:
        driver.execute_script("window.stop();")
    except Exception:
        pass


def _delay_settings(module_name: str, phase: str) -> tuple[float, float]:
    module = str(module_name or "").strip().upper()
    phase_l = str(phase or "").strip().lower()
    if module == "MODULE2" and "window" in phase_l:
        return (
            float(getattr(config, "MODULE2_INTER_WINDOW_DELAY_SECONDS", 10)),
            float(getattr(config, "MODULE2_INTER_WINDOW_DELAY_JITTER_SECONDS", 5)),
        )
    if module == "MODULE2":
        return (
            float(getattr(config, "MODULE2_INTER_PAGE_DELAY_SECONDS", 8)),
            float(getattr(config, "MODULE2_INTER_PAGE_DELAY_JITTER_SECONDS", 4)),
        )
    if module == "MODULE3":
        return (
            float(getattr(config, "MODULE3_INTER_DETAIL_DELAY_SECONDS", 8)),
            float(getattr(config, "MODULE3_INTER_DETAIL_DELAY_JITTER_SECONDS", 4)),
        )
    return (
        float(getattr(config, "MODULE1_INTER_PAGE_DELAY_SECONDS", 8)),
        float(getattr(config, "MODULE1_INTER_PAGE_DELAY_JITTER_SECONDS", 4)),
    )


def _chrome_error_retry_delay(module_name: str) -> float:
    module = str(module_name or "").strip().upper()
    if module == "MODULE2":
        return float(getattr(config, "MODULE2_CHROME_ERROR_RETRY_DELAY_SECONDS", 3))
    if module == "MODULE3":
        return float(getattr(config, "MODULE3_CHROME_ERROR_RETRY_DELAY_SECONDS", 3))
    return float(getattr(config, "MODULE1_CHROME_ERROR_RETRY_DELAY_SECONDS", 3))


def _chrome_error_reset_enabled(module_name: str) -> bool:
    module = str(module_name or "").strip().upper()
    if module == "MODULE2":
        return bool(getattr(config, "MODULE2_CHROME_ERROR_NAV_RESET", True))
    if module == "MODULE3":
        return bool(getattr(config, "MODULE3_CHROME_ERROR_NAV_RESET", True))
    return bool(getattr(config, "MODULE1_CHROME_ERROR_NAV_RESET", True))


def human_inter_navigation_delay(module_name: str, phase: str, log_func=print) -> float:
    base, jitter = _delay_settings(module_name, phase)
    delay = max(0.0, base) + random.uniform(0.0, max(0.0, jitter))
    if log_func:
        log_func(f"{module_name} inter-navigation delay phase={phase}: {delay:.1f}s")
    if delay > 0:
        time.sleep(delay)
    return delay


def reset_chrome_error_tab(driver, log_func=print) -> bool:
    current_url = ""
    try:
        current_url = str(getattr(driver, "current_url", "") or "")
    except Exception:
        current_url = ""
    if not is_chrome_error_url(current_url):
        return False
    if log_func:
        log_func("chrome-error tab reset: current_url=chrome-error://chromewebdata/")
    stop_page_loading(driver)
    try:
        driver.execute_cdp_cmd("Page.stopLoading", {})
    except Exception:
        pass
    try:
        driver.get("about:blank")
    except Exception:
        try:
            driver.execute_script("window.location.href = 'about:blank';")
        except Exception:
            pass
    time.sleep(0.5)
    return True


def safe_realestate_get_with_reset(
    driver,
    url: str,
    module_name: str,
    phase: str,
    log_func=print,
    *,
    apply_delay: bool = True,
) -> tuple[bool, Exception | None]:
    if _chrome_error_reset_enabled(module_name):
        reset_chrome_error_tab(driver, log_func=log_func)
    if apply_delay:
        human_inter_navigation_delay(module_name, phase, log_func=log_func)
    ok, err = safe_driver_get(driver, url, log_func=log_func)
    current_url = ""
    try:
        current_url = str(getattr(driver, "current_url", "") or "")
    except Exception:
        current_url = ""
    if ok or not (_chrome_error_reset_enabled(module_name) and is_chrome_error_url(current_url)):
        return ok, err
    if log_func:
        log_func(f"{module_name} chrome-error navigation reset before retry phase={phase} requested_url={config.mask_sensitive_text(url)}")
    reset_chrome_error_tab(driver, log_func=log_func)
    retry_delay = max(0.0, _chrome_error_retry_delay(module_name))
    if retry_delay:
        if log_func:
            log_func(f"{module_name} chrome-error retry delay phase={phase}: {retry_delay:.1f}s")
        time.sleep(retry_delay)
    retry_ok, retry_err = safe_driver_get(driver, url, log_func=log_func)
    return retry_ok, retry_err or err


def safe_driver_get(driver, url: str, log_func=print) -> tuple[bool, Exception | None]:
    """Navigate without letting retryable Page.goto failures skip DOM classification."""
    try:
        driver.get(url)
        return True, None
    except Exception as exc:
        stop_page_loading(driver)
        if log_func:
            log_func(f"[recovery] Page.goto retryable render/navigation failure url={url} error={config.mask_sensitive_text(exc)}")
        if is_retryable_navigation_error(exc):
            return False, exc
        return False, exc


def recover_browser_for_untrusted_state(
    *,
    driver,
    current_profile_dir: str,
    build_driver_func,
    rotations_used: int,
    max_rotations: int,
    reason: str,
    job_id: int | None = None,
    search_id: int | None = None,
    log_func=print,
):
    old_profile = os.path.abspath(_sanitize_profile_dir_for_platform(current_profile_dir or config.BROWSER_PROFILE_BASE_DIR))
    if log_func:
        log_func(f"[recovery] requested | old_profile={old_profile} | reason={reason} | job_id={job_id} | search_id={search_id}")
    new_driver, new_rotations, new_profile, status = recover_browser_after_429(
        driver=driver,
        current_profile_dir=old_profile,
        build_driver_func=build_driver_func,
        rotations_used=rotations_used,
        max_rotations=max_rotations,
        log_func=log_func,
    )
    if log_func:
        log_func(
            f"[recovery] completed | action={status} | old_profile={old_profile} | "
            f"new_profile={new_profile} | reason={reason} | job_id={job_id} | search_id={search_id}"
        )
    return new_driver, new_rotations, new_profile, status


def has_normal_realestate_content(driver, body_text: str = "") -> bool:
    try:
        result = driver.execute_script(
            """
            const ogTitle = document.querySelector('meta[property="og:title"]')?.content || '';
            const ogDesc = document.querySelector('meta[property="og:description"]')?.content || '';
            const canonical = document.querySelector('link[rel="canonical"]')?.href || '';
            const keyLink = document.querySelector('a[href*="/agent/"], a[href*="/agency/"], a[href*="/property-"]');
            return {ogTitle, ogDesc, canonical, hasKeyLink: !!keyLink};
            """
        ) or {}
    except Exception:
        result = {}
    canonical = (result.get("canonical") or "").lower()
    markers = ["bed", "bath", "parking", "auction", "guide"]
    body_l = (body_text or "").lower()
    marker_hit = any(m in body_l for m in markers)
    return any([
        bool((result.get("ogTitle") or "").strip()),
        bool((result.get("ogDesc") or "").strip()),
        ("realestate.com.au/property" in canonical) or ("/buy/" in canonical),
        bool(result.get("hasKeyLink")),
        marker_hit and len(body_l) > 200,
    ])


def _extract_visible_text_from_html(html: str) -> str:
    raw = unescape(html or "")
    raw = re.sub(r"(?is)<script.*?>.*?</script>", " ", raw)
    raw = re.sub(r"(?is)<style.*?>.*?</style>", " ", raw)
    raw = re.sub(r"(?is)<[^>]+>", " ", raw)
    return re.sub(r"\s+", " ", raw).strip()


def _html_has_normal_realestate_content(html: str, body_text: str = "") -> bool:
    html_l = (html or "").lower()
    body_l = (body_text or "").lower()
    markers = ["bed", "bath", "parking", "auction", "guide"]
    return any(
        [
            'property="og:title"' in html_l,
            'property="og:description"' in html_l,
            "/property-" in html_l,
            "/agent/" in html_l,
            "/agency/" in html_l,
            any(marker in body_l for marker in markers) and len(body_l) > 200,
        ]
    )


def _html_has_no_results(html: str, body_text: str = "") -> bool:
    text = f"{body_text}\n{_extract_visible_text_from_html(html)}".lower()
    return any(
        marker in text
        for marker in (
            "we couldn't find anything",
            "matches your search",
            "no results",
            "no properties found",
            "0 properties",
            "0 results",
            "try changing your filters",
            "try removing some filters",
        )
    )


def _is_429_from_visible_text(title_l: str, body_text: str, has_normal_content: bool) -> bool:
    return _detect_429_reason(title_l, body_text, has_normal_content) is not None


def _detect_429_reason(title_l: str, body_text: str, has_normal_content: bool, html: str = "") -> str | None:
    body_l = (body_text or "").lower()
    html_l = (html or "").lower()
    merged = f"{title_l}\n{body_l}\n{html_l}"
    short_body = len(body_l) < 3000
    if "http error 429" in merged:
        return "realestate_rate_limited_or_blocked_http_429"
    if "too many requests" in merged:
        return "realestate_rate_limited_or_blocked_http_429"
    if "window.kpsdk" in html_l or "kpsdk" in html_l or "ips.js" in html_l:
        return "realestate_rate_limited_or_blocked_kpsdk"
    if "temporarily blocked" in merged:
        return "realestate_rate_limited_or_blocked"
    if "rate limited" in merged:
        return "realestate_rate_limited_or_blocked"
    if "this page isn't working" in body_l and "http error 429" in body_l:
        return "realestate_rate_limited_or_blocked_http_429"
    if "rate limit" in merged and short_body and not has_normal_content:
        return "realestate_rate_limited_or_blocked"
    return None


def _network_blocked_reason(driver) -> str | None:
    try:
        raw_logs = driver.get_log("performance")
    except Exception:
        return None
    for item in raw_logs:
        try:
            message = json.loads(item.get("message", "{}")).get("message", {})
        except Exception:
            continue
        if message.get("method") != "Network.responseReceived":
            continue
        response = (message.get("params") or {}).get("response") or {}
        url = str(response.get("url") or "").lower()
        headers = {str(k).lower(): str(v) for k, v in (response.get("headers") or {}).items()}
        if "realestate.com.au" not in url:
            continue
        if int(response.get("status") or 0) == 429:
            return "realestate_rate_limited_or_blocked_http_429"
        if any(key.startswith("x-kpsdk") for key in headers):
            return "realestate_rate_limited_or_blocked_kpsdk"
    return None


def get_realestate_blocked_reason(driver) -> str | None:
    state = classify_current_page(driver)
    if state.is_usable or state.is_no_results:
        return None
    if not state.is_blocked:
        return None
    if state.state == PageState.BLOCKED_HTTP_429:
        return "realestate_rate_limited_or_blocked_http_429"
    if state.state == PageState.BLOCKED_KPSDK:
        return "realestate_rate_limited_or_blocked_kpsdk"
    return "realestate_rate_limited_or_blocked"


def raise_if_realestate_blocked(driver) -> None:
    reason = get_realestate_blocked_reason(driver)
    if not reason:
        return
    retry_after = getattr(config, "REA_RATE_LIMIT_BACKOFF_SECONDS", 21600)
    if reason.endswith("http_429"):
        raise RealEstateRateLimitedError(reason, retry_after_seconds=retry_after)
    raise RealEstateBlockedError(reason, retry_after_seconds=retry_after)


def _write_runtime_profile_state(profile_dir: str, reason: str) -> None:
    path = config.BROWSER_PROFILE_STATE_PATH
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "current_profile_dir": profile_dir,
                    "updated_at": datetime.now().isoformat(),
                    "reason": reason,
                },
                f,
                ensure_ascii=False,
                indent=2,
            )
    except OSError as exc:
        print(f"[recovery] warning: could not write browser profile state {path}: {exc}")


def rotate_chrome_profile_safely(profile_dir: str, log_func=print) -> str:
    base_dir = os.path.abspath(_sanitize_profile_dir_for_platform(profile_dir))
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = os.path.abspath(f"{config.BROWSER_PROFILE_BACKUP_PREFIX}{ts}")
    generated_dir = os.path.abspath(f"{config.BROWSER_PROFILE_GENERATED_PREFIX}{ts}")
    try:
        if os.path.isdir(base_dir):
            os.rename(base_dir, backup_dir)
            log_func(f"[recovery] rotate success | old={base_dir} | new={backup_dir} | reason=429")
        os.makedirs(base_dir, exist_ok=True)
        _write_runtime_profile_state(base_dir, "429")
        return base_dir
    except (PermissionError, OSError, shutil.Error) as exc:
        log_func(f"[recovery] rotate fallback | old={base_dir} | new={generated_dir} | reason={exc}")
        os.makedirs(generated_dir, exist_ok=True)
        _write_runtime_profile_state(generated_dir, "429_lock_fallback")
        return generated_dir


def cleanup_browser_processes(log_func=print) -> None:
    commands: list[list[str]] = []
    if os.name == "nt":
        if config.BROWSER_KILL_CHROME_ON_RECOVERY:
            commands.append(["taskkill", "/F", "/IM", "chrome.exe", "/T"])
    else:
        if config.BROWSER_KILL_CHROME_ON_RECOVERY:
            commands.append(["pkill", "-f", "chrome"])
    for cmd in commands:
        try:
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        except Exception as exc:
            log_func(f"[recovery] cleanup warning command={' '.join(cmd)} error={exc}")


def recover_browser_after_429(
    driver,
    current_profile_dir: str,
    build_driver_func,
    rotations_used: int,
    max_rotations: int,
    log_func=print,
):
    if rotations_used >= max_rotations:
        return None, rotations_used, current_profile_dir, "rotation_limit"
    try:
        if driver:
            driver.quit()
    except Exception:
        pass
    time.sleep(5)
    cleanup_browser_processes(log_func=log_func)
    profile_dir = rotate_chrome_profile_safely(current_profile_dir or config.BROWSER_PROFILE_BASE_DIR, log_func=log_func)
    time.sleep(max(0, config.BROWSER_COOLDOWN_ON_429_SECONDS))
    new_driver = build_driver_func(profile_dir_override=profile_dir)
    return new_driver, rotations_used + 1, profile_dir, "recovered"
