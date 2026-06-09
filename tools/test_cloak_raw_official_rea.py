from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlparse

REA_URL = os.getenv(
    "TEST_REA_URL",
    "https://www.realestate.com.au/buy/in-noona,+nsw+2835/list-1?activeSort=list-date",
)


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _viewport() -> dict:
    raw = os.getenv("CLOAK_VIEWPORT", "1365x768").strip().lower()
    if "x" in raw:
        left, right = raw.split("x", 1)
        return {"width": int(left), "height": int(right)}
    return {
        "width": int(os.getenv("CLOAK_VIEWPORT_WIDTH", "1365")),
        "height": int(os.getenv("CLOAK_VIEWPORT_HEIGHT", "768")),
    }


def _http2_mode() -> str:
    mode = os.getenv("CLOAK_HTTP2_MODE", "").strip().lower()
    if mode:
        return mode
    return "disable" if _bool_env("CLOAK_DISABLE_HTTP2", False) else "default"


def _mask_proxy(proxy: str | None) -> str:
    text = str(proxy or "").strip()
    if not text:
        return ""
    parsed = urlparse(text if "://" in text else f"//{text}")
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    if not host:
        return "configured"
    return f"{host[:2]}***{host[-2:]}{port}" if len(host) > 4 else "****" + port


def _args(viewport: dict) -> list[str]:
    args = [
        "--fingerprint-platform=windows",
        f"--fingerprint-screen-width={viewport['width']}",
        f"--fingerprint-screen-height={viewport['height']}",
    ]
    if _http2_mode() == "disable":
        args.append("--disable-http2")
    return args


def _body_text(page) -> str:
    try:
        return page.evaluate("() => document.body ? document.body.innerText : ''") or ""
    except Exception:
        return ""


def _card_counts(page) -> dict[str, int]:
    selectors = [
        'article[data-testid="ResidentialCard"]',
        "article.residential-card",
        "article[data-testid]",
        '[data-testid="property-card"]',
    ]
    out = {}
    for selector in selectors:
        try:
            out[selector] = len(page.query_selector_all(selector))
        except Exception:
            out[selector] = 0
    return out


def main() -> int:
    from cloakbrowser import launch_persistent_context

    try:
        from cloakbrowser import binary_info
    except Exception:
        binary_info = None

    profile = os.path.abspath(os.getenv("TEST_PROFILE", "output/cloak_raw_official_profile"))
    Path(profile).mkdir(parents=True, exist_ok=True)
    viewport = _viewport()
    proxy = os.getenv("CLOAK_PROXY", "").strip()
    geoip = _bool_env("CLOAK_GEOIP", False)
    humanize = _bool_env("CLOAK_HUMANIZE", False)
    args = _args(viewport)
    launch_kwargs = {
        "headless": _bool_env("CLOAK_HEADLESS", _bool_env("HEADLESS", False)),
        "humanize": humanize,
        "geoip": geoip,
        "args": args,
        "viewport": viewport,
    }
    if proxy:
        launch_kwargs["proxy"] = proxy

    print("cloakbrowser binary_info=", binary_info() if binary_info else "unavailable")
    print("profile=", profile)
    print("proxy_configured=", bool(proxy), "proxy_host_masked=", _mask_proxy(proxy))
    print("geoip=", geoip, "humanize=", humanize)
    print("args=", json.dumps(args))

    context = launch_persistent_context(profile, **launch_kwargs)
    try:
        pages_attr = getattr(context, "pages", [])
        pages = pages_attr() if callable(pages_attr) else pages_attr
        page = pages[0] if pages else context.new_page()
        try:
            response = page.goto(REA_URL, wait_until="domcontentloaded", timeout=60000)
            goto_status = getattr(response, "status", None) or "ok"
        except Exception as exc:
            goto_status = f"error:{exc}"
        html = page.content() or ""
        body = _body_text(page)
        counts = _card_counts(page)
        print("goto_status=", goto_status)
        print("current_url=", page.url)
        print("title=", page.title())
        print("html_length=", len(html))
        print("body_text_length=", len(body))
        print("sample_body_text=", " ".join(body.split())[:500])
        print("card_counts=", json.dumps(counts, sort_keys=True))
        print("cards_found=", max(counts.values()) if counts else 0)
    finally:
        context.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
