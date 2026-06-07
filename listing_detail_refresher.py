from __future__ import annotations

from typing import Any

import config
import db_layer

ENRICH_DETAIL_ROWS_FUNC = None


RETRYABLE_DETAIL_ERROR_MARKERS = (
    "get_failed", "http_error_429", "429", "timeout", "renderer", "net::",
    "err_internet_disconnected", "err_network_changed", "connection",
    "temporarily blocked", "page_not_ready", "blocked_after_retries", "detail_render_timeout",
)


def is_retryable_detail_error(error_text: str) -> bool:
    text = str(error_text or "").lower()
    return any(marker in text for marker in RETRYABLE_DETAIL_ERROR_MARKERS)


def is_retryable_detail_batch_failure(result: dict[str, Any]) -> bool:
    processed = int(result.get("processed_count") or 0)
    failed = int(result.get("failed_count") or 0)
    if processed <= 0 or failed / processed < 0.8:
        return False
    errors = result.get("errors") or []
    return any(is_retryable_detail_error(error.get("error") if isinstance(error, dict) else error) for error in errors)


def _clamp_limit(limit: int | None) -> int:
    val = int(limit or config.DETAIL_REFRESH_DEFAULT_LIMIT)
    val = max(1, val)
    return min(val, int(config.DETAIL_REFRESH_HARD_LIMIT))


def _detail_refresh_failed(row: dict[str, Any]) -> bool:
    return row.get("detail_refresh_success") is False or row.get("detail_extraction_quality") == "failed" or bool(row.get("detail_error"))


def _failed_item(row: dict[str, Any]) -> dict[str, Any]:
    error = row.get("detail_refresh_error") or row.get("detail_error") or "detail_refresh_failed"
    return {
        "external_id": row.get("external_id") or row.get("listing_id"),
        "db_listing_id": row.get("db_listing_id") or row.get("internal_listing_id"),
        "url": row.get("url"),
        "address": row.get("address"),
        "status": "failed",
        "error": error,
        "detail_refresh_error": error,
        "events_detected": [],
        "events_created": 0,
        "should_notify_events": [],
        "warnings": [{"warning": "detail_refresh_failed", "error": error}],
    }


def refresh_active_listings(
    search_url: str,
    limit: int | None = None,
    stale_hours: int | None = None,
    dry_run: bool = False,
    listing_external_id: str | None = None,
    timeout: int | None = None,
    sleep_between: float | None = None,
    on_log=None,
    context: str | None = None,
    suppress_notifications: bool = False,
    subscription: dict | None = None,
) -> dict:
    safe_limit = _clamp_limit(limit)
    stale = config.DETAIL_REFRESH_STALE_HOURS if stale_hours is None else stale_hours
    stale_filter_enabled = stale is not None and int(stale) > 0
    result: dict[str, Any] = {
        "search_url": search_url,
        "dry_run": dry_run,
        "limit": safe_limit,
        "stale_hours": stale,
        "selection_strategy": "oldest_first",
        "stale_filter_enabled": stale_filter_enabled,
        "first_candidate_listing_ids": [],
        "first_candidate_last_detail_refresh_at": [],
        "candidates_count": 0,
        "processed_count": 0,
        "refreshed_count": 0,
        "failed_count": 0,
        "events_created": 0,
        "should_notify_events": [],
        "items": [],
        "errors": [],
    }
    conn = db_layer.connect(config.DB_PATH)
    try:
        candidates = db_layer.get_active_listings_for_detail_refresh(
            conn, search_url=search_url, limit=safe_limit, stale_hours=stale, listing_external_id=listing_external_id, subscription=subscription
        )
        area_label = db_layer.clean_text((subscription or {}).get("AreaLabel") or (subscription or {}).get("area_label"), 255)
        if area_label:
            for candidate in candidates:
                candidate.setdefault("area_label", area_label)
        result["candidates_count"] = len(candidates)
        result["first_candidate_listing_ids"] = [c.get("listing_id") or c.get("external_id") or c.get("db_listing_id") for c in candidates[:10]]
        result["first_candidate_last_detail_refresh_at"] = [c.get("last_detail_refresh_at") for c in candidates[:10]]
        if not candidates:
            if listing_external_id:
                reason = db_layer.get_detail_refresh_skip_reason(conn, search_url, listing_external_id)
                if reason:
                    result["errors"].append(reason)
            return result
    finally:
        conn.close()

    enrich_func = ENRICH_DETAIL_ROWS_FUNC
    if enrich_func is None:
        from module3_enrich_details import enrich_detail_rows as enrich_func
    enrich_kwargs = {
        "output_dir": config.OUTPUT_DIR,
        "wait_timeout": timeout or config.DETAIL_REFRESH_TIMEOUT,
        "sleep_between": sleep_between if sleep_between is not None else config.DETAIL_REFRESH_SLEEP_BETWEEN,
        "on_log": on_log,
    }
    enriched_rows = enrich_func(candidates, **enrich_kwargs)
    initial_failed = [row for row in enriched_rows if _detail_refresh_failed(row)]
    if enriched_rows and len(initial_failed) / len(enriched_rows) >= 0.8 and any(is_retryable_detail_error(_failed_item(row)["error"]) for row in initial_failed):
        # module3 owns browser lifecycle; reinvoking it forces a fresh driver/recovery boundary.
        enriched_rows = enrich_func(candidates, **enrich_kwargs)
        result["immediate_retry_performed"] = True
    result["processed_count"] = len(enriched_rows)
    failed_rows = [r for r in enriched_rows if _detail_refresh_failed(r)]
    successful_rows = [r for r in enriched_rows if not _detail_refresh_failed(r)]
    result["refreshed_count"] = len(successful_rows)
    result["failed_count"] = len(failed_rows)
    for row in failed_rows:
        item = _failed_item(row)
        result["items"].append(item)
        result["errors"].append({
            "external_id": item.get("external_id"),
            "db_listing_id": item.get("db_listing_id"),
            "error": item.get("error"),
        })

    if dry_run:
        conn = db_layer.connect(config.DB_PATH)
        try:
            for row in successful_rows:
                try:
                    det = db_layer.detect_and_record_changes_for_row(conn, search_url, row, run_id=None, create_events=False, context=context, suppress_notifications=suppress_notifications)
                except Exception as e:
                    result["errors"].append(str(e))
                    continue
                item = {
                    "external_id": det.get("external_id"),
                    "db_listing_id": row.get("db_listing_id") or row.get("internal_listing_id"),
                    "url": row.get("url"),
                    "address": row.get("address"),
                    "status": row.get("current_status") or row.get("status"),
                    "events_detected": det.get("events_detected", []),
                    "events_created": 0,
                    "should_notify_events": det.get("should_notify_events", []),
                    "warnings": det.get("warnings", []),
                }
                result["items"].append(item)
                result["should_notify_events"].extend(det.get("should_notify_events", []))
                for warning in det.get("warnings", []):
                    result["errors"].append(warning)
            conn.rollback()
        finally:
            conn.close()
        return result

    ingest = db_layer.ingest_detail_refresh_rows(config.DB_PATH, search_url, successful_rows, dry_run=False, context=context, suppress_notifications=suppress_notifications)
    result["events_created"] = ingest.get("events_created", 0)
    for item in ingest.get("items", []):
        result_item = {
            "external_id": item.get("external_id"),
            "db_listing_id": item.get("db_listing_id"),
            "url": next((r.get("url") for r in enriched_rows if str(r.get("external_id")) == str(item.get("external_id"))), None),
            "address": next((r.get("address") for r in enriched_rows if str(r.get("external_id")) == str(item.get("external_id"))), None),
            "status": next((r.get("current_status") for r in enriched_rows if str(r.get("external_id")) == str(item.get("external_id"))), None),
            "events_detected": item.get("events_detected", []),
            "events_created": item.get("events_created", 0),
            "should_notify_events": item.get("should_notify_events", []),
            "warnings": item.get("warnings", []),
        }
        result["items"].append(result_item)
        result["should_notify_events"].extend(result_item["should_notify_events"])
        for warning in result_item["warnings"]:
            result["errors"].append(warning)
    return result
