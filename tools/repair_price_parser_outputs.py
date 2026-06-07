from __future__ import annotations

import argparse
import os
import sys
from decimal import Decimal
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import config
import db_layer
from tools.audit_price_quality import analyze_price_row, fetch_price_rows, resolve_search_id


def _decimal_or_none(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _price_value(low: Decimal | None, high: Decimal | None) -> Decimal | None:
    return low if low is not None and high is not None and low == high else None


def _method_for(parsed_low: Decimal | None, parsed_high: Decimal | None) -> str:
    return "parsed_display" if parsed_low is not None or parsed_high is not None else "unknown"


def _same_decimal(a: Any, b: Any) -> bool:
    return _decimal_or_none(a) == _decimal_or_none(b)


def _is_suspicious(reasons: list[str]) -> bool:
    return any(reason != "no_price_phrase" for reason in reasons)


def repair_plan_for_row(row: dict) -> dict:
    analyzed = analyze_price_row(row)
    new_low = analyzed["ParsedLow"]
    new_high = analyzed["ParsedHigh"]
    new_method = _method_for(new_low, new_high)
    new_price = _price_value(new_low, new_high)
    reasons = analyzed["SuspicionReasons"]
    old_low = row.get("EstimatedPriceLow")
    old_high = row.get("EstimatedPriceHigh")
    value_changed = not _same_decimal(old_low, new_low) or not _same_decimal(old_high, new_high)
    method_only = (
        not value_changed
        and str(row.get("PriceMethod") or "unknown") != new_method
    )
    price_value_changed = not _same_decimal(row.get("SnapshotPrice"), new_price)
    suspicious = _is_suspicious(reasons)
    return {
        **row,
        "OldEstimatedPriceLow": old_low,
        "OldEstimatedPriceHigh": old_high,
        "OldPriceMethod": row.get("PriceMethod"),
        "OldSnapshotPrice": row.get("SnapshotPrice"),
        "NewEstimatedPriceLow": new_low,
        "NewEstimatedPriceHigh": new_high,
        "NewPriceMethod": new_method,
        "NewSnapshotPrice": new_price,
        "SuspicionReasons": reasons,
        "IsSuspicious": suspicious,
        "ValueChanged": value_changed,
        "MethodOnly": method_only,
        "PriceValueChanged": price_value_changed,
    }


def build_repair_plan(
    rows: list[dict],
    include_method_only: bool = False,
    only_suspicious: bool = False,
) -> tuple[list[dict], dict]:
    candidates = [repair_plan_for_row(row) for row in rows]
    if only_suspicious:
        candidates = [row for row in candidates if row["IsSuspicious"]]
    suspicious_count = sum(1 for row in candidates if row["IsSuspicious"])
    value_changed_count = sum(1 for row in candidates if row["ValueChanged"])
    method_only_count = sum(1 for row in candidates if row["MethodOnly"])
    will_update: list[dict] = []
    method_only_skipped = 0
    for row in candidates:
        if row["ValueChanged"] or row["IsSuspicious"]:
            will_update.append(row)
        elif row["MethodOnly"]:
            if include_method_only:
                will_update.append(row)
            else:
                method_only_skipped += 1
    summary = {
        "total_candidates": len(candidates),
        "suspicious_rows": suspicious_count,
        "value_changed_rows": value_changed_count,
        "method_only_rows": method_only_count,
        "method_only_skipped_rows": method_only_skipped,
        "rows_that_will_update": len(will_update),
    }
    return will_update, summary


def apply_repair_plan(conn, plan: list[dict]) -> int:
    cur = conn.cursor()
    updated = 0
    for item in plan:
        snapshot_id = item.get("SnapshotID")
        listing_id = item.get("ListingID")
        if snapshot_id is None:
            continue
        cur.execute(
            """
            UPDATE dbo.ListingSnapshot
            SET Price=?, PriceLow=?, PriceHigh=?, PriceMethod=?
            WHERE SnapshotID=?
            """,
            item["NewSnapshotPrice"],
            item["NewEstimatedPriceLow"],
            item["NewEstimatedPriceHigh"],
            item["NewPriceMethod"],
            int(snapshot_id),
        )
        if listing_id is not None:
            cur.execute(
                "UPDATE dbo.Listing SET Price=?, UpdatedAt=SYSDATETIME() WHERE listingID=?",
                item["NewSnapshotPrice"],
                int(listing_id),
            )
        updated += 1
    conn.commit()
    return updated


def _fmt(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, Decimal):
        return str(int(value)) if value == value.to_integral_value() else str(value)
    return str(value)


def _print_row(item: dict) -> None:
    print("- " + " | ".join([
        f"ListingID={item.get('ListingID')}",
        f"ExternalID={item.get('ExternalID')}",
        f"PriceDisplay={item.get('PriceDisplay')}",
        f"old_low={_fmt(item.get('OldEstimatedPriceLow'))}",
        f"old_high={_fmt(item.get('OldEstimatedPriceHigh'))}",
        f"old_method={item.get('OldPriceMethod')}",
        f"new_low={_fmt(item.get('NewEstimatedPriceLow'))}",
        f"new_high={_fmt(item.get('NewEstimatedPriceHigh'))}",
        f"new_method={item.get('NewPriceMethod')}",
        f"reason={','.join(item.get('SuspicionReasons') or ['parser_output_diff'])}",
    ]))


def print_plan(plan: list[dict], summary: dict, dry_run: bool, sample_limit: int, rows_updated: int = 0) -> None:
    print(f"mode: {'dry-run' if dry_run else 'apply'}")
    print(f"total candidates: {summary['total_candidates']}")
    print(f"suspicious rows: {summary['suspicious_rows']}")
    print(f"value_changed rows: {summary['value_changed_rows']}")
    print(f"method_only rows: {summary['method_only_rows']}")
    print(f"method_only_skipped rows: {summary['method_only_skipped_rows']}")
    print(f"rows_that_will_update: {summary['rows_that_will_update']}")
    print(f"rows_updated: {rows_updated}")
    suspicious_rows = [row for row in plan if row["IsSuspicious"]]
    if suspicious_rows:
        print("\nsuspicious rows:")
        for item in suspicious_rows:
            _print_row(item)
    sample_rows = [row for row in plan if not row["IsSuspicious"]]
    if sample_rows and sample_limit:
        print("\nother update samples:")
        for item in sample_rows[:sample_limit]:
            _print_row(item)


def _filter_external_ids(rows: list[dict], external_ids: set[str]) -> list[dict]:
    if not external_ids:
        return rows
    return [row for row in rows if str(row.get("ExternalID") or "").strip() in external_ids]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Safely repair parser-derived price fields without notifications.")
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--user-area-id", type=int)
    scope.add_argument("--search-id", type=int)
    scope.add_argument("--all", action="store_true")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--include-method-only", action="store_true", help="also update rows where only PriceMethod would change")
    parser.add_argument("--only-suspicious", action="store_true", help="only repair rows flagged suspicious by the audit")
    parser.add_argument("--external-id", action="append", default=[], help="inspect/repair a specific external listing id; repeatable")
    parser.add_argument("--external-ids", default="", help="comma-separated external listing ids to inspect/repair")
    parser.add_argument("--sample-limit", type=int, default=25)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> dict:
    args = parse_args(argv)
    conn = db_layer.connect(config.DB_PATH)
    try:
        search_id = None if args.all else resolve_search_id(conn, args.user_area_id, args.search_id)
        rows = fetch_price_rows(conn, search_id=search_id)
        external_ids = {str(v).strip() for v in args.external_id if str(v).strip()}
        external_ids.update({part.strip() for part in str(args.external_ids or "").split(",") if part.strip()})
        rows = _filter_external_ids(rows, external_ids)
        plan, summary = build_repair_plan(rows, include_method_only=args.include_method_only, only_suspicious=args.only_suspicious)
        updated = 0
        if args.apply and plan:
            updated = apply_repair_plan(conn, plan)
        elif args.dry_run:
            conn.rollback()
        print_plan(plan, summary, dry_run=args.dry_run, sample_limit=max(0, args.sample_limit), rows_updated=updated)
        return {**summary, "updated_count": updated, "dry_run": bool(args.dry_run)}
    finally:
        conn.close()


if __name__ == "__main__":
    main()
