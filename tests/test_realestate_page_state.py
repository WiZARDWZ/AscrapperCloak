import json
import unittest
from unittest import mock

import browser_recovery
import config
import realestate_page_state
from realestate_page_state import (
    PageState,
    classify_detail_page,
    classify_search_page,
    wait_for_detail_page_state,
    wait_for_search_page_state,
)


class FakeElement:
    def __init__(self, text="", attrs=None, visible=True):
        self.text = text
        self.attrs = attrs or {}
        self.visible = visible

    def is_displayed(self):
        return self.visible

    def get_attribute(self, name):
        return self.attrs.get(name)

    def find_elements(self, *_args):
        return []


class FakeDriver:
    def __init__(self, html="", body="", title="", url="https://www.realestate.com.au/buy/in-petersham,+nsw+2049/list-1", elements=None, logs=None):
        self.page_source = html
        self._body = body
        self.title = title
        self.current_url = url
        self.elements = elements or {}
        self.logs = logs or []

    def find_elements(self, _by, selector):
        return list(self.elements.get(selector, []))

    def execute_script(self, script):
        if "document.body" in script:
            return self._body
        if "document.querySelectorAll('h1,h2')" in script:
            return ""
        return {}

    def get_log(self, _kind):
        return list(self.logs)


def network_429_log():
    return [{
        "message": json.dumps({
            "message": {
                "method": "Network.responseReceived",
                "params": {
                    "response": {
                        "url": "https://www.realestate.com.au/",
                        "status": 429,
                        "headers": {"x-kpsdk-r": "1-AA"},
                    }
                },
            }
        })
    }]


class RealEstatePageStateTests(unittest.TestCase):
    def setUp(self):
        self.patches = [
            mock.patch.object(config, "BROWSER_BLOCK_POLL_SECONDS", 0.01),
            mock.patch.object(config, "BROWSER_NO_RESULTS_STABLE_SECONDS", 0),
        ]
        for patcher in self.patches:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patches):
            patcher.stop()

    def test_network_429_with_cards_is_listings_not_blocked(self):
        driver = FakeDriver(
            title="Real Estate & Property for Sale in Petersham, NSW 2049 - realestate.com.au",
            body="25 properties with bed bath parking",
            elements={'article[data-testid="ResidentialCard"]': [FakeElement() for _ in range(25)]},
            logs=network_429_log(),
        )
        result = classify_search_page(driver, min_cards=1)
        self.assertEqual(result.state, PageState.LISTINGS)
        self.assertTrue(result.is_usable)
        self.assertFalse(result.is_blocked)
        self.assertFalse(browser_recovery.is_429_page(driver))
        self.assertIsNone(browser_recovery.get_realestate_blocked_reason(driver))

    def test_kpsdk_shell_without_cards_is_blocked(self):
        driver = FakeDriver(html="<html><script>window.KPSDK={};</script><script src='/ips.js'></script></html>")
        result = classify_search_page(driver, timeout=1)
        self.assertEqual(result.state, PageState.BLOCKED_KPSDK)
        self.assertTrue(result.is_blocked)
        self.assertTrue(browser_recovery.is_429_page(driver))

    def test_no_results_is_valid_empty_not_blocked(self):
        driver = FakeDriver(body="We couldn't find anything that matches your search. Try removing some filters.")
        result = classify_search_page(driver, timeout=1)
        self.assertEqual(result.state, PageState.NO_RESULTS)
        self.assertTrue(result.is_no_results)
        self.assertFalse(result.is_blocked)
        self.assertFalse(browser_recovery.is_429_page(driver))

    def test_render_timeout_is_not_no_results_or_blocked(self):
        driver = FakeDriver(html="<html><body><main>Loading property search...</main></body></html>", body="Loading property search...")
        result = classify_search_page(driver, timeout=1)
        self.assertEqual(result.state, PageState.RENDER_TIMEOUT)
        self.assertFalse(result.is_no_results)
        self.assertFalse(result.is_blocked)
        self.assertFalse(browser_recovery.is_429_page(driver))

    def test_detail_ready_with_network_429_is_usable(self):
        driver = FakeDriver(
            body="12 Test Street Petersham NSW 2049 2 bed 1 bath Contact agent",
            elements={"h1": [FakeElement("12 Test Street")], "div.contact-agent-panel": [FakeElement("Agent")]},
            logs=network_429_log(),
        )
        result = classify_detail_page(driver, timeout=1)
        self.assertEqual(result.state, PageState.DETAIL_READY)
        self.assertTrue(result.is_usable)
        self.assertFalse(result.is_blocked)


    def test_chrome_error_url_never_classifies_detail_ready(self):
        driver = FakeDriver(
            url="chrome-error://chromewebdata/",
            html='<html><body><h1>12 Test Street</h1><script id="__NEXT_DATA__">{}</script></body></html>',
            body="12 Test Street 2 bed 1 bath parking guide",
            elements={"h1": [FakeElement("12 Test Street")]},
        )
        result = classify_detail_page(driver, timeout=1)
        self.assertEqual(result.state, PageState.CHROME_ERROR)
        self.assertFalse(result.is_usable)

    def test_search_wait_returns_chrome_error_without_polling(self):
        driver = FakeDriver(
            url="chrome-error://chromewebdata/",
            title="This site can't be reached",
            body="The webpage might be temporarily down.",
            html="<html><body>ERR_HTTP_RESPONSE_CODE_FAILURE</body></html>",
        )

        with mock.patch.object(realestate_page_state.time, "sleep") as sleep:
            result, cards = wait_for_search_page_state(driver, timeout=25)

        self.assertEqual(result.state, PageState.CHROME_ERROR)
        self.assertEqual(cards, [])
        sleep.assert_not_called()

    def test_detail_wait_returns_chrome_error_without_polling(self):
        driver = FakeDriver(
            url="chrome-error://chromewebdata/",
            title="This site can't be reached",
            body="The webpage might be temporarily down.",
            html="<html><body>ERR_HTTP_RESPONSE_CODE_FAILURE</body></html>",
        )

        with mock.patch.object(realestate_page_state.time, "sleep") as sleep:
            result = wait_for_detail_page_state(driver, timeout=25)

        self.assertEqual(result.state, PageState.CHROME_ERROR)
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
