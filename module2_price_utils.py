import re
from db_layer import parse_price_range


def price_needs_inference(price_text: str | None) -> bool:
    s = (price_text or "").strip()
    s_lower = s.lower()
    if s_lower in {"", "n/a", "na", "-", "—"}:
        return True

    low, high = parse_price_range(s)
    if low is not None or high is not None:
        return False

    if any(token in s_lower for token in [
        "auction",
        "contact agent",
        "contact",
        "price on request",
        "expressions of interest",
        "eoi",
        "offers",
    ]):
        return True

    return not bool(re.search(r"\d", s))
