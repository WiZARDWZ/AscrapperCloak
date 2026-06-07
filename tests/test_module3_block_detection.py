import sys
import types
import unittest
from unittest import mock

sys.modules.setdefault("bs4", types.SimpleNamespace(BeautifulSoup=lambda *args, **kwargs: None))

import module3_enrich_details
from realestate_page_state import PageState, PageStateResult


class FakeDriver:
    current_url = "https://www.realestate.com.au/property-test-1"
    page_source = "<html></html>"

    def get(self, url):
        self.current_url = url

    def refresh(self):
        pass

    def quit(self):
        pass


def _state(state, html_length=874, body_text_length=0):
    return PageStateResult(
        state=state,
        reason=state,
        is_usable=state in {PageState.DETAIL_READY, PageState.DETAIL_REMOVED, PageState.DETAIL_SOLD, PageState.DETAIL_NOT_FOUND},
        is_blocked=state in {PageState.BLOCKED_HTTP_429, PageState.BLOCKED_KPSDK, PageState.BLOCKED_ACCESS_DENIED},
        current_url=FakeDriver.current_url,
        html_length=html_length,
        body_text_length=body_text_length,
    )


class Module3BlockDetectionTests(unittest.TestCase):
    def _run_enrich(self, states, extract_data=None):
        driver = FakeDriver()
        logs = []
        with mock.patch.object(module3_enrich_details, "build_driver", return_value=driver), \
             mock.patch.object(module3_enrich_details, "get_with_retries", return_value=(driver, True, None)), \
             mock.patch.object(module3_enrich_details, "wait_for_detail_page_state", side_effect=states), \
             mock.patch.object(module3_enrich_details, "wait_for_detail_ready", return_value=True), \
             mock.patch.object(module3_enrich_details, "extract_detail_data", return_value=extract_data or {"description": "ready"}), \
             mock.patch.object(module3_enrich_details, "write_outputs", return_value=(None, None)), \
             mock.patch.object(module3_enrich_details, "recover_browser_after_429") as recover, \
             mock.patch.object(module3_enrich_details.config, "BROWSER_KPSDK_SETTLE_SECONDS", 0), \
             mock.patch.object(module3_enrich_details.config, "BROWSER_KPSDK_SAME_SESSION_RECHECKS", 2):
            recover.return_value = (driver, 0, "rea_profile", "blocked")
            rows = module3_enrich_details.enrich_detail_rows(
                [{"listing_id": "1", "url": "https://www.realestate.com.au/property-test-1"}],
                on_log=logs.append,
                sleep_between=0,
                wait_timeout=1,
            )
        return rows, recover, logs

    def test_kpsdk_then_detail_ready_extracts_without_recovery(self):
        rows, recover, logs = self._run_enrich([
            _state(PageState.BLOCKED_KPSDK),
            _state(PageState.DETAIL_READY, html_length=50000, body_text_length=1200),
        ])

        self.assertEqual(rows[0].get("description"), "ready")
        self.assertTrue(rows[0].get("detail_refresh_success"))
        recover.assert_not_called()
        self.assertTrue(any("Module3 KPSDK same-session recheck attempt=1 state=detail_ready" in msg for msg in logs))

    def test_kpsdk_then_removed_is_lifecycle_not_recovery(self):
        rows, recover, _logs = self._run_enrich([
            _state(PageState.BLOCKED_KPSDK),
            _state(PageState.DETAIL_REMOVED),
        ])

        self.assertEqual(rows[0].get("ListingLifecycleStatus"), "removed")
        self.assertTrue(rows[0].get("detail_refresh_success"))
        recover.assert_not_called()

    def test_persistent_kpsdk_uses_existing_recovery_path(self):
        with mock.patch.object(module3_enrich_details, "is_429_page", return_value=True):
            rows, recover, _logs = self._run_enrich([
                _state(PageState.BLOCKED_KPSDK),
                _state(PageState.BLOCKED_KPSDK),
                _state(PageState.BLOCKED_KPSDK),
            ])

        self.assertEqual(rows[0].get("detail_error"), "blocked_after_retries")
        recover.assert_called()

    def test_render_timeout_is_technical_failure_not_removed(self):
        rows, recover, _logs = self._run_enrich([
            _state(PageState.RENDER_TIMEOUT),
            _state(PageState.RENDER_TIMEOUT),
        ])

        self.assertEqual(rows[0].get("detail_error"), "detail_render_timeout")
        self.assertNotIn("ListingLifecycleStatus", rows[0])
        recover.assert_not_called()


if __name__ == "__main__":
    unittest.main()
