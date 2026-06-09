import csv
import json
import os
import platform
import re
import time
import gc
import codecs
import tempfile
from collections import defaultdict
from datetime import datetime
from urllib.parse import urljoin, urlparse, urlunparse

from cloak_browser_helper import (
    By,
    EC,
    WebDriverWait,
    TimeoutException,
    NoSuchElementException,
    StaleElementReferenceException,
    WebDriverException,
)

from config import AREA_SEARCH_URL
import config
from chrome_options_helper import build_chrome_driver, cleanup_chrome_driver
from browser_recovery import BrowserSessionHealth, RecoveryPolicy, is_429_page, log_session_health, raise_if_realestate_blocked, recover_browser_for_untrusted_state as recover_browser_after_429, same_session_kpsdk_recheck, safe_realestate_get_with_reset, safe_driver_get, UNTRUSTED_RECOVERY_STATES
from realestate_page_state import PageState, classify_search_page, get_listing_cards, wait_for_search_page_state
from area_parser import extract_area_display, parse_area_to_sqm
from realestate_errors import RealEstateBlockedError


# --- Fix: جلوگیری از WinError 6 در پایان برنامه (UC روی ویندوز) ---
BASE = "https://www.realestate.com.au"

CARD_SELECTORS = [
    'article[data-testid="ResidentialCard"]',
    "article.residential-card",
    "article[data-testid]",
    '[data-testid="property-card"]',
]

PRICE_SELECTORS = [
    '[data-testid="property-price"]',
    '[data-testid="listing-card-price"]',
    '[data-testid="listing-card-price-primary"]',
    '[data-testid="listing-price"]',
    ".property-price",
    ".residential-card__price",
    ".price",
    'span[class*="price"]',
    'p[class*="price"]',
]

ADDRESS_SELECTORS = [
    "h2.residential-card__address-heading a span",
    "h2.residential-card__address-heading a",
    '[data-testid="address-line1"]',
    '[data-testid="addressLine1"]',
    '[data-testid="address"]',
]

PROPERTY_TYPE_SELECTORS = [
    "ul.residential-card__primary > p",
    '[data-testid="property-type"]',
]

AGENCY_SELECTORS = [
    "img.branding__image",
    '[data-testid="property-card-branding"] img',
    ".agency-name",
]

DETAIL_LINK_SELECTORS = [
    "a.details-link",
    'a[href*="/property-"]',
    'a[href*="/sold/property-"]',
]

NEXT_SELECTORS = [
    # فقط داخل body قابل مشاهده باشد
    'a[aria-label*="Next"]',
    'button[aria-label*="Next"]',
    '[data-testid*="next"]',
    '[data-testid*="paginator-next"]',
    'a[rel="next"]',
]

# ✅ برای تشخیص تعداد کل صفحات
PAGINATION_CONTAINERS = [
    'nav[aria-label*="Pagination"]',
    'nav[aria-label*="pagination"]',
    '[data-testid*="pagination"]',
    '[data-testid*="paginator"]',
]


def setup_driver(profile_dir_override: str | None = None):
    return build_chrome_driver(profile_dir_override=profile_dir_override)


def is_linux() -> bool:
    return platform.system().lower() == "linux"


def is_headless_enabled() -> bool:
    return os.getenv("HEADLESS", "0") == "1"


def safe_get(driver, url: str, *, phase: str = "list", apply_delay: bool = False, log_func=print):
    """Navigate while retaining retryable failures for DOM-first recovery decisions."""
    ok, exc = safe_realestate_get_with_reset(
        driver,
        url,
        module_name="Module1",
        phase=phase,
        log_func=log_func,
        apply_delay=apply_delay,
    )
    try:
        driver._module1_last_navigation = {
            "url": url,
            "navigation_failed": not ok,
            "navigation_error": exc,
        }
    except Exception:
        pass
    return ok


def _last_navigation(driver) -> dict:
    try:
        value = getattr(driver, "_module1_last_navigation", {}) or {}
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _current_url(driver, state_result=None) -> str:
    if state_result is not None and getattr(state_result, "current_url", None):
        return str(state_result.current_url or "")
    try:
        return str(getattr(driver, "current_url", "") or "")
    except Exception:
        return ""


def _is_chrome_error_url(value: str | None) -> bool:
    return str(value or "").strip().lower().startswith("chrome-error://chromewebdata")


def _navigation_needs_fast_classification(driver, navigation_info: dict) -> bool:
    return _is_chrome_error_url(_current_url(driver))


def _module1_state_is_recoverable(driver, state_result, navigation_info: dict) -> bool:
    if _is_chrome_error_url(_current_url(driver, state_result)):
        return True
    if getattr(state_result, "state", None) in UNTRUSTED_RECOVERY_STATES:
        return True
    return bool(navigation_info.get("navigation_failed") and getattr(state_result, "state", None) not in {PageState.LISTINGS, PageState.NO_RESULTS})


def _module1_recovery_reason(driver, state_result, navigation_info: dict) -> str:
    state = getattr(state_result, "state", None) or "unknown"
    reason = getattr(state_result, "reason", None) or state
    if _is_chrome_error_url(_current_url(driver, state_result)):
        reason = f"chrome_error:{reason}"
    if navigation_info.get("navigation_error") is not None:
        reason = f"navigation_failed:{reason}:{navigation_info.get('navigation_error')}"
    return config.mask_sensitive_text(reason)


def _module1_is_same_page_kpsdk_candidate(driver, requested_url: str, state_result) -> bool:
    """Return True when a realestate search page should be allowed to hydrate in-place.

    realestate.com.au can briefly expose a KPSDK/429 shell while the same tab is
    still on the requested URL and the DOM is actively hydrating. Re-navigating
    that URL immediately can turn the temporary shell into Chrome's
    ``chrome-error://chromewebdata/`` page, so Module1 first settles the current
    DOM without another ``driver.get``.
    """
    current_url = _current_url(driver, state_result)
    if _is_chrome_error_url(current_url):
        return False
    current_l = str(current_url or "").lower()
    requested_l = str(requested_url or "").lower()
    if "realestate.com.au" not in current_l:
        return False
    if requested_l and "realestate.com.au" in requested_l:
        current_parsed = urlparse(current_l)
        requested_parsed = urlparse(requested_l)
        if (current_parsed.netloc, current_parsed.path) != (requested_parsed.netloc, requested_parsed.path):
            return False
    network_reason = str(getattr(state_result, "network_reason", "") or "").lower()
    reason = str(getattr(state_result, "reason", "") or "").lower()
    state = getattr(state_result, "state", None)
    is_transient_block = state in {PageState.BLOCKED_KPSDK, PageState.BLOCKED_HTTP_429}
    is_network_429 = "blocked_http_429" in network_reason or "blocked_http_429" in reason
    is_kpsdk = "kpsdk" in network_reason or "kpsdk" in reason
    if not (is_transient_block or is_network_429 or is_kpsdk):
        return False
    html_len = int(getattr(state_result, "html_length", 0) or 0)
    body_len = int(getattr(state_result, "body_text_length", 0) or 0)
    return html_len < 5000 or body_len < 250 or state in {PageState.BLOCKED_KPSDK, PageState.BLOCKED_HTTP_429}


def _module1_same_page_kpsdk_settle(driver, requested_url: str, state_result, cards, *, min_cards: int, log):
    """Settle transient KPSDK/429 shells on the same page before re-navigation.

    This preserves the browser tab when the URL is still the requested REA page,
    allowing listings/cards to win over historical network 429s.
    """
    if not _module1_is_same_page_kpsdk_candidate(driver, requested_url, state_result):
        return state_result, cards

    log(
        "Module1 transient KPSDK detected; same-page settle start "
        f"state={getattr(state_result, 'state', None)} current_url={_current_url(driver, state_result)} "
        f"html_length={getattr(state_result, 'html_length', 0) or 0} "
        f"body_text_length={getattr(state_result, 'body_text_length', 0) or 0}"
    )

    settle_seconds = max(0.0, float(getattr(config, "BROWSER_KPSDK_SETTLE_SECONDS", 10)))
    grace_seconds = max(settle_seconds, float(getattr(config, "BROWSER_BLOCK_GRACE_SECONDS", 30)))
    poll_seconds = max(0.05, float(getattr(config, "BROWSER_BLOCK_POLL_SECONDS", 1.0)))
    deadline = time.time() + grace_seconds
    next_sleep = settle_seconds if settle_seconds > 0 else poll_seconds
    previous_html_len = int(getattr(state_result, "html_length", 0) or 0)
    last_result = state_result
    last_cards = cards or []

    while time.time() <= deadline:
        time.sleep(min(next_sleep, max(0.0, deadline - time.time())))
        current = classify_search_page(driver, timeout=True, min_cards=min_cards)
        current_cards = get_listing_cards(driver, min_cards=min_cards)
        if current_cards and current.state != PageState.LISTINGS:
            current = classify_search_page(driver, min_cards=min_cards)
        log(
            "Module1 same-page settle result state={state} cards_found={cards} html_length={html_len} "
            "body_text_length={body_len} network_reason={network} current_url={url}".format(
                state=current.state,
                cards=getattr(current, "cards_count", len(current_cards)),
                html_len=getattr(current, "html_length", 0) or 0,
                body_len=getattr(current, "body_text_length", 0) or 0,
                network=getattr(current, "network_reason", None),
                url=getattr(current, "current_url", "") or _current_url(driver),
            )
        )
        last_result = current
        last_cards = current_cards
        if current.state == PageState.LISTINGS or len(current_cards) >= min_cards:
            log("Module1 DOM-first listings after transient KPSDK; ignoring historical 429")
            if current.state != PageState.LISTINGS:
                current.state = PageState.LISTINGS
                current.reason = current.reason or "listing_cards_present"
                current.cards_count = max(getattr(current, "cards_count", 0) or 0, len(current_cards))
            return current, current_cards
        if current.state == PageState.NO_RESULTS:
            return current, []
        if _is_chrome_error_url(_current_url(driver, current)):
            return current, []

        html_len = int(getattr(current, "html_length", 0) or 0)
        title = str(getattr(current, "title", "") or "").lower()
        still_loading_rea = "real estate" in title or "realestate.com.au" in title or html_len > previous_html_len
        if not still_loading_rea:
            break
        previous_html_len = max(previous_html_len, html_len)
        next_sleep = poll_seconds

    return last_result, last_cards


def _recover_module1_untrusted_page(
    *,
    driver,
    profile_dir_current: str,
    rotations_used: int,
    max_rotations: int,
    state_result,
    navigation_info: dict,
    url: str,
    log,
):
    reason = _module1_recovery_reason(driver, state_result, navigation_info)
    log(f"Module1 untrusted page state={getattr(state_result, 'state', None)} current_url={_current_url(driver, state_result)}. Recovering profile/session reason={reason}.")
    driver, rotations_used, profile_dir_current, recovery_status = recover_browser_after_429(
        driver=driver,
        current_profile_dir=profile_dir_current,
        build_driver_func=setup_driver,
        rotations_used=rotations_used,
        max_rotations=max_rotations,
        reason=reason,
        log_func=log,
    )
    if recovery_status == "recovered":
        safe_get(driver, url, phase="post_rotation_retry", apply_delay=False, log_func=log)
    return driver, rotations_used, profile_dir_current, recovery_status


def _stop_page_loading(driver) -> None:
    try:
        driver.execute_script("window.stop();")
    except Exception:
        pass
    try:
        driver.execute_cdp_cmd("Page.stopLoading", {})
    except Exception:
        pass


def _module1_pagination_nav_mode() -> str:
    mode = str(getattr(config, "MODULE1_PAGINATION_NAV_MODE", "") or "").strip().lower()
    if mode in {"click_next", "direct_url", "fresh_context_per_page"}:
        return mode
    return "click_next" if getattr(config, "BROWSER_ENGINE", "") == "cloak" else "direct_url"


def _make_fresh_module1_profile_dir(page: int) -> str:
    root = os.path.join(str(getattr(config, "OUTPUT_DIR", "output") or "output"), "module1_fresh_profiles")
    os.makedirs(root, exist_ok=True)
    return tempfile.mkdtemp(prefix=f"page_{page}_", dir=root)


def _human_scroll_idle_before_next(driver, log) -> None:
    try:
        driver.execute_script(
            """
            (() => {
              const maxY = Math.max(0, Math.floor(document.body.scrollHeight * 0.72));
              window.scrollTo({top: maxY, behavior: 'smooth'});
              setTimeout(() => window.scrollBy({top: -120, behavior: 'smooth'}), 250);
            })();
            """
        )
    except Exception as exc:
        log(f"Module1 click-next human scroll warning={config.mask_sensitive_text(exc)}")
    time.sleep(0.6)


def _click_next_anchor(driver, next_page: int, log) -> dict:
    """Click the real pagination Next anchor without Selenium-style JS arguments."""
    script = f"""
    (() => {{
      const nextPage = {int(next_page)};
      const selectors = [
        'a[rel="next"]',
        'a[aria-label*="Go to next page" i]',
        `a[href*="/list-${{nextPage}}"]`
      ];
      const seen = new Set();
      const candidates = [];
      for (const selector of selectors) {{
        for (const anchor of Array.from(document.querySelectorAll(selector))) {{
          if (seen.has(anchor)) continue;
          seen.add(anchor);
          candidates.push(anchor);
        }}
      }}
      const isVisible = (el) => {{
        const style = window.getComputedStyle(el);
        const rect = el.getBoundingClientRect();
        return style && style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
      }};
      const isDisabled = (el) => {{
        const cls = String(el.getAttribute('class') || '').toLowerCase();
        return el.getAttribute('aria-disabled') === 'true' || el.hasAttribute('disabled') || cls.includes('disabled');
      }};
      const paginationLike = (el, href, rel, aria, text) => {{
        const haystack = `${{rel}} ${{aria}} ${{text}}`.toLowerCase();
        const bad = `${{href}} ${{aria}} ${{text}}`.toLowerCase();
        if (bad.includes('nextroll') || bad.includes('privacy') || bad.includes('advertising')) return false;
        return rel === 'next' || /\b(next|go to next page|next page|pagination)\b/i.test(haystack);
      }};
      for (const anchor of candidates) {{
        const href = String(anchor.href || anchor.getAttribute('href') || '');
        const rel = String(anchor.getAttribute('rel') || '').toLowerCase();
        const aria = String(anchor.getAttribute('aria-label') || '');
        const text = String(anchor.innerText || anchor.textContent || '').trim();
        const hrefTargetsNextPage = href.includes(`/list-${{nextPage}}`);
        const relNext = rel.split(/\\s+/).includes('next');
        if (!isVisible(anchor) || isDisabled(anchor)) continue;
        if (!(hrefTargetsNextPage || relNext)) continue;
        if (!paginationLike(anchor, href, relNext ? 'next' : rel, aria, text)) continue;
        anchor.scrollIntoView({{block: 'center', inline: 'center'}});
        for (const type of ['mouseover', 'mousemove', 'mousedown', 'mouseup', 'click']) {{
          anchor.dispatchEvent(new MouseEvent(type, {{bubbles: true, cancelable: true, view: window}}));
        }}
        if (document.location.href === href && hrefTargetsNextPage) {{
          return {{clicked: true, href, rel, aria, text, currentUrl: document.location.href}};
        }}
        try {{ anchor.click(); }} catch (err) {{}}
        return {{clicked: true, href, rel, aria, text, currentUrl: document.location.href}};
      }}
      return {{clicked: false, reason: 'next_anchor_not_found', nextPage}};
    }})();
    """
    result = driver.execute_script(script)
    if not isinstance(result, dict):
        result = {"clicked": bool(result)}
    href = result.get("href") or ""
    if href:
        log(f"Module1 click-next href={href}")
    else:
        log(f"Module1 click-next failed reason={result.get('reason', 'unknown')}")
    return result


def _module1_fresh_context_get(driver, page_url: str, page: int, *, log):
    old_driver = driver
    try:
        if old_driver:
            old_driver.quit()
    except Exception:
        pass
    try:
        if old_driver:
            cleanup_chrome_driver(old_driver)
    except Exception:
        pass
    profile_dir = _make_fresh_module1_profile_dir(page)
    log(f"Module1 fresh_context_per_page fallback page={page} profile_dir={profile_dir}")
    new_driver = setup_driver(profile_dir_override=profile_dir)
    navigation_ok = safe_get(new_driver, page_url, phase=f"fresh_context_page_{page}", apply_delay=False, log_func=log)
    navigation_info = _last_navigation(new_driver)
    return new_driver, profile_dir, navigation_ok, navigation_info


def _same_session_kpsdk_recheck(driver, url: str, timeout: int | float, min_cards: int, state_result, cards, log):
    return same_session_kpsdk_recheck(
        driver=driver,
        url=url,
        wait_func=wait_for_search_page_state,
        safe_get_func=safe_get,
        log_func=log,
        module_name="Module1",
        timeout=timeout,
        min_cards=min_cards,
        initial_result=state_result,
        initial_payload=cards,
    )


def _collect_network_debug(driver) -> dict:
    request_meta = {}
    request_bytes = defaultdict(int)
    domain_bytes = defaultdict(int)
    type_bytes = defaultdict(int)
    try:
        raw_logs = driver.get_log("performance")
    except Exception:
        return {"request_count": 0, "transferred_bytes": 0, "top_resource": "n/a", "top_domain": "n/a"}
    for item in raw_logs:
        try:
            payload = json.loads(item.get("message", "{}")).get("message", {})
        except Exception:
            continue
        method = payload.get("method")
        params = payload.get("params", {})
        if method == "Network.responseReceived":
            response = params.get("response", {})
            request_id = params.get("requestId")
            if request_id:
                request_meta[request_id] = {
                    "url": response.get("url", ""),
                    "mimeType": response.get("mimeType", ""),
                    "resourceType": params.get("type", ""),
                    "domain": (urlparse(response.get("url", "")).netloc or "unknown"),
                }
        elif method == "Network.loadingFinished":
            request_id = params.get("requestId")
            encoded_len = int(params.get("encodedDataLength", 0) or 0)
            request_bytes[request_id] += encoded_len
    resource_rows = []
    for request_id, total_bytes in request_bytes.items():
        meta = request_meta.get(request_id, {})
        domain = meta.get("domain", "unknown")
        rtype = meta.get("resourceType", "unknown")
        domain_bytes[domain] += total_bytes
        type_bytes[rtype] += total_bytes
        resource_rows.append(
            {
                "url": meta.get("url", ""),
                "mimeType": meta.get("mimeType", ""),
                "resourceType": rtype,
                "encodedDataLength": total_bytes,
                "domain": domain,
            }
        )
    resource_rows.sort(key=lambda x: x["encodedDataLength"], reverse=True)
    top_n = max(1, int(getattr(config, "NETWORK_DEBUG_TOP_N", 30)))
    if config.NETWORK_DEBUG:
        print("Top downloaded resources by encodedDataLength")
        for row in resource_rows[:top_n]:
            print(f"- {row['encodedDataLength']}B | {row['resourceType']} | {row['mimeType']} | {row['domain']} | {row['url']}")
        print("Top domains by transferred bytes")
        for domain, total_bytes in sorted(domain_bytes.items(), key=lambda x: x[1], reverse=True)[:top_n]:
            print(f"- {domain}: {total_bytes}B")
        print("Resource type totals")
        for rtype, total_bytes in sorted(type_bytes.items(), key=lambda x: x[1], reverse=True):
            print(f"- {rtype or 'unknown'}: {total_bytes}B")
    transferred = sum(request_bytes.values())
    top_resource = resource_rows[0]["url"] if resource_rows else "n/a"
    top_domain = max(domain_bytes, key=domain_bytes.get) if domain_bytes else "n/a"
    return {
        "request_count": len(request_bytes),
        "transferred_bytes": transferred,
        "top_resource": top_resource,
        "top_domain": top_domain,
    }


def _first_text(root, selectors):
    for sel in selectors:
        try:
            el = root.find_element(By.CSS_SELECTOR, sel)
            txt = (el.text or "").strip()
            if txt:
                return txt
        except NoSuchElementException:
            continue
        except StaleElementReferenceException:
            return None
    return None


def _first_attr(root, selectors, attr):
    for sel in selectors:
        try:
            el = root.find_element(By.CSS_SELECTOR, sel)
            val = (el.get_attribute(attr) or "").strip()
            if val:
                return val
        except NoSuchElementException:
            continue
        except StaleElementReferenceException:
            return None
    return None


def _unescape_rea(s: str | None) -> str | None:
    if not s:
        return None
    s = s.replace("\\u002F", "/").replace("\\u002B", "+").replace("\\u0026", "&")
    s = s.replace("\\u003D", "=").replace("\\u002C", ",").replace("\\u003A", ":")
    s = s.replace('\\"', '"')
    try:
        return codecs.decode(s, "unicode_escape")
    except Exception:
        return s


def _parse_listing_id(url: str | None) -> str | None:
    if not url:
        return None
    m = re.search(r"-(\d+)(?:[/?].*)?$", url)
    return m.group(1) if m else None


def _extract_from_page_source(page_source: str, listing_id: str | None) -> dict:
    """
    fallback: نزدیک listing_id در HTML یک window برمی‌داریم و چند فیلد را از JSON تزریق‌شده regex می‌کنیم.
    """
    if not listing_id:
        return {}

    idx = page_source.find(listing_id)
    if idx == -1:
        return {}

    window = page_source[max(0, idx - 12000): idx + 24000]

    def grab(pattern):
        m = re.search(pattern, window)
        if not m:
            return None
        return _unescape_rea(m.group(1))

    def grab_int(pattern):
        m = re.search(pattern, window)
        if not m:
            return None
        return m.group(1)

    inspection_long = grab(r'\\\"inspections\\\":\\\[\\\{.*?\\\"label\\\":\\\"(.*?)\\\"')
    inspection_short = grab(r'\\\"inspections\\\":\\\[\\\{.*?\\\"labelShort\\\":\\\"(.*?)\\\"')

    auction_label = grab(r'\\\"auction\\\":\\\{.*?\\\"labelShort\\\":\\\"(.*?)\\\"')
    auction_long = grab(r'\\\"auction\\\":\\\{.*?\\\"label\\\":\\\"(.*?)\\\"')

    return {
        "price": grab(r'\\\"price\\\":\{\\\"display\\\":\\\"(.*?)\\\"'),
        "full_address": grab(r'\\\"fullAddress\\\":\\\"(.*?)\\\"'),
        "agency": grab(r'\\\"listingCompany\\\":\{\\\"name\\\":\\\"(.*?)\\\"'),
        "property_type": grab(r'\\\"propertyType\\\":\{\\\"id\\\":\\\".*?\\\",\\\"display\\\":\\\"(.*?)\\\"'),
        "bedrooms": grab_int(r'\\\"bedrooms\\\":\{\\\"value\\\":(\d+)'),
        "bathrooms": grab_int(r'\\\"bathrooms\\\":\{\\\"value\\\":(\d+)'),
        "parking": grab_int(r'\\\"parkingSpaces\\\":\{\\\"value\\\":(\d+)'),

        "inspection_short_label": inspection_short,
        "inspection_long_label": inspection_long,
        "auction_label": auction_label,
        "auction_long_label": auction_long,
    }


def wait_for_cards(driver, timeout=25, min_cards=1):
    def _cond(d):
        for sel in CARD_SELECTORS:
            try:
                els = d.find_elements(By.CSS_SELECTOR, sel)
                if len(els) >= min_cards:
                    return els
            except WebDriverException:
                continue
        return False

    return WebDriverWait(driver, timeout).until(_cond)


def _get_feature_by_aria(card, keyword: str):
    try:
        li = card.find_element(By.CSS_SELECTOR, f'ul.residential-card__primary li[aria-label*="{keyword}"]')
        try:
            p = li.find_element(By.CSS_SELECTOR, "p")
            v = (p.text or "").strip()
            return v or None
        except NoSuchElementException:
            v = (li.text or "").strip()
            return v or None
    except NoSuchElementException:
        return None
    except StaleElementReferenceException:
        return None


def _size_feature_dict(kind: str, text: str | None) -> dict:
    display = extract_area_display(text)
    sqm = parse_area_to_sqm(text)
    if not display or sqm is None:
        return {}
    pascal = {
        "land_size": "LandSize",
        "building_size": "BuildingSize",
        "floor_area": "FloorArea",
    }[kind]
    return {
        f"{kind}_display": display,
        f"{kind}_sqm": sqm,
        f"{pascal}Display": display,
        f"{pascal}Sqm": sqm,
    }


def _extract_size_features_from_text_pairs(pairs: list[tuple[str, str]]) -> dict:
    out = {}
    for label, text in pairs:
        label_lower = (label or "").lower()
        source = text or label
        if "land size" in label_lower and not out.get("land_size_display"):
            out.update(_size_feature_dict("land_size", source))
        elif "building size" in label_lower and not out.get("building_size_display"):
            out.update(_size_feature_dict("building_size", source))
        elif any(key in label_lower for key in ("floor area", "internal area", "living area")) and not out.get("floor_area_display"):
            out.update(_size_feature_dict("floor_area", source))
    return out


def _get_size_features_by_aria(card) -> dict:
    pairs: list[tuple[str, str]] = []
    try:
        for li in card.find_elements(By.CSS_SELECTOR, "li[aria-label]"):
            label = (li.get_attribute("aria-label") or "").strip()
            text = ""
            try:
                p = li.find_element(By.CSS_SELECTOR, "p")
                text = (p.text or "").strip()
            except Exception:
                text = (li.text or "").strip()
            pairs.append((label, text or label))
    except Exception:
        pass
    try:
        for ul in card.find_elements(By.CSS_SELECTOR, "ul[aria-label]"):
            label = (ul.get_attribute("aria-label") or "").strip()
            pairs.append((label, label))
    except Exception:
        pass
    return _extract_size_features_from_text_pairs(pairs)


def _extract_size_features_from_html_regex(html_text: str) -> dict:
    pairs = []
    for li in re.finditer(r'<li[^>]*aria-label=["\']([^"\']+)["\'][^>]*>(.*?)</li>', html_text or "", flags=re.I | re.S):
        label = li.group(1)
        text = re.sub(r"<[^>]+>", " ", li.group(2))
        pairs.append((label, re.sub(r"\s+", " ", text).strip() or label))
    for ul in re.finditer(r'<ul[^>]*aria-label=["\']([^"\']+)["\'][^>]*>', html_text or "", flags=re.I | re.S):
        pairs.append((ul.group(1), ul.group(1)))
    return _extract_size_features_from_text_pairs(pairs)


def extract_size_features_from_card_html(html_text: str) -> dict:
    try:
        from bs4 import BeautifulSoup
    except Exception:
        BeautifulSoup = None
    if BeautifulSoup is None:
        return _extract_size_features_from_html_regex(html_text)
    soup = BeautifulSoup(html_text or "", "html.parser")
    if soup is None:
        return _extract_size_features_from_html_regex(html_text)
    pairs: list[tuple[str, str]] = []
    for li in soup.select("li[aria-label]"):
        label = (li.get("aria-label") or "").strip()
        text = li.get_text(" ", strip=True) or label
        pairs.append((label, text))
    for ul in soup.select("ul[aria-label]"):
        label = (ul.get("aria-label") or "").strip()
        pairs.append((label, label))
    return _extract_size_features_from_text_pairs(pairs)


def _get_inspection_labels(card):
    short = _first_text(card, ["span.inspection__short-label", 'span[class*="inspection__short-label"]'])
    long_ = _first_text(card, ["span.inspection__long-label", 'span[class*="inspection__long-label"]'])
    return short or None, long_ or None


def _get_auction_labels(card):
    """
    از HTML کارت:
    div[class*="AuctionDetails"] ... span[role=text] => ["Auction Sat 14 Mar", "11:15 am"]
    """
    try:
        containers = card.find_elements(By.CSS_SELECTOR, 'div[class*="AuctionDetails"]')
        for c in containers:
            if not c.is_displayed():
                continue
            spans = c.find_elements(By.CSS_SELECTOR, 'span[role="text"]')
            txts = [(s.text or "").strip() for s in spans]
            txts = [t for t in txts if t]
            if txts:
                auction_label = txts[0]
                auction_time = txts[1] if len(txts) > 1 else None
                return auction_label, auction_time
    except Exception:
        pass

    try:
        el = card.find_element(By.XPATH, ".//*[contains(normalize-space(.), 'Auction')]")
        t = (el.text or "").strip()
        if t:
            return t, None
    except Exception:
        pass

    return None, None


def extract_card(driver, card, page_source: str):
    try:
        try:
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", card)
        except Exception:
            pass

        href = _first_attr(card, DETAIL_LINK_SELECTORS, "href")
        url = urljoin(BASE, href) if href else None
        listing_id = _parse_listing_id(url)

        full_addr = (
            _first_attr(card, [".property-image__img", ".property-image img"], "alt")
            or (card.get_attribute("aria-label") or "").strip()
            or None
        )
        short_addr = _first_text(card, ADDRESS_SELECTORS)

        price = _first_text(card, PRICE_SELECTORS)

        bedrooms = _get_feature_by_aria(card, "bedroom") or _get_feature_by_aria(card, "bedrooms")
        bathrooms = _get_feature_by_aria(card, "bathroom") or _get_feature_by_aria(card, "bathrooms")
        parking = (
            _get_feature_by_aria(card, "car space")
            or _get_feature_by_aria(card, "car spaces")
            or _get_feature_by_aria(card, "parking")
        )
        size_features = _get_size_features_by_aria(card)

        property_type = _first_text(card, PROPERTY_TYPE_SELECTORS)

        agency = None
        agency_alt = _first_attr(card, AGENCY_SELECTORS, "alt")
        if agency_alt:
            agency = agency_alt.strip()

        # inspection
        insp_short, insp_long = _get_inspection_labels(card)
        inspection_combined = f"{insp_short} | {insp_long}" if (insp_short and insp_long) else (insp_short or insp_long)

        # auction
        auction_label, auction_time = _get_auction_labels(card)
        auction_combined = f"{auction_label} {auction_time}" if (auction_label and auction_time) else (auction_label or auction_time)

        # fallback near listing_id
        fallback = _extract_from_page_source(page_source, listing_id)

        final_price = price or fallback.get("price")
        final_address = full_addr or fallback.get("full_address") or short_addr
        final_agency = agency or fallback.get("agency")
        final_type = property_type or fallback.get("property_type")
        final_bed = bedrooms or fallback.get("bedrooms")
        final_bath = bathrooms or fallback.get("bathrooms")
        final_park = parking or fallback.get("parking")

        if not insp_short:
            insp_short = fallback.get("inspection_short_label")
        if not insp_long:
            insp_long = fallback.get("inspection_long_label")
        if not auction_label:
            auction_label = fallback.get("auction_label")

        if not inspection_combined:
            inspection_combined = f"{insp_short} | {insp_long}" if (insp_short and insp_long) else (insp_short or insp_long)

        if not auction_combined:
            long_auc = fallback.get("auction_long_label")
            if long_auc:
                auction_combined = long_auc
            else:
                auction_combined = f"{auction_label} {auction_time}" if (auction_label and auction_time) else (auction_label or auction_time)

        return {
            "listing_id": listing_id,
            "price": final_price or "N/A",
            "address": final_address or "N/A",
            "bedrooms": final_bed or "N/A",
            "bathrooms": final_bath or "N/A",
            "parking": final_park or "N/A",
            "property_type": final_type or "N/A",
            "agency": final_agency or "N/A",

            "inspection_short_label": insp_short or "N/A",
            "inspection_long_label": insp_long or "N/A",
            "inspection": inspection_combined or "N/A",
            "auction_label": auction_label or "N/A",
            "auction_time": auction_time or "N/A",
            "auction": auction_combined or "N/A",

            "url": url or "N/A",
            "scraped_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "search_url": driver.current_url,
            **size_features,
        }

    except Exception:
        return None


def make_list_url(base_url: str, page: int) -> str:
    """
    list-1 -> list-2 ... با حفظ query مثل activeSort=list-date
    """
    u = urlparse(base_url)
    path = u.path
    if re.search(r"/list-\d+", path):
        path = re.sub(r"/list-\d+", f"/list-{page}", path)
    return urlunparse((u.scheme, u.netloc, path, "", u.query, ""))


def _parse_list_page(url: str) -> int | None:
    m = re.search(r"/list-(\d+)", url or "")
    return int(m.group(1)) if m else None


def get_total_pages(driver) -> int | None:
    """
    تلاش می‌کند تعداد کل صفحات نتایج را از pagination استخراج کند.
    اگر نتوانست، None برمی‌گرداند و fallback می‌رویم روی detect_next.
    """
    texts = []
    for sel in PAGINATION_CONTAINERS:
        try:
            for nav in driver.find_elements(By.CSS_SELECTOR, sel):
                if nav.is_displayed():
                    t = (nav.text or "").strip()
                    if t:
                        texts.append(t)
        except Exception:
            pass

    joined = " | ".join(texts)

    # الگوهای رایج: "Page 1 of 12" یا "1 of 12"
    m = re.search(r"\bof\s+(\d+)\b", joined, flags=re.IGNORECASE)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            pass

    # fallback: بزرگترین شماره صفحه داخل pagination
    max_num = None
    try:
        for sel in PAGINATION_CONTAINERS:
            for nav in driver.find_elements(By.CSS_SELECTOR, sel):
                if not nav.is_displayed():
                    continue
                for el in nav.find_elements(By.CSS_SELECTOR, "a, button"):
                    txt = (el.text or "").strip()
                    if txt.isdigit():
                        n = int(txt)
                        max_num = n if (max_num is None or n > max_num) else max_num
    except Exception:
        pass

    return max_num


def detect_next(driver) -> bool:
    for sel in NEXT_SELECTORS:
        try:
            els = driver.find_elements(By.CSS_SELECTOR, sel)
            if not els:
                continue
            for el in els:
                try:
                    if not el.is_displayed():
                        continue
                except Exception:
                    continue

                aria_disabled = (el.get_attribute("aria-disabled") or "").lower() == "true"
                disabled = el.get_attribute("disabled") is not None
                cls = (el.get_attribute("class") or "").lower()
                if aria_disabled or disabled or ("disabled" in cls):
                    continue

                return True
        except Exception:
            continue
    return False


def save_results(rows: list[dict], out_dir="output"):
    os.makedirs(out_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    csv_path = os.path.join(out_dir, f"realestate_properties_{ts}.csv")
    json_path = os.path.join(out_dir, f"realestate_properties_{ts}.json")

    fields = [
        "listing_id", "price", "address", "bedrooms", "bathrooms", "parking",
        "property_type", "agency",

        "inspection_short_label", "inspection_long_label", "inspection",
        "auction_label", "auction_time", "auction",

        "url", "scraped_at", "search_url"
    ]

    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "N/A") for k in fields})

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)

    return csv_path, json_path



def scrape_search_page(search_url: str, page: int = 1, timeout: int | None = None, on_log=None):
    """Scrape a single search result page and return rows plus page metadata."""
    effective_timeout = timeout or 25
    driver = None
    rows = []
    profile_dir_current = config.get_effective_browser_profile_dir("module1")
    rotations_used = 0
    session_health = BrowserSessionHealth(module_name="Module1")
    recovery_policy = RecoveryPolicy()

    def log(msg: str) -> None:
        print(msg)
        if on_log:
            try:
                on_log(msg)
            except Exception:
                pass

    try:
        driver = setup_driver(profile_dir_override=profile_dir_current)
        page_url = make_list_url(search_url, page)
        navigation_ok = safe_get(driver, page_url, phase=f"list_page_{page}", apply_delay=False, log_func=log)
        navigation_info = _last_navigation(driver)
        session_health.record_navigation(page_url, navigation_ok, navigation_info.get("navigation_error"), _current_url(driver))
        page_429_retries = 0
        while True:
            if _navigation_needs_fast_classification(driver, navigation_info):
                state_result = classify_search_page(driver, timeout=True, min_cards=1)
                cards = []
            else:
                state_result, cards = wait_for_search_page_state(driver, timeout=effective_timeout, min_cards=1)
            session_health.record_page_state(state_result)
            log(
                "Module1 page_state={state} cards_found={cards} network_reason={network} block_reason={reason} "
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
            state_result, cards = _module1_same_page_kpsdk_settle(
                driver,
                page_url,
                state_result,
                cards,
                min_cards=1,
                log=log,
            )
            if state_result.state == PageState.BLOCKED_KPSDK and not _is_chrome_error_url(_current_url(driver, state_result)):
                state_result, cards = _same_session_kpsdk_recheck(
                    driver=driver,
                    url=page_url,
                    timeout=effective_timeout,
                    min_cards=1,
                    state_result=state_result,
                    cards=cards,
                    log=log,
                )
            session_health.record_page_state(state_result)
            if state_result.state == PageState.LISTINGS:
                log_session_health(session_health, url_type="list", page_state=state_result.state, action="success", log_func=log)
                break
            if _module1_state_is_recoverable(driver, state_result, navigation_info) and config.BROWSER_RECOVERY_ON_429:
                reason = _module1_recovery_reason(driver, state_result, navigation_info)
                should_same_url_retry = _is_chrome_error_url(_current_url(driver, state_result)) or bool(navigation_info.get("navigation_failed"))
                if should_same_url_retry and recovery_policy.should_retry_same_profile(session_health):
                    session_health.record_same_url_retry(reason)
                    log_session_health(session_health, url_type="list", page_state=state_result.state, action="retry_same_profile", log_func=log)
                    navigation_ok = safe_get(driver, page_url, phase=f"retry_page_{page}", apply_delay=False, log_func=log)
                    navigation_info = _last_navigation(driver)
                    session_health.record_navigation(page_url, navigation_ok, navigation_info.get("navigation_error"), _current_url(driver))
                    continue
                if not recovery_policy.should_rotate(session_health, explicit_trusted_block=state_result.state == PageState.BLOCKED_ACCESS_DENIED):
                    log_session_health(session_health, url_type="list", page_state=state_result.state, action="retry_wait", log_func=log)
                    raise RealEstateBlockedError(reason, retry_after_seconds=getattr(config, "REA_RATE_LIMIT_BACKOFF_SECONDS", 21600))
                log_session_health(session_health, url_type="list", page_state=state_result.state, action="rotate_profile", log_func=log)
                driver, rotations_used, profile_dir_current, recovery_status = _recover_module1_untrusted_page(
                    driver=driver,
                    profile_dir_current=profile_dir_current,
                    rotations_used=rotations_used,
                    max_rotations=min(config.BROWSER_MAX_PROFILE_ROTATIONS_PER_RUN, config.MODULE1_MAX_PROFILE_ROTATIONS_PER_RUN),
                    state_result=state_result,
                    navigation_info=navigation_info,
                    url=page_url,
                    log=log,
                )
                if recovery_status != "recovered":
                    raise RealEstateBlockedError(_module1_recovery_reason(driver, state_result, navigation_info), retry_after_seconds=getattr(config, "REA_RATE_LIMIT_BACKOFF_SECONDS", 21600))
                session_health.record_rotation(reason)
                page_429_retries += 1
                if page_429_retries > config.MODULE1_RETRY_SAME_PAGE_AFTER_429:
                    raise RealEstateBlockedError(_module1_recovery_reason(driver, state_result, navigation_info), retry_after_seconds=getattr(config, "REA_RATE_LIMIT_BACKOFF_SECONDS", 21600))
                navigation_info = _last_navigation(driver)
                continue
            if state_result.state == PageState.NO_RESULTS:
                return [], {
                    "page": page,
                    "has_next_page": False,
                    "url": page_url,
                    "current_url": state_result.current_url or page_url,
                    "cards_found": 0,
                    "rows_count": 0,
                    "total_pages_detected": None,
                    "stop_reason": "no_results",
                    "page_state": state_result.state,
                }
            return [], {
                "page": page,
                "has_next_page": False,
                "url": page_url,
                "current_url": state_result.current_url or page_url,
                "cards_found": state_result.cards_count,
                "rows_count": 0,
                "total_pages_detected": None,
                "stop_reason": state_result.state if state_result.state != PageState.UNKNOWN else "render_timeout",
                "page_state": state_result.state,
            }

        current_url = driver.current_url or page_url
        actual_page = _parse_list_page(current_url)
        if actual_page is not None and actual_page != page and page > 1:
            log(f"Module1 pagination stop: requested_page={page} requested_url={page_url} current_url={current_url} stop_reason=redirected_back")
            return [], {"page": page, "has_next_page": False, "url": page_url, "current_url": current_url, "cards_found": 0, "rows_count": 0, "total_pages_detected": None, "stop_reason": "redirected_back"}

        WebDriverWait(driver, effective_timeout).until(EC.presence_of_element_located((By.CSS_SELECTOR, "body")))
        _stop_page_loading(driver)
        page_source = driver.page_source
        seen_ids = set()
        for card in cards:
            row = extract_card(driver, card, page_source)
            if not row:
                continue
            lid = row.get("listing_id")
            if lid and lid in seen_ids:
                continue
            if lid:
                seen_ids.add(lid)
            rows.append(row)
        total_pages = get_total_pages(driver)
        has_next = detect_next(driver)
        current_url = driver.current_url or page_url
        log(
            "Module1 pagination: requested_page={page} requested_url={requested} current_url={current} "
            "cards_found={cards} rows_extracted={rows} total_pages_detected={total} has_next={has_next}".format(
                page=page, requested=page_url, current=current_url, cards=len(cards), rows=len(rows),
                total=total_pages if total_pages is not None else "unknown", has_next=bool(has_next),
            )
        )
        return rows, {
            "page": page,
            "has_next_page": bool(has_next),
            "url": page_url,
            "current_url": current_url,
            "cards_found": len(cards),
            "rows_count": len(rows),
            "total_pages_detected": total_pages,
            "stop_reason": "listings",
            "page_state": PageState.LISTINGS,
        }
    except TimeoutException:
        page_url = make_list_url(search_url, page)
        if driver:
            raise_if_realestate_blocked(driver)
        log(f"Module1 pagination stop: requested_page={page} requested_url={page_url} stop_reason=no_cards_timeout")
        return [], {"page": page, "has_next_page": False, "url": page_url, "current_url": getattr(driver, "current_url", page_url), "cards_found": 0, "rows_count": 0, "total_pages_detected": None, "stop_reason": "no_cards_timeout"}
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
            cleanup_chrome_driver(driver)
        gc.collect()

def scrape_search(base_url: str, max_pages=None, timeout=25, cancel_token=None, on_log=None, on_progress=None):
    """
    max_pages:
      - None  => تا آخرین صفحه می‌رود
      - عدد   => فقط تا همان تعداد صفحه
    """
    driver = None
    all_rows = []
    seen_ids = set()
    profile_dir_current = config.get_effective_browser_profile_dir("module1")
    rotations_used = 0
    session_health = BrowserSessionHealth(module_name="Module1")
    recovery_policy = RecoveryPolicy()
    scrape_search.last_result = {"status": "running", "rows": 0, "stop_reason": None, "page_state": None}

    def log(msg: str) -> None:
        print(msg)
        if on_log:
            try:
                on_log(msg)
            except Exception:
                pass

    def progress(payload: dict) -> None:
        if on_progress:
            try:
                on_progress("module1_progress", payload)
            except Exception:
                pass

    try:
        if is_linux() and is_headless_enabled():
            log("Warning: Ubuntu headless may fail. Recommended: HEADLESS=0 + xvfb-run")
        driver = setup_driver(profile_dir_override=profile_dir_current)

        page = 1
        total_pages = None
        hard_cap = 500  # جلوگیری از لوپ بی‌نهایت در شرایط عجیب
        nav_mode = _module1_pagination_nav_mode()
        pagination_landed_by_click = False
        fallback_paths: list[str] = []
        log(f"Module1 pagination nav mode={nav_mode}")

        while page <= hard_cap:
            if getattr(cancel_token, "is_set", lambda: False)():
                log("Cancel requested in module1.")
                break
            if isinstance(max_pages, int) and max_pages > 0 and page > max_pages:
                break

            url = make_list_url(base_url, page)
            log(f"\nPage {page}: {url}")
            if page == 1 or nav_mode == "direct_url":
                navigation_ok = safe_get(driver, url, phase=f"list_page_{page}", apply_delay=page > 1, log_func=log)
                navigation_info = _last_navigation(driver)
                session_health.record_navigation(url, navigation_ok, navigation_info.get("navigation_error"), _current_url(driver))
                pagination_landed_by_click = False
            elif nav_mode == "fresh_context_per_page" or not pagination_landed_by_click:
                driver, profile_dir_current, navigation_ok, navigation_info = _module1_fresh_context_get(driver, url, page, log=log)
                session_health.record_navigation(url, navigation_ok, navigation_info.get("navigation_error"), _current_url(driver))
                fallback_paths.append(f"fresh_context_per_page:page_{page}")
                pagination_landed_by_click = False
            else:
                current = _current_url(driver)
                log(f"Module1 click-next landed current_url={current}")
                navigation_ok = not _is_chrome_error_url(current)
                navigation_info = {"url": url, "navigation_failed": not navigation_ok, "navigation_error": None, "navigation_mode": "click_next"}
                try:
                    driver._module1_last_navigation = navigation_info
                except Exception:
                    pass
                session_health.record_navigation(url, navigation_ok, navigation_info.get("navigation_error"), current)
                if _is_chrome_error_url(current):
                    log(f"Module1 click-next landed on chrome-error; fallback fresh_context_per_page page={page}")
                    driver, profile_dir_current, navigation_ok, navigation_info = _module1_fresh_context_get(driver, url, page, log=log)
                    session_health.record_navigation(url, navigation_ok, navigation_info.get("navigation_error"), _current_url(driver))
                    fallback_paths.append(f"click_next_chrome_error_to_fresh_context_per_page:page_{page}")
                    pagination_landed_by_click = True
            page_429_retries = 0
            while True:
                if _navigation_needs_fast_classification(driver, navigation_info):
                    state_result = classify_search_page(driver, timeout=True, min_cards=1)
                    cards = []
                else:
                    state_result, cards = wait_for_search_page_state(driver, timeout=timeout, min_cards=1)
                session_health.record_page_state(state_result)
                log(
                    "Module1 page_state={state} cards_found={cards} network_reason={network} block_reason={reason} "
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
                state_result, cards = _module1_same_page_kpsdk_settle(
                    driver,
                    url,
                    state_result,
                    cards,
                    min_cards=1,
                    log=log,
                )
                if state_result.state == PageState.BLOCKED_KPSDK and not _is_chrome_error_url(_current_url(driver, state_result)):
                    state_result, cards = _same_session_kpsdk_recheck(
                        driver=driver,
                        url=url,
                        timeout=timeout,
                        min_cards=1,
                        state_result=state_result,
                        cards=cards,
                        log=log,
                    )
                session_health.record_page_state(state_result)
                if state_result.state == PageState.LISTINGS:
                    log_session_health(session_health, url_type="list", page_state=state_result.state, action="success", log_func=log)
                    break
                if _module1_state_is_recoverable(driver, state_result, navigation_info) and config.BROWSER_RECOVERY_ON_429:
                    reason = _module1_recovery_reason(driver, state_result, navigation_info)
                    should_same_url_retry = _is_chrome_error_url(_current_url(driver, state_result)) or bool(navigation_info.get("navigation_failed"))
                    if should_same_url_retry and recovery_policy.should_retry_same_profile(session_health):
                        session_health.record_same_url_retry(reason)
                        log_session_health(session_health, url_type="list", page_state=state_result.state, action="retry_same_profile", log_func=log)
                        navigation_ok = safe_get(driver, url, phase=f"retry_page_{page}", apply_delay=False, log_func=log)
                        navigation_info = _last_navigation(driver)
                        session_health.record_navigation(url, navigation_ok, navigation_info.get("navigation_error"), _current_url(driver))
                        continue
                    if not recovery_policy.should_rotate(session_health, explicit_trusted_block=state_result.state == PageState.BLOCKED_ACCESS_DENIED):
                        log_session_health(session_health, url_type="list", page_state=state_result.state, action="retry_wait", log_func=log)
                        if not all_rows:
                            raise RealEstateBlockedError(reason, retry_after_seconds=getattr(config, "REA_RATE_LIMIT_BACKOFF_SECONDS", 21600))
                        scrape_search.last_result = {"status": "partial_blocked", "rows": len(all_rows), "stop_reason": state_result.state, "page_state": state_result.state}
                        return all_rows
                elif state_result.state == PageState.NO_RESULTS:
                    log("No results page detected. Stop.")
                    scrape_search.last_result = {"status": "no_results", "rows": len(all_rows), "stop_reason": "no_results", "page_state": state_result.state}
                    return all_rows
                else:
                    log(f"Page render not usable state={state_result.state} reason={state_result.reason}. Stop.")
                    scrape_search.last_result = {
                        "status": "render_timeout",
                        "rows": len(all_rows),
                        "stop_reason": state_result.state if state_result.state != PageState.UNKNOWN else "render_timeout",
                        "page_state": state_result.state,
                    }
                    return all_rows
                save_results(all_rows, out_dir=config.OUTPUT_DIR)
                log_session_health(session_health, url_type="list", page_state=state_result.state, action="rotate_profile", log_func=log)
                driver, rotations_used, profile_dir_current, recovery_status = _recover_module1_untrusted_page(
                    driver=driver,
                    profile_dir_current=profile_dir_current,
                    rotations_used=rotations_used,
                    max_rotations=min(config.BROWSER_MAX_PROFILE_ROTATIONS_PER_RUN, config.MODULE1_MAX_PROFILE_ROTATIONS_PER_RUN),
                    state_result=state_result,
                    navigation_info=navigation_info,
                    url=url,
                    log=log,
                )
                if recovery_status != "recovered":
                    if not all_rows:
                        raise RealEstateBlockedError(_module1_recovery_reason(driver, state_result, navigation_info), retry_after_seconds=getattr(config, "REA_RATE_LIMIT_BACKOFF_SECONDS", 21600))
                    log("Module1 recovery rotation limit reached. Returning partial rows gracefully.")
                    scrape_search.last_result = {"status": "partial_blocked", "rows": len(all_rows), "stop_reason": state_result.state, "page_state": state_result.state}
                    return all_rows
                session_health.record_rotation(_module1_recovery_reason(driver, state_result, navigation_info))
                page_429_retries += 1
                if page_429_retries > config.MODULE1_RETRY_SAME_PAGE_AFTER_429:
                    if not all_rows:
                        raise RealEstateBlockedError(_module1_recovery_reason(driver, state_result, navigation_info), retry_after_seconds=getattr(config, "REA_RATE_LIMIT_BACKOFF_SECONDS", 21600))
                    log("Module1 page retry limit after recovery reached. Returning partial rows gracefully.")
                    scrape_search.last_result = {"status": "partial_blocked", "rows": len(all_rows), "stop_reason": state_result.state, "page_state": state_result.state}
                    return all_rows
                navigation_info = _last_navigation(driver)

            # اگر ریدایرکت شد به صفحه دیگری (مثلاً list-2 رفتی ولی برگشت list-1)
            actual_page = _parse_list_page(driver.current_url or "")
            if actual_page is not None and actual_page != page and page > 1:
                log(f"↩️ Redirected to list-{actual_page} while requesting list-{page}. Stop.")
                break

            # صبر برای کارت‌ها
            try:
                cards = wait_for_cards(driver, timeout=timeout, min_cards=1)
            except TimeoutException:
                raise_if_realestate_blocked(driver)
                log("Warning: No cards found (timeout). Stop.")
                break

            WebDriverWait(driver, timeout).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "body"))
            )
            _stop_page_loading(driver)

            # فقط روی صفحه ۱ تعداد کل صفحات را بگیر
            if page == 1:
                total_pages = get_total_pages(driver)
                if total_pages:
                    log(f"Total pages detected: {total_pages}")

            page_source = driver.page_source
            log(f"Found {len(cards)} cards")

            page_count = 0
            for card in cards:
                row = extract_card(driver, card, page_source)
                if not row:
                    continue

                lid = row.get("listing_id")
                if lid and lid in seen_ids:
                    continue
                if lid:
                    seen_ids.add(lid)

                all_rows.append(row)
                page_count += 1
                time.sleep(0.03)

            log(f"Extracted {page_count} rows from this page (total={len(all_rows)})")
            net_summary = _collect_network_debug(driver)
            log("Page network summary:")
            log(
                "requests={requests} transferred_mb={mb:.2f} top domain={domain} top resource={resource}".format(
                    requests=net_summary["request_count"],
                    mb=(net_summary["transferred_bytes"] / (1024 * 1024)),
                    domain=net_summary["top_domain"],
                    resource=net_summary["top_resource"],
                )
            )
            progress(
                {
                    "page": page,
                    "total_pages": total_pages,
                    "cards_found": len(cards),
                    "page_rows": page_count,
                    "total_rows": len(all_rows),
                }
            )

            # شرط توقف
            if total_pages and page >= total_pages:
                log("Reached last page (by pagination).")
                break

            has_next = detect_next(driver)
            log(f"Next detected: {has_next}")

            if not has_next and not total_pages:
                break

            next_page = page + 1
            pagination_landed_by_click = False
            if nav_mode == "click_next":
                log(f"Module1 pagination nav mode=click_next page={page} next_page={next_page}")
                _human_scroll_idle_before_next(driver, log)
                click_result = _click_next_anchor(driver, next_page, log)
                time.sleep(max(0.5, float(getattr(config, "MODULE1_INTER_PAGE_DELAY_SECONDS", 0)) / 4.0))
                current_after_click = _current_url(driver)
                log(f"Module1 click-next landed current_url={current_after_click}")
                actual_next = _parse_list_page(current_after_click)
                if click_result.get("clicked") and actual_next == next_page and not _is_chrome_error_url(current_after_click):
                    pagination_landed_by_click = True
                else:
                    reason = click_result.get("reason") or f"landed_page={actual_next} chrome_error={_is_chrome_error_url(current_after_click)}"
                    log(f"Module1 click-next fallback fresh_context_per_page page={next_page} reason={reason}")
                    driver, profile_dir_current, navigation_ok, navigation_info = _module1_fresh_context_get(driver, make_list_url(base_url, next_page), next_page, log=log)
                    session_health.record_navigation(make_list_url(base_url, next_page), navigation_ok, navigation_info.get("navigation_error"), _current_url(driver))
                    fallback_name = "click_next_chrome_error_to_fresh_context_per_page" if _is_chrome_error_url(current_after_click) else "click_next_to_fresh_context_per_page"
                    fallback_paths.append(f"{fallback_name}:page_{next_page}")
                    pagination_landed_by_click = True
            page = next_page

        scrape_search.last_result = {"status": "completed", "rows": len(all_rows), "stop_reason": "completed", "page_state": PageState.LISTINGS if all_rows else None, "pagination_nav_mode": nav_mode, "fallback_paths": fallback_paths}
        return all_rows

    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
            cleanup_chrome_driver(driver)
        driver = None
        gc.collect()


if __name__ == "__main__":
    TARGET_URL = AREA_SEARCH_URL

    # ✅ اگر می‌خوای تا آخرین صفحه بره: max_pages=None
    # اگر خواستی محدود کنی: مثلاً max_pages=3
    results = scrape_search(TARGET_URL, max_pages=None, timeout=25)

    if results:
        csv_file, json_file = save_results(results, out_dir=config.OUTPUT_DIR)
        print("\n💾 Saved:")
        print(" -", csv_file)
        print(" -", json_file)

        print("\n🔎 Sample:")
        for r in results[:3]:
            print(f"- {r['price']} | {r['address']} | insp={r['inspection']} | auc={r['auction']}")
    else:
        print("\n⚠️ No results extracted.")
