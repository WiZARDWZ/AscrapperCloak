import unittest
from unittest import mock

import module1_list_scraper
from realestate_errors import RealEstateBlockedError


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
             mock.patch.object(module1_list_scraper, "save_results", return_value=(None, None)), \
             mock.patch("builtins.print"):
            with self.assertRaises(RealEstateBlockedError):
                module1_list_scraper.scrape_search(
                    "https://www.realestate.com.au/buy/in-noona,+nsw+2835/list-1",
                    max_pages=1,
                    timeout=1,
                    on_log=lambda _msg: None,
                )


if __name__ == "__main__":
    unittest.main()
