import unittest
from unittest import mock

import browser_recovery
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


class FakeChromeErrorDriver(FakeBlockedDriver):
    current_url = "chrome-error://chromewebdata/"
    page_source = "<html><body></body></html>"


class FakeRecoveredDriver(FakeBlockedDriver):
    current_url = "https://www.realestate.com.au/buy/in-noona,+nsw+2835/list-1"
    page_source = "<html><body><article data-testid='ResidentialCard'></article></body></html>"


class FakeWait:
    def __init__(self, *_args, **_kwargs):
        pass

    def until(self, _condition):
        return True


def _state(state, cards=0, html_length=851, body_text_length=0, network_reason=None, reason=None):
    return PageStateResult(
        state=state,
        reason=reason or state,
        is_usable=state in {PageState.LISTINGS, PageState.NO_RESULTS},
        is_blocked=state in {PageState.BLOCKED_HTTP_429, PageState.BLOCKED_KPSDK, PageState.BLOCKED_ACCESS_DENIED},
        is_no_results=state == PageState.NO_RESULTS,
        has_cards=cards > 0,
        cards_count=cards,
        current_url=FakeBlockedDriver.current_url,
        body_text_length=body_text_length,
        html_length=html_length,
        network_reason=network_reason,
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
             mock.patch.object(module1_list_scraper.config, "BROWSER_KPSDK_SETTLE_SECONDS", 0), \
             mock.patch.object(module1_list_scraper.config, "BROWSER_BLOCK_GRACE_SECONDS", 0.1), \
             mock.patch.object(module1_list_scraper.config, "BROWSER_BLOCK_POLL_SECONDS", 0.05), \
             mock.patch.object(module1_list_scraper.time, "sleep", return_value=None), \
             mock.patch.object(module1_list_scraper, "save_results", return_value=(None, None)), \
             mock.patch.object(browser_recovery.time, "sleep", return_value=None), \
             mock.patch("builtins.print"):
            with self.assertRaises(RealEstateBlockedError):
                module1_list_scraper.scrape_search(
                    "https://www.realestate.com.au/buy/in-noona,+nsw+2835/list-1",
                    max_pages=1,
                    timeout=1,
                    on_log=lambda _msg: None,
                )

    def test_scrape_search_page_settles_kpsdk_same_page_before_regoto(self):
        blocked = _state(PageState.BLOCKED_KPSDK, cards=0, html_length=851, body_text_length=0)
        listings = _state(PageState.LISTINGS, cards=1, html_length=50000, body_text_length=1200)
        fake_card = object()
        logs = []

        with mock.patch.object(module1_list_scraper, "setup_driver", return_value=FakeBlockedDriver()), \
             mock.patch.object(module1_list_scraper, "safe_get", return_value=True) as safe_get, \
             mock.patch.object(
                 module1_list_scraper,
                 "wait_for_search_page_state",
                 return_value=(blocked, []),
             ) as wait_state, \
             mock.patch.object(module1_list_scraper, "classify_search_page", return_value=listings) as classify, \
             mock.patch.object(module1_list_scraper, "get_listing_cards", return_value=[fake_card]), \
             mock.patch.object(module1_list_scraper, "recover_browser_after_429") as recover, \
             mock.patch.object(module1_list_scraper, "WebDriverWait", FakeWait), \
             mock.patch.object(module1_list_scraper, "_stop_page_loading", return_value=None), \
             mock.patch.object(module1_list_scraper, "extract_card", return_value={"listing_id": "123", "price": "$1"}), \
             mock.patch.object(module1_list_scraper, "get_total_pages", return_value=1), \
             mock.patch.object(module1_list_scraper, "detect_next", return_value=False), \
             mock.patch.object(module1_list_scraper.config, "BROWSER_KPSDK_SAME_SESSION_RECHECKS", 2), \
             mock.patch.object(module1_list_scraper.config, "BROWSER_KPSDK_SETTLE_SECONDS", 0), \
             mock.patch.object(module1_list_scraper.config, "BROWSER_BLOCK_GRACE_SECONDS", 0.1), \
             mock.patch.object(module1_list_scraper.config, "BROWSER_BLOCK_POLL_SECONDS", 0.05), \
             mock.patch.object(module1_list_scraper.time, "sleep", return_value=None), \
             mock.patch.object(browser_recovery.time, "sleep", return_value=None), \
             mock.patch("builtins.print"):
            rows, meta = module1_list_scraper.scrape_search_page(
                "https://www.realestate.com.au/buy/in-noona,+nsw+2835/list-1",
                page=1,
                timeout=1,
                on_log=logs.append,
            )

        self.assertEqual(len(rows), 1)
        self.assertEqual(meta["page_state"], PageState.LISTINGS)
        self.assertEqual(safe_get.call_count, 1)
        wait_state.assert_called_once()
        classify.assert_called_once()
        recover.assert_not_called()

    def test_scrape_search_page_blocked_kpsdk_after_same_page_grace_recovers(self):
        blocked = _state(PageState.BLOCKED_KPSDK, cards=0, html_length=851, body_text_length=0)
        logs = []

        with mock.patch.object(module1_list_scraper, "setup_driver", return_value=FakeBlockedDriver()), \
             mock.patch.object(module1_list_scraper, "safe_get", return_value=True) as safe_get, \
             mock.patch.object(module1_list_scraper, "wait_for_search_page_state", return_value=(blocked, [])), \
             mock.patch.object(module1_list_scraper, "classify_search_page", return_value=blocked), \
             mock.patch.object(module1_list_scraper, "get_listing_cards", return_value=[]), \
             mock.patch.object(module1_list_scraper, "same_session_kpsdk_recheck", return_value=(blocked, [])), \
             mock.patch.object(
                 module1_list_scraper,
                 "recover_browser_after_429",
                 return_value=(FakeBlockedDriver(), 0, "rea_profile", "rotation_limit"),
             ) as recover, \
             mock.patch.object(module1_list_scraper.config, "BROWSER_KPSDK_SETTLE_SECONDS", 0), \
             mock.patch.object(module1_list_scraper.config, "BROWSER_BLOCK_GRACE_SECONDS", 0.1), \
             mock.patch.object(module1_list_scraper.config, "BROWSER_BLOCK_POLL_SECONDS", 0.05), \
             mock.patch.object(module1_list_scraper.time, "sleep", return_value=None), \
             mock.patch.object(browser_recovery.time, "sleep", return_value=None), \
             mock.patch("builtins.print"):
            with self.assertRaises(RealEstateBlockedError):
                module1_list_scraper.scrape_search_page(
                    "https://www.realestate.com.au/buy/in-noona,+nsw+2835/list-1",
                    page=1,
                    timeout=1,
                    on_log=logs.append,
                )

        self.assertEqual(safe_get.call_count, 1)
        recover.assert_not_called()
        self.assertTrue(
            any("Module1 transient KPSDK detected; same-page settle start" in item for item in logs)
        )
        self.assertTrue(
            any("Module1 DOM-first listings after transient KPSDK; ignoring historical 429" in item for item in logs)
        )

    def test_scrape_search_page_listings_win_over_historical_http_429(self):
        listings = _state(
            PageState.LISTINGS,
            cards=1,
            html_length=1846239,
            body_text_length=5758,
            network_reason="blocked_http_429",
            reason="listing_cards_present",
        )
        fake_card = object()

        with mock.patch.object(module1_list_scraper, "setup_driver", return_value=FakeBlockedDriver()), \
             mock.patch.object(module1_list_scraper, "safe_get", return_value=True) as safe_get, \
             mock.patch.object(module1_list_scraper, "wait_for_search_page_state", return_value=(listings, [fake_card])), \
             mock.patch.object(module1_list_scraper, "recover_browser_after_429") as recover, \
             mock.patch.object(module1_list_scraper, "WebDriverWait", FakeWait), \
             mock.patch.object(module1_list_scraper, "_stop_page_loading", return_value=None), \
             mock.patch.object(module1_list_scraper, "extract_card", return_value={"listing_id": "123", "price": "$1"}), \
             mock.patch.object(module1_list_scraper, "get_total_pages", return_value=1), \
             mock.patch.object(module1_list_scraper, "detect_next", return_value=False), \
             mock.patch.object(browser_recovery.time, "sleep", return_value=None), \
             mock.patch("builtins.print"):
            rows, meta = module1_list_scraper.scrape_search_page(
                "https://www.realestate.com.au/buy/in-noona,+nsw+2835/list-1?activeSort=list-date",
                page=1,
                timeout=1,
            )

        self.assertEqual(len(rows), 1)
        self.assertEqual(meta["page_state"], PageState.LISTINGS)
        self.assertEqual(safe_get.call_count, 1)
        recover.assert_not_called()

    def test_scrape_search_page_blocked_kpsdk_after_same_page_grace_recovers(self):
        blocked = _state(PageState.BLOCKED_KPSDK, cards=0, html_length=851, body_text_length=0)
        logs = []

        with mock.patch.object(module1_list_scraper, "setup_driver", return_value=FakeBlockedDriver()), \
             mock.patch.object(module1_list_scraper, "safe_get", return_value=True) as safe_get, \
             mock.patch.object(module1_list_scraper, "wait_for_search_page_state", return_value=(blocked, [])), \
             mock.patch.object(module1_list_scraper, "classify_search_page", return_value=blocked), \
             mock.patch.object(module1_list_scraper, "get_listing_cards", return_value=[]), \
             mock.patch.object(module1_list_scraper, "same_session_kpsdk_recheck", return_value=(blocked, [])), \
             mock.patch.object(
                 module1_list_scraper,
                 "recover_browser_after_429",
                 return_value=(FakeBlockedDriver(), 0, "rea_profile", "rotation_limit"),
             ) as recover, \
             mock.patch.object(module1_list_scraper.config, "BROWSER_KPSDK_SETTLE_SECONDS", 0), \
             mock.patch.object(module1_list_scraper.config, "BROWSER_BLOCK_GRACE_SECONDS", 0.1), \
             mock.patch.object(module1_list_scraper.config, "BROWSER_BLOCK_POLL_SECONDS", 0.05), \
             mock.patch.object(module1_list_scraper.time, "sleep", return_value=None), \
             mock.patch.object(browser_recovery.time, "sleep", return_value=None), \
             mock.patch("builtins.print"):
            with self.assertRaises(RealEstateBlockedError):
                module1_list_scraper.scrape_search_page(
                    "https://www.realestate.com.au/buy/in-noona,+nsw+2835/list-1",
                    page=1,
                    timeout=1,
                    on_log=logs.append,
                )

        self.assertEqual(safe_get.call_count, 1)
        recover.assert_not_called()
        self.assertTrue(
            any("Module1 same-page settle result state=blocked_kpsdk" in item for item in logs)
        )

    def test_scrape_search_page_recovers_chrome_error_after_retryable_goto(self):
        chrome_unknown = PageStateResult(
            state=PageState.UNKNOWN,
            reason="no_cards_no_no_results_no_block",
            is_usable=False,
            is_blocked=False,
            is_no_results=False,
            current_url="chrome-error://chromewebdata/",
            cards_count=0,
        )
        listings = _state(PageState.LISTINGS, cards=1, html_length=50000, body_text_length=1200)
        fake_card = object()
        recovered = FakeRecoveredDriver()
        requested_url = module1_list_scraper.make_list_url("https://www.realestate.com.au/buy/in-noona,+nsw+2835/list-1", 1)

        attempts = {"count": 0}
        def fake_safe_driver_get(driver, url, log_func=print):
            attempts["count"] += 1
            if attempts["count"] == 1:
                driver.current_url = "chrome-error://chromewebdata/"
                return False, RuntimeError("Page.goto: net::ERR_HTTP_RESPONSE_CODE_FAILURE")
            driver.current_url = requested_url
            return True, None

        with mock.patch.object(module1_list_scraper, "setup_driver", return_value=FakeChromeErrorDriver()), \
             mock.patch.object(browser_recovery, "safe_driver_get", side_effect=fake_safe_driver_get) as safe_driver_get, \
             mock.patch.object(module1_list_scraper, "classify_search_page", return_value=chrome_unknown) as classify, \
             mock.patch.object(module1_list_scraper, "wait_for_search_page_state", return_value=(listings, [fake_card])) as wait_state, \
             mock.patch.object(module1_list_scraper, "recover_browser_after_429", return_value=(recovered, 1, "rea_profile_recovered", "recovered")) as recover, \
             mock.patch.object(module1_list_scraper, "WebDriverWait", FakeWait), \
             mock.patch.object(module1_list_scraper, "_stop_page_loading", return_value=None), \
             mock.patch.object(module1_list_scraper, "extract_card", return_value={"listing_id": "123", "price": "$1"}), \
             mock.patch.object(module1_list_scraper, "get_total_pages", return_value=1), \
             mock.patch.object(module1_list_scraper, "detect_next", return_value=False), \
             mock.patch.object(browser_recovery.time, "sleep", return_value=None), \
             mock.patch("builtins.print"):
            rows, meta = module1_list_scraper.scrape_search_page(
                "https://www.realestate.com.au/buy/in-noona,+nsw+2835/list-1",
                page=1,
                timeout=25,
            )

        self.assertEqual(len(rows), 1)
        self.assertEqual(meta["page_state"], PageState.LISTINGS)
        classify.assert_not_called()
        recover.assert_not_called()
        self.assertEqual(safe_driver_get.call_args_list[0].args[1], requested_url)
        self.assertEqual(safe_driver_get.call_args_list[1].args[1], requested_url)
        wait_state.assert_called_once()

    def test_scrape_search_page_does_not_trust_chrome_error_no_results(self):
        chrome_no_results = PageStateResult(
            state=PageState.NO_RESULTS,
            reason="stable_no_results",
            is_usable=True,
            is_blocked=False,
            is_no_results=True,
            current_url="chrome-error://chromewebdata/",
            cards_count=0,
        )
        with mock.patch.object(module1_list_scraper, "setup_driver", return_value=FakeChromeErrorDriver()), \
             mock.patch.object(browser_recovery, "safe_driver_get", return_value=(False, RuntimeError("Page.goto: net::ERR_HTTP_RESPONSE_CODE_FAILURE"))), \
             mock.patch.object(module1_list_scraper, "classify_search_page", return_value=chrome_no_results), \
             mock.patch.object(module1_list_scraper, "recover_browser_after_429", return_value=(FakeChromeErrorDriver(), 0, "rea_profile", "rotation_limit")), \
             mock.patch.object(browser_recovery.time, "sleep", return_value=None), \
             mock.patch("builtins.print"):
            with self.assertRaises(RealEstateBlockedError):
                module1_list_scraper.scrape_search_page(
                    "https://www.realestate.com.au/buy/in-noona,+nsw+2835/list-1",
                    page=1,
                    timeout=25,
                )

    def test_scrape_search_page_recovers_chrome_error_after_retryable_goto(self):
        chrome_unknown = PageStateResult(
            state=PageState.UNKNOWN,
            reason="no_cards_no_no_results_no_block",
            is_usable=False,
            is_blocked=False,
            is_no_results=False,
            current_url="chrome-error://chromewebdata/",
            cards_count=0,
        )
        listings = _state(PageState.LISTINGS, cards=1, html_length=50000, body_text_length=1200)
        fake_card = object()
        recovered = FakeRecoveredDriver()
        requested_url = module1_list_scraper.make_list_url("https://www.realestate.com.au/buy/in-noona,+nsw+2835/list-1", 1)

        attempts = {"count": 0}
        def fake_safe_driver_get(driver, url, log_func=print):
            attempts["count"] += 1
            if attempts["count"] == 1:
                driver.current_url = "chrome-error://chromewebdata/"
                return False, RuntimeError("Page.goto: net::ERR_HTTP_RESPONSE_CODE_FAILURE")
            driver.current_url = requested_url
            return True, None

        with mock.patch.object(module1_list_scraper, "setup_driver", return_value=FakeChromeErrorDriver()), \
             mock.patch.object(browser_recovery, "safe_driver_get", side_effect=fake_safe_driver_get) as safe_driver_get, \
             mock.patch.object(module1_list_scraper, "classify_search_page", return_value=chrome_unknown) as classify, \
             mock.patch.object(module1_list_scraper, "wait_for_search_page_state", return_value=(listings, [fake_card])) as wait_state, \
             mock.patch.object(module1_list_scraper, "recover_browser_after_429", return_value=(recovered, 1, "rea_profile_recovered", "recovered")) as recover, \
             mock.patch.object(module1_list_scraper, "WebDriverWait", FakeWait), \
             mock.patch.object(module1_list_scraper, "_stop_page_loading", return_value=None), \
             mock.patch.object(module1_list_scraper, "extract_card", return_value={"listing_id": "123", "price": "$1"}), \
             mock.patch.object(module1_list_scraper, "get_total_pages", return_value=1), \
             mock.patch.object(module1_list_scraper, "detect_next", return_value=False), \
             mock.patch.object(browser_recovery.time, "sleep", return_value=None), \
             mock.patch("builtins.print"):
            rows, meta = module1_list_scraper.scrape_search_page(
                "https://www.realestate.com.au/buy/in-noona,+nsw+2835/list-1",
                page=1,
                timeout=25,
            )

        self.assertEqual(len(rows), 1)
        self.assertEqual(meta["page_state"], PageState.LISTINGS)
        classify.assert_not_called()
        recover.assert_not_called()
        self.assertEqual(safe_driver_get.call_args_list[0].args[1], requested_url)
        self.assertEqual(safe_driver_get.call_args_list[1].args[1], requested_url)
        wait_state.assert_called_once()

    def test_scrape_search_page_does_not_trust_chrome_error_no_results(self):
        chrome_no_results = PageStateResult(
            state=PageState.NO_RESULTS,
            reason="stable_no_results",
            is_usable=True,
            is_blocked=False,
            is_no_results=True,
            current_url="chrome-error://chromewebdata/",
            cards_count=0,
        )
        with mock.patch.object(module1_list_scraper, "setup_driver", return_value=FakeChromeErrorDriver()), \
             mock.patch.object(browser_recovery, "safe_driver_get", return_value=(False, RuntimeError("Page.goto: net::ERR_HTTP_RESPONSE_CODE_FAILURE"))), \
             mock.patch.object(module1_list_scraper, "classify_search_page", return_value=chrome_no_results), \
             mock.patch.object(module1_list_scraper, "recover_browser_after_429", return_value=(FakeChromeErrorDriver(), 0, "rea_profile", "rotation_limit")), \
             mock.patch.object(browser_recovery.time, "sleep", return_value=None), \
             mock.patch("builtins.print"):
            with self.assertRaises(RealEstateBlockedError):
                module1_list_scraper.scrape_search_page(
                    "https://www.realestate.com.au/buy/in-noona,+nsw+2835/list-1",
                    page=1,
                    timeout=25,
                )

    def test_scrape_search_page_recovers_chrome_error_after_retryable_goto(self):
        chrome_unknown = PageStateResult(
            state=PageState.UNKNOWN,
            reason="no_cards_no_no_results_no_block",
            is_usable=False,
            is_blocked=False,
            is_no_results=False,
            current_url="chrome-error://chromewebdata/",
            cards_count=0,
        )
        listings = _state(PageState.LISTINGS, cards=1, html_length=50000, body_text_length=1200)
        fake_card = object()
        recovered = FakeRecoveredDriver()
        requested_url = module1_list_scraper.make_list_url("https://www.realestate.com.au/buy/in-noona,+nsw+2835/list-1", 1)

        attempts = {"count": 0}
        def fake_safe_driver_get(driver, url, log_func=print):
            attempts["count"] += 1
            if attempts["count"] == 1:
                driver.current_url = "chrome-error://chromewebdata/"
                return False, RuntimeError("Page.goto: net::ERR_HTTP_RESPONSE_CODE_FAILURE")
            driver.current_url = requested_url
            return True, None

        with mock.patch.object(module1_list_scraper, "setup_driver", return_value=FakeChromeErrorDriver()), \
             mock.patch.object(browser_recovery, "safe_driver_get", side_effect=fake_safe_driver_get) as safe_driver_get, \
             mock.patch.object(module1_list_scraper, "classify_search_page", return_value=chrome_unknown) as classify, \
             mock.patch.object(module1_list_scraper, "wait_for_search_page_state", return_value=(listings, [fake_card])) as wait_state, \
             mock.patch.object(module1_list_scraper, "recover_browser_after_429", return_value=(recovered, 1, "rea_profile_recovered", "recovered")) as recover, \
             mock.patch.object(module1_list_scraper, "WebDriverWait", FakeWait), \
             mock.patch.object(module1_list_scraper, "_stop_page_loading", return_value=None), \
             mock.patch.object(module1_list_scraper, "extract_card", return_value={"listing_id": "123", "price": "$1"}), \
             mock.patch.object(module1_list_scraper, "get_total_pages", return_value=1), \
             mock.patch.object(module1_list_scraper, "detect_next", return_value=False), \
             mock.patch.object(browser_recovery.time, "sleep", return_value=None), \
             mock.patch("builtins.print"):
            rows, meta = module1_list_scraper.scrape_search_page(
                "https://www.realestate.com.au/buy/in-noona,+nsw+2835/list-1",
                page=1,
                timeout=25,
            )

        self.assertEqual(len(rows), 1)
        self.assertEqual(meta["page_state"], PageState.LISTINGS)
        classify.assert_not_called()
        recover.assert_not_called()
        self.assertEqual(safe_driver_get.call_args_list[0].args[1], requested_url)
        self.assertEqual(safe_driver_get.call_args_list[1].args[1], requested_url)
        wait_state.assert_called_once()

    def test_scrape_search_page_does_not_trust_chrome_error_no_results(self):
        chrome_no_results = PageStateResult(
            state=PageState.NO_RESULTS,
            reason="stable_no_results",
            is_usable=True,
            is_blocked=False,
            is_no_results=True,
            current_url="chrome-error://chromewebdata/",
            cards_count=0,
        )
        with mock.patch.object(module1_list_scraper, "setup_driver", return_value=FakeChromeErrorDriver()), \
             mock.patch.object(browser_recovery, "safe_driver_get", return_value=(False, RuntimeError("Page.goto: net::ERR_HTTP_RESPONSE_CODE_FAILURE"))), \
             mock.patch.object(module1_list_scraper, "classify_search_page", return_value=chrome_no_results), \
             mock.patch.object(module1_list_scraper, "recover_browser_after_429", return_value=(FakeChromeErrorDriver(), 0, "rea_profile", "rotation_limit")), \
             mock.patch.object(browser_recovery.time, "sleep", return_value=None), \
             mock.patch("builtins.print"):
            with self.assertRaises(RealEstateBlockedError):
                module1_list_scraper.scrape_search_page(
                    "https://www.realestate.com.au/buy/in-noona,+nsw+2835/list-1",
                    page=1,
                    timeout=25,
                )


if __name__ == "__main__":
    unittest.main()
