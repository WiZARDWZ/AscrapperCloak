from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from notification_formatter import format_notification_message

PAYLOAD = {
    "area_label": "Petersham NSW 2049",
    "address": "10 Test Street, Petersham NSW 2049",
    "listing_url": "https://example.test/listing/10",
    "property_type": "House",
    "bedrooms": 3,
    "bathrooms": 2,
    "car_spaces": 1,
    "price_display": "$1,250,000",
    "agency_name": "New Agency",
    "agent_names": ["New Agent"],
    "inspection_summary": "Saturday 10:00 am",
    "auction_summary": "Saturday 12:00 pm",
}


def event(event_type, old=None, new=None, payload=None):
    return {
        "EventType": event_type,
        "OldValueJson": json.dumps(old) if old is not None else None,
        "NewValueJson": json.dumps(new) if new is not None else None,
        "EventPayloadJson": json.dumps({**PAYLOAD, **(payload or {})}),
    }


def assert_clean(message):
    for forbidden in ("None", "null", "{}", "[]"):
        assert forbidden not in message, (forbidden, message)


def test_price_changed():
    message = format_notification_message(event("price_changed", {"price_display": "$1,300,000"}, {"price_display": "$1,250,000"}))
    for expected in ("💰 Price changed", "Petersham NSW 2049", "10 Test Street", "Previous price:\n$1,300,000", "Current price:\n$1,250,000", "https://example.test/listing/10"):
        assert expected in message, message


def test_price_changed_estimated_range():
    message = format_notification_message(event("price_changed", {"price_display": "Contact agent"}, {"price_display": "Price guide", "estimated_price_low": 1200000, "estimated_price_high": 1300000}))
    assert "Estimated: $1,200,000 - $1,300,000" in message, message


def test_agent_and_agency_changed():
    old = {"agent_names": ["Old Agent"], "agency_name": "Old Agency"}
    new = {"agent_names": ["New Agent"], "agency_name": "New Agency"}
    agent = format_notification_message(event("agent_changed", old, new))
    agency = format_notification_message(event("agency_changed", old, new))
    assert "👤 Agent changed" in agent and "Agent: Old Agent" in agent and "Agent: New Agent" in agent
    assert "🏢 Agency changed" in agency and "Agency: Old Agency" in agency and "Agency: New Agency" in agency


def test_agent_contact_changed_includes_previous_and_current_phone():
    message = format_notification_message(event("agent_contact_changed", {"name": "Agent One", "phone": "0400"}, {"name": "Agent One", "phone": "0500"}))
    assert "Agent: Agent One (0400)" in message and "Agent: Agent One (0500)" in message, message


def test_inspection_changed_added_removed():
    changed = format_notification_message(event("inspection_changed", {"inspection_summary": "Sat 9am"}, {"inspection_summary": "Sun 10am"}))
    added = format_notification_message(event("inspection_changed", {"inspection_times": []}, {"inspection_summary": "Sun 10am"}))
    removed = format_notification_message(event("inspection_changed", {"inspection_summary": "Sat 9am"}, {"inspection_times": []}))
    assert "Previous inspection:\nSat 9am" in changed and "Current inspection:\nSun 10am" in changed
    assert "🕒 Inspection added" in added and "New inspection:\nSun 10am" in added and "Previous inspection" not in added
    assert "🕒 Inspection removed" in removed and "Removed inspection:\nSat 9am" in removed and "Current inspection" not in removed


def test_auction_and_status_changed():
    auction = format_notification_message(event("auction_changed", {"auction_label": "Auction", "auction_time": "Sat 11am"}, {"auction_label": "Auction", "auction_time": "Sun 1pm"}))
    status = format_notification_message(event("status_changed", {"status": "active"}, {"status": "under_offer"}))
    assert "Previous auction:\nAuction | Sat 11am" in auction and "Current auction:\nAuction | Sun 1pm" in auction
    assert "⚠️ Under offer" in status and "Previous status:\nactive" in status and "Current status:\nunder_offer" in status


def test_new_listing_and_clean_output():
    message = format_notification_message(event("new_listing", None, PAYLOAD))
    for expected in ("🆕 New listing", "$1,250,000", "🛏 3 bed | 🛁 2 bath | 🚗 1 car", "🏢 House", "Inspection:\nSaturday 10:00 am", "Auction:\nSaturday 12:00 pm", "Agent:\nNew Agent"):
        assert expected in message, message
    messages = [
        message,
        format_notification_message(event("inspection_changed", {"inspection_summary": None}, {"inspection_summary": None})),
        format_notification_message(event("agent_changed", {"agent_names": []}, {"agent_names": []})),
    ]
    for output in messages:
        assert_clean(output)


def run_tests():
    test_price_changed()
    test_price_changed_estimated_range()
    test_agent_and_agency_changed()
    test_agent_contact_changed_includes_previous_and_current_phone()
    test_inspection_changed_added_removed()
    test_auction_and_status_changed()
    test_new_listing_and_clean_output()


if __name__ == "__main__":
    run_tests()
    print("OK")
