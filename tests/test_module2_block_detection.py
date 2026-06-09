import tempfile
import sys
import types
import unittest
from unittest import mock

sys.modules.setdefault("pyodbc", types.SimpleNamespace(connect=lambda *args, **kwargs: None))

import module2_infer_prices
import browser_recovery
import tools.test_module2_cloak_small_windows as module2_smoke
from realestate_page_state import PageState, PageStateResult


class FakeDriver:
    current_url = "https://www.realestate.com.au/buy/between-0-100-in-test/list-1"
    page_source = "<html></html>"

    def get(self, url):
        self.current_url = url

    def execute_script(self, *_args):
        return ""

    def quit(self):
        pass


class FakeWait:
    def __init__(self, *_args, **_kwargs):
        pass

    def until(self, _condition):
        return True


def _state(state, cards=0, html_length=874, body_text_length=0):
    return PageStateResult(
        state=state,
        reason=state,
        is_usable=state in {PageState.LISTINGS, PageState.NO_RESULTS},
        is_blocked=state in {PageState.BLOCKED_HTTP_429, PageState.BLOCKED_KPSDK, PageState.BLOCKED_ACCESS_DENIED},
        is_no_results=state == PageState.NO_RESULTS,
        has_cards=cards > 0,
        cards_count=cards,
        current_url=FakeDriver.current_url,
        html_length=html_length,
        body_text_length=body_text_length,
    )


class Module2BlockDetectionTests(unittest.TestCase):
    def _run_window(self, states, wait_cards=("cards", [object()])):
        driver = FakeDriver()
        ck = {
            "inferred_map": {},
            "remaining_ids": ["target-1"],
            "next_window_index": 0,
            "window_idx": 0,
            "profile_rotations": 0,
        }
        logs = []
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(module2_infer_prices, "get_with_retries", return_value=(driver, True, None)), \
             mock.patch.object(module2_infer_prices, "wait_for_search_page_state", side_effect=[(s, [] if not s.has_cards else [object()]) for s in states]), \
             mock.patch.object(module2_infer_prices, "wait_for_cards_or_no_results", return_value=wait_cards) as wait_cards_mock, \
             mock.patch.object(module2_infer_prices, "extract_listing_ids_from_cards", return_value={"target-1"}), \
             mock.patch.object(module2_infer_prices, "recover_browser_after_429") as recover, \
             mock.patch.object(module2_infer_prices, "WebDriverWait", FakeWait), \
             mock.patch.object(module2_infer_prices.config, "BROWSER_KPSDK_SETTLE_SECONDS", 0), \
             mock.patch.object(module2_infer_prices.config, "BROWSER_KPSDK_SAME_SESSION_RECHECKS", 2), \
             mock.patch.object(browser_recovery.config, "BROWSER_KPSDK_SETTLE_SECONDS", 0), \
             mock.patch.object(browser_recovery.config, "BROWSER_KPSDK_SAME_SESSION_RECHECKS", 2), \
             mock.patch.object(browser_recovery.time, "sleep", return_value=None), \
             mock.patch.object(module2_infer_prices.config, "MODULE2_SLEEP_BETWEEN_WINDOWS_MIN", 0), \
             mock.patch.object(module2_infer_prices.config, "MODULE2_SLEEP_BETWEEN_WINDOWS_MAX", 0):
            inferred, _driver, status = module2_infer_prices.infer_prices_window_based_with_checkpoint(
                driver=driver,
                base_list_url="https://www.realestate.com.au/buy/in-test/list-1",
                target_ids={"target-1"},
                window_width=100,
                step=100,
                start_low=0,
                max_high=100,
                max_pages_per_window=1,
                wait_timeout=1,
                ck_path=f"{tmp}/ck.json",
                ck=ck,
                log_func=logs.append,
                sweep_windows=[(0, 100, 100)],
                max_windows_per_run=1,
            )
        return inferred, status, recover, wait_cards_mock, logs

    def test_kpsdk_then_listings_rechecks_without_recovery(self):
        inferred, status, recover, _wait_cards, logs = self._run_window([
            _state(PageState.BLOCKED_KPSDK),
            _state(PageState.LISTINGS, cards=1, html_length=50000, body_text_length=1200),
        ])

        self.assertEqual(status, "done")
        self.assertIn("target-1", inferred)
        recover.assert_not_called()
        self.assertTrue(any("Module2 KPSDK same-page settle attempt=1 state=listings" in msg for msg in logs))
        self.assertTrue(any("Module2 trusted_window=True state=listings" in msg for msg in logs))

    def test_kpsdk_then_no_results_is_valid_empty_window(self):
        inferred, status, recover, wait_cards, logs = self._run_window([
            _state(PageState.BLOCKED_KPSDK),
            _state(PageState.NO_RESULTS),
        ], wait_cards=("no_results", []))

        self.assertEqual(status, "done")
        self.assertEqual(inferred, {})
        recover.assert_not_called()
        wait_cards.assert_not_called()
        self.assertTrue(any("Module2 trusted_window=True state=no_results" in msg for msg in logs))

    def test_module2_kpsdk_same_page_settle_does_not_call_safe_get(self):
        driver = FakeDriver()
        blocked = _state(PageState.BLOCKED_KPSDK)
        listings = _state(PageState.LISTINGS, cards=1, html_length=50000, body_text_length=1200)
        logs = []

        with mock.patch.object(module2_infer_prices, "wait_for_search_page_state", return_value=(listings, [object()])) as wait_state, \
             mock.patch.object(module2_infer_prices, "_same_driver_get") as same_get, \
             mock.patch.object(module2_infer_prices.time, "sleep", return_value=None), \
             mock.patch.object(module2_infer_prices.config, "BROWSER_KPSDK_SETTLE_SECONDS", 0), \
             mock.patch.object(module2_infer_prices.config, "BROWSER_KPSDK_SAME_SESSION_RECHECKS", 2):
            result, cards = module2_infer_prices._module2_same_page_kpsdk_settle(
                driver,
                driver.current_url,
                blocked,
                [],
                min_cards=1,
                timeout=1,
                log_func=logs.append,
            )

        self.assertEqual(result.state, PageState.LISTINGS)
        self.assertEqual(len(cards), 1)
        wait_state.assert_called_once()
        same_get.assert_not_called()
        self.assertTrue(any("same-page settle attempt=1 state=listings" in msg for msg in logs))

    def test_persistent_kpsdk_returns_existing_recovery_status(self):
        _inferred, status, recover, _wait_cards, logs = self._run_window([
            _state(PageState.BLOCKED_KPSDK),
            _state(PageState.BLOCKED_KPSDK),
            _state(PageState.BLOCKED_KPSDK),
        ])

        self.assertEqual(status, "retry_wait_browser_recovery")
        recover.assert_not_called()
        self.assertTrue(any("Module2 trusted_window=False state=blocked_kpsdk" in msg for msg in logs))

    def test_render_timeout_is_retryable_not_no_results(self):
        inferred, status, recover, wait_cards, logs = self._run_window([
            _state(PageState.RENDER_TIMEOUT),
        ])

        self.assertEqual(status, "render_timeout")
        self.assertEqual(inferred, {})
        recover.assert_not_called()
        wait_cards.assert_not_called()
        self.assertTrue(any("Module2 trusted_window=False state=render_timeout" in msg for msg in logs))

    def test_no_results_first_window_is_trusted_and_advances_to_next_window(self):
        driver = FakeDriver()
        ck = {"inferred_map": {}, "remaining_ids": ["target-1"], "next_window_index": 0, "window_idx": 0, "profile_rotations": 0}
        states = [
            _state(PageState.BLOCKED_KPSDK),
            _state(PageState.NO_RESULTS),
            _state(PageState.LISTINGS, cards=1, html_length=50000, body_text_length=1200),
        ]
        logs = []
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(module2_infer_prices, "get_with_retries", return_value=(driver, True, None)), \
             mock.patch.object(module2_infer_prices, "wait_for_search_page_state", side_effect=[(s, [] if not s.has_cards else [object()]) for s in states]), \
             mock.patch.object(module2_infer_prices, "wait_for_cards_or_no_results", return_value=("cards", [object()])), \
             mock.patch.object(module2_infer_prices, "extract_listing_ids_from_cards", return_value={"target-1"}), \
             mock.patch.object(module2_infer_prices, "recover_browser_after_429") as recover, \
             mock.patch.object(module2_infer_prices, "WebDriverWait", FakeWait), \
             mock.patch.object(module2_infer_prices.time, "sleep", return_value=None), \
             mock.patch.object(module2_infer_prices.config, "BROWSER_KPSDK_SETTLE_SECONDS", 0), \
             mock.patch.object(module2_infer_prices.config, "BROWSER_KPSDK_SAME_SESSION_RECHECKS", 2), \
             mock.patch.object(module2_infer_prices.config, "MODULE2_SLEEP_BETWEEN_WINDOWS_MIN", 0), \
             mock.patch.object(module2_infer_prices.config, "MODULE2_SLEEP_BETWEEN_WINDOWS_MAX", 0), \
             mock.patch.object(module2_infer_prices.config, "MODULE2_MIN_WINDOWS_BEFORE_SESSION_RECOVERY", 5):
            inferred, _driver, status = module2_infer_prices.infer_prices_window_based_with_checkpoint(
                driver=driver,
                base_list_url="https://www.realestate.com.au/buy/in-test/list-1",
                target_ids={"target-1"},
                window_width=100,
                step=100,
                start_low=0,
                max_high=200,
                max_pages_per_window=1,
                wait_timeout=1,
                ck_path=f"{tmp}/ck.json",
                ck=ck,
                log_func=logs.append,
                sweep_windows=[(0, 100, 100), (100, 200, 100)],
                max_windows_per_run=2,
            )

        self.assertEqual(status, "done")
        self.assertIn("target-1", inferred)
        recover.assert_not_called()
        self.assertGreaterEqual(int(ck["window_idx"]), 2)
        self.assertTrue(any("Module2 trusted_window=True state=no_results" in msg for msg in logs))

    def test_test_max_windows_limits_setup_full_sweep(self):
        driver = FakeDriver()
        ck = {"inferred_map": {}, "remaining_ids": ["target-1"], "next_window_index": 0, "window_idx": 0, "profile_rotations": 0}
        states = [_state(PageState.LISTINGS, cards=1, html_length=50000, body_text_length=1200) for _ in range(3)]
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(module2_infer_prices, "get_with_retries", return_value=(driver, True, None)), \
             mock.patch.object(module2_infer_prices, "wait_for_search_page_state", side_effect=[(s, [object()]) for s in states]), \
             mock.patch.object(module2_infer_prices, "wait_for_cards_or_no_results", return_value=("cards", [object()])), \
             mock.patch.object(module2_infer_prices, "extract_listing_ids_from_cards", return_value=set()), \
             mock.patch.object(module2_infer_prices, "WebDriverWait", FakeWait), \
             mock.patch.object(module2_infer_prices.config, "MODULE2_SLEEP_BETWEEN_WINDOWS_MIN", 0), \
             mock.patch.object(module2_infer_prices.config, "MODULE2_SLEEP_BETWEEN_WINDOWS_MAX", 0), \
             mock.patch.object(module2_infer_prices.config, "MODULE2_SLEEP_BETWEEN_PAGES_MIN", 0), \
             mock.patch.object(module2_infer_prices.config, "MODULE2_SLEEP_BETWEEN_PAGES_MAX", 0):
            inferred, _driver, status = module2_infer_prices.infer_prices_window_based_with_checkpoint(
                driver=driver,
                base_list_url="https://www.realestate.com.au/buy/in-test/list-1",
                target_ids={"target-1"},
                window_width=100,
                step=100,
                start_low=0,
                max_high=500,
                max_pages_per_window=1,
                wait_timeout=1,
                ck_path=f"{tmp}/ck.json",
                ck=ck,
                log_func=lambda _msg: None,
                sweep_windows=[(0, 100, 100), (100, 200, 100), (200, 300, 100), (300, 400, 100), (400, 500, 100)],
                max_windows_per_run=3,
                test_limit_mode=True,
            )

        self.assertEqual(status, "max_windows_test_limit")
        self.assertLessEqual(int(ck["window_idx"]), 3)
        self.assertEqual(inferred, {})
        self.assertEqual(ck["remaining_ids"], ["target-1"])

    def test_without_test_max_windows_setup_full_sweep_keeps_existing_behavior(self):
        driver = FakeDriver()
        ck = {"inferred_map": {}, "remaining_ids": ["target-1"], "next_window_index": 0, "window_idx": 0, "profile_rotations": 0}
        states = [_state(PageState.LISTINGS, cards=1, html_length=50000, body_text_length=1200) for _ in range(5)]
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(module2_infer_prices, "get_with_retries", return_value=(driver, True, None)), \
             mock.patch.object(module2_infer_prices, "wait_for_search_page_state", side_effect=[(s, [object()]) for s in states]), \
             mock.patch.object(module2_infer_prices, "wait_for_cards_or_no_results", return_value=("cards", [object()])), \
             mock.patch.object(module2_infer_prices, "extract_listing_ids_from_cards", return_value=set()), \
             mock.patch.object(module2_infer_prices, "WebDriverWait", FakeWait), \
             mock.patch.object(module2_infer_prices.config, "MODULE2_SLEEP_BETWEEN_WINDOWS_MIN", 0), \
             mock.patch.object(module2_infer_prices.config, "MODULE2_SLEEP_BETWEEN_WINDOWS_MAX", 0), \
             mock.patch.object(module2_infer_prices.config, "MODULE2_SLEEP_BETWEEN_PAGES_MIN", 0), \
             mock.patch.object(module2_infer_prices.config, "MODULE2_SLEEP_BETWEEN_PAGES_MAX", 0):
            _inferred, _driver, status = module2_infer_prices.infer_prices_window_based_with_checkpoint(
                driver=driver,
                base_list_url="https://www.realestate.com.au/buy/in-test/list-1",
                target_ids={"target-1"},
                window_width=100,
                step=100,
                start_low=0,
                max_high=500,
                max_pages_per_window=1,
                wait_timeout=1,
                ck_path=f"{tmp}/ck.json",
                ck=ck,
                log_func=lambda _msg: None,
                sweep_windows=[(0, 100, 100), (100, 200, 100), (200, 300, 100), (300, 400, 100), (400, 500, 100)],
                max_windows_per_run=0,
                test_limit_mode=False,
            )

        self.assertEqual(status, "done")
        self.assertEqual(int(ck["window_idx"]), 5)


class FakeModule2PaginationDriver(FakeDriver):
    def __init__(self, has_next: bool = False):
        self.current_url = "https://www.realestate.com.au/buy/between-0-100-in-test/list-1"
        self.page_source = "<html></html>"
        self.has_next = has_next
        self.scripts: list[str] = []

    def execute_script(self, script):
        self.scripts.append(script)
        if "dispatchEvent" in script:
            if self.has_next:
                self.current_url = "https://www.realestate.com.au/buy/between-0-100-in-test/list-2"
                return {"clicked": True, "href": self.current_url}
            return {"clicked": False, "reason": "next_anchor_not_found"}
        if "querySelectorAll" in script:
            if self.has_next:
                return {"exists": True, "href": "https://www.realestate.com.au/buy/between-0-100-in-test/list-2", "rel": "next", "aria": "Go to next page", "text": "Next"}
            return {"exists": False, "reason": "no_next_anchor"}
        return ""

    def find_elements(self, *_args):
        return []


class Module2PaginationNavigationTests(unittest.TestCase):
    def test_trusted_short_page_without_next_anchor_does_not_navigate_list2(self):
        driver = FakeModule2PaginationDriver(has_next=False)
        ck = {"inferred_map": {}, "remaining_ids": ["target-1"], "next_window_index": 0, "window_idx": 0, "profile_rotations": 0}
        get_urls = []
        logs = []

        def fake_get(drv, url, **_kwargs):
            get_urls.append(url)
            drv.current_url = url
            return drv, True, None

        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(module2_infer_prices.config, "MODULE2_PAGINATION_NAV_MODE", "click_next"), \
             mock.patch.object(module2_infer_prices, "get_with_retries", side_effect=fake_get), \
             mock.patch.object(module2_infer_prices, "wait_for_search_page_state", return_value=(_state(PageState.LISTINGS, cards=4, html_length=50000, body_text_length=1200), [object() for _ in range(4)])), \
             mock.patch.object(module2_infer_prices, "wait_for_cards_or_no_results", return_value=("cards", [object() for _ in range(4)])), \
             mock.patch.object(module2_infer_prices, "extract_listing_ids_from_cards", return_value=set()), \
             mock.patch.object(module2_infer_prices, "WebDriverWait", FakeWait), \
             mock.patch.object(module2_infer_prices.config, "MODULE2_SLEEP_BETWEEN_WINDOWS_MIN", 0), \
             mock.patch.object(module2_infer_prices.config, "MODULE2_SLEEP_BETWEEN_WINDOWS_MAX", 0):
            _inferred, _driver, status = module2_infer_prices.infer_prices_window_based_with_checkpoint(
                driver=driver,
                base_list_url="https://www.realestate.com.au/buy/in-test/list-1",
                target_ids={"target-1"},
                window_width=100,
                step=100,
                start_low=0,
                max_high=100,
                max_pages_per_window=2,
                wait_timeout=1,
                ck_path=f"{tmp}/ck.json",
                ck=ck,
                log_func=logs.append,
                sweep_windows=[(0, 100, 100)],
                max_windows_per_run=1,
                test_limit_mode=True,
            )

        self.assertNotIn("retry_wait_network_interrupted", status)
        self.assertFalse(any("/list-2" in url for url in get_urls))
        self.assertTrue(any("Module2 window pagination ended page=1 reason=no_next_anchor cards_found=4" in msg for msg in logs))
        self.assertEqual(ck["ended_window_reasons"][-1]["reason"], "no_next_anchor")

    def test_page2_uses_click_next_and_skips_direct_url_after_success(self):
        driver = FakeModule2PaginationDriver(has_next=True)
        ck = {"inferred_map": {}, "remaining_ids": ["target-1"], "next_window_index": 0, "window_idx": 0, "profile_rotations": 0}
        get_urls = []
        states = [
            (_state(PageState.LISTINGS, cards=25, html_length=50000, body_text_length=1200), [object() for _ in range(25)]),
            (_state(PageState.LISTINGS, cards=3, html_length=50000, body_text_length=1200), [object() for _ in range(3)]),
        ]
        payloads = [("cards", [object() for _ in range(25)]), ("cards", [object() for _ in range(3)])]

        def fake_get(drv, url, **_kwargs):
            get_urls.append(url)
            drv.current_url = url
            return drv, True, None

        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(module2_infer_prices.config, "MODULE2_PAGINATION_NAV_MODE", "click_next"), \
             mock.patch.object(module2_infer_prices, "get_with_retries", side_effect=fake_get), \
             mock.patch.object(module2_infer_prices, "wait_for_search_page_state", side_effect=states), \
             mock.patch.object(module2_infer_prices, "wait_for_cards_or_no_results", side_effect=payloads), \
             mock.patch.object(module2_infer_prices, "extract_listing_ids_from_cards", side_effect=[set(), {"target-1"}]), \
             mock.patch.object(module2_infer_prices, "WebDriverWait", FakeWait), \
             mock.patch.object(module2_infer_prices.time, "sleep", return_value=None), \
             mock.patch.object(module2_infer_prices.config, "MODULE2_SLEEP_BETWEEN_WINDOWS_MIN", 0), \
             mock.patch.object(module2_infer_prices.config, "MODULE2_SLEEP_BETWEEN_WINDOWS_MAX", 0):
            inferred, _driver, status = module2_infer_prices.infer_prices_window_based_with_checkpoint(
                driver=driver,
                base_list_url="https://www.realestate.com.au/buy/in-test/list-1",
                target_ids={"target-1"},
                window_width=100,
                step=100,
                start_low=0,
                max_high=100,
                max_pages_per_window=2,
                wait_timeout=1,
                ck_path=f"{tmp}/ck.json",
                ck=ck,
                log_func=lambda _msg: None,
                sweep_windows=[(0, 100, 100)],
                max_windows_per_run=1,
            )

        self.assertEqual(status, "done")
        self.assertIn("target-1", inferred)
        self.assertEqual(len(get_urls), 1)
        self.assertFalse(any("/list-2" in url for url in get_urls))
        self.assertEqual(ck["window_page_stats"][1]["nav"], "click_next")

    def test_window2_goto_failure_uses_fresh_context_retry_and_continues(self):
        driver = FakeModule2PaginationDriver(has_next=False)
        fresh_driver = FakeModule2PaginationDriver(has_next=False)
        ck = {"inferred_map": {}, "remaining_ids": ["target-1"], "next_window_index": 0, "window_idx": 0, "profile_rotations": 0}
        calls = []
        states = [
            (_state(PageState.LISTINGS, cards=4, html_length=50000, body_text_length=1200), [object() for _ in range(4)]),
            (_state(PageState.LISTINGS, cards=4, html_length=50000, body_text_length=1200), [object() for _ in range(4)]),
        ]

        def fake_get(drv, url, *, phase="page", **_kwargs):
            calls.append((phase, url))
            if "between-100-200" in url and phase == "window":
                return drv, False, module2_infer_prices.WebDriverException("Page.goto: net::ERR_HTTP_RESPONSE_CODE_FAILURE")
            drv.current_url = url
            return drv, True, None

        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(module2_infer_prices.config, "MODULE2_WINDOW_NAV_MODE", "fresh_context_on_failure"), \
             mock.patch.object(module2_infer_prices.config, "MODULE2_PAGINATION_NAV_MODE", "click_next"), \
             mock.patch.object(module2_infer_prices, "get_with_retries", side_effect=fake_get), \
             mock.patch.object(module2_infer_prices, "build_driver", return_value=fresh_driver), \
             mock.patch.object(module2_infer_prices, "wait_for_search_page_state", side_effect=states), \
             mock.patch.object(module2_infer_prices, "wait_for_cards_or_no_results", side_effect=[("cards", [object() for _ in range(4)]), ("cards", [object() for _ in range(4)])]), \
             mock.patch.object(module2_infer_prices, "extract_listing_ids_from_cards", side_effect=[set(), {"target-1"}]), \
             mock.patch.object(module2_infer_prices, "WebDriverWait", FakeWait), \
             mock.patch.object(module2_infer_prices.config, "MODULE2_SLEEP_BETWEEN_WINDOWS_MIN", 0), \
             mock.patch.object(module2_infer_prices.config, "MODULE2_SLEEP_BETWEEN_WINDOWS_MAX", 0):
            inferred, _driver, status = module2_infer_prices.infer_prices_window_based_with_checkpoint(
                driver=driver,
                base_list_url="https://www.realestate.com.au/buy/in-test/list-1",
                target_ids={"target-1"},
                window_width=100,
                step=100,
                start_low=0,
                max_high=200,
                max_pages_per_window=1,
                wait_timeout=1,
                ck_path=f"{tmp}/ck.json",
                ck=ck,
                log_func=lambda _msg: None,
                sweep_windows=[(0, 100, 100), (100, 200, 100)],
                max_windows_per_run=2,
            )

        self.assertEqual(status, "done")
        self.assertIn("target-1", inferred)
        self.assertEqual(len(ck["window_page_stats"]), 2)
        self.assertEqual(ck["window_page_stats"][0]["window"], 1)
        self.assertEqual(ck["window_page_stats"][1]["nav"], "fresh_context_retry")
        self.assertTrue(any(path.startswith("fresh_context_retry:window_2:page_1") for path in ck["window_fallback_paths"]))
        self.assertTrue(any(item.get("action") == "fresh_context_retry" for item in ck["recovery_attempts"]))

    def test_window_fresh_context_retry_exhaustion_returns_network_retry_without_traceback(self):
        driver = FakeModule2PaginationDriver(has_next=False)
        fresh_driver = FakeModule2PaginationDriver(has_next=False)
        ck = {"inferred_map": {}, "remaining_ids": ["target-1"], "next_window_index": 0, "window_idx": 0, "profile_rotations": 0}
        logs = []

        def fake_get(drv, url, *, phase="page", **_kwargs):
            if "between-100-200" in url:
                return drv, False, module2_infer_prices.WebDriverException("net::ERR_HTTP_RESPONSE_CODE_FAILURE")
            drv.current_url = url
            return drv, True, None

        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(module2_infer_prices.config, "MODULE2_WINDOW_NAV_MODE", "fresh_context_on_failure"), \
             mock.patch.object(module2_infer_prices, "get_with_retries", side_effect=fake_get), \
             mock.patch.object(module2_infer_prices, "build_driver", return_value=fresh_driver), \
             mock.patch.object(module2_infer_prices, "wait_for_search_page_state", return_value=(_state(PageState.LISTINGS, cards=4, html_length=50000, body_text_length=1200), [object() for _ in range(4)])), \
             mock.patch.object(module2_infer_prices, "wait_for_cards_or_no_results", return_value=("cards", [object() for _ in range(4)])), \
             mock.patch.object(module2_infer_prices, "extract_listing_ids_from_cards", return_value=set()), \
             mock.patch.object(module2_infer_prices, "WebDriverWait", FakeWait), \
             mock.patch.object(module2_infer_prices.config, "MODULE2_SLEEP_BETWEEN_WINDOWS_MIN", 0), \
             mock.patch.object(module2_infer_prices.config, "MODULE2_SLEEP_BETWEEN_WINDOWS_MAX", 0):
            _inferred, _driver, status = module2_infer_prices.infer_prices_window_based_with_checkpoint(
                driver=driver,
                base_list_url="https://www.realestate.com.au/buy/in-test/list-1",
                target_ids={"target-1"},
                window_width=100,
                step=100,
                start_low=0,
                max_high=200,
                max_pages_per_window=1,
                wait_timeout=1,
                ck_path=f"{tmp}/ck.json",
                ck=ck,
                log_func=logs.append,
                sweep_windows=[(0, 100, 100), (100, 200, 100)],
                max_windows_per_run=2,
            )

        self.assertEqual(status, "retry_wait_network_interrupted")
        self.assertEqual(len(ck["window_page_stats"]), 1)
        self.assertTrue(any(path.startswith("fresh_context_retry:window_2:page_1") for path in ck["window_fallback_paths"]))
        self.assertTrue(any("fresh_context window retry failed window=2" in msg for msg in logs))

    def test_click_next_script_does_not_use_selenium_arguments(self):
        import inspect
        script = inspect.getsource(module2_infer_prices._module2_click_next_anchor)
        self.assertNotIn("arguments[0]", script)

class BrowserRecoveryKpsdkRecheckTests(unittest.TestCase):
    def test_same_session_kpsdk_recheck_catches_safe_get_exception(self):
        driver = FakeDriver()
        logs = []
        blocked = _state(PageState.BLOCKED_KPSDK)
        chrome_error = _state(PageState.CHROME_ERROR)
        chrome_error.current_url = "chrome-error://chromewebdata/"

        def safe_get_raises(_driver, _url):
            _driver.current_url = "chrome-error://chromewebdata/"
            raise module2_infer_prices.WebDriverException("Page.goto: net::ERR_HTTP_RESPONSE_CODE_FAILURE")

        result, cards = browser_recovery.same_session_kpsdk_recheck(
            driver=driver,
            url="https://www.realestate.com.au/buy/in-test/list-1",
            wait_func=mock.Mock(return_value=(chrome_error, [])),
            safe_get_func=safe_get_raises,
            log_func=logs.append,
            module_name="Module2",
            timeout=1,
            min_cards=1,
            initial_result=blocked,
            initial_payload=[],
        )

        self.assertEqual(result.state, PageState.CHROME_ERROR)
        self.assertEqual(cards, [])
        self.assertTrue(any("Module2 KPSDK recheck navigation failed" in msg for msg in logs))


class Module2SmallWindowSmokeToolTests(unittest.TestCase):
    def test_build_summary_reports_graceful_crash_json_fields(self):
        module2_infer_prices.module2_run.last_result = {"status": "retry_wait_browser_recovery", "target_count": 3, "remaining_count": 2, "session_failure_windows": 1}
        logs = [
            "Window 1: between-0-200000 | page 1",
            "   Module2 trusted_window=True state=no_results",
            "retry_wait",
        ]

        summary = module2_smoke.build_summary(logs=logs, crash=False)

        self.assertEqual(summary["status"], "retry_wait_browser_recovery")
        self.assertEqual(summary["windows_attempted"], 1)
        self.assertEqual(summary["trusted_windows"], 1)
        self.assertFalse(summary["crash"])
        self.assertEqual(summary["inferred_count"], 1)
        self.assertEqual(summary["session_failure_count"], 1)
        self.assertIn("window_page_stats", summary)
        self.assertIn("ended_window_reasons", summary)
        self.assertIn("fallback_paths", summary)
        self.assertIn("pagination_nav_mode", summary)
        self.assertIn("window_nav_mode", summary)
        self.assertIn("window_fallback_paths", summary)
        self.assertIn("recovery_attempts", summary)
        self.assertTrue(summary["retry_wait_logged"])


if __name__ == "__main__":
    unittest.main()
