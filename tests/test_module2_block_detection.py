import tempfile
import sys
import types
import unittest
from unittest import mock

sys.modules.setdefault("pyodbc", types.SimpleNamespace(connect=lambda *args, **kwargs: None))

import module2_infer_prices
import browser_recovery
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
        self.assertTrue(any("Module2 KPSDK same-session recheck attempt=1 state=listings" in msg for msg in logs))
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

    def test_persistent_kpsdk_returns_existing_recovery_status(self):
        _inferred, status, recover, _wait_cards, logs = self._run_window([
            _state(PageState.BLOCKED_KPSDK),
            _state(PageState.BLOCKED_KPSDK),
            _state(PageState.BLOCKED_KPSDK),
        ])

        self.assertTrue(str(status).startswith("429"))
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


if __name__ == "__main__":
    unittest.main()
