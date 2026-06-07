from __future__ import annotations

import re
from typing import Optional


AREA_RE = re.compile(
    r"(?P<value>\d+(?:,\d{3})*(?:\.\d+)?)\s*"
    r"(?P<unit>m\s*(?:²|2)|sqm|sq\s*m|square\s*met(?:re|er)s?|ha|hectares?)\b",
    re.IGNORECASE,
)


def parse_area_to_sqm(text: str | None) -> Optional[float]:
    if not text:
        return None
    match = AREA_RE.search(str(text).replace("\xa0", " "))
    if not match:
        return None
    try:
        value = float(match.group("value").replace(",", ""))
    except ValueError:
        return None
    unit = re.sub(r"\s+", "", match.group("unit").lower())
    if unit in {"ha", "hectare", "hectares"}:
        value *= 10000
    return int(value) if value.is_integer() else value


def extract_area_display(text: str | None) -> str | None:
    if not text:
        return None
    match = AREA_RE.search(str(text).replace("\xa0", " "))
    return match.group(0).strip() if match else None
