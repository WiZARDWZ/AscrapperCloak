import hashlib
import json
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Optional

import notification_formatter

LEGACY_EVENT_TYPE_MAP = {
    "agent_change": "agent_changed",
    "description_change": "description_changed",
    "price_change": "price_changed",
}

NOTIFYABLE_EVENT_TYPES = {
    "new_listing",
    "price_changed",
    "detail_price_changed",
    "status_changed",
    "sold",
    "under_offer",
    "withdrawn",
    "back_on_market",
    "agent_changed",
    "agent_contact_changed",
    "agency_changed",
    "inspection_changed",
    "auction_changed",
    "description_changed",
}

INTERNAL_REASONS = {
    "initial_agent_enrichment",
    "initial_agency_enrichment",
    "initial_price_enrichment",
    "initial_description_enrichment",
    "initial_auction_enrichment",
    "initial_inspection_enrichment",
    "initial_detail_baseline",
    "agent_metadata_enrichment",
    "detail_refresh_failed_skip_change_detection",
}

INTERNAL_EVENT_TYPES = {
    "initial_agent_enrichment",
    "initial_agency_enrichment",
    "initial_price_enrichment",
    "initial_description_enrichment",
    "initial_auction_enrichment",
    "initial_inspection_enrichment",
    "initial_detail_baseline",
    "agent_metadata_enrichment",
    "detail_refresh_failed_skip_change_detection",
    "removed_or_missing",
    "inspection_or_auction_change",
}

LOW_SEVERITY_EVENT_TYPES = {"description_changed", "detail_price_changed"}
HIGH_SEVERITY_EVENT_TYPES = {"sold", "withdrawn", "back_on_market"}


def _parse_json(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
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


def canonical_event_type(event_type: Any) -> str:
    text = safe_text(event_type, "").strip().lower()
    return LEGACY_EVENT_TYPE_MAP.get(text, text)


def _coalesce(*values: Any) -> Any:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


def _get_any(mapping: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping:
            return mapping.get(key)
    lowered = {str(k).lower(): v for k, v in mapping.items()}
    for key in keys:
        if key.lower() in lowered:
            return lowered[key.lower()]
    return None


def _payload_flag(event: Dict[str, Any], key: str) -> Any:
    if key in event:
        return event.get(key)
    for container_key in ("payload", "new", "old"):
        payload = event.get(container_key)
        if isinstance(payload, dict) and key in payload:
            return payload.get(key)
    return None


def _is_false(value: Any) -> bool:
    if value is False:
        return True
    if isinstance(value, str):
        return value.strip().lower() in {"false", "0", "no", "n"}
    if isinstance(value, int):
        return value == 0
    return False


def is_event_notifyable(event: dict) -> bool:
    payload = _parse_json(event.get("EventPayloadJson") or event.get("event_payload_json"))
    if isinstance(payload, dict):
        event = {"payload": payload, **event}
    normalized_type = canonical_event_type(event.get("event_type") or event.get("EventType"))
    if _is_false(_coalesce(_get_any(event, "ShouldNotify", "should_notify"), _payload_flag(event, "should_notify"))):
        return False
    reason = safe_text(_coalesce(_get_any(event, "Reason", "reason"), _payload_flag(event, "reason")), "").strip().lower()
    if reason in INTERNAL_REASONS:
        return False
    if normalized_type in INTERNAL_EVENT_TYPES:
        return False
    return normalized_type in NOTIFYABLE_EVENT_TYPES


def _extract_value(payload: Any) -> Any:
    if isinstance(payload, dict) and set(payload.keys()) <= {"field", "value"}:
        return payload.get("value")
    return payload


def _extract_field(event_row: Dict[str, Any], old_payload: Any, new_payload: Any) -> Optional[str]:
    direct = _get_any(event_row, "field", "Field")
    if direct:
        return str(direct)
    for payload in (new_payload, old_payload):
        if isinstance(payload, dict) and payload.get("field"):
            return str(payload.get("field"))
    return None


def _extract_agents(value: Any) -> List[Dict[str, Any]]:
    parsed = _parse_json(value)
    if parsed is None:
        return []
    if isinstance(parsed, list):
        return [a if isinstance(a, dict) else {"name": str(a)} for a in parsed if a]
    if isinstance(parsed, dict):
        if isinstance(parsed.get("agents"), list):
            return _extract_agents(parsed.get("agents"))
        if isinstance(parsed.get("value"), list):
            return _extract_agents(parsed.get("value"))
        if any(k in parsed for k in ("name", "agent_name", "AgentName")):
            return [parsed]
    if isinstance(parsed, str):
        return [{"name": part.strip()} for part in parsed.split(";") if part.strip()]
    return []


def _extract_agency(value: Any) -> Dict[str, Any]:
    parsed = _parse_json(value)
    if isinstance(parsed, dict):
        if isinstance(parsed.get("agency"), dict):
            return parsed.get("agency") or {}
        return {
            "name": _coalesce(parsed.get("agency_name"), parsed.get("name"), parsed.get("Name")),
            "code": _coalesce(parsed.get("agency_code"), parsed.get("AgencyExternalCode")),
            "profile_url": _coalesce(parsed.get("agency_profile_url"), parsed.get("AgencyProfileURL")),
        }
    if parsed:
        return {"name": str(parsed)}
    return {}


def normalize_event_for_notification(event_row: dict) -> dict:
    full_payload = _parse_json(_get_any(event_row, "EventPayloadJson", "event_payload_json", "payload"))
    if not isinstance(full_payload, dict):
        full_payload = {}
    old_payload = _coalesce(full_payload.get("old_value"), _parse_json(_get_any(event_row, "OldValueJson", "old_value_json", "old")))
    new_payload = _coalesce(full_payload.get("new_value"), _parse_json(_get_any(event_row, "NewValueJson", "new_value_json", "new")))
    event_type = canonical_event_type(_get_any(event_row, "EventType", "event_type"))
    field = _extract_field(event_row, old_payload, new_payload)
    old_value = _extract_value(old_payload)
    new_value = _extract_value(new_payload)

    listing = {
        "address": _coalesce(_get_any(event_row, "address", "Address"), _nested(new_payload, "address"), _nested(new_payload, "Address")),
        "url": _coalesce(_get_any(event_row, "url", "URL", "ListingURL"), _nested(new_payload, "url"), _nested(new_payload, "URL")),
        "price_display": _coalesce(_get_any(event_row, "price_display", "PriceDisplay", "CurrentPriceDisplay"), _nested(new_payload, "price_display"), _nested(new_payload, "PriceDisplay"), _nested(new_payload, "price")),
        "inferred_price_low": _coalesce(_get_any(event_row, "inferred_price_low", "InferredPriceLow"), _nested(new_payload, "inferred_price_low")),
        "inferred_price_high": _coalesce(_get_any(event_row, "inferred_price_high", "InferredPriceHigh"), _nested(new_payload, "inferred_price_high")),
        "status": _coalesce(_get_any(event_row, "status", "Status", "CurrentStatus"), _nested(new_payload, "status"), _nested(new_payload, "Status")),
        "property_type": _coalesce(_get_any(event_row, "property_type", "PropertyType"), _nested(new_payload, "property_type")),
        "bedrooms": _coalesce(_get_any(event_row, "bedrooms", "NumberOfBedroom"), _nested(new_payload, "bedrooms")),
        "bathrooms": _coalesce(_get_any(event_row, "bathrooms", "NumberOfBath"), _nested(new_payload, "bathrooms")),
        "parking": _coalesce(_get_any(event_row, "parking", "Parkingslot"), _nested(new_payload, "parking")),
    }
    agency = _extract_agency(_coalesce(_get_any(event_row, "agency", "agency_json"), _get_any(event_row, "agency_name", "AgencyName"), _nested(new_payload, "agency"), new_payload if field == "agency" else None))
    if not agency.get("name"):
        agency["name"] = _coalesce(_get_any(event_row, "agency_name", "AgencyName"), _nested(new_payload, "agency_name"))
    agents = _extract_agents(_coalesce(_get_any(event_row, "agents", "agents_json"), _nested(new_payload, "agents")))

    combined = {"event_type": event_type, "old": old_payload, "new": new_payload, "payload": full_payload, **event_row}
    explicit_should_notify = _coalesce(_get_any(event_row, "ShouldNotify", "should_notify"), _payload_flag(combined, "should_notify"))
    severity = _coalesce(_get_any(event_row, "Severity", "severity"), _payload_flag(combined, "severity"), None)
    reason = _coalesce(_get_any(event_row, "Reason", "reason"), _payload_flag(combined, "reason"))
    if not severity:
        if event_type in LOW_SEVERITY_EVENT_TYPES:
            severity = "low"
        elif event_type in HIGH_SEVERITY_EVENT_TYPES:
            severity = "high"
        else:
            severity = "normal"

    return {
        "event_id": _get_any(event_row, "EventID", "event_id"),
        "event_type": event_type,
        "listing_id": _get_any(event_row, "ListingID", "listing_id"),
        "external_id": _get_any(event_row, "ExternalID", "external_id"),
        "search_id": _get_any(event_row, "SearchID", "search_id"),
        "run_id": _get_any(event_row, "RunID", "run_id"),
        "created_at": _get_any(event_row, "CreatedAt", "created_at"),
        "old": old_value,
        "new": new_value,
        "field": field,
        "severity": str(severity),
        "should_notify": False if _is_false(explicit_should_notify) else is_event_notifyable({**combined, "reason": reason}),
        "reason": reason,
        "listing": listing,
        "agency": agency,
        "agents": agents,
        "raw_old": old_payload,
        "raw_new": new_payload,
    }


def _nested(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        return value.get(key)
    return None


def safe_text(value: Any, default: str = "Unknown") -> str:
    if value is None:
        return default
    if isinstance(value, Decimal):
        value = int(value) if value == value.to_integral_value() else value
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, default=str)
    else:
        text = str(value)
    text = " ".join(text.split())
    return text if text else default


def _to_money_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    text = str(value).strip().replace("$", "").replace(",", "")
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _format_money_number(value: Any) -> str | None:
    number = _to_money_decimal(value)
    if number is None:
        return None
    return f"${number:,.0f}" if number == number.to_integral_value() else f"${number:,.2f}"


def format_money(value: Any) -> str:
    if value is None:
        return "Not available"
    if isinstance(value, dict):
        for key in ("price_display", "display", "value", "price", "sold_price"):
            if value.get(key):
                return format_money(value.get(key))
        low = _coalesce(value.get("price_low"), value.get("low"), value.get("min"))
        high = _coalesce(value.get("price_high"), value.get("high"), value.get("max"))
        low_text = _format_money_number(low)
        high_text = _format_money_number(high)
        if low_text and high_text:
            return low_text if low_text == high_text else f"{low_text} - {high_text}"
        if low_text or high_text:
            return low_text or high_text or "Not available"
        return safe_text(value, "Not available")
    formatted = _format_money_number(value)
    if formatted:
        return formatted
    return safe_text(value, "Not available")


def format_property_features(bedrooms: Any, bathrooms: Any, parking: Any) -> str:
    return f"🛏 {safe_text(bedrooms, 'Unknown')} | 🛁 {safe_text(bathrooms, 'Unknown')} | 🚗 {safe_text(parking, 'Unknown')}"


def _agent_display(agent: Any) -> str:
    if isinstance(agent, dict):
        name = _coalesce(agent.get("name"), agent.get("agent_name"), agent.get("AgentName"))
        phone = _coalesce(agent.get("phone"), agent.get("AgentPhoneNumber"), agent.get("contact"))
        profile_url = _coalesce(agent.get("profile_url"), agent.get("AgentProfileURL"), agent.get("url"))
        parts = []
        if name:
            parts.append(safe_text(name))
        if phone:
            parts.append(f"({safe_text(phone)})")
        if profile_url:
            parts.append(safe_text(profile_url))
        return " ".join(parts) if parts else "Unknown"
    return safe_text(agent, "Unknown")


def format_agent_list(agents: Any) -> str:
    parsed = _extract_agents(agents)
    if not parsed:
        return "Not available"
    rendered = [_agent_display(agent) for agent in parsed]
    rendered = [item for item in rendered if item and item != "Unknown"]
    return "; ".join(rendered) or "Not available"


def _format_agent_change_value(value: Any) -> str:
    rendered = format_agent_list(value)
    if rendered == "Not available":
        return "Agent changed, details unavailable"
    return rendered


def _summarize_value(value: Any) -> str:
    if value is None:
        return "Unknown"
    if isinstance(value, dict):
        if "value" in value and len(value) <= 3:
            return _summarize_value(value.get("value"))
        if "agents" in value:
            return format_agent_list(value.get("agents"))
        if any(k in value for k in ("price_display", "price", "sold_price", "price_low", "price_high")):
            return format_money(value)
        return safe_text(value, "Unknown")
    if isinstance(value, list):
        if all(isinstance(item, dict) and any(k in item for k in ("name", "agent_name", "AgentName")) for item in value):
            return format_agent_list(value)
        return safe_text(value, "Unknown")
    return safe_text(value, "Unknown")


def format_old_new(old: Any, new: Any) -> str:
    return f"Before: {_summarize_value(old)}\nAfter: {_summarize_value(new)}"


def truncate_text(text: Any, max_len: int = 3500) -> str:
    out = safe_text(text, "")
    if len(out) <= max_len:
        return out
    suffix = "\n… truncated"
    return out[: max(0, max_len - len(suffix))].rstrip() + suffix


def _agency_name(event: Dict[str, Any]) -> str:
    agency = event.get("agency") or {}
    if isinstance(agency, dict):
        return safe_text(_coalesce(agency.get("name"), agency.get("Name")), "Not available")
    return safe_text(agency, "Not available")


def _listing_value(event: Dict[str, Any], key: str, default: str = "Unknown") -> str:
    return safe_text((event.get("listing") or {}).get(key), default)


def _status_old_new(event: Dict[str, Any]) -> tuple[str, str]:
    old = event.get("old")
    new = event.get("new")
    if isinstance(old, dict):
        old = _coalesce(old.get("status"), old.get("value"), old)
    if isinstance(new, dict):
        new = _coalesce(new.get("status"), new.get("value"), new)
    return safe_text(old), safe_text(new)


def _sold_value(event: Dict[str, Any], key: str, default: str) -> str:
    for container in (event.get("new"), event.get("raw_new"), event.get("listing")):
        if isinstance(container, dict):
            value = _coalesce(container.get(key), container.get(key.replace("_", "")))
            if value is not None:
                return format_money(value) if "price" in key else safe_text(value, default)
    return default


def build_notification_message(notification_event: dict) -> str:
    """Compatibility wrapper for the dedicated plain-text formatter."""
    message = notification_formatter.format_notification_message(notification_event)
    return message if len(message) <= 3900 else message[:3888].rstrip() + "\n… truncated"


def build_notification_key(event_id: Any, recipient_key: Optional[Any], channel: str = "telegram") -> str:
    recipient = safe_text(recipient_key, "default")
    raw = f"{safe_text(channel, 'telegram')}:{recipient}:{safe_text(event_id, '')}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
