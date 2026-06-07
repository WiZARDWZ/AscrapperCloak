import sys
import types
import unittest
from unittest import mock

sys.modules.setdefault("pyodbc", types.SimpleNamespace(connect=lambda *args, **kwargs: None))

import area_light_checker


class FakeConn:
    def close(self):
        pass


class AreaLightCheckerTrustTests(unittest.TestCase):
    def _run(self, rows, meta):
        with mock.patch.object(area_light_checker, "connect", return_value=FakeConn()), \
             mock.patch.object(area_light_checker, "get_existing_external_ids_for_search", return_value=set()), \
             mock.patch("module1_list_scraper.scrape_search_page", return_value=(rows, meta)), \
             mock.patch.object(area_light_checker, "ingest_light_check_rows") as ingest:
            result = area_light_checker.light_check_area(
                "unused",
                "https://example.test/search",
                max_pages=1,
                timeout=1,
                full_scan=True,
            )
        return result, ingest

    def test_no_results_is_trusted_empty(self):
        result, ingest = self._run([], {"stop_reason": "no_results", "page_state": "no_results", "has_next_page": False})

        self.assertTrue(result["trusted_scan"])
        self.assertEqual(result["scan_status"], "valid_empty_result")
        ingest.assert_not_called()

    def test_blocked_is_untrusted_blocked(self):
        result, ingest = self._run([], {"stop_reason": "blocked_kpsdk", "page_state": "blocked_kpsdk", "has_next_page": False})

        self.assertFalse(result["trusted_scan"])
        self.assertEqual(result["scan_status"], "blocked_rate_limited")
        self.assertEqual(result["blocked_reason"], "blocked_kpsdk")
        ingest.assert_not_called()

    def test_render_timeout_is_untrusted_technical_failure(self):
        result, ingest = self._run([], {"stop_reason": "render_timeout", "page_state": "render_timeout", "has_next_page": False})

        self.assertFalse(result["trusted_scan"])
        self.assertEqual(result["scan_status"], "technical_failure")
        ingest.assert_not_called()


if __name__ == "__main__":
    unittest.main()
