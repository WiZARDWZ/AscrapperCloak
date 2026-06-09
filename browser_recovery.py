import json
import os
import shutil
import time
import re
import subprocess
from datetime import datetime
from html import unescape

import config
from realestate_errors import RealEstateBlockedError, RealEstateRateLimitedError
from realestate_page_state import (
    PageState,
    classify_current_page,
    get_html_block_reason,
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
