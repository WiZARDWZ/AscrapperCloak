from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from html import unescape
from typing import Any

import config
from cloak_browser_helper import By


class PageState:
    CHROME_ERROR = "chrome_error"
    LISTINGS = "listings"
    NO_RESULTS = "no_results"
    DETAIL_READY = "detail_ready"
    DETAIL_REMOVED = "detail_removed"
    DETAIL_SOLD = "detail_sold"
    DETAIL_NOT_FOUND = "detail_not_found"
    BLOCKED_HTTP_429 = "blocked_http_429"
    BLOCKED_KPSDK = "blocked_kpsdk"
    BLOCKED_ACCESS_DENIED = "blocked_access_denied"
    BLANK_RENDER = "blank_render"
    RENDER_TIMEOUT = "render_timeout"
    UNKNOWN = "unknown"


@dataclass
class PageStateResult:
    state: str
    reason: str | None = None
    is_usable: bool = False
    is_blocked: bool = False
    is_no_results: bool = False
    has_cards: bool = False
    cards_count: int = 0
    title: str = ""
    current_url: str = ""
    body_text_length: int = 0
    html_length: int = 0
    network_reason: str | None = None
    detected_markers: list[str] = field(default_factory=list)


CARD_SELECTORS = [
    'article[data-testid="ResidentialCard"]',
    "article.residential-card",
    "article[data-testid]",
    '[data-testid="property-card"]',
]

NO_RESULTS_SELECTORS = [
    '[data-testid*="no-results"]',
    '[data-testid*="noResult"]',
    '[data-testid*="empty"]',
    'div[class*="no-results"]',
    'section[class*="no-results"]',
]

NO_RESULTS_TEXT_MARKERS = [
    "we couldn't find anything",
    "matches your search",
    "no results",
    "no properties found",
    "0 properties",
    "0 results",
    "try changing your filters",
    "try removing some filters",
]

DETAIL_READY_SELECTORS = [
    "script#__NEXT_DATA__",
    "h1",
    '[data-testid="property-info-address"]',
    '[class*="property-info-address"]',
    "div.contact-agent-panel",
    "ul.agent-info",
    '[data-testid="listing-description"]',
    '[data-testid="property-description"]',
    '[data-testid*="listing-price"]',
    '[data-testid="property-price"]',
    "a.sidebar-traffic-driver__name",
]

DETAIL_REMOVED_MARKERS = [
    "listing not found",
    "property not found",
    "this property is no longer available",
    "no longer available",
    "no longer on realestate.com.au",
    "off market",
    "removed",
]

DETAIL_SOLD_PATTERNS = [
    r"\bsold prior to auction\b",
    r"\bsold at auction\b",
    r"\bsold on\b",
    r"\bsold\b",
]

BLOCK_MARKERS = {
    "blocked_kpsdk": ["window.kpsdk", "kpsdk", "ips.js"],
    "blocked_http_429": ["http error 429", "too many requests"],
    "blocked_access_denied": ["access denied", "verify you are human", "captcha", "temporarily blocked"],
}


def _safe_attr(obj: Any, name: str, default: str = "") -> str:
    try:
        return str(getattr(obj, name, default) or "")
    except Exception:
        return default


def is_chrome_error_url(value: str | None) -> bool:
    return str(value or "").strip().lower().startswith("chrome-error://chromewebdata/")


def _safe_script(driver, script: str, default: Any = "") -> Any:
    try:
        return driver.execute_script(script)
    except Exception:
        return default


def _safe_find_elements(driver, selector: str) -> list:
    try:
        return driver.find_elements(By.CSS_SELECTOR, selector)
    except Exception:
        return []


def _visible(el) -> bool:
    try:
        return bool(el.is_displayed())
    except Exception:
        return True


def _html_text(html: str) -> str:
    raw = unescape(html or "")
    raw = re.sub(r"(?is)<script.*?>.*?</script>", " ", raw)
    raw = re.sub(r"(?is)<style.*?>.*?</style>", " ", raw)
    raw = re.sub(r"(?is)<[^>]+>", " ", raw)
    return re.sub(r"\s+", " ", raw).strip()


def _page_snapshot(driver) -> tuple[str, str, str, str]:
    title = _safe_attr(driver, "title")
    current_url = _safe_attr(driver, "current_url")
    body_text = _safe_script(driver, "return document.body ? document.body.innerText : ''", "") or ""
    try:
        html = driver.page_source or ""
    except Exception:
        html = ""
    if not body_text and html:
        body_text = _html_text(html)
    return title, current_url, str(body_text or ""), str(html or "")


def _debug(result: PageStateResult) -> None:
    if not getattr(config, "BROWSER_PAGE_STATE_DEBUG", False):
        return
    print(
        "page_state={state} cards_found={cards} network_reason={network} block_reason={reason} "
        "no_results_detected={no_results} current_url={url} html_length={html_len} body_text_length={body_len}".format(
            state=result.state,
            cards=result.cards_count,
            network=result.network_reason,
            reason=result.reason,
            no_results=result.is_no_results,
            url=result.current_url[:220],
            html_len=result.html_length,
            body_len=result.body_text_length,
        )
    )


def count_listing_cards(driver) -> int:
    max_count = 0
    for selector in CARD_SELECTORS:
        count = sum(1 for el in _safe_find_elements(driver, selector) if _visible(el))
        max_count = max(max_count, count)
    return max_count


def get_listing_cards(driver, min_cards: int = 1) -> list:
    for selector in CARD_SELECTORS:
        cards = [el for el in _safe_find_elements(driver, selector) if _visible(el)]
        if len(cards) >= min_cards:
            return cards
    return []


def has_listing_cards(driver) -> bool:
    return count_listing_cards(driver) > 0


def has_no_results(driver) -> bool:
    if has_listing_cards(driver):
        return False
    for selector in NO_RESULTS_SELECTORS:
        for el in _safe_find_elements(driver, selector):
            if not _visible(el):
                continue
            text = str(getattr(el, "text", "") or "").strip().lower()
            if not text or any(marker in text for marker in NO_RESULTS_TEXT_MARKERS):
                return True
    title, _url, body_text, html = _page_snapshot(driver)
    merged = f"{title}\n{body_text}\n{_html_text(html)}".lower()
    return any(marker in merged for marker in NO_RESULTS_TEXT_MARKERS)


def has_detail_ready_markers(driver) -> bool:
    for selector in DETAIL_READY_SELECTORS:
        for el in _safe_find_elements(driver, selector):
            if _visible(el):
                return True
    title, _url, body_text, html = _page_snapshot(driver)
    html_l = html.lower()
    body_l = body_text.lower()
    title_l = title.lower()
    return any(
        [
            "__next_data__" in html_l,
            'property="og:title"' in html_l and ("property" in title_l or "real estate" in title_l),
            'property="og:description"' in html_l and "property" in html_l,
            "/property-" in html_l,
            "/agent/" in html_l,
            "/agency/" in html_l,
            len(body_l) > 200 and any(marker in body_l for marker in ("bed", "bath", "parking", "auction", "guide")),
        ]
    )


def has_normal_realestate_content(driver) -> bool:
    if has_listing_cards(driver) or has_no_results(driver) or has_detail_ready_markers(driver):
        return True
    title, _url, body_text, html = _page_snapshot(driver)
    html_l = html.lower()
    body_l = body_text.lower()
    title_l = title.lower()
    return any(
        [
            "real estate & property" in title_l,
            'property="og:title"' in html_l and ("realestate.com.au" in html_l or "property" in html_l),
            'property="og:description"' in html_l and "realestate.com.au" in html_l,
            'rel="canonical"' in html_l and ("realestate.com.au/buy" in html_l or "realestate.com.au/property" in html_l),
            "/property-" in html_l,
            len(body_l) > 200 and any(marker in body_l for marker in ("bed", "bath", "parking", "auction", "guide")),
        ]
    )


def get_network_block_reason(driver) -> str | None:
    try:
        raw_logs = driver.get_log("performance")
    except Exception:
        return None
    best = None
    for item in raw_logs:
        try:
            message = json.loads(item.get("message", "{}")).get("message", {})
        except Exception:
            continue
        if message.get("method") != "Network.responseReceived":
            continue
        response = (message.get("params") or {}).get("response") or {}
        url = str(response.get("url") or "").lower()
        if "realestate.com.au" not in url:
            continue
        headers = {str(k).lower(): str(v) for k, v in (response.get("headers") or {}).items()}
        if int(response.get("status") or 0) == 429:
            best = PageState.BLOCKED_HTTP_429
        if any(key.startswith("x-kpsdk") for key in headers):
            best = PageState.BLOCKED_KPSDK if best is None else best
    return best


def get_html_block_reason(driver_or_title, html: str | None = None) -> str | None:
    if html is None:
        driver = driver_or_title
        title, _url, body_text, html_text = _page_snapshot(driver)
    else:
        title = str(driver_or_title or "")
        html_text = html or ""
        body_text = _html_text(html_text)
    merged = f"{title}\n{body_text}\n{html_text}".lower()
    for state, markers in BLOCK_MARKERS.items():
        for marker in markers:
            if marker in merged:
                return state
    return None


def _markers_for_reason(network_reason: str | None, html_reason: str | None) -> list[str]:
    markers = []
    if network_reason:
        markers.append(f"network:{network_reason}")
    if html_reason:
        markers.append(f"html:{html_reason}")
    return markers


def _result(driver, state: str, reason: str | None = None, cards_count: int | None = None, network_reason: str | None = None, markers: list[str] | None = None) -> PageStateResult:
    title, current_url, body_text, html = _page_snapshot(driver)
    cards = count_listing_cards(driver) if cards_count is None else int(cards_count)
    blocked = state in {PageState.BLOCKED_HTTP_429, PageState.BLOCKED_KPSDK, PageState.BLOCKED_ACCESS_DENIED}
    usable = state in {PageState.LISTINGS, PageState.NO_RESULTS, PageState.DETAIL_READY, PageState.DETAIL_REMOVED, PageState.DETAIL_SOLD, PageState.DETAIL_NOT_FOUND}
    out = PageStateResult(
        state=state,
        reason=reason,
        is_usable=usable,
        is_blocked=blocked,
        is_no_results=state == PageState.NO_RESULTS,
        has_cards=cards > 0,
        cards_count=cards,
        title=title,
        current_url=current_url,
        body_text_length=len(body_text),
        html_length=len(html),
        network_reason=network_reason,
        detected_markers=markers or [],
    )
    _debug(out)
    return out


def _block_state(reason: str | None) -> str:
    if reason == PageState.BLOCKED_HTTP_429:
        return PageState.BLOCKED_HTTP_429
    if reason == PageState.BLOCKED_KPSDK:
        return PageState.BLOCKED_KPSDK
    if reason == PageState.BLOCKED_ACCESS_DENIED:
        return PageState.BLOCKED_ACCESS_DENIED
    return PageState.BLOCKED_ACCESS_DENIED


def classify_search_page(driver, timeout=None, min_cards: int = 1, grace_seconds=None) -> PageStateResult:
    title, current_url, body_text, html = _page_snapshot(driver)
    if is_chrome_error_url(current_url):
        return _result(driver, PageState.CHROME_ERROR, "chrome_error_page", cards_count=0)
    cards = count_listing_cards(driver)
    network_reason = get_network_block_reason(driver)
    if cards >= min_cards:
        return _result(driver, PageState.LISTINGS, "listing_cards_present", cards_count=cards, network_reason=network_reason)
    if has_no_results(driver):
        return _result(driver, PageState.NO_RESULTS, "stable_no_results", cards_count=cards, network_reason=network_reason)
    if has_normal_realestate_content(driver):
        return _result(driver, PageState.UNKNOWN, "normal_content_without_cards", cards_count=cards, network_reason=network_reason)
    html_reason = get_html_block_reason(driver)
    if html_reason or network_reason:
        reason = html_reason or network_reason
        return _result(driver, _block_state(reason), reason, cards_count=cards, network_reason=network_reason, markers=_markers_for_reason(network_reason, html_reason))
    title, _url, body_text, html = _page_snapshot(driver)
    if len(body_text.strip()) == 0 and len(html.strip()) < 1200:
        return _result(driver, PageState.BLANK_RENDER, "blank_or_tiny_render", cards_count=cards, network_reason=network_reason)
    return _result(driver, PageState.RENDER_TIMEOUT if timeout else PageState.UNKNOWN, "no_cards_no_no_results_no_block", cards_count=cards, network_reason=network_reason)


def classify_detail_page(driver, timeout=None, grace_seconds=None) -> PageStateResult:
    network_reason = get_network_block_reason(driver)
    title, _url, body_text, html = _page_snapshot(driver)
    if is_chrome_error_url(_url):
        return _result(driver, PageState.CHROME_ERROR, "chrome_error_page", network_reason=network_reason)
    merged = f"{title}\n{body_text}\n{_html_text(html)}".lower()
    if has_detail_ready_markers(driver):
        return _result(driver, PageState.DETAIL_READY, "detail_ready_marker", network_reason=network_reason)
    if any(marker in merged for marker in DETAIL_REMOVED_MARKERS):
        state = PageState.DETAIL_NOT_FOUND if "not found" in merged else PageState.DETAIL_REMOVED
        return _result(driver, state, state, network_reason=network_reason)
    if any(re.search(pattern, merged, flags=re.I) for pattern in DETAIL_SOLD_PATTERNS):
        return _result(driver, PageState.DETAIL_SOLD, "sold_evidence", network_reason=network_reason)
    if has_normal_realestate_content(driver):
        return _result(driver, PageState.UNKNOWN, "normal_content_without_detail_ready", network_reason=network_reason)
    html_reason = get_html_block_reason(driver)
    if html_reason or network_reason:
        reason = html_reason or network_reason
        return _result(driver, _block_state(reason), reason, network_reason=network_reason, markers=_markers_for_reason(network_reason, html_reason))
    if len(body_text.strip()) == 0 and len(html.strip()) < 1200:
        return _result(driver, PageState.BLANK_RENDER, "blank_or_tiny_render", network_reason=network_reason)
    return _result(driver, PageState.RENDER_TIMEOUT if timeout else PageState.UNKNOWN, "detail_not_ready_no_block", network_reason=network_reason)


def classify_current_page(driver) -> PageStateResult:
    search = classify_search_page(driver)
    if search.state in {PageState.LISTINGS, PageState.NO_RESULTS} or search.is_blocked:
        return search
    detail = classify_detail_page(driver)
    if detail.is_usable or detail.is_blocked:
        return detail
    return search if search.state != PageState.UNKNOWN else detail


def wait_for_search_page_state(driver, timeout, min_cards: int = 1) -> tuple[PageStateResult, list]:
    deadline = time.time() + float(timeout or getattr(config, "BROWSER_BLOCK_GRACE_SECONDS", 30))
    poll = max(0.05, float(getattr(config, "BROWSER_BLOCK_POLL_SECONDS", 1.0)))
    no_results_seen_at = None
    last = classify_search_page(driver, min_cards=min_cards)
    while time.time() <= deadline:
        cards = get_listing_cards(driver, min_cards=min_cards)
        if len(cards) >= min_cards:
            return classify_search_page(driver, min_cards=min_cards), cards
        current = classify_search_page(driver, timeout=timeout, min_cards=min_cards)
        last = current
        if current.state == PageState.NO_RESULTS:
            if no_results_seen_at is None:
                no_results_seen_at = time.time()
            if time.time() - no_results_seen_at >= float(getattr(config, "BROWSER_NO_RESULTS_STABLE_SECONDS", 1.0)):
                return current, []
        else:
            no_results_seen_at = None
        # DOM block markers are definitive. Network-only 429/x-kpsdk is noisy on
        # realestate.com.au and must survive the grace window before it wins.
        if current.is_blocked and any(marker.startswith("html:") for marker in current.detected_markers):
            return current, []
        time.sleep(poll)
    final = classify_search_page(driver, timeout=timeout, min_cards=min_cards)
    if final.state == PageState.UNKNOWN:
        final.state = PageState.RENDER_TIMEOUT
        final.reason = final.reason or "render_timeout"
    return final, get_listing_cards(driver, min_cards=min_cards)


def wait_for_detail_page_state(driver, timeout) -> PageStateResult:
    deadline = time.time() + float(timeout or getattr(config, "BROWSER_BLOCK_GRACE_SECONDS", 30))
    poll = max(0.05, float(getattr(config, "BROWSER_BLOCK_POLL_SECONDS", 1.0)))
    last = classify_detail_page(driver, timeout=timeout)
    while time.time() <= deadline:
        current = classify_detail_page(driver, timeout=timeout)
        last = current
        if current.is_usable:
            return current
        if current.is_blocked and any(marker.startswith("html:") for marker in current.detected_markers):
            return current
        time.sleep(poll)
    if last.state == PageState.UNKNOWN:
        last.state = PageState.RENDER_TIMEOUT
        last.reason = last.reason or "detail_render_timeout"
    return last
