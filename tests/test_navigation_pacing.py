import importlib
import os
import tempfile
import types
from pathlib import Path
from unittest import mock

import pytest

import browser_recovery
import config
import module2_infer_prices
import module3_enrich_details


class ChromeErrorDriver:
    def __init__(self):
        self.current_url = "chrome-error://chromewebdata/"
        self.get_calls = []
        self.scripts = []
        self.cdp = []

    def execute_script(self, script):
        self.scripts.append(script)

    def execute_cdp_cmd(self, command, params=None):
        self.cdp.append((command, params or {}))

    def get(self, url):
        self.get_calls.append(url)
        self.current_url = url


def test_reset_chrome_error_tab_resets_to_about_blank():
    driver = ChromeErrorDriver()
    logs = []
    with mock.patch.object(browser_recovery.time, "sleep", return_value=None):
        assert browser_recovery.reset_chrome_error_tab(driver, log_func=logs.append) is True
    assert driver.get_calls == ["about:blank"]
    assert any("chrome-error tab reset" in msg for msg in logs)
    assert driver.current_url == "about:blank"


def test_human_inter_navigation_delay_logs_module1_env(monkeypatch):
    monkeypatch.setattr(browser_recovery.config, "MODULE1_INTER_PAGE_DELAY_SECONDS", 10)
    monkeypatch.setattr(browser_recovery.config, "MODULE1_INTER_PAGE_DELAY_JITTER_SECONDS", 0)
    logs = []
    with mock.patch.object(browser_recovery.time, "sleep", return_value=None) as sleep_mock:
        delay = browser_recovery.human_inter_navigation_delay("Module1", "inter-page before next page", log_func=logs.append)
    assert delay == 10
    sleep_mock.assert_called_once_with(10)
    assert logs == ["Module1 inter-navigation delay phase=inter-page before next page: 10.0s"]


def test_config_parses_inter_navigation_delay_env(monkeypatch):
    monkeypatch.setenv("MODULE1_INTER_PAGE_DELAY_SECONDS", "10")
    monkeypatch.setenv("MODULE1_INTER_PAGE_DELAY_JITTER_SECONDS", "5")
    monkeypatch.setenv("MODULE2_INTER_PAGE_DELAY_SECONDS", "11")
    monkeypatch.setenv("MODULE2_INTER_WINDOW_DELAY_SECONDS", "12")
    monkeypatch.setenv("MODULE3_INTER_DETAIL_DELAY_SECONDS", "13")
    reloaded = importlib.reload(config)
    try:
        assert reloaded.MODULE1_INTER_PAGE_DELAY_SECONDS == 10
        assert reloaded.MODULE1_INTER_PAGE_DELAY_JITTER_SECONDS == 5
        assert reloaded.MODULE2_INTER_PAGE_DELAY_SECONDS == 11
        assert reloaded.MODULE2_INTER_WINDOW_DELAY_SECONDS == 12
        assert reloaded.MODULE3_INTER_DETAIL_DELAY_SECONDS == 13
    finally:
        importlib.reload(config)


def test_module1_no_hardcoded_045_sleep():
    assert "time.sleep(0.45)" not in Path("module1_list_scraper.py").read_text(encoding="utf-8")


def test_safe_realestate_get_resets_chrome_error_and_retries_same_url(monkeypatch):
    driver = ChromeErrorDriver()
    attempts = {"count": 0}
    requested = "https://www.realestate.com.au/buy/in-test/list-2"

    def fake_safe_get(driver_obj, url, log_func=print):
        attempts["count"] += 1
        if attempts["count"] == 1:
            driver_obj.current_url = "chrome-error://chromewebdata/"
            return False, RuntimeError("Page.goto: net::ERR_HTTP_RESPONSE_CODE_FAILURE")
        driver_obj.current_url = url
        return True, None

    monkeypatch.setattr(browser_recovery.config, "MODULE1_CHROME_ERROR_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(browser_recovery.config, "MODULE1_CHROME_ERROR_NAV_RESET", True)
    logs = []
    with mock.patch.object(browser_recovery, "safe_driver_get", side_effect=fake_safe_get), \
         mock.patch.object(browser_recovery.time, "sleep", return_value=None):
        ok, err = browser_recovery.safe_realestate_get_with_reset(
            driver,
            requested,
            module_name="Module1",
            phase="list_page_2",
            log_func=logs.append,
            apply_delay=False,
        )
    assert ok is True
    assert attempts["count"] == 2
    assert driver.current_url == requested
    assert any("chrome-error navigation reset before retry" in msg for msg in logs)


def test_module2_get_with_retries_uses_chrome_error_reset_retry(monkeypatch):
    driver = ChromeErrorDriver()
    attempts = {"count": 0}
    requested = "https://www.realestate.com.au/buy/between-0-100-in-test/list-2"

    def fake_safe_get(driver_obj, url, log_func=print):
        attempts["count"] += 1
        if attempts["count"] == 1:
            driver_obj.current_url = "chrome-error://chromewebdata/"
            return False, RuntimeError("Page.goto: net::ERR_HTTP_RESPONSE_CODE_FAILURE")
        driver_obj.current_url = url
        return True, None

    monkeypatch.setattr(browser_recovery.config, "MODULE2_CHROME_ERROR_RETRY_DELAY_SECONDS", 0)
    logs = []
    with mock.patch.object(browser_recovery, "safe_driver_get", side_effect=fake_safe_get), \
         mock.patch.object(browser_recovery.time, "sleep", return_value=None):
        _driver, ok, err = module2_infer_prices.get_with_retries(
            driver,
            requested,
            tries=1,
            phase="page",
            apply_delay=False,
            log_func=logs.append,
        )
    assert ok is True
    assert err is None
    assert attempts["count"] == 2
    assert any("Module2 chrome-error navigation reset before retry" in msg for msg in logs)


def test_module3_chrome_error_does_not_advance_checkpoint_or_done(tmp_path, monkeypatch):
    input_path = tmp_path / "module1.json"
    input_path.write_text(
        '[{"listing_id":"1","url":"https://www.realestate.com.au/property-house-nsw-test-1"}]',
        encoding="utf-8",
    )
    chrome_state = types.SimpleNamespace(
        state="chrome_error",
        reason="chrome_error_page",
        current_url="chrome-error://chromewebdata/",
        html_length=100,
        body_text_length=0,
        network_reason=None,
        is_blocked=False,
    )

    with mock.patch.object(module3_enrich_details, "build_driver", return_value=ChromeErrorDriver()), \
         mock.patch.object(browser_recovery, "safe_driver_get", return_value=(False, RuntimeError("Page.goto: net::ERR_HTTP_RESPONSE_CODE_FAILURE"))), \
         mock.patch.object(module3_enrich_details, "classify_detail_page", return_value=chrome_state), \
         mock.patch.object(browser_recovery.time, "sleep", return_value=None), \
         mock.patch.object(module3_enrich_details.time, "sleep", return_value=None):
        csv_path, json_path = module3_enrich_details.module3_run(
            area_search_url="https://www.realestate.com.au/buy/in-test/list-1",
            input_file=str(input_path),
            out_dir=str(tmp_path),
            wait_timeout=1,
            sleep_between=0,
        )

    assert csv_path is None and json_path is None
    checkpoint_files = list(tmp_path.glob("module3_details_checkpoint_*.json"))
    assert checkpoint_files
    checkpoint = __import__("json").loads(checkpoint_files[0].read_text(encoding="utf-8"))
    assert checkpoint.get("done_listing_ids", []) == []
    assert checkpoint.get("last_index", -1) == -1
