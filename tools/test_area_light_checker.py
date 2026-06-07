import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from area_light_checker import (
    compact_listing_for_notification,
    detect_new_listing_rows,
    normalize_external_id,
    page_has_existing_listing,
)


def test_detect_new_listing_rows() -> None:
    existing_ids = {"100", "101"}
    rows = [{"listing_id": "100"}, {"listing_id": "102"}]
    result = detect_new_listing_rows(rows, existing_ids)
    assert len(result) == 1
    assert result[0]["listing_id"] == "102"


def test_page_has_existing_listing_true() -> None:
    existing_ids = {"100", "101"}
    rows = [{"listing_id": "102"}, {"listing_id": "101"}]
    assert page_has_existing_listing(rows, existing_ids) is True


def test_page_has_existing_listing_false() -> None:
    existing_ids = {"100", "101"}
    rows = [{"listing_id": "102"}, {"listing_id": "103"}]
    assert page_has_existing_listing(rows, existing_ids) is False


def test_compact_listing_for_notification() -> None:
    row = {
        "listing_id": "151231352",
        "url": "https://example.com/1",
        "address": "12 Example St",
        "price": "Guide $900,000",
        "property_type": "House",
        "bedrooms": "3",
        "bathrooms": "2",
        "parking": "1",
    }
    compact = compact_listing_for_notification(row)
    assert compact == {
        "listing_id": "151231352",
        "url": "https://example.com/1",
        "address": "12 Example St",
        "price": "Guide $900,000",
        "property_type": "House",
        "bedrooms": "3",
        "bathrooms": "2",
        "parking": "1",
    }


def test_normalize_external_id() -> None:
    assert normalize_external_id(None) is None
    assert normalize_external_id("") is None
    assert normalize_external_id("N/A") is None
    assert normalize_external_id(151231352) == "151231352"
    assert normalize_external_id(" 151231352 ") == "151231352"


def main() -> None:
    test_detect_new_listing_rows()
    test_page_has_existing_listing_true()
    test_page_has_existing_listing_false()
    test_compact_listing_for_notification()
    test_normalize_external_id()
    test_full_scan_bypasses_light_cap_and_checks_detected_total_pages()
    test_full_scan_reports_known_max_pages_truncation()
    print("All tests passed.")



def test_full_scan_bypasses_light_cap_and_checks_detected_total_pages() -> None:
    import area_light_checker
    import types

    pages = []
    ingested = []
    old_connect = area_light_checker.connect
    old_existing = area_light_checker.get_existing_external_ids_for_search
    old_ingest = area_light_checker.ingest_light_check_rows
    old_module = sys.modules.get("module1_list_scraper")
    fake_module = types.ModuleType("module1_list_scraper")
    try:
        class Conn:
            def close(self): pass
        area_light_checker.connect = lambda path: Conn()
        area_light_checker.get_existing_external_ids_for_search = lambda conn, url: {"old"}
        area_light_checker.ingest_light_check_rows = lambda *args, **kwargs: ingested.append((args, kwargs)) or {"run_id": 9}
        def scrape(search_url, page, **kwargs):
            pages.append(page)
            return [{"listing_id": str(page)}], {"page": page, "url": f"url-{page}", "current_url": f"url-{page}", "cards_found": 1, "rows_count": 1, "has_next_page": page < 10, "total_pages_detected": 10}
        fake_module.scrape_search_page = scrape
        sys.modules["module1_list_scraper"] = fake_module
        out = area_light_checker.light_check_area("db", "url", max_pages=50, full_scan=True)
        assert pages == list(range(1, 11))
        assert out["pages_checked"] == 10 and out["total_pages_detected"] == 10
        assert out["stop_reason"] == "reached_total_pages" and out["rows_scraped"] == 10
        assert ingested and ingested[0][1]["full_scan"] is True
    finally:
        area_light_checker.connect = old_connect
        area_light_checker.get_existing_external_ids_for_search = old_existing
        area_light_checker.ingest_light_check_rows = old_ingest
        if old_module is None:
            sys.modules.pop("module1_list_scraper", None)
        else:
            sys.modules["module1_list_scraper"] = old_module


def test_full_scan_reports_known_max_pages_truncation() -> None:
    import area_light_checker
    import types

    old_connect = area_light_checker.connect
    old_existing = area_light_checker.get_existing_external_ids_for_search
    old_ingest = area_light_checker.ingest_light_check_rows
    old_module = sys.modules.get("module1_list_scraper")
    fake_module = types.ModuleType("module1_list_scraper")
    try:
        class Conn:
            def close(self): pass
        area_light_checker.connect = lambda path: Conn()
        area_light_checker.get_existing_external_ids_for_search = lambda conn, url: set()
        area_light_checker.ingest_light_check_rows = lambda *args, **kwargs: {"run_id": 1}
        fake_module.scrape_search_page = lambda search_url, page, **kwargs: ([{"listing_id": str(page)}], {"page": page, "url": "url", "current_url": "url", "cards_found": 1, "rows_count": 1, "has_next_page": True, "total_pages_detected": 10})
        sys.modules["module1_list_scraper"] = fake_module
        out = area_light_checker.light_check_area("db", "url", max_pages=3, full_scan=True)
        assert out["pages_checked"] == 3 and out["total_pages_detected"] == 10
        assert out["stop_reason"] == "max_pages_reached"
    finally:
        area_light_checker.connect = old_connect
        area_light_checker.get_existing_external_ids_for_search = old_existing
        area_light_checker.ingest_light_check_rows = old_ingest
        if old_module is None:
            sys.modules.pop("module1_list_scraper", None)
        else:
            sys.modules["module1_list_scraper"] = old_module


if __name__ == "__main__":
    main()
