"""Resolve user-friendly NSW suburb queries against dbo.NSWSuburbDirectory."""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any

DIGIT_TRANSLATION = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
REMOVED_TOKENS = {"nsw", "australia"}
NON_NSW_STATE_CODES = {"act", "nt", "qld", "sa", "tas", "vic", "wa"}


def normalize_area_text(text: str) -> str:
    value = str(text or "").translate(DIGIT_TRANSLATION).lower()
    value = re.sub(r"\bn\s*\.\s*s\s*\.\s*w\b", " nsw ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    tokens = [token for token in value.split() if token not in REMOVED_TOKENS]
    return " ".join(tokens)


def compact_area_text(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", normalize_area_text(text))


def normalize_postcode(value: Any) -> str | None:
    text = str(value or "").translate(DIGIT_TRANSLATION).strip()
    return text if re.fullmatch(r"\d{4}", text) else None


def extract_postcode_tokens(text: str) -> list[str]:
    normalized = str(text or "").translate(DIGIT_TRANSLATION)
    return list(dict.fromkeys(re.findall(r"(?<!\d)\d{4}(?!\d)", normalized)))


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9\s-]", "", str(value or "").lower())
    return re.sub(r"[-\s]+", "-", cleaned).strip("-")


def build_realestate_buy_url(suburb_name: str, postcode: str) -> str:
    postcode_value = normalize_postcode(postcode)
    suburb_slug = _slug(suburb_name)
    if not suburb_slug or postcode_value is None:
        raise ValueError("A NSW suburb name and four-digit postcode are required")
    return f"https://www.realestate.com.au/buy/in-{suburb_slug},+nsw+{postcode_value}/list-1?activeSort=list-date"


def _row_dicts(cur) -> list[dict[str, Any]]:
    columns = [str(column[0]) for column in cur.description]
    return [{columns[index]: row[index] for index in range(len(columns))} for row in cur.fetchall()]


def _find_value(row: dict[str, Any], *names: str) -> Any:
    lowered = {str(key).lower().replace("_", ""): value for key, value in row.items()}
    for name in names:
        if name.lower().replace("_", "") in lowered:
            return lowered[name.lower().replace("_", "")]
    return None


def _directory_rows(conn) -> list[dict[str, str]]:
    cur = conn.cursor()
    cur.execute("SELECT * FROM dbo.NSWSuburbDirectory")
    output: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for row in _row_dicts(cur):
        suburb = _find_value(row, "SuburbName", "Suburb", "Locality", "Name")
        postcode = normalize_postcode(_find_value(row, "Postcode", "PostalCode"))
        state = str(_find_value(row, "StateCode", "State", "StateAbbreviation") or "NSW").upper().strip()
        suburb_text = " ".join(str(suburb or "").split())
        if not suburb_text or postcode is None or state != "NSW":
            continue
        key = (suburb_text.lower(), postcode)
        if key not in seen:
            seen.add(key)
            output.append({"suburb_name": suburb_text, "postcode": postcode})
    return output


def _match(row: dict[str, str]) -> dict[str, str]:
    suburb = row["suburb_name"]
    postcode = row["postcode"]
    return {
        "suburb_name": suburb,
        "postcode": postcode,
        "state_code": "NSW",
        "label": f"{suburb}, NSW {postcode}",
        "search_url": build_realestate_buy_url(suburb, postcode),
    }


def _result(status: str, query: str, matches: list[dict[str, str]], message: str) -> dict[str, Any]:
    return {"status": status, "query": query, "matches": matches, "message": message}


def resolve_nsw_area_query(conn, query: str, limit: int = 10) -> dict[str, Any]:
    raw = str(query or "").strip()
    normalized = normalize_area_text(raw)
    safe_limit = max(1, min(int(limit or 10), 10))
    raw_tokens = set(re.sub(r"[^a-z]+", " ", raw.translate(DIGIT_TRANSLATION).lower()).split())
    if not normalized or raw.lower().startswith(("http://", "https://")) or raw_tokens & NON_NSW_STATE_CODES:
        return _result("invalid", raw, [], "Please send a NSW suburb name or postcode.")
    postcode_tokens = extract_postcode_tokens(raw)
    if len(postcode_tokens) > 1:
        return _result("invalid", raw, [], "Please send one NSW suburb or postcode.")
    postcode = postcode_tokens[0] if postcode_tokens else None
    name_query = " ".join(token for token in normalized.split() if token != postcode)
    compact_query = compact_area_text(name_query)
    rows = _directory_rows(conn)
    if postcode and not name_query:
        matches = [_match(row) for row in rows if row["postcode"] == postcode][:safe_limit]
        if len(matches) == 1:
            return _result("exact", raw, matches, "One NSW suburb matched that postcode.")
        if matches:
            return _result("multiple", raw, matches, "Several NSW suburbs matched that postcode.")
        return _result("not_found", raw, [], "No NSW suburb matched that postcode.")
    exact_rows = [row for row in rows if compact_area_text(row["suburb_name"]) == compact_query and (not postcode or row["postcode"] == postcode)]
    exact = [_match(row) for row in exact_rows][:safe_limit]
    if len(exact) == 1:
        return _result("exact", raw, exact, "One NSW suburb matched.")
    if exact:
        return _result("multiple", raw, exact, "Several NSW suburbs matched. Please choose one.")
    suggestions = []
    if compact_query:
        scored = sorted(
            ((SequenceMatcher(None, compact_query, compact_area_text(row["suburb_name"])).ratio(), row) for row in rows if not postcode or row["postcode"] == postcode),
            key=lambda item: (-item[0], item[1]["suburb_name"], item[1]["postcode"]),
        )
        suggestions = [_match(row) for score, row in scored if score >= 0.62][:safe_limit]
    if suggestions:
        return _result("suggestions", raw, suggestions, "No exact NSW suburb matched. Please choose a suggestion.")
    return _result("not_found", raw, [], "No NSW suburb matched that query.")
