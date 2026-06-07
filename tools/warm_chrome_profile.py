import time

import config
from chrome_options_helper import build_chrome_driver, cleanup_chrome_driver

WARM_URLS = [
    "https://www.realestate.com.au/buy/in-petersham,+nsw+2049/list-1?activeSort=list-date",
]


def main() -> None:
    print(f"browser engine: {getattr(config, 'BROWSER_ENGINE', 'cloak')}")
    print(f"cloak profile path: {getattr(config, 'CLOAK_PROFILE_DIR', config.CHROME_PROFILE_DIR)}")
    print(f"cloak viewport: {getattr(config, 'CLOAK_VIEWPORT_WIDTH', 1365)}x{getattr(config, 'CLOAK_VIEWPORT_HEIGHT', 768)}")
    driver = build_chrome_driver()
    try:
        print(f"Warming profile. persistent={getattr(config, 'CLOAK_USE_PERSISTENT_CONTEXT', True)}")
        for url in WARM_URLS:
            print(f"Opening: {url}")
            driver.get(url)
            time.sleep(3)
        print("Waiting 20 seconds to allow cache/service worker warm-up...")
        time.sleep(20)
    finally:
        try:
            driver.quit()
        except Exception:
            pass
        cleanup_chrome_driver(driver)


if __name__ == "__main__":
    main()
