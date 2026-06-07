from __future__ import annotations

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# Keep this unit test independent from browser package availability.
sys.modules.setdefault("chrome_options_helper", types.SimpleNamespace(build_chrome_driver=lambda *a, **k: None, cleanup_chrome_driver=lambda *a, **k: None))

import module2_infer_prices as m2


def test_setup_full_sweep_starts_at_configured_min_low_and_uses_step_bands():
    windows = m2.generate_full_sweep_windows(window_width=200_000, max_high=6_300_000)
    assert windows[0][0] == 0
    assert windows[0][:2] == (0, 200_000)
    assert next(w for w in windows if w[0] == 1_750_000)[2] == 50_000
    assert next(w for w in windows if w[0] == 1_800_000)[2] == 100_000
    assert next(w for w in windows if w[0] == 6_100_000)[2] == 200_000


def test_smart_refresh_and_expanded_bounds():
    assert m2.smart_sweep_bounds(900_000, 1_500_000, 4, 5_000_000) == (700_000, 1_700_000)
    assert m2.smart_sweep_bounds(900_000, 1_500_000, 10, 5_000_000) == (400_000, 2_000_000)
    assert m2.smart_sweep_bounds(120_000, 1_500_000, 10, 5_000_000) == (0, 2_000_000)


def test_sweep_stops_when_all_targets_found_with_fake_driver():
    class Driver:
        current_url = "https://example.test/list-1"

        def get(self, url):
            self.current_url = url

    calls = {"pages": 0}
    original = {
        "get_with_retries": m2.get_with_retries,
        "wait_for_cards_or_no_results": m2.wait_for_cards_or_no_results,
        "extract_listing_ids_from_cards": m2.extract_listing_ids_from_cards,
        "get_max_pages_from_pagination": m2.get_max_pages_from_pagination,
        "is_429_page": m2.is_429_page,
        "has_next_results_page": m2.has_next_results_page,
        "sleep": m2.time.sleep,
    }
    try:
        m2.get_with_retries = lambda driver, url, tries=2: (driver, True, None)
        m2.wait_for_cards_or_no_results = lambda driver, timeout, min_cards=1: ("cards", [object()])
        def extract(cards):
            calls["pages"] += 1
            return {"target-1"}
        m2.extract_listing_ids_from_cards = extract
        m2.get_max_pages_from_pagination = lambda driver: 1
        m2.is_429_page = lambda driver: False
        m2.has_next_results_page = lambda driver, page: False
        m2.time.sleep = lambda *_args, **_kwargs: None
        inferred, _driver, status = m2.infer_prices_window_based_with_checkpoint(
            driver=Driver(),
            base_list_url="https://www.realestate.com.au/buy/in-test/list-1",
            missing_ids={"target-1"},
            window_width=200_000,
            step=50_000,
            start_low=0,
            max_high=1_000_000,
            max_pages_per_window=1,
            wait_timeout=1,
            ck_path="/tmp/module2_sweep_test_checkpoint.json",
            ck={},
            sweep_windows=m2.generate_full_sweep_windows(200_000, 1_000_000),
            max_windows_per_run=0,
        )
        assert status == "done"
        assert set(inferred) == {"target-1"}
        assert calls["pages"] == 1
    finally:
        m2.get_with_retries = original["get_with_retries"]
        m2.wait_for_cards_or_no_results = original["wait_for_cards_or_no_results"]
        m2.extract_listing_ids_from_cards = original["extract_listing_ids_from_cards"]
        m2.get_max_pages_from_pagination = original["get_max_pages_from_pagination"]
        m2.is_429_page = original["is_429_page"]
        m2.has_next_results_page = original["has_next_results_page"]
        m2.time.sleep = original["sleep"]


def test_no_range_status_not_emitted_for_smart_plan():
    plan = m2.build_sweep_plan("smart_refresh", 200_000, 5_000_000, low_anchor=900_000, high_anchor=1_500_000)
    assert plan["sweep_mode"] == "smart_refresh"
    assert "skipped_no_range_after_full_sweep" not in plan


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("module2 sweep strategy tests passed")
