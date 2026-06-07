from __future__ import annotations

import argparse
import os
import sys
from decimal import Decimal
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import config
import db_layer

SAMPLE_LIMIT = 25


def _as_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(Decimal(str(value)))
    except Exception:
        return None


def resolve_search_id(conn, user_area_id: int | None = None, search_id: int | None = None) -> int | None:
    if search_id is not None:
        return int(search_id)
    if user_area_id is None:
        return None
    sub = db_layer.get_user_area_subscription(conn, int(user_area_id))
    if not sub:
        raise ValueError(f"UserAreaID {user_area_id} was not found")
    return int(sub["SearchID"])


def fetch_price_rows(conn, search_id: int | None = None) -> list[dict]:
    params: list[Any] = []
    filters = [
        "(l.CurrentStatus IS NULL OR LOWER(l.CurrentStatus) IN ('active','unknown','under_offer'))",
        "(lss.Status IS NULL OR LOWER(lss.Status) NOT IN ('sold','withdrawn'))",
    ]
    if search_id is not None:
        filters.append("lss.SearchID = ?")
        params.append(int(search_id))
    search_filter = "WHERE " + " AND ".join(filters)
    sql = f"""
    WITH latest AS (
        SELECT
            ls.*,
            ROW_NUMBER() OVER (PARTITION BY ls.ListingID, ls.SearchID ORDER BY ls.SnapshotID DESC) AS rn
        FROM dbo.ListingSnapshot ls
    )
    SELECT
        l.listingID AS ListingID,
        CAST(l.ExternalID AS NVARCHAR(50)) AS ExternalID,
        COALESCE(p.Address, p.AddressRaw, p.AddressNormalized) AS Address,
        COALESCE(latest.PriceDisplay, l.CurrentPriceDisplay) AS PriceDisplay,
        latest.PriceLow AS EstimatedPriceLow,
        latest.PriceHigh AS EstimatedPriceHigh,
        latest.PriceMethod AS PriceMethod,
        COALESCE(latest.URL, l.ListingURL) AS ListingURL,
        lss.SearchID AS SearchID,
        latest.SnapshotID AS SnapshotID,
        latest.Price AS SnapshotPrice,
        pt.PropertyType AS PropertyType
    FROM dbo.ListingSearchState lss
    JOIN dbo.Listing l ON l.listingID = lss.ListingID
    LEFT JOIN latest ON latest.ListingID = l.listingID AND latest.SearchID = lss.SearchID AND latest.rn = 1
    LEFT JOIN dbo.Property p ON p.PropertyID = l.PropertyID
    LEFT JOIN dbo.PropertyType pt ON pt.ID = p.PropertyTypeID
    {search_filter}
    ORDER BY lss.SearchID, l.listingID
    """
    cur = conn.cursor()
    cur.execute(sql, *params)
    cols = [col[0] for col in cur.description]
    return [{cols[i]: row[i] for i in range(len(cols))} for row in cur.fetchall()]


def analyze_price_row(row: dict) -> dict:
    display = row.get("PriceDisplay")
    current_low = row.get("EstimatedPriceLow")
    current_high = row.get("EstimatedPriceHigh")
    parsed_low, parsed_high = db_layer.parse_price_range(display)
    current_reasons = db_layer.assess_price_quality(
        display,
        current_low,
        current_high,
        row.get("PriceMethod"),
        row.get("PropertyType"),
    )
    if db_layer.price_text_has_no_price_phrase(display) and (current_low in (None, "") and current_high in (None, "")):
        current_reasons.append("no_price_phrase")
    if db_layer.price_text_has_date_or_deadline(display) and current_reasons:
        current_reasons.append("date_or_deadline_present")
    return {
        **row,
        "ParsedLow": parsed_low,
        "ParsedHigh": parsed_high,
        "SuspicionReasons": sorted(set(current_reasons)),
    }


def build_audit(rows: list[dict]) -> dict:
    analyzed = [analyze_price_row(row) for row in rows]
    total = len(analyzed)
    with_display = [row for row in analyzed if str(row.get("PriceDisplay") or "").strip()]
    with_numeric = [row for row in analyzed if row.get("EstimatedPriceLow") is not None or row.get("EstimatedPriceHigh") is not None]
    display_no_numeric = [row for row in with_display if row.get("EstimatedPriceLow") is None and row.get("EstimatedPriceHigh") is None]
    suspicious = [row for row in analyzed if row["SuspicionReasons"] and any(reason != "no_price_phrase" for reason in row["SuspicionReasons"])]
    no_price = [row for row in analyzed if db_layer.price_text_has_no_price_phrase(row.get("PriceDisplay"))]
    date_derived = [row for row in suspicious if any(reason in row["SuspicionReasons"] for reason in ("date_deadline_numeric_price", "date_or_deadline_present"))]
    high_less_low = [row for row in analyzed if "high_less_than_low" in row["SuspicionReasons"]]
    low_under = [row for row in analyzed if "low_under_100000" in row["SuspicionReasons"]]
    return {
        "total_listings": total,
        "listings_with_price_display": len(with_display),
        "listings_with_numeric_estimated_price": len(with_numeric),
        "listings_with_price_display_but_no_numeric_estimate": len(display_no_numeric),
        "suspicious_numeric_estimates_count": len(suspicious),
        "no_price_phrase_count": len(no_price),
        "date_derived_suspicious_count": len(date_derived),
        "high_less_than_low_count": len(high_less_low),
        "low_under_100000_count": len(low_under),
        "suspicious_rows": suspicious,
        "rows": analyzed,
    }


def print_audit(audit: dict, sample_limit: int = SAMPLE_LIMIT) -> None:
    print(f"total listings: {audit['total_listings']}")
    print(f"listings with price_display: {audit['listings_with_price_display']}")
    print(f"listings with numeric estimated price: {audit['listings_with_numeric_estimated_price']}")
    print(f"listings with price_display but no numeric estimate: {audit['listings_with_price_display_but_no_numeric_estimate']}")
    print(f"suspicious numeric estimates count: {audit['suspicious_numeric_estimates_count']}")
    print(f"no-price phrase count: {audit['no_price_phrase_count']}")
    print(f"date-derived suspicious count: {audit['date_derived_suspicious_count']}")
    print(f"rows where estimated_price_high < estimated_price_low: {audit['high_less_than_low_count']}")
    print(f"rows where estimated_price_low < 100000: {audit['low_under_100000_count']}")
    print("\ntop sample suspicious rows:")
    for row in audit["suspicious_rows"][:sample_limit]:
        print("- " + " | ".join([
            f"ListingID={row.get('ListingID')}",
            f"ExternalID={row.get('ExternalID')}",
            f"Address={row.get('Address')}",
            f"PriceDisplay={row.get('PriceDisplay')}",
            f"EstimatedPriceLow={_as_int(row.get('EstimatedPriceLow'))}",
            f"EstimatedPriceHigh={_as_int(row.get('EstimatedPriceHigh'))}",
            f"PriceMethod={row.get('PriceMethod')}",
            f"ListingURL={row.get('ListingURL')}",
            f"SuspicionReason={','.join(row.get('SuspicionReasons') or [])}",
        ]))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit parser-derived price quality using SearchID scope.")
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--user-area-id", type=int)
    scope.add_argument("--search-id", type=int)
    scope.add_argument("--all", action="store_true")
    parser.add_argument("--sample-limit", type=int, default=SAMPLE_LIMIT)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> dict:
    args = parse_args(argv)
    conn = db_layer.connect(config.DB_PATH)
    try:
        search_id = None if args.all else resolve_search_id(conn, args.user_area_id, args.search_id)
        rows = fetch_price_rows(conn, search_id=search_id)
        audit = build_audit(rows)
        print_audit(audit, sample_limit=max(0, args.sample_limit))
        return audit
    finally:
        conn.close()


if __name__ == "__main__":
    main()
