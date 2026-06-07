import json
import os
import tempfile
import unittest
from unittest import mock

import browser_recovery
import chrome_options_helper
import config


class ChromeProfilePathTests(unittest.TestCase):
    def test_linux_ignores_windows_runtime_profile_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = os.path.join(tmp, "browser_profile_state.json")
            with open(state_path, "w", encoding="utf-8") as f:
                json.dump({"current_profile_dir": r"C:\Users\Amin\repo\rea_profile"}, f)

            with mock.patch.object(config, "BROWSER_PROFILE_STATE_PATH", state_path), \
                 mock.patch.object(config, "BROWSER_USE_RUNTIME_PROFILE_STATE", True), \
                 mock.patch.object(chrome_options_helper.os, "name", "posix"):
                self.assertIsNone(chrome_options_helper._read_runtime_profile_override())

    def test_linux_recovery_sanitizes_windows_profile_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(browser_recovery.config, "BROWSER_PROFILE_BASE_DIR", os.path.join(tmp, "rea_profile")), \
                 mock.patch.object(browser_recovery.os, "name", "posix"):
                out = browser_recovery.rotate_chrome_profile_safely(
                    r"C:\Users\Amin\repo\rea_profile",
                    log_func=lambda *_args: None,
                )
            self.assertEqual(os.path.abspath(out), os.path.abspath(os.path.join(tmp, "rea_profile")))
            self.assertTrue(os.path.isdir(out))

    def test_kpsdk_shell_detection(self):
        html = "<html><script>window.KPSDK={};</script><script src='/ips.js'></script></html>"
        self.assertTrue(browser_recovery.is_429_html("", html))

    def test_network_http_429_detection(self):
        class FakeDriver:
            title = ""
            current_url = "https://www.realestate.com.au/buy/in-noona,+nsw+2835/list-1"
            page_source = ""

            def get_log(self, _kind):
                return [{
                    "message": json.dumps({
                        "message": {
                            "method": "Network.responseReceived",
                            "params": {
                                "response": {
                                    "url": self.current_url,
                                    "status": 429,
                                    "headers": {"x-kpsdk-r": "1-AA"},
                                }
                            },
                        }
                    })
                }]

            def execute_script(self, *_args):
                return ""

        self.assertEqual(browser_recovery.get_realestate_blocked_reason(FakeDriver()), "realestate_rate_limited_or_blocked_http_429")


if __name__ == "__main__":
    unittest.main()
