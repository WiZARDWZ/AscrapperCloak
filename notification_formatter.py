"""Plain-text, diff-aware Telegram notification templates for listing events."""
from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from typing import Any


def _parse_json(value: Any) -> Any:
    if value is None or isinstance(value, (dict, list)):
        return value
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
    return value


def _get(mapping: Any, *keys: str) -> Any:
    if not isinstance(mapping, dict):
        return None
    lowered = {str(key).lower(): value for key, value in mapping.items()}
    for key in keys:
        if key in mapping:
            return mapping[key]
        if key.lower() in lowered:
            return lowered[key.lower()]
    return None


def _first(*values: Any) -> Any:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if isinstance(value, (list, dict)) and not value:
            continue
        return value
    return None


def clean_display_value(value: Any, fallback: str = "Not available") -> str:
    """Render user-facing values without leaking Python/JSON missing-value tokens."""
    if value is None:
        return fallback
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, Decimal):
        value = int(value) if value == value.to_integral_value() else value
    if isinstance(value, dict):
        useful = [clean_display_value(item, "") for item in value.values()]
        text = ", ".join(item for item in useful if item)
    elif isinstance(value, (list, tuple, set)):
        useful = [clean_display_value(item, "") for item in value]
        text = ", ".join(item for item in useful if item)
    else:
        text = " ".join(str(value).split())
    if not text or text.lower() in {"none", "null", "{}", "[]", "n/a", "na"}:
        return fallback
    return text



NO_PRICE_PHRASES = {
    "", "n/a", "na", "none", "null", "unknown",
}


def _displayed_price_is_valid(value: Any) -> bool:
    text = clean_display_value(value, "").strip()
    lower = text.lower()
    if not text or lower in NO_PRICE_PHRASES:
        return False
    return True


def effective_price_text(value: dict, fallback: str = "Unknown") -> str:
    display = _first(value.get("price_display"), value.get("displayed_price"), value.get("value"))
    low = _first(value.get("inferred_price_low"), value.get("estimated_price_low"), value.get("price_low"))
    high = _first(value.get("inferred_price_high"), value.get("estimated_price_high"), value.get("price_high"))
    if _displayed_price_is_valid(display):
        primary = clean_display_value(display, fallback)
        if value.get("inferred_price_low") is not None or value.get("inferred_price_high") is not None:
            estimate = _estimated_range(low, high)
            if estimate != "Not available":
                return f"{primary}\nEstimated range: {estimate}"
        return primary
    if low is not None or high is not None:
        estimate = _estimated_range(low, high)
        return f"Estimated range: {estimate}" if estimate != "Not available" else fallback
    return fallback

def _event_parts(event_row: dict) -> tuple[str, dict, dict, dict]:
    payload = _parse_json(_get(event_row, "EventPayloadJson", "event_payload_json", "payload"))
    payload = payload if isinstance(payload, dict) else {}
    old = _parse_json(_first(_get(event_row, "OldValueJson", "old_value_json", "raw_old"), payload.get("old_value"), _get(event_row, "old")))
    new = _parse_json(_first(_get(event_row, "NewValueJson", "new_value_json", "raw_new"), payload.get("new_value"), _get(event_row, "new")))
    old = old if isinstance(old, dict) else {"value": old}
    new = new if isinstance(new, dict) else {"value": new}
    event_type = clean_display_value(_first(_get(event_row, "EventType", "event_type"), payload.get("event_type")), "listing_event").lower()
    return event_type, old, new, payload


def _context(event_row: dict, payload: dict) -> tuple[str, str, str | None]:
    listing = _get(event_row, "listing") or {}
    area = clean_display_value(_first(payload.get("area_label"), _get(event_row, "AreaLabel", "area_label", "search_display_name")), "Area not available")
    address = clean_display_value(_first(payload.get("address"), _get(event_row, "address", "Address"), _get(listing, "address")), "Address not available")
    url = _first(payload.get("listing_url"), _get(event_row, "listing_url", "url", "URL", "ListingURL"), _get(listing, "url"))
    return area, address, clean_display_value(url, "") or None


def _base(title: str, event_row: dict, payload: dict) -> list[str]:
    area, address, _ = _context(event_row, payload)
    return [title, f"📍 {area}", "", f"🏠 {address}"]


def _add_url(lines: list[str], event_row: dict, payload: dict) -> str:
    _, _, url = _context(event_row, payload)
    if url:
        lines.extend(["", "🔗 View listing", url])
    return "\n".join(line for line in lines if line is not None).strip()


def _money_number(value: Any) -> str | None:
    if value is None or value == "":
        return None
    try:
        amount = Decimal(str(value).replace("$", "").replace(",", "").strip())
    except InvalidOperation:
        return None
    return f"${amount:,.0f}"


def _price(value: dict, fallback: str) -> str:
    return effective_price_text(value, fallback)


def _estimated_range(low: Any, high: Any) -> str:
    low_text, high_text = _money_number(low), _money_number(high)
    if low_text and high_text and low_text != high_text:
        return f"{low_text} - {high_text}"
    return low_text or high_text or "Not available"


def _append_estimated(lines: list[str], value: dict) -> None:
    low = _first(value.get("estimated_price_low"), value.get("price_low"))
    high = _first(value.get("estimated_price_high"), value.get("price_high"))
    if low is not None or high is not None:
        lines.append(f"Estimated: {_estimated_range(low, high)}")


def format_price_changed(event_row: dict) -> str:
    _, old, new, payload = _event_parts(event_row)
    lines = _base("💰 Price changed", event_row, payload)
    lines.extend(["", "Previous price:", _price(old, "Not listed"), "", "Current price:", _price(new, "Removed")])
    _append_estimated(lines, new)
    return _add_url(lines, event_row, payload)


def format_inferred_price_range_changed(event_row: dict) -> str:
    _, old, new, payload = _event_parts(event_row)
    lines = _base("Estimated range changed", event_row, payload)
    lines.extend([
        "",
        "Previous range:",
        _estimated_range(_first(old.get("inferred_price_low"), old.get("estimated_price_low")), _first(old.get("inferred_price_high"), old.get("estimated_price_high"))),
        "",
        "Current range:",
        _estimated_range(_first(new.get("inferred_price_low"), new.get("estimated_price_low")), _first(new.get("inferred_price_high"), new.get("estimated_price_high"))),
    ])
    return _add_url(lines, event_row, payload)


def format_listing_update(event_row: dict) -> str:
    _, _, _, payload = _event_parts(event_row)
    lines = _base("Listing update", event_row, payload)
    combined = payload.get("combined_events") if isinstance(payload.get("combined_events"), list) else []
    for item in combined:
        event_type = str(item.get("event_type") or "")
        old = item.get("old_value") if isinstance(item.get("old_value"), dict) else {"value": item.get("old_value")}
        new = item.get("new_value") if isinstance(item.get("new_value"), dict) else {"value": item.get("new_value")}
        if event_type == "ad_price_changed":
            lines.extend(["", "Ad price changed:", f"was: {_price(old, 'empty')}", f"now: {_price(new, 'empty')}"])
        elif event_type == "inferred_price_range_changed":
            lines.extend([
                "",
                "Estimated range changed:",
                f"was: {_estimated_range(_first(old.get('inferred_price_low'), old.get('estimated_price_low')), _first(old.get('inferred_price_high'), old.get('estimated_price_high')))}",
                f"now: {_estimated_range(_first(new.get('inferred_price_low'), new.get('estimated_price_low')), _first(new.get('inferred_price_high'), new.get('estimated_price_high')))}",
            ])
    return _add_url(lines, event_row, payload)


FIELD_LABELS = {
    "adpricedisplay": ("Price", "Price"),
    "price": ("Price", "Price"),
    "land_size_display": ("Land size", "Land size"),
    "land_size_sqm": ("Land size", "Land size"),
    "building_size_display": ("Building size", "Building size"),
    "building_size_sqm": ("Building size", "Building size"),
    "floor_area_display": ("Floor area", "Floor area"),
    "floor_area_sqm": ("Floor area", "Floor area"),
    "bedrooms": ("Beds", "Beds"),
    "bathrooms": ("Baths", "Baths"),
    "car_spaces": ("Parking", "Parking"),
    "parking": ("Parking", "Parking"),
    "property_type": ("Property type", "Property type"),
    "address": ("Address", "Address"),
    "agency_name": ("Agency", "Agency"),
    "agents": ("Agent", "Agent"),
    "inspection": ("Inspection", "Inspection"),
    "auction": ("Auction", "Auction"),
    "description": ("Description", "Description"),
}


def _field_name(event_row: dict, payload: dict) -> str:
    return clean_display_value(_first(payload.get("field"), _get(event_row, "Field", "field")), "field")


def _field_value(value: dict, fallback: str) -> str:
    return clean_display_value(_first(value.get("value"), value.get("price_display"), value), fallback)


def format_field_changed(event_row: dict) -> str:
    event_type, old, new, payload = _event_parts(event_row)
    raw_field = _field_name(event_row, payload)
    label, title_label = FIELD_LABELS.get(raw_field.lower(), (raw_field.replace("_", " ").title(), raw_field.replace("_", " ").title()))
    old_text = _field_value(old, "empty")
    new_text = _field_value(new, "empty")
    action = "added" if event_type == "field_discovered" or old_text == "empty" else "changed"
    lines = _base(f"{title_label} {action}", event_row, payload)
    current_price = clean_display_value(payload.get("price_display"), "")
    if current_price:
        lines.extend(["", f"Current price: {current_price}"])
    if raw_field.lower() in {"bedrooms", "bathrooms", "car_spaces", "parking"} and old_text != "empty" and new_text != "empty":
        lines.extend(["", f"{label} changed: {old_text} -> {new_text}"])
    else:
        lines.extend(["", f"{label} {action}:", f"was: {old_text}", f"now: {new_text}"])
    return _add_url(lines, event_row, payload)


def _agents(value: Any, fallback: str) -> str:
    if isinstance(value, dict):
        value = _first(value.get("agent_names"), value.get("agents"), value.get("value"), value if _get(value, "name", "agent_name") else None)
    if not value:
        return fallback
    if not isinstance(value, (list, tuple)):
        value = [value]
    names = []
    for agent in value:
        name = _first(_get(agent, "name", "agent_name"), agent if isinstance(agent, str) else None)
        phone = _get(agent, "phone", "agent_phone")
        if name:
            text = clean_display_value(name, "")
            if phone:
                text = f"{text} ({clean_display_value(phone, '')})"
            names.append(text)
    return ", ".join(name for name in names if name) or fallback


def _agency(value: dict, fallback: str) -> str:
    agency = _first(value.get("agency_name"), value.get("agency"), value.get("value"))
    if isinstance(agency, (list, tuple)):
        agency = agency[0] if agency else None
    if isinstance(agency, dict):
        agency = _get(agency, "name", "agency_name")
    return clean_display_value(agency, fallback)


def _format_agent_agency(event_row: dict, title: str) -> str:
    _, old, new, payload = _event_parts(event_row)
    lines = _base(title, event_row, payload)
    lines.extend(["", "Previous:", f"Agent: {_agents(old, 'Not listed')}", f"Agency: {_agency(old, 'Not listed')}", "", "Current:", f"Agent: {_agents(new, 'Removed')}", f"Agency: {_agency(new, 'Removed')}"])
    return _add_url(lines, event_row, payload)


def format_agent_changed(event_row: dict) -> str:
    return _format_agent_agency(event_row, "👤 Agent changed")


def format_agency_changed(event_row: dict) -> str:
    return _format_agent_agency(event_row, "🏢 Agency changed")


def _inspection(value: dict, fallback: str) -> str:
    return clean_display_value(_first(value.get("inspection_summary"), value.get("inspection_times"), value.get("value")), fallback)


def format_inspection_changed(event_row: dict) -> str:
    _, old, new, payload = _event_parts(event_row)
    old_text, new_text = _inspection(old, ""), _inspection(new, "")
    if not old_text and new_text:
        lines = _base("🕒 Inspection added", event_row, payload) + ["", "New inspection:", new_text]
    elif old_text and not new_text:
        lines = _base("🕒 Inspection removed", event_row, payload) + ["", "Removed inspection:", old_text]
    else:
        lines = _base("🕒 Inspection changed", event_row, payload) + ["", "Previous inspection:", old_text or "Not listed", "", "Current inspection:", new_text or "Removed"]
    return _add_url(lines, event_row, payload)


def _auction(value: dict, fallback: str) -> str:
    summary = value.get("auction_summary")
    if summary:
        return clean_display_value(summary, fallback)
    details = [clean_display_value(value.get(key), "") for key in ("auction_label", "auction_time")]
    combined = " | ".join(detail for detail in details if detail)
    return combined or clean_display_value(value.get("value"), fallback)


def format_auction_changed(event_row: dict) -> str:
    _, old, new, payload = _event_parts(event_row)
    old_text, new_text = _auction(old, ""), _auction(new, "")
    if not old_text and new_text:
        lines = _base("🔨 Auction added", event_row, payload) + ["", "New auction:", new_text]
    elif old_text and not new_text:
        lines = _base("🔨 Auction removed", event_row, payload) + ["", "Removed auction:", old_text]
    else:
        lines = _base("🔨 Auction changed", event_row, payload) + ["", "Previous auction:", old_text or "Not listed", "", "Current auction:", new_text or "Removed"]
    return _add_url(lines, event_row, payload)


def format_status_changed(event_row: dict) -> str:
    _, old, new, payload = _event_parts(event_row)
    old_status = clean_display_value(_first(old.get("status"), old.get("value")), "Not listed")
    new_status = clean_display_value(_first(new.get("status"), new.get("value")), "Removed")
    normalized = new_status.lower().replace(" ", "_")
    title = {"sold": "✅ Sold", "removed": "🚫 Listing removed", "not_found": "⚠️ Listing not found"}.get(normalized, "⚠️ Status changed")
    lines = _base(title, event_row, payload) + ["", "Previous status:", old_status, "", "Current status:", new_status]
    return _add_url(lines, event_row, payload)


def format_new_listing(event_row: dict) -> str:
    _, _, new, payload = _event_parts(event_row)
    merged = {**(_get(event_row, "listing") or {}), **new, **{key: value for key, value in payload.items() if value is not None}}
    lines = _base("🆕 New listing", event_row, payload)
    price = _price(merged, "Unknown")
    if price:
        lines.append(f"💰 {price}")
    features = []
    for icon, key, label in (("🛏", "bedrooms", "bed"), ("🛁", "bathrooms", "bath"), ("🚗", "car_spaces", "car")):
        value = merged.get(key)
        if value is not None and clean_display_value(value, ""):
            features.append(f"{icon} {clean_display_value(value, '')} {label}")
    if features:
        lines.append(" | ".join(features))
    property_type = clean_display_value(merged.get("property_type"), "")
    if property_type:
        lines.append(f"🏢 {property_type}")
    for label, value in (("Inspection", merged.get("inspection_summary")), ("Auction", merged.get("auction_summary")), ("Agent", _agents(merged, ""))):
        text = clean_display_value(value, "")
        if text:
            lines.extend(["", f"{label}:", text])
    return _add_url(lines, event_row, payload)


def format_notification_message(event_row: dict) -> str:
    event_type, _, _, _ = _event_parts(event_row)
    if event_type in {"price_changed", "detail_price_changed", "ad_price_changed"}:
        return format_price_changed(event_row)
    if event_type == "inferred_price_range_changed":
        return format_inferred_price_range_changed(event_row)
    if event_type == "listing_update":
        return format_listing_update(event_row)
    if event_type in {"field_discovered", "size_changed", "property_attributes_changed", "description_changed"}:
        return format_field_changed(event_row)
    if event_type == "new_listing":
        return format_new_listing(event_row)
    if event_type in {"status_changed", "sold", "removed", "not_found", "back_on_market"}:
        return format_status_changed(event_row)
    if event_type == "inspection_changed":
        return format_inspection_changed(event_row)
    if event_type == "auction_changed":
        return format_auction_changed(event_row)
    if event_type in {"agent_changed", "agent_contact_changed"}:
        return format_agent_changed(event_row)
    if event_type == "agency_changed":
        return format_agency_changed(event_row)
    _, old, new, payload = _event_parts(event_row)
    lines = _base(f"🔔 {clean_display_value(event_type, 'Listing event').replace('_', ' ').title()}", event_row, payload)
    lines.extend(["", "Previous:", clean_display_value(old.get("value"), "Not listed"), "", "Current:", clean_display_value(new.get("value"), "Removed")])
    return _add_url(lines, event_row, payload)
