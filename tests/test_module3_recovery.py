import json
import sys
import types
from pathlib import Path
from unittest import mock

sys.modules.setdefault("pyodbc", types.SimpleNamespace(connect=lambda *args, **kwargs: None))

import browser_recovery
import job_queue
import monitoring_scheduler
import module3_enrich_details
from realestate_errors import RealEstateBlockedError
from realestate_page_state import PageState, PageStateResult


class FakeDriver:
    def __init__(self, current_url="https://www.realestate.com.au/property-house-nsw-cobar-151402500"):
        self.current_url = current_url
        self.title = "Real Estate & Property"
        self.page_source = "<html><body><h1>1 Test Street</h1><p>House detail ready</p></body></html>"

    def execute_script(self, *_args):
        return "complete"

    def get_log(self, _kind):
        return []

    def refresh(self):
        pass

    def quit(self):
        pass


class FakeChromeErrorDriver(FakeDriver):
    def __init__(self):
        super().__init__("chrome-error://chromewebdata/")
        self.title = ""
        self.page_source = "<html><body></body></html>"


class FakeWait:
    def __init__(self, *_args, **_kwargs):
        pass

    def until(self, _condition):
        return True


def _detail_state(state, current_url=None, html_length=50000, body_text_length=1200, network_reason=None, reason=None):
    return PageStateResult(
        state=state,
        reason=reason or state,
        is_usable=state in {PageState.DETAIL_READY, PageState.DETAIL_REMOVED, PageState.DETAIL_NOT_FOUND, PageState.DETAIL_SOLD},
        is_blocked=state in {PageState.BLOCKED_HTTP_429, PageState.BLOCKED_KPSDK, PageState.BLOCKED_ACCESS_DENIED},
        current_url=current_url or "https://www.realestate.com.au/property-house-nsw-cobar-151402500",
        body_text_length=body_text_length,
        html_length=html_length,
        network_reason=network_reason,
    )


def _write_input(tmp_path: Path):
    path = tmp_path / "module1.json"
    path.write_text(
        json.dumps([
            {
                "listing_id": "151402500",
                "url": "https://www.realestate.com.au/property-house-nsw-cobar-151402500",
                "address": "1 Test Street",
            }
        ]),
        encoding="utf-8",
    )
    return path


def test_module3_chrome_error_recovery_retries_same_detail_and_succeeds(tmp_path):
    input_path = _write_input(tmp_path)
    failed = FakeChromeErrorDriver()
    recovered = FakeDriver()
    ready = _detail_state(PageState.DETAIL_READY)
    chrome_unknown = _detail_state(PageState.UNKNOWN, current_url="chrome-error://chromewebdata/", html_length=100, body_text_length=0)
    requested_url = "https://www.realestate.com.au/property-house-nsw-cobar-151402500"

    attempts = {"count": 0}

    def fake_safe_get(driver, url, log_func=print):
        attempts["count"] += 1
        if attempts["count"] == 1:
            driver.current_url = "chrome-error://chromewebdata/"
            return False, RuntimeError("Page.goto: net::ERR_HTTP_RESPONSE_CODE_FAILURE")
        driver.current_url = url
        return True, None

    with mock.patch.object(module3_enrich_details, "build_driver", return_value=failed), \
         mock.patch.object(browser_recovery, "safe_driver_get", side_effect=fake_safe_get) as safe_get, \
         mock.patch.object(module3_enrich_details, "classify_detail_page", return_value=chrome_unknown), \
         mock.patch.object(module3_enrich_details, "wait_for_detail_page_state", return_value=ready), \
         mock.patch.object(module3_enrich_details, "recover_browser_after_429", return_value=(recovered, 1, "rea_profile_recovered", "recovered")) as recover, \
         mock.patch.object(module3_enrich_details, "WebDriverWait", FakeWait), \
         mock.patch.object(module3_enrich_details, "wait_for_detail_ready", return_value=True), \
         mock.patch.object(module3_enrich_details, "extract_detail_data", return_value={"description": "ready"}), \
         mock.patch.object(module3_enrich_details.time, "sleep", return_value=None), \
         mock.patch.object(browser_recovery.time, "sleep", return_value=None), \
         mock.patch("builtins.print"):
        _csv_path, json_path = module3_enrich_details.module3_run(
            area_search_url="https://www.realestate.com.au/buy/in-cobar,+nsw+2835/list-1",
            input_file=str(input_path),
            out_dir=str(tmp_path),
            wait_timeout=1,
            sleep_between=0,
        )

    assert json_path
    rows = json.loads(Path(json_path).read_text(encoding="utf-8"))
    assert rows[0]["description"] == "ready"
    assert [call.args[1] for call in safe_get.call_args_list[:2]] == [requested_url, requested_url]
    recover.assert_not_called()


def test_module3_transient_detail_kpsdk_settles_without_recovery(tmp_path):
    input_path = _write_input(tmp_path)
    driver = FakeDriver()
    blocked = _detail_state(PageState.BLOCKED_KPSDK, html_length=846, body_text_length=0, network_reason="blocked_http_429")
    ready = _detail_state(PageState.DETAIL_READY, html_length=80000, body_text_length=2000)
    logs = []

    with mock.patch.object(module3_enrich_details, "build_driver", return_value=driver), \
         mock.patch.object(browser_recovery, "safe_driver_get", return_value=(True, None)) as safe_get, \
         mock.patch.object(module3_enrich_details, "wait_for_detail_page_state", return_value=blocked), \
         mock.patch.object(module3_enrich_details, "classify_detail_page", return_value=ready) as classify, \
         mock.patch.object(module3_enrich_details, "recover_browser_after_429") as recover, \
         mock.patch.object(module3_enrich_details, "WebDriverWait", FakeWait), \
         mock.patch.object(module3_enrich_details, "wait_for_detail_ready", return_value=True), \
         mock.patch.object(module3_enrich_details, "extract_detail_data", return_value={"description": "hydrated"}), \
         mock.patch.object(module3_enrich_details.config, "BROWSER_KPSDK_SETTLE_SECONDS", 0), \
         mock.patch.object(module3_enrich_details.config, "BROWSER_BLOCK_GRACE_SECONDS", 0.1), \
         mock.patch.object(module3_enrich_details.config, "BROWSER_BLOCK_POLL_SECONDS", 0.05), \
         mock.patch.object(module3_enrich_details.config, "SCRAPER_VERBOSE_PAGE_STATE", True), \
         mock.patch.object(module3_enrich_details.time, "sleep", return_value=None), \
         mock.patch.object(browser_recovery.time, "sleep", return_value=None), \
         mock.patch("builtins.print"):
        _csv_path, json_path = module3_enrich_details.module3_run(
            area_search_url="https://www.realestate.com.au/buy/in-cobar,+nsw+2835/list-1",
            input_file=str(input_path),
            out_dir=str(tmp_path),
            wait_timeout=1,
            sleep_between=0,
            on_log=logs.append,
        )

    assert json_path
    assert safe_get.call_count == 1
    classify.assert_called_once()
    recover.assert_not_called()
    assert any("Module3 transient KPSDK detected; same-page settle start" in item for item in logs)
    assert any("Module3 DOM-first detail ready after transient KPSDK; ignoring historical 429" in item for item in logs)


def test_module3_recovery_limit_records_retry_wait_result(tmp_path):
    input_path = _write_input(tmp_path)
    failed = FakeChromeErrorDriver()
    chrome_unknown = _detail_state(PageState.UNKNOWN, current_url="chrome-error://chromewebdata/", html_length=100, body_text_length=0)

    with mock.patch.object(module3_enrich_details, "build_driver", return_value=failed), \
         mock.patch.object(browser_recovery, "safe_driver_get", return_value=(False, RuntimeError("Page.goto: net::ERR_HTTP_RESPONSE_CODE_FAILURE"))), \
         mock.patch.object(module3_enrich_details, "classify_detail_page", return_value=chrome_unknown), \
         mock.patch.object(module3_enrich_details, "recover_browser_after_429", return_value=(failed, 0, "rea_profile", "rotation_limit")), \
         mock.patch.object(module3_enrich_details.time, "sleep", return_value=None), \
         mock.patch.object(browser_recovery.time, "sleep", return_value=None), \
         mock.patch("builtins.print"):
        csv_path, json_path = module3_enrich_details.module3_run(
            area_search_url="https://www.realestate.com.au/buy/in-cobar,+nsw+2835/list-1",
            input_file=str(input_path),
            out_dir=str(tmp_path),
            wait_timeout=1,
            sleep_between=0,
        )

    assert csv_path is None and json_path is None
    assert module3_enrich_details.module3_run.last_result["status"] == "retry_wait_browser_recovery"


def test_baseline_setup_area_module3_retryable_interruption_marks_retry_wait(monkeypatch):
    job_queue.enable_in_memory_store()
    try:
        job = job_queue.enqueue_job(job_queue.JOB_TYPE_BASELINE_SETUP_AREA, search_id=1, user_area_id=1, payload={"search_url": "https://example.test/search"}, max_attempts=3)
        class DummyConn:
            def commit(self):
                pass
            def close(self):
                pass
        monkeypatch.setattr(monitoring_scheduler, "_search_is_active_for_monitoring", lambda search_id: True)
        monkeypatch.setattr(monitoring_scheduler.db_layer, "connect", lambda path=None: DummyConn())
        monkeypatch.setattr(monitoring_scheduler.db_layer, "upsert_area_monitoring_state", lambda *args, **kwargs: None)
        monkeypatch.setattr(
            monitoring_scheduler,
            "baseline_setup_area",
            lambda area_url: (_ for _ in ()).throw(RealEstateBlockedError("Module3 retryable browser/navigation interruption", retry_after_seconds=60)),
        )
        out = monitoring_scheduler.run_next_job_once(worker_id="module3-test", send_telegram=False)
        stored = job_queue._TEST_STORE[0]
        assert out["status"] == "completed"
        assert out["job_result"]["status"] == "retry_wait"
        assert stored["Status"] == "retry_wait"
        assert stored["RunAfter"] is not None
    finally:
        job_queue.disable_in_memory_store()


def test_preparing_subscription_skips_operational_monitoring_job(monkeypatch):
    preparing_sub = {
        "UserAreaID": 1,
        "SearchID": 1,
        "SearchURL": "https://example.test/search",
        "SubscriptionStatus": "preparing",
        "UserAreaSubscriptionStatus": "preparing",
        "SubscriptionNotifyEnabled": 1,
        "UserAreaNotifyEnabled": 1,
        "NotificationReadyAt": None,
        "BaselineStatus": "pending",
        "DetailBaselineStatus": "pending",
        "PriceBaselineStatus": "pending",
        "AreaSetupStatus": "preparing",
    }
    monkeypatch.setattr(monitoring_scheduler, "_search_is_active_for_monitoring", lambda search_id: True)
    monkeypatch.setattr(monitoring_scheduler.db_layer, "connect", lambda path=None: types.SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(monitoring_scheduler.db_layer, "get_active_user_area_subscriptions_for_search", lambda conn, search_id: [preparing_sub])
    out = monitoring_scheduler.execute_job(
        {"JobType": job_queue.JOB_TYPE_LIGHT_CHECK_NEW_LISTINGS, "SearchID": 1, "UserAreaID": 1},
        send_telegram=False,
    )
    assert out["status"] == "skipped"
    assert out["reason"] == "search_not_ready_for_operational_monitoring"
