import unittest
from unittest import mock

import module1_list_scraper
from realestate_errors import RealEstateBlockedError
from realestate_page_state import PageState, PageStateResult


class FakeBlockedDriver:
    title = ""
    current_url = "https://www.realestate.com.au/buy/in-noona,+nsw+2835/list-1"
    page_source = "<html><script>window.KPSDK={};</script><script src='/ips.js'></script></html>"

    def execute_script(self, *_args):
        return ""

    def get_log(self, _kind):
        return []

    def quit(self):
        pass


class FakeWait:
    def __init__(self, *_args, **_kwargs):
        pass

    def until(self, _condition):
        return True


def _state(state, cards=0, html_length=851, body_text_length=0):
    return PageStateResult(
        state=state,
        reason=state,
        is_usable=state in {PageState.LISTINGS, PageState.NO_RESULTS},
        is_blocked=state in {PageState.BLOCKED_HTTP_429, PageState.BLOCKED_KPSDK, PageState.BLOCKED_ACCESS_DENIED},
        is_no_results=state == PageState.NO_RESULTS,
        has_cards=cards > 0,
        cards_count=cards,
        current_url=FakeBlockedDriver.current_url,
        body_text_length=body_text_length,
        html_length=html_length,
    )


class Module1BlockDetectionTests(unittest.TestCase):
    def test_scrape_search_raises_for_kpsdk_shell_instead_of_returning_empty_rows(self):
        with mock.patch.object(module1_list_scraper, "setup_driver", return_value=FakeBlockedDriver()), \
             mock.patch.object(module1_list_scraper, "safe_get", return_value=True), \
             mock.patch.object(module1_list_scraper, "wait_for_cards", side_effect=module1_list_scraper.TimeoutException()), \
             mock.patch.object(
                 module1_list_scraper,
                 "recover_browser_after_429",
                 return_value=(FakeBlockedDriver(), 0, "rea_profile", "blocked"),
             ), \
             mock.patch.object(module1_list_scraper.config, "BROWSER_KPSDK_SAME_SESSION_RECHECKS", 0), \
             mock.patch.object(module1_list_scraper, "save_results", return_value=(None, None)), \
             mock.patch("builtins.print"):
            with self.assertRaises(RealEstateBlockedError):
                module1_list_scraper.scrape_search(
                    "https://www.realestate.com.au/buy/in-noona,+nsw+2835/list-1",
                    max_pages=1,
                    timeout=1,
                    on_log=lambda _msg: None,
                )

    def test_scrape_search_page_rechecks_kpsdk_same_session_before_recovery(self):
        blocked = _state(PageState.BLOCKED_KPSDK, cards=0, html_length=851, body_text_length=0)
        listings = _state(PageState.LISTINGS, cards=1, html_length=50000, body_text_length=1200)
        fake_card = object()
        logs = []

        with mock.patch.object(module1_list_scraper, "setup_driver", return_value=FakeBlockedDriver()), \
             mock.patch.object(module1_list_scraper, "safe_get", return_value=True) as safe_get, \
             mock.patch.object(
                 module1_list_scraper,
                 "wait_for_search_page_state",
                 side_effect=[(blocked, []), (listings, [fake_card])],
             ) as wait_state, \
             mock.patch.object(module1_list_scraper, "recover_browser_after_429") as recover, \
             mock.patch.object(module1_list_scraper, "WebDriverWait", FakeWait), \
             mock.patch.object(module1_list_scraper, "_stop_page_loading", return_value=None), \
             mock.patch.object(module1_list_scraper, "extract_card", return_value={"listing_id": "123", "price": "$1"}), \
             mock.patch.object(module1_list_scraper, "get_total_pages", return_value=1), \
             mock.patch.object(module1_list_scraper, "detect_next", return_value=False), \
             mock.patch.object(module1_list_scraper.config, "BROWSER_KPSDK_SAME_SESSION_RECHECKS", 2), \
             mock.patch.object(module1_list_scraper.config, "BROWSER_KPSDK_SETTLE_SECONDS", 0), \
             mock.patch("builtins.print"):
            rows, meta = module1_list_scraper.scrape_search_page(
                "https://www.realestate.com.au/buy/in-noona,+nsw+2835/list-1",
                page=1,
                timeout=1,
                on_log=logs.append,
            )

        self.assertEqual(len(rows), 1)
        self.assertEqual(meta["page_state"], PageState.LISTINGS)
        self.assertEqual(safe_get.call_count, 2)
        self.assertEqual(wait_state.call_count, 2)
        recover.assert_not_called()
        self.assertTrue(
            any("Module1 KPSDK same-session recheck attempt=1 state=listings cards_found=1" in item for item in logs)
        )


if __name__ == "__main__":
    unittest.main()
