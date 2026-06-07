from __future__ import annotations

import os
import sys
import time

import config
from chrome_options_helper import build_chrome_driver, cleanup_chrome_driver


def main() -> int:
    print(f"HEADLESS={os.getenv('HEADLESS', '0')}")
    print(f"BROWSER_ENGINE={getattr(config, 'BROWSER_ENGINE', 'cloak')}")
    print(f"CLOAK_PROFILE_DIR={getattr(config, 'CLOAK_PROFILE_DIR', 'rea_profile')}")
    print(f"CLOAK_VIEWPORT={getattr(config, 'CLOAK_VIEWPORT_WIDTH', 1365)}x{getattr(config, 'CLOAK_VIEWPORT_HEIGHT', 768)}")
    driver = None
    try:
        driver = build_chrome_driver()
        driver.get("data:text/html,<html><title>AScrapper Browser Check</title><body>ok</body></html>")
        time.sleep(1)
        title = driver.title
        if title != "AScrapper Browser Check":
            print(f"Unexpected page title: {title!r}", file=sys.stderr)
            return 1
        print("CloakBrowser startup OK.")
        return 0
    finally:
        if driver is not None:
            try:
                driver.quit()
            finally:
                cleanup_chrome_driver(driver)


if __name__ == "__main__":
    raise SystemExit(main())
