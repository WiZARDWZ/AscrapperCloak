from __future__ import annotations

import re
from typing import Any
from urllib.parse import unquote_plus, urlparse

import config
from area_url_builder import parse_realestate_search_url
from db_layer import connect, get_existing_external_ids_for_search, ingest_light_check_rows
from realestate_errors import RealEstateBlockedError


LIGHT_CHECK_DEFAULT_MAX_PAGES = config.LIGHT_CHECK_DEFAULT_MAX_PAGES
LIGHT_CHECK_HARD_MAX_PAGES = config.LIGHT_CHECK_HARD_MAX_PAGES
BLOCKED_PAGE_STATES = {"blocked_http_429", "blocked_kpsdk", "blocked_access_denied", "partial_blocked"}
TECHNICAL_PAGE_STATES = {"render_timeout", "blank_render", "unknown"}
UNTRUSTED_STOP_REASONS = {
    "redirected_back",
    "normal_content_without_cards",
    "no_cards_timeout",
    "wrong_area",
    "wrong_area_current_url",
    "wrong_area_mismatch_heavy",
}


def _compact_area(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _slug_area(value: Any) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower())
    return re.sub(r"-+", "-", text).strip("-")


def _target_area(search_url: str) -> dict[str, str]:
    parsed = parse_realestate_search_url(search_url)
    suburb = str(parsed.get("suburb") or "").strip()
    state = str(parsed.get("state") or "").strip().upper()
    postcode = str(parsed.get("postcode") or "").strip()
    return {
        "suburb": suburb,
        "state": state,
        "postcode": postcode,
        "area_label": f"{suburb}, {state} {postcode}",
        "suburb_key": _compact_area(suburb),
        "suburb_slug": _slug_area(suburb),
    }


def _parse_address_area(address: Any) -> dict[str, str | None]:
    text = re.sub(r"\s+", " ", str(address or "").strip())
    if not text or text.upper() == "N/A":
        return {"suburb": None, "state": None, "postcode": None}
    match = re.search(r"([^,]+?)\s*,\s*([A-Z]{2,3})\s+(\d{4})(?:\b|$)", text, flags=re.I)
    if not match:
        return {"suburb": None, "state": None, "postcode": None}
    return {
        "suburb": re.sub(r"\s+", " ", match.group(1).strip()).title(),
        "state": match.group(2).upper(),
        "postcode": match.group(3),
    }


def _parse_listing_url_area(url: Any) -> dict[str, str | None]:
    text = str(url or "").strip()
    if not text or text.upper() == "N/A":
        return {"suburb": None, "state": None, "postcode": None}
    path = unquote_plus(urlparse(text).path or "").lower()
    match = re.search(r"/property-[^/]*-(act|nsw|nt|qld|sa|tas|vic|wa)-([a-z0-9-]+)-\d+", path, flags=re.I)
    if not match:
        return {"suburb": None, "state": None, "postcode": None}
    suburb_slug = match.group(2).strip("-")
    return {
        "suburb": suburb_slug.replace("-", " ").title(),
        "state": match.group(1).upper(),
        "postcode": None,
    }


def _current_url_matches_target(current_url: str | None, target: dict[str, str]) -> tuple[bool, str | None]:
    text = str(current_url or "").strip()
    if not text:
        return False, "missing_current_url"
    try:
        parsed = _target_area(text)
    except Exception:
        return False, "current_url_not_area_search"
    if parsed["state"] != target["state"]:
        return False, "current_url_wrong_state"
    if parsed["postcode"] != target["postcode"]:
        return False, "current_url_wrong_postcode"
    if parsed["suburb_key"] != target["suburb_key"]:
        return False, "current_url_wrong_suburb"
    return True, None


def row_matches_target_area(row: dict, target: dict[str, str]) -> dict[str, Any]:
    address_area = _parse_address_area(row.get("address"))
    url_area = _parse_listing_url_area(row.get("url") or row.get("listing_url"))
    reasons: list[str] = []

    if address_area.get("state") and address_area["state"] != target["state"]:
        reasons.append("wrong_state")
    if address_area.get("postcode") and address_area["postcode"] != target["postcode"]:
        reasons.append("wrong_postcode")
    if address_area.get("suburb") and _compact_area(address_area["suburb"]) != target["suburb_key"]:
        if address_area.get("postcode") == target["postcode"]:
            reasons.append("wrong_suburb_same_postcode")
        else:
            reasons.append("wrong_suburb")

    if url_area.get("state") and url_area["state"] != target["state"]:
        reasons.append("url_wrong_state")
    if url_area.get("suburb") and _compact_area(url_area["suburb"]) != target["suburb_key"]:
        reasons.append("url_wrong_suburb")

    if address_area.get("suburb") and url_area.get("suburb") and _compact_area(address_area["suburb"]) != _compact_area(url_area["suburb"]):
        reasons.append("address_url_suburb_conflict")
    if not address_area.get("suburb") and not url_area.get("suburb"):
        reasons.append("suburb_unknown")

    accepted = not reasons
    return {
        "accepted": accepted,
        "reason": "accepted" if accepted else reasons[0],
        "reasons": reasons,
        "address_area": address_area,
        "url_area": url_area,
        "target_area": target,
    }


def apply_target_area_guard(rows: list[dict], search_url: str, current_url: str | None = None) -> dict[str, Any]:
    target = _target_area(search_url)
    current_ok, current_reason = _current_url_matches_target(current_url or search_url, target)
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    rejection_reasons: dict[str, int] = {}

    if not current_ok:
        return {
            "target": target,
            "accepted_rows": [],
            "rejected_rows": rows,
            "rejection_reasons": {current_reason or "current_url_mismatch": len(rows) or 1},
            "trusted": False,
            "untrusted_reason": "wrong_area_current_url",
            "current_url_ok": False,
            "current_url_reason": current_reason,
        }

    for row in rows:
        verdict = row_matches_target_area(row, target)
        if verdict["accepted"]:
            safe = dict(row)
            safe["area_label"] = target["area_label"]
            safe["target_suburb"] = target["suburb"]
            safe["target_state"] = target["state"]
            safe["target_postcode"] = target["postcode"]
            accepted.append(safe)
        else:
            reason = str(verdict["reason"])
            rejection_reasons[reason] = rejection_reasons.get(reason, 0) + 1
            rejected.append({**row, "area_rejection_reason": reason, "area_rejection_reasons": verdict["reasons"]})

    mismatch_heavy = bool(rejected and len(rejected) > len(accepted))
    no_valid = bool(rejected and not accepted)
    trusted = not mismatch_heavy and not no_valid
    untrusted_reason = "wrong_area_mismatch_heavy" if mismatch_heavy else ("wrong_area" if no_valid else None)
    return {
        "target": target,
        "accepted_rows": accepted,
        "rejected_rows": rejected,
        "rejection_reasons": rejection_reasons,
        "trusted": trusted,
        "untrusted_reason": untrusted_reason,
        "current_url_ok": True,
        "current_url_reason": None,
    }


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
        "area_label": row.get("area_label"),
    }


def light_check_area(db_path: str, search_url: str, max_pages: int | None = None, timeout: int | None = None, full_scan: bool = False, dry_run: bool = False, on_log=None, enforce_target_area: bool = False) -> dict:
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
    area_rejected_rows: list[dict[str, Any]] = []
    area_rejection_reasons: dict[str, int] = {}
    area_untrusted_reason = None
    target_area = None
    last_current_url = None

    try:
        for page in range(1, effective_max_pages + 1):
            from module1_list_scraper import scrape_search_page
            rows, meta = scrape_search_page(search_url=search_url, page=page, timeout=timeout, on_log=on_log)
            pages_checked += 1
            total_pages_detected = meta.get("total_pages_detected") or total_pages_detected
            last_current_url = meta.get("current_url") or meta.get("url")
            raw_rows_count = len(rows)
            if enforce_target_area:
                guard = apply_target_area_guard(rows, search_url, current_url=last_current_url)
                target_area = guard["target"]
                rows = guard["accepted_rows"]
                area_rejected_rows.extend(guard["rejected_rows"])
                for reason, count in guard["rejection_reasons"].items():
                    area_rejection_reasons[reason] = area_rejection_reasons.get(reason, 0) + int(count)
                if not guard["trusted"]:
                    area_untrusted_reason = guard["untrusted_reason"] or "wrong_area"
                    stopped_reason = area_untrusted_reason
                    log(
                        "area_guard scan_trusted=False expected_area={area} target_suburb={suburb} target_state={state} "
                        "target_postcode={postcode} current_url={current} requested_url={requested} rows_extracted={raw} "
                        "rows_area_matched={matched} rows_area_rejected={rejected} rejection_reasons={reasons}".format(
                            area=target_area["area_label"], suburb=target_area["suburb"], state=target_area["state"],
                            postcode=target_area["postcode"], current=last_current_url, requested=meta.get("url"),
                            raw=raw_rows_count, matched=len(rows), rejected=len(guard["rejected_rows"]),
                            reasons=area_rejection_reasons,
                        )
                    )
                    break
            all_rows.extend(rows)
            log(
                "pagination page={page} requested_url={requested} current_url={current} cards_found={cards} "
                "rows_extracted={rows} rows_area_matched={matched} rows_area_rejected={rejected} total_rows={total_rows} total_pages_detected={total} has_next={has_next}".format(
                    page=page, requested=meta.get("url"), current=meta.get("current_url"),
                    cards=meta.get("cards_found", raw_rows_count), rows=raw_rows_count, matched=len(rows),
                    rejected=max(0, raw_rows_count - len(rows)), total_rows=len(all_rows),
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

        trusted_scan = blocked_reason is None and not errors and area_untrusted_reason is None
        if stopped_reason == "no_results":
            trusted_scan = True
        if stopped_reason in UNTRUSTED_STOP_REASONS:
            trusted_scan = False
        scan_status = "blocked_rate_limited" if blocked_reason else ("skipped_untrusted" if area_untrusted_reason or stopped_reason in UNTRUSTED_STOP_REASONS else ("valid_empty_result" if stopped_reason == "no_results" else ("technical_failure" if errors else "ok")))

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

        log(
            "pagination stop_reason={stop} pages_checked={pages} total_pages_detected={total} total_rows={rows} "
            "scan_trusted={trusted} scan_status={status} rows_area_matched={matched} rows_area_rejected={rejected} rejection_reasons={reasons}".format(
                stop=stopped_reason, pages=pages_checked, total=total_pages_detected if total_pages_detected is not None else "unknown",
                rows=len(all_rows), trusted=trusted_scan, status=scan_status, matched=len(all_rows),
                rejected=len(area_rejected_rows), reasons=area_rejection_reasons,
            )
        )
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
            "target_area": target_area,
            "current_url": last_current_url,
            "rows_area_matched": len(all_rows),
            "rows_area_rejected": len(area_rejected_rows),
            "area_rejection_reasons": area_rejection_reasons,
            "area_rejected_listings": [compact_listing_for_notification(row) | {"reason": row.get("area_rejection_reason")} for row in area_rejected_rows[:25]],
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
