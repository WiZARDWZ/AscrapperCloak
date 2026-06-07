import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import config
import db_layer
import listing_detail_refresher as ldr


ORIGINAL_CONNECT = db_layer.connect
ORIGINAL_GET_ACTIVE = db_layer.get_active_listings_for_detail_refresh
ORIGINAL_GET_SKIP_REASON = db_layer.get_detail_refresh_skip_reason
ORIGINAL_DETECT = db_layer.detect_and_record_changes_for_row
ORIGINAL_INGEST = db_layer.ingest_detail_refresh_rows
ORIGINAL_LATEST_STATE = db_layer.get_latest_listing_state


class FakeConn:
    def __init__(self, cursor=None):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def close(self):
        pass

    def rollback(self):
        pass


def test_limit_clamp():
    assert ldr._clamp_limit(config.DETAIL_REFRESH_HARD_LIMIT + 100) == config.DETAIL_REFRESH_HARD_LIMIT


def test_result_structure_and_dry_run_no_ingest():
    calls = {"ingest": 0}
    original_enrich = ldr.ENRICH_DETAIL_ROWS_FUNC
    try:
        ldr.db_layer.connect = lambda _p=None: FakeConn()
        ldr.db_layer.get_active_listings_for_detail_refresh = lambda *a, **k: [
            {"external_id": "1", "url": "http://x", "address": "a", "current_status": "active"}
        ]
        ldr.ENRICH_DETAIL_ROWS_FUNC = lambda rows, **k: [dict(rows[0], description="desc", price="$1", listing_id="1")]
        ldr.db_layer.detect_and_record_changes_for_row = lambda *a, **k: {
            "external_id": "1",
            "events_detected": [],
            "events_created": 0,
            "should_notify_events": [],
        }

        def _ingest(*a, **k):
            calls["ingest"] += 1
            return {}

        ldr.db_layer.ingest_detail_refresh_rows = _ingest

        result = ldr.refresh_active_listings("http://search", dry_run=True, listing_external_id="1")
        assert calls["ingest"] == 0
        for key in ["search_url", "dry_run", "limit", "candidates_count", "processed_count", "items", "errors"]:
            assert key in result
    finally:
        ldr.ENRICH_DETAIL_ROWS_FUNC = original_enrich
        db_layer.connect = ORIGINAL_CONNECT
        db_layer.get_active_listings_for_detail_refresh = ORIGINAL_GET_ACTIVE
        db_layer.detect_and_record_changes_for_row = ORIGINAL_DETECT
        db_layer.ingest_detail_refresh_rows = ORIGINAL_INGEST



def test_subscription_area_label_is_attached_to_detail_candidates():
    original_enrich = ldr.ENRICH_DETAIL_ROWS_FUNC
    seen = {}
    try:
        ldr.db_layer.connect = lambda _p=None: FakeConn()
        ldr.db_layer.get_active_listings_for_detail_refresh = lambda *a, **k: [
            {"external_id": "1", "url": "http://x", "address": "nearby", "current_status": "active"}
        ]
        def fake_enrich(rows, **kwargs):
            seen["rows"] = rows
            return [dict(rows[0], detail_refresh_success=False, detail_refresh_error="stop", detail_extraction_quality="failed")]
        ldr.ENRICH_DETAIL_ROWS_FUNC = fake_enrich
        out = ldr.refresh_active_listings("http://search", dry_run=True, subscription={"AreaLabel": "Tanglewood, NSW 2488"})
        assert out["processed_count"] == 1
        assert seen["rows"][0]["area_label"] == "Tanglewood, NSW 2488"
    finally:
        ldr.ENRICH_DETAIL_ROWS_FUNC = original_enrich
        db_layer.connect = ORIGINAL_CONNECT
        db_layer.get_active_listings_for_detail_refresh = ORIGINAL_GET_ACTIVE

def test_listing_external_id_passed_to_candidate_helper():
    seen = {}
    try:
        ldr.db_layer.connect = lambda _p=None: FakeConn()

        def fake_get(conn, **kwargs):
            seen.update(kwargs)
            return []

        ldr.db_layer.get_active_listings_for_detail_refresh = fake_get
        ldr.db_layer.get_detail_refresh_skip_reason = lambda *a, **k: None
        out = ldr.refresh_active_listings("http://search", listing_external_id="151319868", dry_run=True)
        assert out["processed_count"] == 0
        assert seen.get("listing_external_id") == "151319868"
    finally:
        db_layer.connect = ORIGINAL_CONNECT
        db_layer.get_active_listings_for_detail_refresh = ORIGINAL_GET_ACTIVE
        db_layer.get_detail_refresh_skip_reason = ORIGINAL_GET_SKIP_REASON


def test_targeted_listing_empty_candidates_reports_clear_error():
    try:
        ldr.db_layer.connect = lambda _p=None: FakeConn()
        ldr.db_layer.get_active_listings_for_detail_refresh = lambda *a, **k: []
        ldr.db_layer.get_detail_refresh_skip_reason = lambda *a, **k: "listing_not_found"
        out = ldr.refresh_active_listings("http://search", listing_external_id="404", dry_run=True)
        assert out["candidates_count"] == 0
        assert out["errors"] == ["listing_not_found"]
    finally:
        db_layer.connect = ORIGINAL_CONNECT
        db_layer.get_active_listings_for_detail_refresh = ORIGINAL_GET_ACTIVE
        db_layer.get_detail_refresh_skip_reason = ORIGINAL_GET_SKIP_REASON


def test_targeted_candidate_bypasses_stale_and_uses_string_safe_external_id():
    original_upsert_search = db_layer._upsert_search

    class CaptureCursor:
        def __init__(self):
            self.sql = None
            self.params = None
            self.description = [
                ("db_listing_id",),
                ("external_id",),
                ("listing_id",),
                ("url",),
                ("address",),
                ("property_type",),
                ("price",),
                ("price_display",),
                ("bedrooms",),
                ("bathrooms",),
                ("parking",),
                ("current_status",),
                ("stale_at",),
            ]

        def execute(self, sql, *params):
            self.sql = sql
            self.params = params

        def fetchall(self):
            return [
                (1002, "151319868", "151319868", "http://x", "addr", "house", None, "$1", 2, 1, 1, "active", "fresh_timestamp")
            ]

    cursor = CaptureCursor()
    try:
        db_layer._upsert_search = lambda conn, url: 123
        rows = db_layer.get_active_listings_for_detail_refresh(
            FakeConn(cursor), "http://search", limit=50, stale_hours=24, listing_external_id=151319868
        )
    finally:
        db_layer._upsert_search = original_upsert_search

    assert rows and rows[0]["external_id"] == "151319868"
    assert rows[0]["db_listing_id"] == 1002
    assert rows[0]["listing_id"] == "151319868"
    assert rows[0]["listing_id"] != 1002
    assert "CAST(l.ExternalID AS NVARCHAR(50))" in cursor.sql
    assert cursor.params[1] == 1
    assert cursor.params[3] == "151319868"
    assert cursor.params[4] == "151319868"
    assert cursor.params[5] is None
    assert cursor.params[6] is None



def test_detail_refresh_candidates_use_search_id_scope_not_exact_address():
    original_upsert_search = db_layer._upsert_search

    class CaptureCursor:
        def __init__(self):
            self.sql = None
            self.params = None
            self.description = [
                ("db_listing_id",),
                ("external_id",),
                ("listing_id",),
                ("url",),
                ("address",),
                ("property_type",),
                ("price",),
                ("price_display",),
                ("bedrooms",),
                ("bathrooms",),
                ("parking",),
                ("current_status",),
                ("stale_at",),
            ]

        def execute(self, sql, *params):
            self.sql = sql
            self.params = params

        def fetchall(self):
            return [
                (1001, "nearby-1", "nearby-1", "http://nearby", "12 Road, Pottsville, NSW 2489", "house", None, "Contact agent", 3, 2, 2, "active", "old_timestamp"),
            ]

    cursor = CaptureCursor()
    subscription = {"SearchID": 1, "Suburb": "Tanglewood", "StateCode": "NSW", "Postcode": "2488", "DetailBaselineStartedAt": None}
    try:
        db_layer._upsert_search = lambda conn, url: 1
        rows = db_layer.get_active_listings_for_detail_refresh(
            FakeConn(cursor), "http://search", limit=10, stale_hours=None, subscription=subscription
        )
    finally:
        db_layer._upsert_search = original_upsert_search

    assert rows[0]["address"] == "12 Road, Pottsville, NSW 2489"
    assert "LOWER(COALESCE(p.Address" not in cursor.sql
    assert "%tanglewood%" not in cursor.params
    assert "%nsw 2488%" not in cursor.params
    assert "NULLIF(LTRIM(RTRIM(COALESCE(l.ListingURL" in cursor.sql

def test_detect_external_id_priority_ignores_internal_listing_id():
    try:
        db_layer.get_latest_listing_state = lambda conn, external_id: {
            "listing_id": 1002,
            "external_id": external_id,
            "status": "active",
            "price_display": "$1",
            "price_low": None,
            "price_high": None,
            "price_method": "unknown",
            "agents": [],
        }
        result = db_layer.detect_and_record_changes_for_row(
            FakeConn(),
            "http://search",
            {"db_listing_id": 1002, "listing_id": 1002, "external_id": "151319868", "price": "$1"},
            create_events=False,
        )
    finally:
        db_layer.get_latest_listing_state = ORIGINAL_LATEST_STATE
    assert result["external_id"] == "151319868"


def test_existing_listing_does_not_emit_new_listing():
    try:
        db_layer.get_latest_listing_state = lambda conn, external_id: {
            "listing_id": 1002,
            "external_id": external_id,
            "status": "active",
            "price_display": "$1",
            "price_low": None,
            "price_high": None,
            "price_method": "unknown",
            "agents": [],
        }
        result = db_layer.detect_and_record_changes_for_row(
            FakeConn(),
            "http://search",
            {"db_listing_id": 1002, "listing_id": 1002, "external_id": "151319868", "price": "$1"},
            create_events=False,
        )
    finally:
        db_layer.get_latest_listing_state = ORIGINAL_LATEST_STATE
    assert all(event.get("event_type") != "new_listing" for event in result["events_detected"])


def test_existing_db_listing_without_old_state_returns_mapping_warning_not_new_listing():
    try:
        db_layer.get_latest_listing_state = lambda conn, external_id: None
        result = db_layer.detect_and_record_changes_for_row(
            FakeConn(),
            "http://search",
            {"db_listing_id": 1002, "listing_id": "151319868", "external_id": "151319868", "price": "$1"},
            create_events=False,
        )
    finally:
        db_layer.get_latest_listing_state = ORIGINAL_LATEST_STATE
    assert result["events_detected"] == []
    assert result["should_notify_events"] == []
    assert result["warnings"][0]["warning"] == "old_state_missing_for_existing_listing"


def test_failed_refresh_result_has_failed_item_and_no_notify_events():
    original_enrich = ldr.ENRICH_DETAIL_ROWS_FUNC
    detect_calls = {"count": 0}
    try:
        ldr.db_layer.connect = lambda _p=None: FakeConn()
        ldr.db_layer.get_active_listings_for_detail_refresh = lambda *a, **k: [
            {"external_id": "151319868", "db_listing_id": 1002, "url": "http://detail", "address": "addr", "current_status": "active"}
        ]
        ldr.ENRICH_DETAIL_ROWS_FUNC = lambda rows, **k: [dict(
            rows[0],
            detail_refresh_success=False,
            detail_refresh_error="timeout",
            detail_extraction_quality="failed",
            agents=[],
            agency_name=None,
        )]

        def fake_detect(*a, **k):
            detect_calls["count"] += 1
            return {"external_id": "151319868", "events_detected": [{"event_type": "agent_changed", "should_notify": True}], "should_notify_events": [{"event_type": "agent_changed", "should_notify": True}]}

        ldr.db_layer.detect_and_record_changes_for_row = fake_detect
        result = ldr.refresh_active_listings("http://search", dry_run=True, listing_external_id="151319868")
    finally:
        ldr.ENRICH_DETAIL_ROWS_FUNC = original_enrich
        db_layer.connect = ORIGINAL_CONNECT
        db_layer.get_active_listings_for_detail_refresh = ORIGINAL_GET_ACTIVE
        db_layer.detect_and_record_changes_for_row = ORIGINAL_DETECT

    assert detect_calls["count"] == 0
    assert result["processed_count"] == 1
    assert result["refreshed_count"] == 0
    assert result["failed_count"] == 1
    assert result["should_notify_events"] == []
    assert result["items"][0]["status"] == "failed"
    assert result["items"][0]["detail_refresh_error"] == "timeout"


def test_successful_refresh_missing_agents_is_not_notify_ready():
    original_enrich = ldr.ENRICH_DETAIL_ROWS_FUNC
    try:
        ldr.db_layer.connect = lambda _p=None: FakeConn()
        ldr.db_layer.get_active_listings_for_detail_refresh = lambda *a, **k: [
            {"external_id": "151319868", "db_listing_id": 1002, "url": "http://detail", "address": "addr", "current_status": "active"}
        ]
        ldr.ENRICH_DETAIL_ROWS_FUNC = lambda rows, **k: [dict(
            rows[0],
            detail_refresh_success=True,
            detail_refresh_error=None,
            detail_extraction_quality="ok",
            detail_agents_reliable=False,
            agents_explicitly_absent=False,
            detail_agency_reliable=False,
            agency_explicitly_absent=False,
            detail_reliable_fields=["description"],
            agents=[],
            agency_name=None,
            description="new description",
        )]
        db_layer.get_latest_listing_state = lambda conn, external_id: {
            "listing_id": 1002,
            "external_id": external_id,
            "status": "active",
            "price_display": None,
            "price_low": None,
            "price_high": None,
            "price_method": "unknown",
            "detail_price_display": None,
            "agency_name": "Known Agency",
            "agency_code": "KNOWN",
            "agency_profile_url": None,
            "agents": [{"name": "Jonathan Hammond"}, {"name": "Stephanie Zerial"}],
            "description_hash": db_layer._sha("old description"),
        }
        result = ldr.refresh_active_listings("http://search", dry_run=True, listing_external_id="151319868")
    finally:
        ldr.ENRICH_DETAIL_ROWS_FUNC = original_enrich
        db_layer.connect = ORIGINAL_CONNECT
        db_layer.get_active_listings_for_detail_refresh = ORIGINAL_GET_ACTIVE
        db_layer.get_latest_listing_state = ORIGINAL_LATEST_STATE

    assert result["processed_count"] == 1
    assert result["refreshed_count"] == 1
    assert result["failed_count"] == 0
    assert result["should_notify_events"] == []
    assert not any(ev.get("event_type") == "agent_changed" for item in result["items"] for ev in item.get("events_detected", []))
    assert not any(ev.get("event_type") == "agency_changed" for item in result["items"] for ev in item.get("events_detected", []))


def test_db_failed_refresh_skips_change_detection_with_warning():
    try:
        db_layer.get_latest_listing_state = lambda conn, external_id: {
            "listing_id": 1002,
            "external_id": external_id,
            "agency_name": "Old Agency",
            "agents": [{"name": "Old Agent"}],
        }
        result = db_layer.detect_and_record_changes_for_row(
            FakeConn(),
            "http://search",
            {
                "db_listing_id": 1002,
                "external_id": "151319868",
                "detail_refresh_success": False,
                "detail_refresh_error": "timeout",
                "detail_extraction_quality": "failed",
                "agents": [],
                "agency_name": None,
            },
            create_events=False,
        )
    finally:
        db_layer.get_latest_listing_state = ORIGINAL_LATEST_STATE
    assert result["events_detected"] == []
    assert result["should_notify_events"] == []
    assert result["warnings"][0]["warning"] == "detail_refresh_failed_skip_change_detection"


def test_enrich_detail_rows_failed_extract_preserves_candidate_contact_fields():
    import importlib
    import sys
    import types

    sys.modules.setdefault("bs4", types.SimpleNamespace(BeautifulSoup=lambda *a, **k: None))

    module3 = importlib.import_module("module3_enrich_details")

    class FakeDriver:
        def get(self, url):
            pass

        def quit(self):
            pass

    original_build_driver = module3.build_driver
    original_is_429_page = module3.is_429_page
    original_wait = module3.wait_for_detail_ready
    original_extract = module3.extract_detail_data
    original_write_outputs = module3.write_outputs
    try:
        module3.build_driver = lambda *a, **k: FakeDriver()
        module3.is_429_page = lambda driver: False
        module3.wait_for_detail_ready = lambda *a, **k: None
        module3.extract_detail_data = lambda driver: {}
        module3.write_outputs = lambda *a, **k: (None, None)
        rows = module3.enrich_detail_rows(
            [{
                "db_listing_id": 1002,
                "external_id": "151319868",
                "listing_id": "151319868",
                "url": "http://detail",
                "agents": [{"name": "Known Agent"}],
                "agency_name": "Known Agency",
                "description": "known desc",
                "detail_price_display": "$1",
            }],
            sleep_between=0,
            empty_retry=0,
        )
    finally:
        module3.build_driver = original_build_driver
        module3.is_429_page = original_is_429_page
        module3.wait_for_detail_ready = original_wait
        module3.extract_detail_data = original_extract
        module3.write_outputs = original_write_outputs

    row = rows[0]
    assert row["detail_refresh_success"] is False
    assert row["detail_extraction_quality"] == "failed"
    assert row["detail_agents_reliable"] is False
    assert row["detail_agency_reliable"] is False
    assert row["agents_explicitly_absent"] is False
    assert row["agency_explicitly_absent"] is False
    assert row["agents"] == [{"name": "Known Agent"}]
    assert row["agency_name"] == "Known Agency"
    assert row["description"] == "known desc"
    assert row["detail_price_display"] == "$1"


def test_enrich_detail_rows_preserves_listing_ids_when_detail_data_contains_internal_like_id():
    import importlib
    import sys
    import types

    sys.modules.setdefault("bs4", types.SimpleNamespace(BeautifulSoup=lambda *a, **k: None))

    module3 = importlib.import_module("module3_enrich_details")

    class FakeDriver:
        def get(self, url):
            pass

        def quit(self):
            pass

    original_build_driver = module3.build_driver
    original_is_429_page = module3.is_429_page
    original_wait = module3.wait_for_detail_ready
    original_extract = module3.extract_detail_data
    original_write_outputs = module3.write_outputs
    try:
        module3.build_driver = lambda *a, **k: FakeDriver()
        module3.is_429_page = lambda driver: False
        module3.wait_for_detail_ready = lambda *a, **k: None
        module3.extract_detail_data = lambda driver: {"listing_id": 1002, "description": "detail"}
        module3.write_outputs = lambda *a, **k: (None, None)
        rows = module3.enrich_detail_rows(
            [{"db_listing_id": 1002, "external_id": "151319868", "listing_id": "151319868", "url": "http://detail"}],
            sleep_between=0,
        )
    finally:
        module3.build_driver = original_build_driver
        module3.is_429_page = original_is_429_page
        module3.wait_for_detail_ready = original_wait
        module3.extract_detail_data = original_extract
        module3.write_outputs = original_write_outputs

    assert rows[0]["external_id"] == "151319868"
    assert rows[0]["listing_id"] == "151319868"
    assert rows[0]["db_listing_id"] == 1002
    assert rows[0]["detail_agents_reliable"] is False
    assert rows[0]["agents_explicitly_absent"] is False


if __name__ == "__main__":
    test_limit_clamp()
    test_result_structure_and_dry_run_no_ingest()
    test_listing_external_id_passed_to_candidate_helper()
    test_targeted_listing_empty_candidates_reports_clear_error()
    test_targeted_candidate_bypasses_stale_and_uses_string_safe_external_id()
    test_detect_external_id_priority_ignores_internal_listing_id()
    test_existing_listing_does_not_emit_new_listing()
    test_existing_db_listing_without_old_state_returns_mapping_warning_not_new_listing()
    test_failed_refresh_result_has_failed_item_and_no_notify_events()
    test_successful_refresh_missing_agents_is_not_notify_ready()
    test_db_failed_refresh_skips_change_detection_with_warning()
    test_enrich_detail_rows_failed_extract_preserves_candidate_contact_fields()
    test_enrich_detail_rows_preserves_listing_ids_when_detail_data_contains_internal_like_id()
    print("OK")
