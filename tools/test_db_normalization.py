from pathlib import Path
import sys
from decimal import Decimal

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db_layer import normalize_listing_row, parse_price_range


def run():
    assert parse_price_range("Auction Guide $1,500,000") == (Decimal("1500000"), Decimal("1500000"))
    assert parse_price_range("Auction Guide $700k") == (Decimal("700000"), Decimal("700000"))
    assert parse_price_range("Auction Guide $500,000 - $550,000") == (Decimal("500000"), Decimal("550000"))
    assert parse_price_range("Auction") == (None, None)
    assert parse_price_range("Contact Agent") == (None, None)

    rows = [
        {
            "listing_id": "999000001",
            "price": "N/A",
            "address": "1 Test St",
            "bedrooms": "N/A",
            "bathrooms": "2",
            "parking": "1",
            "property_type": "House",
            "agency": "Test Agency",
            "url": "https://example.com/property-999000001",
        },
        {
            "listing_id": "999000002",
            "price": "$1.2m",
            "address": "2 Test St",
            "bedrooms": "3",
            "bathrooms": "N/A",
            "parking": "no parking",
            "property_type": "Apartment",
            "agency": "Test Agency",
            "url": "https://example.com/property-999000002",
        },
    ]
    n1 = normalize_listing_row(rows[0])
    n2 = normalize_listing_row(rows[1])

    assert n1["bedrooms"] is None
    assert n1["bathrooms"] == 2
    assert n1["parking"] == 1
    assert n1["price_value"] is None
    assert n1["price_low"] is None and n1["price_high"] is None

    assert n2["bedrooms"] == 3
    assert n2["bathrooms"] is None
    assert n2["parking"] == 0
    assert int(n2["price_value"]) == 1200000
    assert int(n2["price_low"]) == 1200000
    assert int(n2["price_high"]) == 1200000

    n3 = normalize_listing_row({
        "listing_id": "999000003",
        "price": "$750,000-$1,000,000",
        "detail_price_display": "Auction - Contact Agent",
        "price_inferred_low": 750000,
        "price_inferred_high": 1000000,
        "price_inferred_method": "sliding_between_window",
        "address": "3 Test St",
        "url": "https://example.com/property-999000003",
    })
    assert n3["price_display"] == "Auction - Contact Agent"
    assert n3["price_low"] == Decimal("750000")
    assert n3["price_high"] == Decimal("1000000")
    assert n3["price_method"] == "sliding_between_window"
    assert n3["price_value"] is None

    n4 = normalize_listing_row({
        "listing_id": "999000004",
        "price": "$750,000-$1,000,000",
        "detail_price_display": "Guide: $1,100,000",
        "price_inferred_low": 750000,
        "price_inferred_high": 1000000,
        "price_inferred_method": "sliding_between_window",
        "address": "4 Test St",
        "url": "https://example.com/property-999000004",
    })
    assert n4["price_display"] == "Guide: $1,100,000"
    assert n4["price_low"] == Decimal("1100000")
    assert n4["price_high"] == Decimal("1100000")
    assert n4["price_method"] == "direct_from_pdp"
    assert n4["price_value"] == Decimal("1100000")

    n5 = normalize_listing_row({
        "listing_id": "999000005",
        "price": "N/A",
        "detail_price_display": "Guide: $1,100,000",
        "address": "5 Test St",
        "url": "https://example.com/property-999000005",
    })
    assert n5["price_display"] == "Guide: $1,100,000"
    assert n5["price_low"] == Decimal("1100000")
    assert n5["price_high"] == Decimal("1100000")
    assert n5["price_method"] == "direct_from_pdp"

    n6 = normalize_listing_row({
        "listing_id": "999000006",
        "price": "Auction",
        "detail_price_display": "Auction - Contact Agent",
        "address": "6 Test St",
        "url": "https://example.com/property-999000006",
    })
    assert n6["price_display"] == "Auction - Contact Agent"
    assert n6["price_low"] is None and n6["price_high"] is None
    assert n6["price_method"] == "unknown"

    print("Normalization test passed")


if __name__ == "__main__":
    run()
