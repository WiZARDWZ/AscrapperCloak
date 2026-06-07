from __future__ import annotations

from typing import Any

import config
from db_layer import connect, get_existing_external_ids_for_search, ingest_light_check_rows
from realestate_errors import RealEstateBlockedError


LIGHT_CHECK_DEFAULT_MAX_PAGES = config.LIGHT_CHECK_DEFAULT_MAX_PAGES
LIGHT_CHECK_HARD_MAX_PAGES = config.LIGHT_CHECK_HARD_MAX_PAGES
BLOCKED_PAGE_STATES = {"blocked_http_429", "blocked_kpsdk", "blocked_access_denied", "partial_blocked"}
TECHNICAL_PAGE_STATES = {"render_timeout", "blank_render", "unknown"}


def normalize_external_id(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.lower() in {"n/a", "na", "none", "null", "unknown", "-"}:
        return None
    return text


def detect_new_listing_rows(rows: list[dict], existing_ids: set[str]) -> list[dict]:
    out = []
    for row in rows:
        listing_id = normalize_external_id(row.get("listing_id") or row.get("external_id"))
        if listing_id and listing_id not in existing_ids:
            out.append(row)
    return out


def page_has_existing_listing(rows: list[dict], existing_ids: set[str]) -> bool:
    for row in rows:
        listing_id = normalize_external_id(row.get("listing_id") or row.get("external_id"))
        if listing_id and listing_id in existing_ids:
            return True
    return False


def compact_listing_for_notification(row: dict) -> dict:
    return {
        "listing_id": normalize_external_id(row.get("listing_id") or row.get("external_id")),
        "url": row.get("url"),
        "address": row.get("address"),
        "price": row.get("price"),
        "property_type": row.get("property_type"),
        "bedrooms": row.get("bedrooms"),
        "bathrooms": row.get("bathrooms"),
        "parking": row.get("parking"),
    }


def light_check_area(db_path: str, search_url: str, max_pages: int | None = None, timeout: int | None = None, full_scan: bool = False, dry_run: bool = False, on_log=None) -> dict:
    def log(msg: str) -> None:
        if on_log:
            try:
                on_log(msg)
            except Exception:
                pass

    if full_scan:
        effective_max_pages = 500 if max_pages is None else max(1, int(max_pages))
    else:
        effective_max_pages = LIGHT_CHECK_DEFAULT_MAX_PAGES if max_pages is None else max(1, int(max_pages))
        effective_max_pages = min(effective_max_pages, LIGHT_CHECK_HARD_MAX_PAGES)

    conn = connect(db_path)
    try:
        existing_ids = get_existing_external_ids_for_search(conn, search_url)
    finally:
        conn.close()

    all_rows: list[dict[str, Any]] = []
    new_rows_total: list[dict[str, Any]] = []
    pages_checked = 0
    stopped_reason = "max_pages_reached"
    total_pages_detected = None
    all_checked_pages_were_new = True
    errors: list[str] = []
    blocked_reason = None

    try:
        for page in range(1, effective_max_pages + 1):
            from module1_list_scraper import scrape_search_page
            rows, meta = scrape_search_page(search_url=search_url, page=page, timeout=timeout, on_log=on_log)
            pages_checked += 1
            total_pages_detected = meta.get("total_pages_detected") or total_pages_detected
            all_rows.extend(rows)
            log(
                "pagination page={page} requested_url={requested} current_url={current} cards_found={cards} "
                "rows_extracted={rows} total_rows={total_rows} total_pages_detected={total} has_next={has_next}".format(
                    page=page, requested=meta.get("url"), current=meta.get("current_url"),
                    cards=meta.get("cards_found", len(rows)), rows=len(rows), total_rows=len(all_rows),
                    total=total_pages_detected if total_pages_detected is not None else "unknown",
                    has_next=bool(meta.get("has_next_page")),
                )
            )
            page_new_rows = detect_new_listing_rows(rows, existing_ids)
            new_rows_total.extend(page_new_rows)

            if not rows:
                stopped_reason = meta.get("stop_reason") or "duplicate_or_empty_page"
                all_checked_pages_were_new = False
                if stopped_reason in BLOCKED_PAGE_STATES:
                    blocked_reason = stopped_reason
                    errors.append(stopped_reason)
                if stopped_reason in TECHNICAL_PAGE_STATES:
                    errors.append(stopped_reason)
                break

            page_found_existing = page_has_existing_listing(rows, existing_ids)
            if page_found_existing:
                all_checked_pages_were_new = False
                if not full_scan:
                    stopped_reason = "found_existing_listing"
                    break

            has_next = bool(meta.get("has_next_page"))
            if total_pages_detected is not None and page >= int(total_pages_detected):
                stopped_reason = "reached_total_pages"
                break
            if not has_next:
                stopped_reason = "no_next"
                break
            if page == effective_max_pages:
                stopped_reason = "max_pages_reached"
                break

        trusted_scan = blocked_reason is None and not errors
        if stopped_reason == "no_results":
            trusted_scan = True
        scan_status = "blocked_rate_limited" if blocked_reason else ("valid_empty_result" if stopped_reason == "no_results" else ("technical_failure" if errors else "ok"))

        run_id = None
        if not dry_run and all_rows and trusted_scan:
            new_ids = {
                normalize_external_id(r.get("listing_id") or r.get("external_id"))
                for r in new_rows_total
            }
            new_ids = {x for x in new_ids if x}
            ingest_summary = ingest_light_check_rows(
                db_path,
                search_url,
                all_rows,
                new_external_ids=new_ids,
                full_scan=full_scan,
            )
            run_id = ingest_summary.get("run_id")

        log(f"pagination stop_reason={stopped_reason} pages_checked={pages_checked} total_pages_detected={total_pages_detected if total_pages_detected is not None else 'unknown'} total_rows={len(all_rows)}")
        return {
            "search_url": search_url,
            "rows_scraped": len(all_rows),
            "pages_checked": pages_checked,
            "total_pages_detected": total_pages_detected,
            "stop_reason": stopped_reason,
            "scan_status": scan_status,
            "trusted_scan": trusted_scan,
            "page_state": stopped_reason if stopped_reason in ({"no_results"} | BLOCKED_PAGE_STATES | TECHNICAL_PAGE_STATES) else None,
            "existing_count_before": len(existing_ids),
            "new_count": len(new_rows_total),
            "new_listings": [compact_listing_for_notification(row) for row in new_rows_total],
            "run_id": run_id,
            "dry_run": dry_run,
            "stopped_reason": stopped_reason,
            "all_checked_pages_were_new": all_checked_pages_were_new,
            "excel_path": None,
            "errors": errors,
            "blocked_reason": blocked_reason,
        }
    except RealEstateBlockedError as exc:
        blocked_reason = getattr(exc, "reason", str(exc)) or "blocked"
        errors.append(blocked_reason)
        log(f"light_check_area blocked: {blocked_reason}")
        return {
            "search_url": search_url,
            "rows_scraped": len(all_rows),
            "pages_checked": pages_checked,
            "total_pages_detected": total_pages_detected,
            "stop_reason": blocked_reason,
            "scan_status": "blocked_rate_limited",
            "trusted_scan": False,
            "page_state": blocked_reason,
            "existing_count_before": len(existing_ids),
            "new_count": len(new_rows_total),
            "new_listings": [compact_listing_for_notification(row) for row in new_rows_total],
            "run_id": None,
            "dry_run": dry_run,
            "stopped_reason": blocked_reason,
            "all_checked_pages_were_new": all_checked_pages_were_new,
            "excel_path": None,
            "errors": errors,
            "blocked_reason": blocked_reason,
            "retry_after_seconds": int(getattr(exc, "retry_after_seconds", None) or getattr(config, "REA_RATE_LIMIT_BACKOFF_SECONDS", 21600)),
        }
    except Exception as exc:
        errors.append(str(exc))
        log(f"light_check_area error: {exc}")
        return {
            "search_url": search_url,
            "rows_scraped": len(all_rows),
            "pages_checked": pages_checked,
            "total_pages_detected": total_pages_detected,
            "stop_reason": stopped_reason,
            "scan_status": "technical_failure",
            "trusted_scan": False,
            "page_state": stopped_reason,
            "existing_count_before": len(existing_ids),
            "new_count": len(new_rows_total),
            "new_listings": [compact_listing_for_notification(row) for row in new_rows_total],
            "run_id": None,
            "dry_run": dry_run,
            "stopped_reason": "error",
            "all_checked_pages_were_new": all_checked_pages_were_new,
            "excel_path": None,
            "errors": errors,
            "blocked_reason": None,
        }
