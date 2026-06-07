import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from decimal import Decimal

from listing_change_detector import compare_listing_state
import db_layer


def _has(events, et):
    return any(e.get("event_type") == et for e in events)


def _get(events, et):
    return next(e for e in events if e.get("event_type") == et)


def run_tests():
    assert _has(compare_listing_state({"external_id": "1", "status": "active", "price_low": Decimal("900000"), "price_high": Decimal("900000"), "price_display": "$900,000", "detail_price_display": "$900,000", "agents": []}, {"external_id": "1", "status": "active", "detail_price_display": "Guide $950,000", "price_display": "Guide $950,000", "agents": []}), "price_changed")

    assert not _has(compare_listing_state({"external_id": "1", "status": "active", "price_low": Decimal("950000"), "price_high": Decimal("950000"), "price_display": "$950,000", "detail_price_display": "$950,000", "agents": []}, {"external_id": "1", "status": "active", "price_low": Decimal("950000"), "price_high": Decimal("950000"), "price_display": "Guide $950,000", "detail_price_display": "Guide $950,000", "agents": []}), "price_changed")

    sold_events = compare_listing_state({"external_id": "1", "status": "active", "agents": []}, {"external_id": "1", "status": "sold", "sold_price": 1100000, "agents": []})
    assert _has(sold_events, "sold") and _has(sold_events, "status_changed")
    assert _has(compare_listing_state({"external_id": "1", "status": "active", "agents": []}, {"external_id": "1", "status": "under offer", "agents": []}), "under_offer")

    assert _has(compare_listing_state({"external_id": "1", "agency_code": "ABC", "agents": []}, {"external_id": "1", "agency_code": "XYZ", "agents": []}), "agency_changed")
    desc = compare_listing_state({"external_id": "1", "description_hash": "A", "agents": []}, {"external_id": "1", "description_hash": "B", "agents": []})
    assert _has(desc, "description_changed")

    same = {"external_id": "1", "status": "active", "price_low": Decimal("1"), "price_high": Decimal("1"), "price_display": "$1", "detail_price_display": "$1", "agents": []}
    assert compare_listing_state(same, dict(same)) == []


def test_status_unknown_ignored():
    events = compare_listing_state({"external_id": "1", "status": "active", "agents": []}, {"external_id": "1", "status": "unknown", "agents": []})
    assert events == []


def test_agency_internal_id_ignored():
    events = compare_listing_state(
        {"external_id": "1", "agency_id": 31, "agency_code": "XNKGWU", "agency_name": "A", "agency_profile_url": "https://same", "agents": []},
        {"external_id": "1", "agency_id": None, "agency_code": "XNKGWU", "agency_name": "A", "agency_profile_url": "https://same", "agents": []},
    )
    assert not _has(events, "agency_changed")


def test_initial_agent_enrichment_low_no_notify():
    events = compare_listing_state({"external_id": "1", "agents": []}, {"external_id": "1", "agents": [{"name": "A"}]})
    ev = _get(events, "agent_changed")
    assert ev["should_notify"] is False
    assert ev["severity"] == "low"
    assert ev.get("reason") == "initial_agent_enrichment"


def test_real_agent_change_notifies():
    events = compare_listing_state({"external_id": "1", "agents": [{"name": "A"}]}, {"external_id": "1", "agents": [{"name": "A"}, {"name": "B"}]})
    ev = _get(events, "agent_changed")
    assert ev["should_notify"] is True


def _notify_events(events):
    return [e for e in events if e.get("should_notify")]


def _agent(name, phone=None, profile_url=None, agent_id=None):
    return {"agent_id": agent_id, "name": name, "phone": phone, "profile_url": profile_url}


def test_agent_id_metadata_enrichment_ignored():
    old_agent = _agent(
        "Jonathan Hammond",
        phone="0425252686",
        profile_url="https://www.realestate.com.au/agent/jonathan-hammond-1534750",
        agent_id=None,
    )
    new_agent = dict(old_agent, agent_id="1534750")
    events = compare_listing_state({"external_id": "1", "agents": [old_agent]}, {"external_id": "1", "agents": [new_agent]})
    assert not _has(events, "agent_changed")
    assert not _notify_events(events)


def test_real_added_agent_notifies():
    jonathan = _agent("Jonathan Hammond", phone="0425252686", profile_url="https://www.realestate.com.au/agent/jonathan-hammond-1534750")
    stephanie = _agent("Stephanie Smith", phone="0400000000", profile_url="https://www.realestate.com.au/agent/stephanie-smith-111")
    events = compare_listing_state({"external_id": "1", "agents": [jonathan]}, {"external_id": "1", "agents": [jonathan, stephanie]})
    ev = _get(events, "agent_changed")
    assert ev["should_notify"] is True


def test_real_removed_agent_notifies():
    jonathan = _agent("Jonathan Hammond", phone="0425252686", profile_url="https://www.realestate.com.au/agent/jonathan-hammond-1534750")
    stephanie = _agent("Stephanie Smith", phone="0400000000", profile_url="https://www.realestate.com.au/agent/stephanie-smith-111")
    events = compare_listing_state({"external_id": "1", "agents": [jonathan, stephanie]}, {"external_id": "1", "agents": [jonathan]})
    ev = _get(events, "agent_changed")
    assert ev["should_notify"] is True


def test_real_replaced_agent_notifies():
    jonathan = _agent("Jonathan Hammond", phone="0425252686", profile_url="https://www.realestate.com.au/agent/jonathan-hammond-1534750")
    michael = _agent("Michael Other", phone="0411111111", profile_url="https://www.realestate.com.au/agent/michael-other-222")
    events = compare_listing_state({"external_id": "1", "agents": [jonathan]}, {"external_id": "1", "agents": [michael]})
    ev = _get(events, "agent_changed")
    assert ev["should_notify"] is True


def test_same_agent_phone_change_notifies_contact_change():
    old_agent = _agent("Jonathan Hammond", phone="0425", profile_url="https://www.realestate.com.au/agent/jonathan-hammond-1534750")
    new_agent = _agent("Jonathan Hammond", phone="0400", profile_url="https://www.realestate.com.au/agent/jonathan-hammond-1534750")
    events = compare_listing_state({"external_id": "1", "agents": [old_agent]}, {"external_id": "1", "agents": [new_agent]})
    ev = _get(events, "agent_contact_changed")
    assert ev["should_notify"] is True


def test_agent_order_only_change_ignored():
    a = _agent("Agent A", phone="0400000001", profile_url="https://www.realestate.com.au/agent/agent-a-1")
    b = _agent("Agent B", phone="0400000002", profile_url="https://www.realestate.com.au/agent/agent-b-2")
    events = compare_listing_state({"external_id": "1", "agents": [a, b]}, {"external_id": "1", "agents": [b, a]})
    assert not _has(events, "agent_changed")
    assert not _notify_events(events)


def test_initial_description_enrichment_reason():
    old_empty_hash = db_layer._sha("")
    events = compare_listing_state({"external_id": "1", "description_hash": old_empty_hash, "agents": []}, {"external_id": "1", "description": "new desc", "agents": []})
    ev = _get(events, "description_changed")
    assert ev["should_notify"] is False
    assert ev.get("reason") == "initial_description_enrichment"


def test_failed_detail_refresh_skips_all_changes():
    old = {
        "external_id": "151319868",
        "status": "active",
        "agency_name": "Old Agency",
        "agents": [_agent("Old Agent", phone="0400")],
    }
    new = {
        "external_id": "151319868",
        "detail_refresh_success": False,
        "detail_extraction_quality": "failed",
        "agents": [],
        "agency_name": None,
    }
    assert compare_listing_state(old, new) == []


def test_partial_detail_refresh_empty_agents_not_removed():
    old = {"external_id": "151319868", "agency_name": "Old Agency", "agents": [_agent("Old Agent", phone="0400")]}
    new = {"external_id": "151319868", "detail_refresh_success": True, "detail_extraction_quality": "partial", "agents": [], "agency_name": None}
    events = compare_listing_state(old, new)
    assert not _has(events, "agent_changed")
    assert not _has(events, "agency_changed")
    assert not _notify_events(events)


def test_ok_detail_refresh_empty_agents_requires_explicit_absence_for_removal():
    old = {"external_id": "151319868", "agents": [_agent("Old Agent", phone="0400")]}
    new = {"external_id": "151319868", "detail_refresh_success": True, "detail_extraction_quality": "ok", "detail_agents_reliable": False, "agents_explicitly_absent": False, "agents": []}
    assert not _has(compare_listing_state(old, new), "agent_changed")


def test_explicit_agent_removal_notifies():
    old = {"external_id": "151319868", "agents": [_agent("Agent A"), _agent("Agent B")]}
    new = {"external_id": "151319868", "detail_refresh_success": True, "detail_extraction_quality": "ok", "detail_agents_reliable": True, "agents_explicitly_absent": True, "agents": []}
    ev = _get(compare_listing_state(old, new), "agent_changed")
    assert ev["should_notify"] is True


def test_missing_agents_not_treated_as_removal_when_not_reliable():
    old = {"external_id": "151319868", "agents": [_agent("Agent A"), _agent("Agent B")]}
    new = {
        "external_id": "151319868",
        "detail_refresh_success": True,
        "detail_extraction_quality": "ok",
        "detail_agents_reliable": False,
        "agents_explicitly_absent": False,
        "agents": [],
    }
    assert not _has(compare_listing_state(old, new), "agent_changed")


def test_missing_agency_not_treated_as_removal_when_not_reliable():
    old = {"external_id": "151319868", "agency_code": "OLD", "agency_name": "Old Agency", "agents": []}
    new = {
        "external_id": "151319868",
        "detail_refresh_success": True,
        "detail_extraction_quality": "ok",
        "detail_agency_reliable": False,
        "agency_explicitly_absent": False,
        "agency_name": None,
        "agency_code": None,
        "agency_profile_url": None,
        "agents": [],
    }
    assert not _has(compare_listing_state(old, new), "agency_changed")


def test_real_agency_change_with_reliable_value_notifies():
    old = {"external_id": "151319868", "agency_code": "A", "agency_name": "Agency A", "agents": []}
    new = {"external_id": "151319868", "detail_agency_reliable": True, "agency_code": "B", "agency_name": "Agency B", "agents": []}
    ev = _get(compare_listing_state(old, new), "agency_changed")
    assert ev["should_notify"] is True


def test_normalize_scrape_run_type():
    assert db_layer.normalize_scrape_run_type(source="change_detection") == "enrich_single"
    assert db_layer.normalize_scrape_run_type(source="light_check") == "light"
    assert db_layer.normalize_scrape_run_type(source="full_refresh") == "full"


def test_create_listing_event_requires_run_id():
    try:
        db_layer.create_listing_event_if_new(None, 1, "x", {"field": "f"}, run_id=None)
        raise AssertionError("expected ValueError when run_id is None")
    except ValueError as e:
        assert "run_id is required" in str(e)


def test_duplicate_only_no_new_run_created():
    orig_get_latest = db_layer.get_latest_listing_state
    orig_exists = db_layer.listing_event_exists_by_hash
    orig_create_run = db_layer.create_lightweight_scrape_run
    orig_create_event = db_layer.create_listing_event_if_new
    orig_upsert_search = db_layer._upsert_search

    calls = {"run": 0, "insert": 0}

    def fake_get_latest(_conn, _external_id):
        return {"listing_id": 10, "external_id": "123", "status": "active", "price_display": "$1", "price_low": Decimal("1"), "price_high": Decimal("1"), "price_method": "parsed_display", "detail_price_display": "$1", "description_hash": db_layer._sha(""), "agency_name": None, "agency_code": None, "agency_profile_url": None, "agents": [], "inspection_short": None, "inspection_long": None, "auction_label": None, "auction_time": None}

    db_layer.get_latest_listing_state = fake_get_latest
    db_layer.listing_event_exists_by_hash = lambda _c, _h: True
    db_layer.create_lightweight_scrape_run = lambda *_a, **_k: calls.__setitem__("run", calls["run"] + 1) or 999
    db_layer.create_listing_event_if_new = lambda *_a, **_k: calls.__setitem__("insert", calls["insert"] + 1) or False
    db_layer._upsert_search = lambda *_a, **_k: 1
    try:
        row = {"listing_id": "123", "price": "$2", "detail_price_display": "$2", "status": "active", "description": "", "url": "https://x", "property_type": "house"}
        result = db_layer.detect_and_record_changes_for_row(object(), "https://example.com/in-x/list-1?activeSort=list-date", row, run_id=None, create_events=True)
        assert result["events_detected"]
        assert result["events_created"] == 0
        assert result["run_id"] is None
        assert calls["run"] == 0
        assert calls["insert"] == 0
    finally:
        db_layer.get_latest_listing_state = orig_get_latest
        db_layer.listing_event_exists_by_hash = orig_exists
        db_layer.create_lightweight_scrape_run = orig_create_run
        db_layer.create_listing_event_if_new = orig_create_event
        db_layer._upsert_search = orig_upsert_search


def test_initial_detail_baseline_suppresses_price_auction_and_inspection():
    old = {"external_id": "1", "agents": []}
    new = {"external_id": "1", "agents": [], "detail_price_display": "$890,000", "price_method": "direct_from_pdp", "auction_label": "Auction Sat 10am", "inspection_short": "Sat 9am"}
    events = compare_listing_state(old, new, context="initial_detail_baseline", suppress_notifications=True)
    for event_type in ("price_changed", "auction_changed", "inspection_changed"):
        event = _get(events, event_type)
        assert event["should_notify"] is False
        assert event.get("reason") in {"initial_price_enrichment", "initial_auction_enrichment", "initial_inspection_enrichment", "initial_detail_baseline"}


def test_price_unknown_known_inferred_direct_and_drop_notify_after_ready():
    unknown = {"external_id": "1", "agents": []}
    known = {"external_id": "1", "agents": [], "detail_price_display": "$890,000", "price_method": "direct_from_pdp"}
    assert _get(compare_listing_state(unknown, known), "price_changed")["should_notify"] is True
    inferred = {"external_id": "1", "agents": [], "price_low": 750000, "price_high": 1000000, "price_method": "sliding_between_window"}
    assert _get(compare_listing_state(inferred, known), "price_changed")["should_notify"] is True
    assert _get(compare_listing_state({"external_id": "1", "agents": [], "price_low": 100, "price_high": 100}, {"external_id": "1", "agents": [], "price_low": 95, "price_high": 95}), "price_changed")["should_notify"] is True


def test_auction_and_inspection_explicit_absence_rules():
    old = {"external_id": "1", "agents": [], "auction_label": "Auction Sat", "inspection_short": "Sat 9am"}
    assert not _has(compare_listing_state(old, {"external_id": "1", "agents": []}), "auction_changed")
    assert not _has(compare_listing_state(old, {"external_id": "1", "agents": []}), "inspection_changed")
    removed = compare_listing_state(old, {"external_id": "1", "agents": [], "auction_explicitly_absent": True, "inspection_explicitly_absent": True})
    assert _get(removed, "auction_changed")["should_notify"] is True
    assert _get(removed, "inspection_changed")["should_notify"] is True
    added = compare_listing_state({"external_id": "1", "agents": []}, {"external_id": "1", "agents": [], "auction_label": "Auction Sat", "inspection_short": "Sat 9am"})
    assert _get(added, "auction_changed")["should_notify"] is True
    assert _get(added, "inspection_changed")["should_notify"] is True


def test_agency_metadata_and_query_changes_ignored_but_real_change_notifies():
    old = {"external_id": "1", "agents": [], "agency_name": "Belle Property - Drummoyne"}
    metadata = {"external_id": "1", "agents": [], "agency_name": "Belle Property - Drummoyne", "agency_code": "BELLE", "agency_profile_url": "https://example/agency/belle?source=buy"}
    assert not _has(compare_listing_state(old, metadata), "agency_changed")
    sold_url = dict(metadata, agency_profile_url="https://example/agency/belle?source=sold")
    assert not _has(compare_listing_state(metadata, sold_url), "agency_changed")
    real = dict(metadata, agency_name="Other Agency", agency_code="OTHER")
    assert _get(compare_listing_state(metadata, real), "agency_changed")["should_notify"] is True


def test_agent_profile_url_only_change_ignored():
    old = {"external_id": "1", "agents": [_agent("Agent A", profile_url="https://x/old", agent_id=None)]}
    new = {"external_id": "1", "agents": [_agent("Agent A", profile_url="https://x/new", agent_id="123")] }
    assert not _has(compare_listing_state(old, new), "agent_changed")
    assert not _notify_events(compare_listing_state(old, new))


def test_events_include_diff_aware_payloads():
    old = {"external_id": "1", "status": "active", "address": "10 Test St", "url": "https://example.test/1", "property_type": "House", "bedrooms": 3, "bathrooms": 2, "parking": 1, "price_display": "$900,000", "price_low": 900000, "price_high": 900000, "price_method": "direct", "agents": [{"name": "Old Agent"}], "agency_name": "Old Agency", "inspection_short": "Sat 9am", "auction_label": "Sat 11am"}
    new = {**old, "price_display": "$950,000", "price_low": 950000, "price_high": 950000, "agents": [{"name": "New Agent"}], "agency_name": "New Agency", "inspection_short": "Sun 10am", "auction_label": "Sun 1pm"}
    events = compare_listing_state(old, new)
    price = _get(events, "price_changed")
    agent = _get(events, "agent_changed")
    inspection = _get(events, "inspection_changed")
    auction = _get(events, "auction_changed")
    assert price["old_value"]["price_display"] == "$900,000"
    assert price["new_value"]["estimated_price_low"] == 950000
    assert agent["old_value"] == {"agent_names": ["Old Agent"], "agency_name": "Old Agency"}
    assert inspection["new_value"]["inspection_times"] == ["Sun 10am"]
    assert auction["old_value"]["auction_label"] == "Sat 11am"
    required = {"area_label", "address", "listing_url", "external_id", "property_type", "bedrooms", "bathrooms", "car_spaces", "price_display", "estimated_price_low", "estimated_price_high", "agency_name", "agent_names", "inspection_summary", "auction_summary"}
    for event in events:
        assert required <= set(event["event_payload"]), event


if __name__ == "__main__":
    run_tests()
    test_status_unknown_ignored()
    test_agency_internal_id_ignored()
    test_initial_agent_enrichment_low_no_notify()
    test_real_agent_change_notifies()
    test_agent_id_metadata_enrichment_ignored()
    test_real_added_agent_notifies()
    test_real_removed_agent_notifies()
    test_real_replaced_agent_notifies()
    test_same_agent_phone_change_notifies_contact_change()
    test_agent_order_only_change_ignored()
    test_initial_description_enrichment_reason()
    test_failed_detail_refresh_skips_all_changes()
    test_partial_detail_refresh_empty_agents_not_removed()
    test_ok_detail_refresh_empty_agents_requires_explicit_absence_for_removal()
    test_explicit_agent_removal_notifies()
    test_missing_agents_not_treated_as_removal_when_not_reliable()
    test_missing_agency_not_treated_as_removal_when_not_reliable()
    test_real_agency_change_with_reliable_value_notifies()
    test_normalize_scrape_run_type()
    test_create_listing_event_requires_run_id()
    test_duplicate_only_no_new_run_created()
    test_initial_detail_baseline_suppresses_price_auction_and_inspection()
    test_price_unknown_known_inferred_direct_and_drop_notify_after_ready()
    test_auction_and_inspection_explicit_absence_rules()
    test_agency_metadata_and_query_changes_ignored_but_real_change_notifies()
    test_agent_profile_url_only_change_ignored()
    test_events_include_diff_aware_payloads()
    print("OK")
