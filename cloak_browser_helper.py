import fnmatch
import inspect
import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

import config


LAST_CLOAK_LAUNCH_CONFIG: dict = {}


class TimeoutException(Exception):
    pass


class WebDriverException(Exception):
    pass


class NoSuchElementException(Exception):
    pass


class StaleElementReferenceException(Exception):
    pass


class By:
    CSS_SELECTOR = "css selector"
    XPATH = "xpath"


class EC:
    @staticmethod
    def presence_of_element_located(locator):
        by, selector = locator

        def _predicate(driver):
            try:
                return driver.find_element(by, selector)
            except NoSuchElementException:
                return False

        return _predicate


class WebDriverWait:
    def __init__(self, driver, timeout, poll_frequency=0.5):
        self.driver = driver
        self.timeout = float(timeout)
        self.poll_frequency = float(poll_frequency)

    def until(self, condition):
        deadline = time.time() + self.timeout
        last_exc = None
        while time.time() <= deadline:
            try:
                value = condition(self.driver)
                if value:
                    return value
            except Exception as exc:
                last_exc = exc
            time.sleep(self.poll_frequency)
        raise TimeoutException(str(last_exc or f"Timed out after {self.timeout}s"))


def _playwright_timeout_types() -> tuple[type, ...]:
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

        return (PlaywrightTimeoutError,)
    except Exception:
        return ()


def _selector(by: str, value: str) -> str:
    if by == By.XPATH:
        return value if str(value).startswith("xpath=") else f"xpath={value}"
    return value


def _safe_call(func, default=None):
    try:
        return func()
    except Exception:
        return default


def _as_millis(seconds: int | float | None, default_seconds: int = 60) -> int:
    value = default_seconds if seconds is None else seconds
    return int(float(value) * 1000)


def _bool(value) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _cloak_headless() -> bool:
    if os.getenv("CLOAK_HEADLESS") is not None:
        return _bool(os.getenv("CLOAK_HEADLESS"))
    if os.getenv("HEADLESS") is not None:
        return _bool(os.getenv("HEADLESS", "0"))
    return bool(getattr(config, "CLOAK_HEADLESS", False))


def _profile_dir(profile_dir_override: str | None = None) -> tuple[str, bool]:
    if getattr(config, "CLOAK_USE_PERSISTENT_CONTEXT", True):
        raw = profile_dir_override or getattr(config, "CLOAK_PROFILE_DIR", None) or getattr(config, "CHROME_PROFILE_DIR", "rea_profile")
        return os.path.abspath(raw), False
    return tempfile.mkdtemp(prefix="ascrapper_cloak_profile_"), True


def _cloak_http2_mode() -> str:
    mode = str(getattr(config, "CLOAK_HTTP2_MODE", "default") or "default").strip().lower()
    if mode not in {"default", "disable", "warmup_only"}:
        mode = "disable" if getattr(config, "CLOAK_DISABLE_HTTP2", False) else "default"
    return mode


def _cloak_args(profile_dir: str | None = None) -> list[str]:
    width = int(getattr(config, "CLOAK_VIEWPORT_WIDTH", 1365))
    height = int(getattr(config, "CLOAK_VIEWPORT_HEIGHT", 768))
    args = [
        f"--fingerprint-platform={getattr(config, 'CLOAK_FINGERPRINT_PLATFORM', 'windows')}",
        f"--fingerprint-screen-width={width}",
        f"--fingerprint-screen-height={height}",
    ]
    seed = str(getattr(config, "CLOAK_FINGERPRINT_SEED", "") or "").strip()
    if seed:
        args.insert(0, f"--fingerprint={seed}")
    storage_quota = str(getattr(config, "CLOAK_FINGERPRINT_STORAGE_QUOTA", "") or "").strip()
    if storage_quota:
        args.append(f"--fingerprint-storage-quota={storage_quota}")
    if os.name != "nt":
        args.extend(["--no-sandbox", "--disable-dev-shm-usage"])
    if _cloak_http2_mode() == "disable":
        args.append("--disable-http2")
    extra = os.getenv("CLOAK_EXTRA_ARGS", "").strip()
    if extra:
        args.extend([part.strip() for part in extra.split() if part.strip()])
    return args


def _fingerprint_seed_source() -> str:
    return "explicit" if str(getattr(config, "CLOAK_FINGERPRINT_SEED", "") or "").strip() else "none"


def _mask_proxy(proxy: str | None) -> str:
    text = str(proxy or "").strip()
    if not text:
        return ""
    try:
        parsed = urlparse(text if "://" in text else f"//{text}")
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        if not host:
            return "configured"
        if len(host) <= 4:
            masked_host = "*" * len(host)
        else:
            masked_host = f"{host[:2]}***{host[-2:]}"
        return f"{masked_host}{port}"
    except Exception:
        return "configured"


def _cloak_launch_kwargs(profile_dir: str) -> dict:
    width = int(getattr(config, "CLOAK_VIEWPORT_WIDTH", 1365))
    height = int(getattr(config, "CLOAK_VIEWPORT_HEIGHT", 768))
    geoip = bool(getattr(config, "CLOAK_GEOIP", False))
    kwargs = {
        "headless": _cloak_headless(),
        "args": _cloak_args(profile_dir),
        "viewport": {"width": width, "height": height},
    }
    proxy = str(getattr(config, "CLOAK_PROXY", "") or "").strip()
    if proxy:
        kwargs["proxy"] = proxy
    if geoip:
        kwargs["geoip"] = True
    if bool(getattr(config, "CLOAK_HUMANIZE", False)):
        kwargs["humanize"] = True
    human_preset = str(getattr(config, "CLOAK_HUMAN_PRESET", "") or "").strip()
    if human_preset:
        kwargs["human_preset"] = human_preset
    human_config = getattr(config, "CLOAK_HUMAN_CONFIG", None)
    if human_config:
        kwargs["human_config"] = human_config
    if not geoip:
        locale = str(getattr(config, "CLOAK_LOCALE", "") or "").strip()
        timezone = str(getattr(config, "CLOAK_TIMEZONE", "") or "").strip()
        if locale:
            kwargs["locale"] = locale
        if timezone:
            kwargs["timezone"] = timezone
    return kwargs


def _validate_launch_kwargs(func, kwargs: dict) -> None:
    try:
        sig = inspect.signature(func)
    except (TypeError, ValueError):
        return
    params = sig.parameters
    if any(param.kind == inspect.Parameter.VAR_KEYWORD for param in params.values()):
        return
    unsupported = sorted(key for key in kwargs if key not in params)
    if unsupported:
        raise RuntimeError(
            "Installed cloakbrowser.launch_persistent_context does not support configured option(s): "
            + ", ".join(unsupported)
            + ". Upgrade cloakbrowser or unset those CLOAK_* options."
        )


def _record_and_log_launch_config(profile_dir: str, kwargs: dict) -> None:
    global LAST_CLOAK_LAUNCH_CONFIG
    proxy = str(getattr(config, "CLOAK_PROXY", "") or "").strip()
    launch_config = {
        "profile_dir": profile_dir,
        "headless": bool(kwargs.get("headless")),
        "humanize": bool(kwargs.get("humanize", False)),
        "geoip": bool(kwargs.get("geoip", False)),
        "proxy_configured": bool(proxy),
        "proxy_host_masked": _mask_proxy(proxy),
        "viewport": kwargs.get("viewport"),
        "locale": kwargs.get("locale"),
        "timezone": kwargs.get("timezone"),
        "http2_mode": _cloak_http2_mode(),
        "fingerprint_seed_source": _fingerprint_seed_source(),
        "storage_quota_configured": any(str(arg).startswith("--fingerprint-storage-quota=") for arg in kwargs.get("args", [])),
        "args_count": len(kwargs.get("args", [])),
    }
    LAST_CLOAK_LAUNCH_CONFIG = dict(launch_config)
    print(
        "cloak launch: profile_dir={profile_dir} headless={headless} humanize={humanize} geoip={geoip} "
        "proxy_configured={proxy_configured} proxy_host_masked={proxy_host_masked} viewport={viewport} "
        "locale={locale} timezone={timezone} http2_mode={http2_mode} fingerprint_seed_source={fingerprint_seed_source} "
        "storage_quota_configured={storage_quota_configured} args_count={args_count}".format(**launch_config)
    )


def _blocked_url_patterns() -> list[str]:
    patterns: list[str] = []
    if getattr(config, "BLOCK_IMAGES", False):
        patterns.extend(["*.png", "*.jpg", "*.jpeg", "*.webp", "*.gif", "*.svg", "*.ico", "*.avif"])
    if getattr(config, "BLOCK_MEDIA", False):
        patterns.extend(["*.mp4", "*.webm", "*.avi", "*.mov", "*.m4v", "*.m3u8", "*.ts", "*.mp3", "*.wav", "*.ogg"])
    if getattr(config, "BLOCK_FONTS", False):
        patterns.extend(["*.woff", "*.woff2", "*.ttf", "*.otf", "*.eot"])
    if getattr(config, "BLOCK_MAPS", False):
        patterns.extend(["*maps.googleapis.com*", "*maps.gstatic.com*", "*streetview*", "*mapbox*", "*openstreetmap*", "*tile*", "*tiles*"])
    if getattr(config, "BLOCK_ADS", False) or getattr(config, "BLOCK_ANALYTICS", False) or getattr(config, "BLOCK_TRACKERS", False):
        patterns.extend(
            [
                "*doubleclick.net*",
                "*googlesyndication.com*",
                "*google-analytics.com*",
                "*googletagmanager.com*",
                "*facebook.net*",
                "*adnxs.com*",
                "*adsystem*",
                "*analytics*",
                "*tracking*",
                "*pixel*",
            ]
        )
    if getattr(config, "BLOCK_HEAVY_RESOURCES", False):
        patterns.extend(["*image*", "*media*", "*video*", "*font*", "*map*"])
    return patterns


def _should_abort_request(request, patterns: list[str]) -> bool:
    url = (request.url or "").lower()
    resource_type = (request.resource_type or "").lower()
    if getattr(config, "BLOCK_IMAGES", False) and resource_type == "image":
        return True
    if getattr(config, "BLOCK_MEDIA", False) and resource_type == "media":
        return True
    if getattr(config, "BLOCK_FONTS", False) and resource_type == "font":
        return True
    return any(fnmatch.fnmatch(url, pattern.lower()) for pattern in patterns)


class CloakElement:
    def __init__(self, driver: "CloakDriver", handle):
        self.driver = driver
        self.handle = handle

    @property
    def text(self) -> str:
        return _safe_call(lambda: self.handle.evaluate("(el) => el.innerText || el.textContent || ''"), "") or ""

    def get_attribute(self, attr: str):
        return _safe_call(lambda: self.handle.get_attribute(attr))

    def find_elements(self, by: str, selector: str) -> list["CloakElement"]:
        try:
            handles = self.handle.query_selector_all(_selector(by, selector))
            return [CloakElement(self.driver, h) for h in handles]
        except Exception as exc:
            raise WebDriverException(str(exc))

    def find_element(self, by: str, selector: str) -> "CloakElement":
        els = self.find_elements(by, selector)
        if not els:
            raise NoSuchElementException(selector)
        return els[0]

    def is_displayed(self) -> bool:
        return bool(
            _safe_call(
                lambda: self.handle.evaluate(
                    """(el) => {
                        const style = window.getComputedStyle(el);
                        const box = el.getBoundingClientRect();
                        return style && style.visibility !== 'hidden' && style.display !== 'none'
                            && box.width >= 0 && box.height >= 0;
                    }"""
                ),
                False,
            )
        )

    def is_enabled(self) -> bool:
        disabled = self.get_attribute("disabled")
        aria_disabled = (self.get_attribute("aria-disabled") or "").lower() == "true"
        return disabled is None and not aria_disabled


class CloakDriver:
    def __init__(self, context, page, profile_dir: str, temp_profile: bool = False):
        self.context = context
        self.page = page
        self.profile_dir = profile_dir
        self._temp_profile = temp_profile
        self._page_load_timeout_seconds = 60
        self._performance_logs: list[dict] = []
        self._http_errors: list[dict] = []
        self._install_event_handlers()
        self._install_routes()

    def _install_event_handlers(self) -> None:
        def on_response(response):
            try:
                headers = {str(k).lower(): str(v) for k, v in (response.headers or {}).items()}
                status = int(response.status)
                url = response.url
                self._performance_logs.append(
                    {
                        "message": json.dumps(
                            {
                                "message": {
                                    "method": "Network.responseReceived",
                                    "params": {
                                        "type": "Document" if response.request.resource_type == "document" else response.request.resource_type,
                                        "response": {
                                            "url": url,
                                            "status": status,
                                            "headers": headers,
                                            "mimeType": headers.get("content-type", ""),
                                        },
                                    },
                                }
                            }
                        )
                    }
                )
                if status >= 400:
                    self._http_errors.append({"url": url, "status": status, "headers": headers})
            except Exception:
                pass

        self.page.on("response", on_response)

    def _install_routes(self) -> None:
        if not (getattr(config, "LOW_BANDWIDTH_MODE", False) or getattr(config, "BLOCK_HEAVY_RESOURCES", False)):
            return
        patterns = _blocked_url_patterns()
        if not patterns:
            return

        def handler(route, request):
            try:
                if _should_abort_request(request, patterns):
                    route.abort()
                    return
            except Exception:
                pass
            route.continue_()

        try:
            self.page.route("**/*", handler)
        except Exception:
            pass

    @property
    def current_url(self) -> str:
        return self.page.url

    @property
    def title(self) -> str:
        return _safe_call(lambda: self.page.title(), "") or ""

    @property
    def page_source(self) -> str:
        return _safe_call(lambda: self.page.content(), "") or ""

    @property
    def http_errors(self) -> list[dict]:
        return list(self._http_errors)

    def set_page_load_timeout(self, seconds: int | float) -> None:
        self._page_load_timeout_seconds = float(seconds)
        try:
            self.page.set_default_navigation_timeout(_as_millis(seconds))
            self.page.set_default_timeout(_as_millis(seconds))
        except Exception:
            pass

    def get(self, url: str):
        try:
            return self.page.goto(url, wait_until="domcontentloaded", timeout=_as_millis(self._page_load_timeout_seconds))
        except _playwright_timeout_types() as exc:
            raise TimeoutException(str(exc))
        except Exception as exc:
            raise WebDriverException(str(exc))

    def refresh(self):
        try:
            return self.page.reload(wait_until="domcontentloaded", timeout=_as_millis(self._page_load_timeout_seconds))
        except _playwright_timeout_types() as exc:
            raise TimeoutException(str(exc))
        except Exception as exc:
            raise WebDriverException(str(exc))

    def find_elements(self, by: str, selector: str) -> list[CloakElement]:
        try:
            handles = self.page.query_selector_all(_selector(by, selector))
            return [CloakElement(self, h) for h in handles]
        except Exception as exc:
            raise WebDriverException(str(exc))

    def find_element(self, by: str, selector: str) -> CloakElement:
        els = self.find_elements(by, selector)
        if not els:
            raise NoSuchElementException(selector)
        return els[0]

    def execute_script(self, script: str, *args):
        text = script.strip()
        try:
            if args and isinstance(args[0], CloakElement) and "scrollIntoView" in text:
                return args[0].handle.evaluate("(el) => el.scrollIntoView({block: 'center'})")
            if text == "window.stop();":
                return self.page.evaluate("() => window.stop()")
            if text.startswith("return "):
                expr = text[len("return ") :].rstrip(";")
                return self.page.evaluate(f"() => ({expr})")
            return self.page.evaluate(f"() => {{ {text} }}")
        except _playwright_timeout_types() as exc:
            raise TimeoutException(str(exc))
        except Exception as exc:
            raise WebDriverException(str(exc))

    def execute_cdp_cmd(self, command: str, params: dict | None = None):
        try:
            session = self.context.new_cdp_session(self.page)
            return session.send(command, params or {})
        except Exception as exc:
            raise WebDriverException(str(exc))

    def get_log(self, kind: str):
        if kind != "performance":
            return []
        logs = list(self._performance_logs)
        self._performance_logs.clear()
        return logs

    def screenshot(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.page.screenshot(path=path, full_page=True)

    def fingerprint(self) -> dict:
        return self.page.evaluate(
            """() => ({
                userAgent: navigator.userAgent,
                platform: navigator.platform,
                webdriver: navigator.webdriver,
                language: navigator.language,
                languages: navigator.languages,
                timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
                screen: `${screen.width}x${screen.height}`,
                inner: `${innerWidth}x${innerHeight}`
            })"""
        )

    def quit(self) -> None:
        try:
            self.context.close()
        finally:
            if self._temp_profile and os.path.isdir(self.profile_dir):
                shutil.rmtree(self.profile_dir, ignore_errors=True)


def build_cloak_driver(profile_dir_override: str | None = None) -> CloakDriver:
    try:
        from cloakbrowser import launch_persistent_context
    except Exception as exc:
        raise RuntimeError("cloakbrowser is not installed. Run: python -m pip install cloakbrowser playwright") from exc

    profile_dir, temp_profile = _profile_dir(profile_dir_override)
    Path(profile_dir).mkdir(parents=True, exist_ok=True)
    launch_kwargs = _cloak_launch_kwargs(profile_dir)
    _validate_launch_kwargs(launch_persistent_context, launch_kwargs)
    _record_and_log_launch_config(profile_dir, launch_kwargs)
    try:
        context = launch_persistent_context(profile_dir, **launch_kwargs)
    except TypeError as exc:
        raise RuntimeError(
            "Installed cloakbrowser.launch_persistent_context rejected the configured launch options. "
            "Upgrade cloakbrowser or unset unsupported CLOAK_* options."
        ) from exc
    pages_attr = getattr(context, "pages", [])
    pages = pages_attr() if callable(pages_attr) else pages_attr
    page = pages[0] if pages else context.new_page()
    driver = CloakDriver(context=context, page=page, profile_dir=profile_dir, temp_profile=temp_profile)
    driver.set_page_load_timeout(60)
    return driver


def cleanup_cloak_driver(driver) -> None:
    temp_profile_dir = getattr(driver, "_temp_profile_dir", None)
    if temp_profile_dir and os.path.isdir(temp_profile_dir):
        shutil.rmtree(temp_profile_dir, ignore_errors=True)


def debug_page_snapshot(driver: CloakDriver) -> dict:
    html_text = driver.page_source or ""
    body_text = _safe_call(lambda: driver.execute_script("return document.body ? document.body.innerText : ''"), "") or ""
    card_counts = {}
    for selector in [
        'article[data-testid="ResidentialCard"]',
        "article.residential-card",
        "article[data-testid]",
        '[data-testid="property-card"]',
    ]:
        card_counts[selector] = len(driver.find_elements(By.CSS_SELECTOR, selector))
    blocked_marker = any(token in html_text.lower() for token in ("window.kpsdk", "kpsdk", "ips.js", "too many requests"))
    return {
        "title": driver.title,
        "current_url": driver.current_url,
        "cards_found": max(card_counts.values()) if card_counts else 0,
        "card_counts": card_counts,
        "body_text_length": len(body_text),
        "html_length": len(html_text),
        "blocked_or_429_marker_found": blocked_marker,
        "blank_render_detected": len(body_text.strip()) == 0 and max(card_counts.values() or [0]) == 0,
        "fingerprint": driver.fingerprint(),
        "http_errors": driver.http_errors,
    }
