import hashlib
import json
import re
from decimal import Decimal
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import db_layer
import config


def normalize_change_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        s = re.sub(r"\s+", " ", value).strip()
        if s.lower() in {"", "n/a", "na", "-", "—"}:
            return None
        return s
    return value


def normalize_status(text_or_row: Any) -> str:
    candidates: List[str] = []
    if isinstance(text_or_row, dict):
        for k in ["status", "current_status", "area_status", "detail_status", "CurrentStatus", "Status"]:
            v = normalize_change_value(text_or_row.get(k))
            if v:
                candidates.append(str(v))
        for k in ["price", "price_display", "detail_price_display"]:
            v = normalize_change_value(text_or_row.get(k))
            if v:
                candidates.append(str(v))
    else:
        v = normalize_change_value(text_or_row)
        if v:
            candidates.append(str(v))

    text = " | ".join(candidates).lower()
    parts = [p.strip() for p in re.split(r"\s*\|\s*", text) if p.strip()]
    if any(
        re.fullmatch(r"sold", part)
        or re.search(r"\bsold\s+(?:prior\s+to\s+auction|at\s+auction|on\s+\d{1,2}\s+[a-z]{3,9}\s+\d{4}|for\s+\$?\s*[0-9][0-9,]*(?:\.[0-9]+)?)\b", part)
        for part in parts
    ):
        return "sold"
    if any("withdrawn" in part or "off market" in part for part in parts):
        return "removed"
    if any("active" in part or "current" in part or "for sale" in part for part in parts):
        return "active"
    return "unknown"


def normalize_price_for_compare(row_or_snapshot: Dict[str, Any]) -> Dict[str, Any]:
    price_display = normalize_change_value(row_or_snapshot.get("price_display") or row_or_snapshot.get("price") or row_or_snapshot.get("detail_price_display"))
    detail_price_display = normalize_change_value(row_or_snapshot.get("detail_price_display"))
    low = row_or_snapshot.get("price_low")
    high = row_or_snapshot.get("price_high")
    if low is None and high is None:
        low, high = db_layer.parse_price_range(detail_price_display or price_display)
    else:
        low = db_layer.to_decimal(low)
        high = db_layer.to_decimal(high)
    method = normalize_change_value(row_or_snapshot.get("price_method")) or ("parsed_display" if (low is not None or high is not None) else "unknown")
    return {"price_display": price_display, "price_low": low, "price_high": high, "price_method": method}


def normalize_agents_for_compare(row: Dict[str, Any]) -> List[Dict[str, Optional[str]]]:
    agents = row.get("agents")
    if isinstance(agents, str):
        try:
            agents = json.loads(agents)
        except json.JSONDecodeError:
            agents = []
    if not agents:
        agents = []
        for i in range(1, 6):
            name = normalize_change_value(row.get(f"agent_{i}_name"))
            if not name:
                continue
            agents.append(
                {
                    "agent_id": normalize_change_value(row.get(f"agent_{i}_id") or row.get(f"agent_{i}_external_id")),
                    "name": name,
                    "phone": normalize_change_value(row.get(f"agent_{i}_phone")),
                    "profile_url": normalize_change_value(row.get(f"agent_{i}_profile_url")),
                }
            )
    normalized: List[Dict[str, Optional[str]]] = []
    for a in agents:
        if isinstance(a, str):
            a = {"name": a}
        normalized.append(
            {
                "agent_id": normalize_change_value(a.get("agent_id") or a.get("external_id")),
                "name": normalize_change_value(a.get("name")),
                "phone": normalize_change_value(a.get("phone")),
                "profile_url": normalize_change_value(a.get("profile_url")),
            }
        )
    return normalized




def _normalize_agent_url(value: Any) -> Optional[str]:
    norm = normalize_change_value(value)
    if not norm:
        return None
    return str(norm).strip().rstrip("/").lower()


def _normalize_agent_name(value: Any) -> Optional[str]:
    norm = normalize_change_value(value)
    if not norm:
        return None
    return str(norm).lower()


def _normalize_agent_phone(value: Any) -> Optional[str]:
    norm = normalize_change_value(value)
    if not norm:
        return None
    digits = re.sub(r"\D+", "", str(norm))
    return digits or str(norm).lower()


def _agent_identity(agent: Dict[str, Optional[str]]) -> tuple[str, str] | None:
    """Use stable human identity; URL/id metadata must not create agent changes."""
    name = _normalize_agent_name(agent.get("name"))
    if name:
        return ("name", name)
    agent_id = normalize_change_value(agent.get("agent_id"))
    if agent_id:
        return ("agent_id", str(agent_id).lower())
    return None


def _agent_identity_map(agents: List[Dict[str, Optional[str]]]) -> Dict[tuple[str, str], Dict[str, Optional[str]]]:
    out: Dict[tuple[str, str], Dict[str, Optional[str]]] = {}
    for agent in agents:
        ident = _agent_identity(agent)
        if ident is not None:
            out[ident] = agent
    return out


def _sorted_agents_for_event(agents: List[Dict[str, Optional[str]]]) -> List[Dict[str, Optional[str]]]:
    return sorted(agents, key=lambda agent: _agent_identity(agent) or ("", ""))


def _agent_contact_changes(old_map: Dict[tuple[str, str], Dict[str, Optional[str]]], new_map: Dict[tuple[str, str], Dict[str, Optional[str]]]) -> List[Dict[str, Any]]:
    changes: List[Dict[str, Any]] = []
    for ident in sorted(set(old_map) & set(new_map)):
        old_agent = old_map[ident]
        new_agent = new_map[ident]
        old_phone = _normalize_agent_phone(old_agent.get("phone"))
        new_phone = _normalize_agent_phone(new_agent.get("phone"))
        if old_phone != new_phone:
            changes.append({
                "event_type": "agent_contact_changed",
                "field": "agent_phone",
                "old_value": old_agent,
                "new_value": new_agent,
                "severity": "normal",
                "should_notify": True,
            })
    return changes

def hash_description(text: Any) -> Optional[str]:
    norm = normalize_change_value(text)
    if not norm:
        return None
    return hashlib.sha256(str(norm).encode("utf-8")).hexdigest()


def _canonical_profile_path(value: Any) -> Optional[str]:
    norm = normalize_change_value(value)
    if not norm:
        return None
    parsed = urlparse(str(norm))
    return (parsed.path or str(norm)).strip().rstrip("/").lower() or None


def _normalize_agency_identity(state: Dict[str, Any]) -> tuple[Optional[str], Optional[str], Optional[str]]:
    code = normalize_change_value(state.get("agency_code"))
    name = normalize_change_value(state.get("agency_name"))
    url_path = _canonical_profile_path(state.get("agency_profile_url"))
    return (str(name).lower() if name else None, str(code).lower() if code else None, url_path)


def _agency_identity_changed(old: tuple[Optional[str], Optional[str], Optional[str]], new: tuple[Optional[str], Optional[str], Optional[str]]) -> bool:
    old_name, old_code, old_path = old
    new_name, new_code, new_path = new
    if old_name and new_name:
        return old_name != new_name
    if old_code and new_code:
        return old_code != new_code
    if old_path and new_path:
        return old_path != new_path
    return bool(any(old)) != bool(any(new))


def _suppression_reason(context: Optional[str], suppress_notifications: bool, fallback: str) -> Optional[str]:
    if context == "initial_detail_baseline":
        return fallback
    if suppress_notifications:
        return "initial_detail_baseline"
    return None


def _append_event(events: List[Dict[str, Any]], payload: Dict[str, Any], context: Optional[str], suppress_notifications: bool, enrichment_reason: Optional[str] = None) -> None:
    reason = _suppression_reason(context, suppress_notifications, enrichment_reason or "initial_detail_baseline")
    if reason:
        payload["should_notify"] = False
        payload["reason"] = reason
    events.append(payload)


def _summary(*values: Any) -> Optional[str]:
    parts = [str(value).strip() for value in values if normalize_change_value(value)]
    return " | ".join(parts) or None


def _agent_names(state: Dict[str, Any]) -> List[str]:
    return [str(agent["name"]) for agent in normalize_agents_for_compare(state) if agent.get("name")]


def build_event_payload(state: Dict[str, Any]) -> Dict[str, Any]:
    """Return stable listing context shared by every user-facing event."""
    price = normalize_price_for_compare(state)
    return {
        "area_label": normalize_change_value(state.get("area_label") or state.get("search_display_name")),
        "address": normalize_change_value(state.get("address")),
        "listing_url": normalize_change_value(state.get("listing_url") or state.get("url")),
        "external_id": normalize_change_value(state.get("external_id")),
        "property_type": normalize_change_value(state.get("property_type")),
        "bedrooms": state.get("bedrooms"),
        "bathrooms": state.get("bathrooms"),
        "car_spaces": state.get("car_spaces") if "car_spaces" in state else state.get("parking"),
        "price_display": price.get("price_display"),
        "estimated_price_low": price.get("price_low"),
        "estimated_price_high": price.get("price_high"),
        "agency_name": normalize_change_value(state.get("agency_name")),
        "agent_names": _agent_names(state),
        "inspection_summary": _summary(state.get("inspection_short"), state.get("inspection_long")),
        "auction_summary": _summary(state.get("auction_label"), state.get("auction_time")),
        "land_size_display": normalize_change_value(state.get("land_size_display")),
        "building_size_display": normalize_change_value(state.get("building_size_display")),
        "floor_area_display": normalize_change_value(state.get("floor_area_display")),
    }


def _price_event_value(state: Dict[str, Any]) -> Dict[str, Any]:
    price = normalize_price_for_compare(state)
    return {
        "price_display": price.get("price_display"),
        "estimated_price_low": price.get("price_low"),
        "estimated_price_high": price.get("price_high"),
        "price_method": price.get("price_method"),
    }


def _agent_event_value(state: Dict[str, Any]) -> Dict[str, Any]:
    return {"agent_names": _agent_names(state), "agency_name": normalize_change_value(state.get("agency_name"))}


def _inspection_event_value(state: Dict[str, Any]) -> Dict[str, Any]:
    values = [normalize_change_value(state.get(key)) for key in ("inspection_short", "inspection_long")]
    return {"inspection_summary": _summary(*values), "inspection_times": [value for value in values if value]}


def _auction_event_value(state: Dict[str, Any]) -> Dict[str, Any]:
    return {"auction_label": normalize_change_value(state.get("auction_label")), "auction_time": normalize_change_value(state.get("auction_time"))}


def _detail_refresh_failed(new: Dict[str, Any]) -> bool:
    return new.get("detail_refresh_success") is False or new.get("detail_extraction_quality") == "failed"


def _detail_quality(new: Dict[str, Any]) -> Optional[str]:
    quality = new.get("detail_extraction_quality")
    return str(quality).lower() if quality is not None else None


def _is_partial_detail_refresh(new: Dict[str, Any]) -> bool:
    return _detail_quality(new) == "partial"




def _field_reliable(new: Dict[str, Any], field: str, flag_name: str) -> Optional[bool]:
    if flag_name in new:
        return bool(new.get(flag_name))
    reliable_fields = new.get("detail_reliable_fields")
    if reliable_fields is not None:
        try:
            return field in set(reliable_fields)
        except TypeError:
            return False
    return None


def _quality_allows_explicit_absence(new: Dict[str, Any]) -> bool:
    quality = _detail_quality(new)
    return quality is None or quality == "ok"


def _has_any_agency(agency: tuple[Optional[str], Optional[str], Optional[str]]) -> bool:
    return any(agency)


def _event_value_for_field(state: Dict[str, Any], field: str) -> Any:
    return normalize_change_value(state.get(field))


def _append_scalar_field_change(
    events: List[Dict[str, Any]],
    old: Dict[str, Any],
    new: Dict[str, Any],
    field: str,
    event_type: str,
    context: Optional[str],
    suppress_notifications: bool,
    *,
    severity: str = "normal",
    notify: bool = True,
) -> None:
    if field not in new:
        return
    old_value = _event_value_for_field(old, field)
    new_value = _event_value_for_field(new, field)
    if old_value == new_value:
        return
    if old_value and not new_value:
        return
    if not old_value and new_value:
        discovery_notify = notify and bool(config.NOTIFY_ON_FIELD_DISCOVERED)
        if field in {"land_size_display", "land_size_sqm", "building_size_display", "building_size_sqm", "floor_area_display", "floor_area_sqm"}:
            discovery_notify = discovery_notify and bool(config.NOTIFY_ON_SIZE_DISCOVERED)
        _append_event(events, {
            "event_type": "field_discovered",
            "field": field,
            "old_value": None,
            "new_value": new_value,
            "severity": severity,
            "should_notify": discovery_notify,
        }, context, suppress_notifications)
        return
    _append_event(events, {
        "event_type": event_type,
        "field": field,
        "old_value": old_value,
        "new_value": new_value,
        "severity": severity,
        "should_notify": notify,
    }, context, suppress_notifications)


def compare_listing_state(old: Optional[Dict[str, Any]], new: Dict[str, Any], context: Optional[str] = None, suppress_notifications: bool = False) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    if _detail_refresh_failed(new):
        return events
    partial = _is_partial_detail_refresh(new)
    if old is None:
        _append_event(events, {"event_type": "new_listing", "field": "listing", "old_value": None, "new_value": build_event_payload(new), "severity": "normal", "should_notify": True}, context, suppress_notifications)
        events[0]["event_payload"] = build_event_payload(new)
        return events

    compare_price = not (partial and not any(k in new for k in ("price", "price_display", "price_low", "price_high", "detail_price_display")))
    if compare_price:
        old_price, new_price = normalize_price_for_compare(old), normalize_price_for_compare(new)
        old_num = (old_price["price_low"], old_price["price_high"])
        new_num = (new_price["price_low"], new_price["price_high"])
        old_display = normalize_change_value(old_price.get("price_display"))
        new_display = normalize_change_value(new_price.get("price_display"))
        direct_reveal = old_price.get("price_method") != "direct_from_pdp" and new_price.get("price_method") == "direct_from_pdp"
        if not old_display and new_display:
            _append_event(events, {"event_type": "field_discovered", "field": "AdPriceDisplay", "old_value": None, "new_value": new_display, "severity": "normal", "should_notify": bool(config.NOTIFY_ON_FIELD_DISCOVERED)}, context, suppress_notifications)
        elif old_display != new_display or old_num != new_num or (direct_reveal and any(new_num)):
            _append_event(events, {"event_type": "ad_price_changed", "field": "price", "old_value": _price_event_value(old), "new_value": _price_event_value(new), "severity": "normal", "should_notify": True}, context, suppress_notifications, "initial_price_enrichment")

    old_status, new_status = normalize_status(old), normalize_status(new)
    explicit = {"active", "sold", "removed", "not_found"}
    if new_status in explicit and old_status != new_status and (old_status in explicit or new_status != "active"):
        _append_event(events, {"event_type": "status_changed", "field": "status", "old_value": {"status": old_status}, "new_value": {"status": new_status}, "severity": "normal", "should_notify": True}, context, suppress_notifications)
        if new_status in {"sold", "removed"}:
            _append_event(events, {"event_type": new_status, "field": "status", "old_value": old_status, "new_value": new_status, "sold_price": new.get("sold_price"), "sold_date": new.get("sold_date"), "severity": "high" if new_status == "sold" else "normal", "should_notify": True}, context, suppress_notifications)
        if old_status in {"sold", "removed", "not_found"} and new_status == "active":
            _append_event(events, {"event_type": "back_on_market", "field": "status", "old_value": old_status, "new_value": "active", "severity": "high", "should_notify": True}, context, suppress_notifications)

    old_agents, new_agents = normalize_agents_for_compare(old), normalize_agents_for_compare(new)
    old_map, new_map = _agent_identity_map(old_agents), _agent_identity_map(new_agents)
    reliable = _field_reliable(new, "agents", "detail_agents_reliable")
    explicit_absent = new.get("agents_explicitly_absent") is True
    can_remove = not old_agents or new_agents or (explicit_absent and reliable is not False and _quality_allows_explicit_absence(new))
    if can_remove and set(old_map) != set(new_map):
        initial_add = not old_agents and bool(new_agents) and new.get("old_agents_reliable") is not True
        _append_event(events, {"event_type": "agent_changed", "field": "agents", "old_value": _agent_event_value(old), "new_value": _agent_event_value(new), "severity": "low" if initial_add else "normal", "should_notify": not initial_add, **({"reason": "initial_agent_enrichment"} if initial_add else {})}, context, suppress_notifications, "initial_agent_enrichment")
    elif set(old_map) == set(new_map):
        for event in _agent_contact_changes(old_map, new_map):
            _append_event(events, event, context, suppress_notifications, "initial_agent_enrichment")

    old_agency, new_agency = _normalize_agency_identity(old), _normalize_agency_identity(new)
    reliable = _field_reliable(new, "agency", "detail_agency_reliable")
    explicit_absent = new.get("agency_explicitly_absent") is True
    can_remove = not _has_any_agency(old_agency) or _has_any_agency(new_agency) or (explicit_absent and reliable is not False and _quality_allows_explicit_absence(new))
    if can_remove and _agency_identity_changed(old_agency, new_agency):
        _append_event(events, {"event_type": "agency_changed", "field": "agency", "old_value": _agent_event_value(old), "new_value": _agent_event_value(new), "severity": "normal", "should_notify": True}, context, suppress_notifications, "initial_agency_enrichment")

    for field in ("land_size_display", "land_size_sqm", "building_size_display", "building_size_sqm", "floor_area_display", "floor_area_sqm"):
        _append_scalar_field_change(
            events,
            old,
            new,
            field,
            "size_changed",
            context,
            suppress_notifications,
            severity="normal",
            notify=bool(config.NOTIFY_ON_SIZE_CHANGED),
        )

    for field in ("bedrooms", "bathrooms", "car_spaces", "property_type", "address"):
        _append_scalar_field_change(events, old, new, field, "property_attributes_changed", context, suppress_notifications)

    for field, keys, absent_key, reason in [
        ("inspection", ("inspection_short", "inspection_long"), "inspection_explicitly_absent", "initial_inspection_enrichment"),
        ("auction", ("auction_label", "auction_time", "auction_date", "auction_result"), "auction_explicitly_absent", "initial_auction_enrichment"),
    ]:
        old_value = tuple(normalize_change_value(old.get(k)) for k in keys)
        new_value = tuple(normalize_change_value(new.get(k)) for k in keys)
        if old_value == new_value:
            continue
        old_present, new_present = any(old_value), any(new_value)
        if old_present and not new_present and new.get(absent_key) is not True:
            continue
        if partial and not new_present and new.get(absent_key) is not True:
            continue
        event_value = _inspection_event_value if field == "inspection" else _auction_event_value
        _append_event(events, {"event_type": f"{field}_changed", "field": field, "old_value": event_value(old), "new_value": event_value(new), "severity": "low", "should_notify": True}, context, suppress_notifications, reason)

    old_desc = normalize_change_value(old.get("description_hash")) or hash_description(old.get("description"))
    new_desc = normalize_change_value(new.get("description_hash")) or hash_description(new.get("description"))
    if not (partial and "description" not in new and "description_hash" not in new) and old_desc != new_desc:
        initial = old_desc in {None, hashlib.sha256(b"").hexdigest()} and new_desc is not None
        _append_event(events, {"event_type": "description_changed", "field": "description", "old_value": old_desc, "new_value": new_desc, "severity": "low", "should_notify": True, **({"reason": "initial_description_enrichment"} if initial else {})}, context, suppress_notifications, "initial_description_enrichment")
    for event in events:
        event["event_payload"] = build_event_payload(new)
    return events
