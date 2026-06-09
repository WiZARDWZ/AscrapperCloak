import inspect
import unittest
from unittest import mock

import module1_list_scraper
from realestate_page_state import PageState, PageStateResult


class FakeDriver:
    def __init__(self, url="https://www.realestate.com.au/buy/in-noona,+nsw+2835/list-1?activeSort=list-date"):
        self.current_url = url
        self.page_source = "<html><body><article data-testid='ResidentialCard'></article></body></html>"
        self.scripts = []
        self._module1_last_navigation = {}

    def execute_script(self, script):
        self.scripts.append(script)
        return {"clicked": True, "href": "https://www.realestate.com.au/buy/in-noona,+nsw+2835/list-2?activeSort=list-date", "aria": "Go to next page", "text": "Next"}

    def execute_cdp_cmd(self, *_args):
        return None

    def quit(self):
        pass


class FakeWait:
    def __init__(self, *_args, **_kwargs):
        pass

    def until(self, _condition):
        return True


def state_for(page: int, cards: int = 1):
    return PageStateResult(
        state=PageState.LISTINGS,
        reason=PageState.LISTINGS,
        is_usable=True,
        is_blocked=False,
        is_no_results=False,
        has_cards=cards > 0,
        cards_count=cards,
        current_url=f"https://www.realestate.com.au/buy/in-noona,+nsw+2835/list-{page}?activeSort=list-date",
        body_text_length=1200,
        html_length=50000,
        network_reason=None,
    )


class Module1PaginationNavigationTests(unittest.TestCase):
    def test_click_next_script_prioritizes_real_pagination_and_excludes_nextroll_privacy_links(self):
        driver = FakeDriver()
        logs = []
        result = module1_list_scraper._click_next_anchor(driver, 2, logs.append)
        script = driver.scripts[-1]

        self.assertTrue(result["clicked"])
        self.assertIn('a[rel="next"]', script)
        self.assertIn('a[aria-label*="Go to next page" i]', script)
        self.assertIn('a[href*="/list-${nextPage}"]', script)
        self.assertIn("nextroll", script.lower())
        self.assertIn("privacy", script.lower())
        self.assertIn("advertising", script.lower())

    def test_click_next_script_requires_list_next_page_or_rel_next(self):
        script = inspect.getsource(module1_list_scraper._click_next_anchor)
        self.assertIn("hrefTargetsNextPage || relNext", script)
        self.assertIn("paginationLike", script)

    def test_click_next_execute_script_does_not_use_selenium_arguments(self):
        script = inspect.getsource(module1_list_scraper._click_next_anchor)
        self.assertNotIn("arguments[0]", script)

    def test_click_next_reaches_page2_without_safe_get_direct_url(self):
        driver = FakeDriver()
        logs = []
        safe_get_calls = []

        def fake_safe_get(drv, url, *, phase, apply_delay=False, log_func=print):
            safe_get_calls.append((url, phase))
            drv.current_url = url
            drv._module1_last_navigation = {"url": url, "navigation_failed": False, "navigation_error": None}
            return True

        def fake_click(drv, next_page, log):
            drv.current_url = f"https://www.realestate.com.au/buy/in-noona,+nsw+2835/list-{next_page}?activeSort=list-date"
            return {"clicked": True, "href": drv.current_url}

        with mock.patch.object(module1_list_scraper.config, "MODULE1_PAGINATION_NAV_MODE", "click_next"), \
             mock.patch.object(module1_list_scraper, "setup_driver", return_value=driver), \
             mock.patch.object(module1_list_scraper, "safe_get", side_effect=fake_safe_get), \
             mock.patch.object(module1_list_scraper, "wait_for_search_page_state", side_effect=[(state_for(1), ["card1"]), (state_for(2), ["card2"])]), \
             mock.patch.object(module1_list_scraper, "wait_for_cards", side_effect=[["card1"], ["card2"]]), \
             mock.patch.object(module1_list_scraper, "WebDriverWait", FakeWait), \
             mock.patch.object(module1_list_scraper, "_stop_page_loading", return_value=None), \
             mock.patch.object(module1_list_scraper, "extract_card", side_effect=[{"listing_id": "1", "page": 1}, {"listing_id": "2", "page": 2}]), \
             mock.patch.object(module1_list_scraper, "get_total_pages", return_value=2), \
             mock.patch.object(module1_list_scraper, "detect_next", side_effect=[True, False]), \
             mock.patch.object(module1_list_scraper, "_click_next_anchor", side_effect=fake_click), \
             mock.patch.object(module1_list_scraper.time, "sleep", return_value=None), \
             mock.patch("builtins.print"):
            rows = module1_list_scraper.scrape_search(
                "https://www.realestate.com.au/buy/in-noona,+nsw+2835/list-1?activeSort=list-date",
                max_pages=2,
                timeout=1,
                on_log=logs.append,
            )

        self.assertEqual([row["page"] for row in rows], [1, 2])
        self.assertFalse(any(phase == "list_page_2" for _url, phase in safe_get_calls))
        self.assertTrue(any("click-next landed" in item for item in logs))

    def test_fresh_context_fallback_preserves_page1_rows(self):
        first_driver = FakeDriver()
        second_driver = FakeDriver(url="https://www.realestate.com.au/buy/in-noona,+nsw+2835/list-2?activeSort=list-date")

        def fake_safe_get(drv, url, *, phase, apply_delay=False, log_func=print):
            drv.current_url = url
            drv._module1_last_navigation = {"url": url, "navigation_failed": False, "navigation_error": None}
            return True

        with mock.patch.object(module1_list_scraper.config, "MODULE1_PAGINATION_NAV_MODE", "click_next"), \
             mock.patch.object(module1_list_scraper, "setup_driver", side_effect=[first_driver, second_driver]), \
             mock.patch.object(module1_list_scraper, "safe_get", side_effect=fake_safe_get), \
             mock.patch.object(module1_list_scraper, "wait_for_search_page_state", side_effect=[(state_for(1), ["card1"]), (state_for(2), ["card2"])]), \
             mock.patch.object(module1_list_scraper, "wait_for_cards", side_effect=[["card1"], ["card2"]]), \
             mock.patch.object(module1_list_scraper, "WebDriverWait", FakeWait), \
             mock.patch.object(module1_list_scraper, "_stop_page_loading", return_value=None), \
             mock.patch.object(module1_list_scraper, "extract_card", side_effect=[{"listing_id": "1", "page": 1}, {"listing_id": "2", "page": 2}]), \
             mock.patch.object(module1_list_scraper, "get_total_pages", return_value=2), \
             mock.patch.object(module1_list_scraper, "detect_next", side_effect=[True, False]), \
             mock.patch.object(module1_list_scraper, "_click_next_anchor", return_value={"clicked": False, "reason": "next_anchor_not_found"}), \
             mock.patch.object(module1_list_scraper, "_make_fresh_module1_profile_dir", return_value="output/test_fresh_profile"), \
             mock.patch.object(module1_list_scraper.time, "sleep", return_value=None), \
             mock.patch("builtins.print"):
            rows = module1_list_scraper.scrape_search(
                "https://www.realestate.com.au/buy/in-noona,+nsw+2835/list-1?activeSort=list-date",
                max_pages=2,
                timeout=1,
            )

        self.assertEqual([row["listing_id"] for row in rows], ["1", "2"])
        self.assertIn("click_next_to_fresh_context_per_page:page_2", module1_list_scraper.scrape_search.last_result["fallback_paths"])

    def test_chrome_error_after_click_is_not_polled_before_fresh_context_fallback(self):
        first_driver = FakeDriver()
        second_driver = FakeDriver(url="https://www.realestate.com.au/buy/in-noona,+nsw+2835/list-2?activeSort=list-date")
        classify_calls = []

        def fake_safe_get(drv, url, *, phase, apply_delay=False, log_func=print):
            drv.current_url = url
            drv._module1_last_navigation = {"url": url, "navigation_failed": False, "navigation_error": None}
            return True

        def fake_click(drv, next_page, log):
            drv.current_url = "chrome-error://chromewebdata/"
            return {"clicked": True, "href": f"https://www.realestate.com.au/buy/in-noona,+nsw+2835/list-{next_page}"}

        def fake_classify(*args, **kwargs):
            classify_calls.append(args[0].current_url)
            return state_for(99)

        with mock.patch.object(module1_list_scraper.config, "MODULE1_PAGINATION_NAV_MODE", "click_next"), \
             mock.patch.object(module1_list_scraper, "setup_driver", side_effect=[first_driver, second_driver]), \
             mock.patch.object(module1_list_scraper, "safe_get", side_effect=fake_safe_get), \
             mock.patch.object(module1_list_scraper, "wait_for_search_page_state", side_effect=[(state_for(1), ["card1"]), (state_for(2), ["card2"])]), \
             mock.patch.object(module1_list_scraper, "classify_search_page", side_effect=fake_classify), \
             mock.patch.object(module1_list_scraper, "wait_for_cards", side_effect=[["card1"], ["card2"]]), \
             mock.patch.object(module1_list_scraper, "WebDriverWait", FakeWait), \
             mock.patch.object(module1_list_scraper, "_stop_page_loading", return_value=None), \
             mock.patch.object(module1_list_scraper, "extract_card", side_effect=[{"listing_id": "1", "page": 1}, {"listing_id": "2", "page": 2}]), \
             mock.patch.object(module1_list_scraper, "get_total_pages", return_value=2), \
             mock.patch.object(module1_list_scraper, "detect_next", side_effect=[True, False]), \
             mock.patch.object(module1_list_scraper, "_click_next_anchor", side_effect=fake_click), \
             mock.patch.object(module1_list_scraper, "_make_fresh_module1_profile_dir", return_value="output/test_fresh_profile"), \
             mock.patch.object(module1_list_scraper.time, "sleep", return_value=None), \
             mock.patch("builtins.print"):
            rows = module1_list_scraper.scrape_search(
                "https://www.realestate.com.au/buy/in-noona,+nsw+2835/list-1?activeSort=list-date",
                max_pages=2,
                timeout=1,
            )

        self.assertEqual(len(rows), 2)
        self.assertEqual(classify_calls, [])
        self.assertIn("click_next_chrome_error_to_fresh_context_per_page:page_2", module1_list_scraper.scrape_search.last_result["fallback_paths"])


if __name__ == "__main__":
    unittest.main()
