import os
import sys
import types
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

sys.modules.setdefault(
    "chrome_options_helper",
    types.SimpleNamespace(build_chrome_driver=lambda *a, **k: None, cleanup_chrome_driver=lambda *a, **k: None),
)

import db_layer
from module2_infer_prices import parse_any_price_number
from module2_price_utils import price_needs_inference


PRICE_CASES = [
    ("Buyers Guide $12,000,000", 12000000, 12000000),
    ("Guide: $2,295,000 Per Block", 2295000, 2295000),
    ("Guide $1,125,000 to $1,245,000", 1125000, 1245000),
    ("Price Guide $2,850,000 - $2,950,000", 2850000, 2950000),
    ("Guiding Offers | $2,800,000 - $3,000,000", 2800000, 3000000),
    ("Buyer Guide $1.7M", 1700000, 1700000),
    ("$3.595m - Resort Style Amenities", 3595000, 3595000),
    ("$2.475m", 2475000, 2475000),
    ("PRICE GUIDE $5.15m - ALL OFFERS BY 30 JUNE", 5150000, 5150000),
    ("EOI $1.9 - $1.95 Submit all offers Ends 21/6", 1900000, 1950000),
    ("Auction 11th June. Guide: $1.95m", 1950000, 1950000),
    ("3,350,000", 3350000, 3350000),
    ("1,250,000", 1250000, 1250000),
    ("850,000 - 875,000", 850000, 875000),
    ("Guide 1,250,000", 1250000, 1250000),
]

NO_PRICE_CASES = [
    "EOI - ALL OFFERS BY 20 JUNE 2026",
    "Auction 22nd July",
    "Contact Agent",
    "CONTACT AGENT FOR PRICING",
    "Price on Application",
    "POA",
    "Expression of Interest",
    "A GRADE BEACHFRONT HOME",
    "JUST LISTED!",
    "Price/development/joint venture by negotiation.",
    "EOI Closing 22/06",
    "EOI - Closing 22/06",
    "EXPRESSIONS OF INTEREST CLOSING WED 17 JUNE AT 4PM",
    "20 JUNE 2026",
    "AUCTION | 18TH JUNE 4PM",
    "Expressions Of Interest I Closing Monday 8th June",
]


def _ints(pair):
    low, high = pair
    return (int(low) if low is not None else None, int(high) if high is not None else None)


def test_expected_price_examples():
    for text, low, high in PRICE_CASES:
        assert _ints(db_layer.parse_price_range(text)) == (low, high), text
        assert db_layer.parse_price_bounds_from_text(text) == (low, high), text
        assert parse_any_price_number(text) == low, text
        assert price_needs_inference(text) is False, text


def test_no_price_and_date_examples_do_not_parse():
    for text in NO_PRICE_CASES:
        assert db_layer.parse_price_range(text) == (None, None), text
        assert db_layer.parse_price_bounds_from_text(text) == (None, None), text
        assert parse_any_price_number(text) is None, text


def test_no_price_phrase_with_real_money_keeps_money_and_ignores_deadline():
    text = "PRICE GUIDE $5.15m - ALL OFFERS BY 30 JUNE"
    assert db_layer.price_text_has_no_price_phrase(text) is True
    assert db_layer.price_text_has_date_or_deadline(text) is True
    assert _ints(db_layer.parse_price_range(text)) == (5150000, 5150000)


def test_suffix_without_price_context_is_not_enough():
    assert db_layer.parse_price_range("Land size 2m frontage") == (None, None)


def test_suspicious_quality_flags_existing_bad_outputs():
    assert "date_deadline_numeric_price" in db_layer.assess_price_quality("EOI - ALL OFFERS BY 20 JUNE 2026", 20, 2026)
    assert "no_price_phrase_with_numeric_estimate" in db_layer.assess_price_quality("Auction 22nd July", 22, 22)
    assert "high_less_than_low" in db_layer.assess_price_quality("Guide", 1200000, 1100000)
    assert "low_under_100000" in db_layer.assess_price_quality("$20", 20, 20)


def test_repair_plan_skips_method_only_by_default():
    from tools.repair_price_parser_outputs import build_repair_plan

    rows = [
        {"ListingID": 1, "ExternalID": "method", "PriceDisplay": "Guide $1,200,000", "EstimatedPriceLow": 1200000, "EstimatedPriceHigh": 1200000, "PriceMethod": "direct_from_pdp", "SnapshotPrice": 1200000},
        {"ListingID": 2, "ExternalID": "bad", "PriceDisplay": "EOI - ALL OFFERS BY 20 JUNE 2026", "EstimatedPriceLow": 20, "EstimatedPriceHigh": 2026, "PriceMethod": "parsed_display", "SnapshotPrice": None},
        {"ListingID": 72, "ExternalID": "150892060", "PriceDisplay": "3,350,000", "EstimatedPriceLow": 3350000, "EstimatedPriceHigh": 3350000, "PriceMethod": "parsed_display", "SnapshotPrice": 3350000},
        {"ListingID": 148, "ExternalID": "204270892", "PriceDisplay": "Guide $1,125,000 to $1,245,000", "EstimatedPriceLow": 1125000, "EstimatedPriceHigh": 1125000, "PriceMethod": "parsed_display", "SnapshotPrice": 1125000},
    ]
    plan, summary = build_repair_plan(rows)
    assert summary["total_candidates"] == 4
    assert summary["suspicious_rows"] == 1
    assert summary["value_changed_rows"] == 2
    assert summary["method_only_rows"] == 1
    assert summary["method_only_skipped_rows"] == 1
    assert summary["rows_that_will_update"] == 2
    assert [row["ExternalID"] for row in plan] == ["bad", "204270892"]

    method_plan, method_summary = build_repair_plan(rows, include_method_only=True)
    assert method_summary["rows_that_will_update"] == 3
    assert [row["ExternalID"] for row in method_plan] == ["method", "bad", "204270892"]

    suspicious_plan, suspicious_summary = build_repair_plan(rows, only_suspicious=True)
    assert suspicious_summary["total_candidates"] == 1
    assert [row["ExternalID"] for row in suspicious_plan] == ["bad"]


def test_normalize_listing_row_uses_hardened_parser():
    row = {"external_id": "REA-1", "price": "Guide $1,125,000 to $1,245,000"}
    normalized = db_layer.normalize_listing_row(row)
    assert normalized["price_low"] == Decimal("1125000")
    assert normalized["price_high"] == Decimal("1245000")
    assert normalized["price_method"] == "parsed_display"

    no_price = db_layer.normalize_listing_row({"external_id": "REA-2", "price": "EOI - ALL OFFERS BY 20 JUNE 2026"})
    assert no_price["price_low"] is None and no_price["price_high"] is None
    assert no_price["price_method"] == "unknown"


def run_all():
    for name, func in sorted(globals().items()):
        if name.startswith("test_") and callable(func):
            func()
            print(f"PASS {name}")


if __name__ == "__main__":
    run_all()
