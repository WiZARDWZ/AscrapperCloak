from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse, unquote_plus

REAL_ESTATE_BASE = "https://www.realestate.com.au"
STATE_CODES = {"ACT", "NSW", "NT", "QLD", "SA", "TAS", "VIC", "WA"}


def _clean_space(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def _slug_part(value: str) -> str:
    return _clean_space(value).lower().replace(" ", "-")


def build_realestate_buy_url(suburb: str, state: str, postcode: str) -> str:
    suburb_clean = _clean_space(suburb)
    state_clean = _clean_space(state).upper()
    postcode_clean = _clean_space(postcode)
    if not suburb_clean or state_clean not in STATE_CODES or not re.fullmatch(r"\d{4}", postcode_clean):
        raise ValueError("Area must include suburb, Australian state code, and 4 digit postcode")
    slug = f"{_slug_part(suburb_clean)},+{state_clean.lower()}+{postcode_clean}"
    return f"{REAL_ESTATE_BASE}/buy/in-{slug}/list-1?activeSort=list-date"


def parse_realestate_search_url(url: str) -> dict[str, Any]:
    text = (url or "").strip()
    parsed = urlparse(text)
    host = parsed.netloc.lower()
    if not host.endswith("realestate.com.au"):
        raise ValueError("Only realestate.com.au search URLs are supported")
    path = unquote_plus(parsed.path or "")
    match = re.search(r"/buy/in-([^/]+)/list-\d+", path, flags=re.I)
    if not match:
        raise ValueError("URL must look like a realestate.com.au buy search URL")
    raw_area = _clean_space(match.group(1).replace(",", " ").replace("+", " "))
    area = normalize_area_input(raw_area)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["activeSort"] = "list-date"
    canonical_path = f"/buy/in-{_slug_part(area['suburb'])},+{area['state'].lower()}+{area['postcode']}/list-1"
    canonical_url = urlunparse(("https", "www.realestate.com.au", canonical_path, "", urlencode(query), ""))
    return {**area, "search_url": canonical_url, "source": "realestate.com.au"}


def normalize_area_input(text: str) -> dict[str, Any]:
    raw = _clean_space((text or "").replace(",", " "))
    if not raw:
        raise ValueError("Please send a suburb, state and postcode")
    if raw.lower().startswith(("http://", "https://")):
        return parse_realestate_search_url(raw)
    match = re.match(r"^(.+?)\s+([A-Za-z]{2,3})\s+(\d{4})$", raw)
    if not match:
        raise ValueError("Please use a format like: Petersham NSW 2049")
    suburb = _clean_space(match.group(1)).title()
    state = match.group(2).upper()
    postcode = match.group(3)
    search_url = build_realestate_buy_url(suburb, state, postcode)
    return {
        "suburb": suburb,
        "state": state,
        "postcode": postcode,
        "area_label": f"{suburb}, {state} {postcode}",
        "search_url": search_url,
        "source": "realestate.com.au",
    }


def area_label_from_url_or_input(value: str | dict[str, Any]) -> str:
    data = normalize_area_input(value) if isinstance(value, str) else value
    label = data.get("area_label")
    if label:
        return str(label)
    suburb = data.get("suburb") or "Unknown suburb"
    state = data.get("state") or data.get("state_code") or ""
    postcode = data.get("postcode") or ""
    return _clean_space(f"{suburb}, {state} {postcode}")
