# CloakBrowser Migration Report

## Scope

AScrapper / OzHome Monitor browser automation has been migrated from the previous Chrome automation stack to CloakBrowser while preserving public scraper function signatures and output schemas.

Business logic, Telegram UX, scheduling, SQL Server persistence, event detection, price inference math, and Excel export behavior were not intentionally changed.

## Page-State Classification Fix

After the CloakBrowser migration, Ubuntu testing showed a usable realestate.com.au list page with 25 listing cards while the same run also had a homepage HTTP 429 and `x-kpsdk` headers in network logs. The old recovery logic treated the network signal as a block too early.

This is now fixed by centralizing page-state classification in `realestate_page_state.py`.

DOM/content is the source of truth:

- listing cards present => `listings`, usable, not blocked
- stable empty-search copy => `no_results`, valid empty page, not blocked
- detail markers present => `detail_ready`, usable, not blocked
- removed/not-found/sold detail evidence => lifecycle state, not blocked
- KPSDK/429/access-denied shell with no usable DOM => blocked
- no cards/no no-results/no block => `render_timeout` or unknown render failure

Network-only 429/KPSDK no longer triggers recovery when cards or normal detail content are available.

## Browser Configuration

Default production browser settings:

```dotenv
BROWSER_ENGINE=cloak
CLOAK_PROFILE_DIR=rea_profile
CLOAK_FINGERPRINT_SEED=42069
CLOAK_FINGERPRINT_PLATFORM=windows
CLOAK_FINGERPRINT_STORAGE_QUOTA=5000
CLOAK_VIEWPORT_WIDTH=1365
CLOAK_VIEWPORT_HEIGHT=768
CLOAK_LOCALE=en-AU
CLOAK_TIMEZONE=Australia/Sydney
CLOAK_DISABLE_HTTP2=1
CLOAK_USE_PERSISTENT_CONTEXT=1
CLOAK_HEADLESS=0
MODULE2_PROFILE_BASE_DIR=
LOW_BANDWIDTH_MODE=0
BLOCK_HEAVY_RESOURCES=0
BLOCK_TRACKERS=0
BLOCK_IMAGES=0
BLOCK_MEDIA=0
BLOCK_FONTS=0
BLOCK_MAPS=0
BLOCK_ADS=0
BLOCK_ANALYTICS=0
BROWSER_KPSDK_SAME_SESSION_RECHECKS=2
BROWSER_KPSDK_SETTLE_SECONDS=10
BROWSER_PAGE_STATE_DEBUG=1
BROWSER_USE_RUNTIME_PROFILE_STATE=1
```

Production should still run headed inside Xvfb:

```bash
HEADLESS=0 xvfb-run -a -s "-screen 0 1365x768x24" .venv/bin/python telegram_bot.py
```

No homepage warm-up is used by default.

Module1, Module2, and Module3 share a KPSDK same-session recheck helper. A first `blocked_kpsdk` shell does not rotate the profile immediately; the scraper waits, reloads the same URL with the same driver/profile, reclassifies, and only falls back to existing checkpoint/recovery behavior if the page remains blocked.

Scan trust is explicit across monitoring: `listings` and stable DOM-confirmed `no_results` are trusted, while `blocked_*`, `blank_render`, `render_timeout`, and `unknown` are untrusted. Untrusted scans do not complete baselines, do not represent zero listings, and must not drive missing/removed lifecycle transitions.

## Files Changed

- `cloak_browser_helper.py`: new CloakBrowser adapter with Selenium-like project API.
- `chrome_options_helper.py`: compatibility shim now backed by CloakBrowser.
- `module1_list_scraper.py`: imports migrated to adapter classes.
- `module2_infer_prices.py`: imports migrated to adapter classes.
- `module3_enrich_details.py`: imports migrated to adapter classes.
- `config.py`: CloakBrowser config keys and runtime diagnostics.
- `requirements.txt`: added `cloakbrowser` and `playwright`; removed old browser packages.
- `deploy.sh`: installs requirements and runs `python -m cloakbrowser install`.
- `.env.example`: CloakBrowser runtime template.
- `README_DEPLOY_UBUNTU.md`: updated deployment and smoke-test steps.
- `tools/test_cloak_single_page.py`: live single-page diagnostics.
- `tools/test_module1_cloak_one_page.py`: Module1 one-page smoke test.
- `tools/test_module2_cloak_small.py`: constrained Module2 smoke test.
- `tools/test_module3_cloak_single_listing.py`: single-listing Module3 smoke test.
- `tools/test_pipeline_cloak_limited.py`: limited Module1 -> Module2 -> Module3 smoke pipeline.
- `tests/test_cloak_migration_static.py`: static regression test for old browser imports and Module1 schema keys.
- `realestate_page_state.py`: centralized page-state classifier.
- `tests/test_realestate_page_state.py`: fake-driver tests for network 429 with cards, KPSDK block, no-results, render timeout, and detail-ready pages.
- `tests/test_module2_block_detection.py`: Module2 KPSDK same-session recheck, no-results, persistent block, and render-timeout regressions.
- `tests/test_module3_block_detection.py`: Module3 KPSDK detail-ready/lifecycle/recovery and render-timeout regressions.
- `tests/test_area_light_checker_trust.py`: light-check trusted versus blocked/technical scan metadata.

## Local Verification

Passed locally:

```bash
python -m py_compile $(rg --files -g '*.py' -g '!.git/**' -g '!.venv/**')
python -m unittest tests.test_realestate_page_state tests.test_chrome_profile_paths tests.test_module1_block_detection
python -m unittest tests.test_cloak_migration_static tests.test_deployment_config tests.test_monitoring_refactor.MonitoringRefactorTests.test_worker_keeps_realestate_rate_limit_as_retry_wait_without_consuming_attempt
python tools/test_browser_recovery.py
rg -n "selenium|undetected_chromedriver" . -g '*.py' -g 'requirements.txt' -g 'README_DEPLOY_UBUNTU.md' -g 'deploy.sh' -g '!.venv/**' -g '!.git/**' -g '!output/**' -g '!rea_profile*/**'
```

The final grep returns no matches.

Live browser tests were not run from the local Codex sandbox because `cloakbrowser` is not installed there.

## Ubuntu Sandbox Commands

Run only in a separate test directory. Do not modify `/opt/A-scrapper` directly.

```bash
sudo mkdir -p /opt/A-scrapper-cloak-migration-test
sudo rsync -a --delete \
  --exclude '.git' \
  --exclude '.venv' \
  --exclude 'rea_profile*' \
  --exclude 'output' \
  /opt/A-scrapper/ /opt/A-scrapper-cloak-migration-test/
sudo chown -R ascrapper:ascrapper /opt/A-scrapper-cloak-migration-test
cd /opt/A-scrapper-cloak-migration-test
sudo -u ascrapper python3.10 -m venv .venv
sudo -u ascrapper .venv/bin/python -m pip install --upgrade pip
sudo -u ascrapper .venv/bin/python -m pip install -r requirements.txt
sudo -u ascrapper .venv/bin/python -m cloakbrowser install
sudo -u ascrapper mkdir -p output/cloak_tests runtime/temp
```

Create a test `.env` without production secrets if Telegram/DB are not needed:

```bash
sudo -u ascrapper cp .env.example .env
sudo -u ascrapper chmod 600 .env
```

Smoke tests:

```bash
sudo -u ascrapper bash -lc 'cd /opt/A-scrapper-cloak-migration-test && HEADLESS=0 xvfb-run -a -s "-screen 0 1365x768x24" .venv/bin/python -m tools.test_cloak_single_page --fresh-profile --disable-http2 --wait 15'
sudo -u ascrapper bash -lc 'cd /opt/A-scrapper-cloak-migration-test && HEADLESS=0 xvfb-run -a -s "-screen 0 1365x768x24" .venv/bin/python -m tools.test_module1_cloak_one_page --timeout 25'
sudo -u ascrapper bash -lc 'cd /opt/A-scrapper-cloak-migration-test && HEADLESS=0 xvfb-run -a -s "-screen 0 1365x768x24" .venv/bin/python -m tools.test_module2_cloak_small --max-high 1500000 --max-pages-per-window 1 --max-windows 3'
sudo -u ascrapper bash -lc 'cd /opt/A-scrapper-cloak-migration-test && HEADLESS=0 xvfb-run -a -s "-screen 0 1365x768x24" .venv/bin/python -m tools.test_module3_cloak_single_listing --wait 25'
sudo -u ascrapper bash -lc 'cd /opt/A-scrapper-cloak-migration-test && HEADLESS=0 xvfb-run -a -s "-screen 0 1365x768x24" .venv/bin/python -m tools.test_pipeline_cloak_limited --module2-max-high 1500000 --module2-max-windows 3 --module3-limit 3'
```

Expected single-page result for Petersham:

- `cards_found > 0`
- `blank_render_detected == false`
- fingerprint `platform == Win32`
- fingerprint `webdriver == false`
- generated JSON/HTML/screenshot under `output/cloak_tests`

## Rollback

Rollback is code-level only:

1. Restore the previous revision of `requirements.txt`, browser helper files, and Module1/2/3 imports.
2. Reinstall the old requirements in a fresh venv.
3. Restore the previous systemd service only if its command was changed.

No database schema rollback is required because this migration does not change DB schema.

## Remaining Risks

- CloakBrowser binary download is large and should be preinstalled before systemd restart.
- Some Ubuntu hosts may still receive site-level rate limits; those are classified by existing blocked/429 diagnostics and should not be treated as zero listings.
- The limited pipeline script intentionally does not touch production DB or run Excel export without an explicit test DB workflow.
