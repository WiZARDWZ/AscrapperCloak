from __future__ import annotations

import time
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo
import asyncio

import config
import db_layer
from monitor import baseline_setup_area
from area_light_checker import light_check_area
from listing_detail_refresher import is_retryable_detail_batch_failure, refresh_active_listings
from json_safe import json_safe
from realestate_errors import RealEstateBlockedError


def _utcnow() -> datetime:
    # Historical helper name retained for existing code paths. The project stores
    # SQL Server DATETIME2 values with SYSDATETIME(), so scheduler comparisons use
    # local-naive project time rather than UTC-naive time.
    return datetime.now()


def _fetch_sql_server_local_time(conn) -> datetime:
    cur = conn.cursor()
    cur.execute("SELECT SYSDATETIME()")
    row = cur.fetchone()
    return row[0]


def _is_due(last_value, interval: timedelta, now: datetime) -> bool:
    if last_value is None:
        return True
    return last_value <= now - interval


def _detail_retry_is_due(next_retry_at, now: datetime) -> bool:
    return next_retry_at is None or next_retry_at <= now


def _baseline_is_incomplete(summary: dict[str, Any]) -> bool:
    pages_checked = int(summary.get("pages_checked") or 0)
    total_pages = summary.get("total_pages_detected")
    stop_reason = summary.get("stop_reason") or summary.get("stopped_reason")
    return stop_reason == "max_pages_reached" and total_pages is not None and pages_checked < int(total_pages)


def run_initial_baseline_for_subscription(user_area_id: int, max_pages: int | None = None, dry_run: bool = False, on_log=None) -> dict[str, Any]:
    conn = db_layer.connect(config.DB_PATH)
    try:
        sub = db_layer.get_user_area_subscription(conn, user_area_id)
        if not sub:
            return {"user_area_id": user_area_id, "status": "missing", "errors": ["subscription not found"]}
        if dry_run:
            return {"user_area_id": user_area_id, "status": "dry_run", "search_url": sub.get("SearchURL"), "area_label": sub.get("AreaLabel"), "max_pages": config.INITIAL_BASELINE_MAX_PAGES if max_pages is None else max_pages}
        db_layer.mark_subscription_baseline_started(conn, user_area_id)
    finally:
        conn.close()

    try:
        baseline_max_pages = config.INITIAL_BASELINE_MAX_PAGES if max_pages is None else max_pages
        summary = light_check_area(config.DB_PATH, sub["SearchURL"], max_pages=baseline_max_pages, timeout=config.PIPELINE_TIMEOUT, full_scan=True, dry_run=False, on_log=on_log)
        safe_errors = [config.mask_sensitive_text(error) for error in summary.get("errors", [])]
        incomplete = _baseline_is_incomplete(summary)
        scan_status = summary.get("scan_status")
        trusted_scan = bool(summary.get("trusted_scan"))
        if incomplete:
            safe_errors.append("initial baseline incomplete: max_pages_reached before total_pages_detected")
        if scan_status == "blocked_rate_limited":
            safe_errors = safe_errors or [config.mask_sensitive_text(summary.get("blocked_reason") or summary.get("stop_reason") or "blocked_rate_limited")]
        elif not trusted_scan:
            safe_errors = safe_errors or [config.mask_sensitive_text(summary.get("stop_reason") or "untrusted_baseline_scan")]
        metrics = {
            "listings_collected": summary.get("rows_scraped", 0),
            "new_count": summary.get("new_count", 0),
            "pages_checked": summary.get("pages_checked", 0),
            "total_pages_detected": summary.get("total_pages_detected"),
            "stop_reason": summary.get("stop_reason") or summary.get("stopped_reason"),
        }
        conn = db_layer.connect(config.DB_PATH)
        try:
            if scan_status == "blocked_rate_limited":
                db_layer.mark_subscription_baseline_failed(conn, user_area_id, str(safe_errors), **metrics)
                status = "retry_wait"
            elif safe_errors:
                db_layer.mark_subscription_baseline_failed(conn, user_area_id, str(safe_errors), **metrics)
                status = "incomplete" if incomplete else "failed"
            else:
                db_layer.mark_subscription_baseline_completed(conn, user_area_id, **metrics)
                status = "completed"
        finally:
            conn.close()
        out = {"user_area_id": user_area_id, "status": status, "area_label": sub.get("AreaLabel"), "search_url": sub.get("SearchURL"), **metrics, "errors": safe_errors, "trusted_scan": trusted_scan, "scan_status": scan_status}
        if status == "retry_wait":
            out["retry_after_seconds"] = int(summary.get("retry_after_seconds") or getattr(config, "REA_RATE_LIMIT_BACKOFF_SECONDS", 21600))
        return out
    except RealEstateBlockedError as exc:
        safe_error = config.mask_sensitive_text(getattr(exc, "reason", str(exc)))
        conn = db_layer.connect(config.DB_PATH)
        try:
            db_layer.mark_subscription_baseline_failed(conn, user_area_id, safe_error)
        finally:
            conn.close()
        return {
            "user_area_id": user_area_id,
            "status": "retry_wait",
            "area_label": sub.get("AreaLabel"),
            "search_url": sub.get("SearchURL"),
            "errors": [safe_error],
            "retry_after_seconds": int(getattr(exc, "retry_after_seconds", None) or getattr(config, "REA_RATE_LIMIT_BACKOFF_SECONDS", 21600)),
        }
    except Exception as exc:
        safe_error = config.mask_sensitive_text(exc)
        conn = db_layer.connect(config.DB_PATH)
        try:
            db_layer.mark_subscription_baseline_failed(conn, user_area_id, safe_error)
        finally:
            conn.close()
        return {"user_area_id": user_area_id, "status": "failed", "errors": [safe_error]}


def _pages_checked_text(subscription: dict[str, Any]) -> str:
    checked = subscription.get("BaselinePagesChecked")
    total = subscription.get("BaselineTotalPagesDetected")
    return f"{checked}/{total}" if total is not None else str(checked if checked is not None else "Unknown")


def _baseline_summary_text(subscription: dict[str, Any], listings: int | str) -> str:
    return (
        "✅ Area baseline collected\n"
        f"{subscription.get('AreaLabel')}\n"
        f"Listings found: {listings}\n"
        f"Pages checked: {_pages_checked_text(subscription)}\n"
        "Now preparing detail baseline. Change alerts are paused until setup is complete."
    )


def _baseline_incomplete_text(subscription: dict[str, Any]) -> str:
    return (
        "⚠️ Baseline may be incomplete\n"
        f"{subscription.get('AreaLabel')}\n"
        f"Listings found so far: {subscription.get('BaselineListingsCollected', 0)}\n"
        f"Pages checked: {_pages_checked_text(subscription)}\n"
        f"Reason: {subscription.get('BaselineStopReason') or 'unknown'}\n"
        "Setup is paused. Increase INITIAL_BASELINE_MAX_PAGES or retry the baseline scan."
    )


def _send_baseline_incomplete_warning(subscription: dict[str, Any]) -> dict[str, Any]:
    base = {"user_area_id": int(subscription["UserAreaID"]), "summary_type": "baseline_incomplete", "chat_id": str(subscription.get("ChatID"))}
    if not config.TELEGRAM_BOT_TOKEN:
        return {**base, "status": "warning", "warning": "telegram token not set; incomplete baseline warning was not sent"}
    try:
        asyncio.run(_send_setup_summary(str(subscription.get("ChatID")), _baseline_incomplete_text(subscription)))
        return {**base, "status": "sent"}
    except Exception as exc:
        return {**base, "status": "failed", "error": config.mask_sensitive_text(exc)}


SETUP_SUMMARY_TEXT = {
    "baseline": lambda sub, listings: _baseline_summary_text(sub, listings),
    "detail_started": lambda sub, _listings: (
        "⏳ Preparing monitoring\n"
        f"{sub.get('AreaLabel')}\n"
        "I am collecting listing details in batches. You will not receive change alerts until setup is complete."
    ),
    "ready": lambda sub, _listings: _ready_summary_text(sub),
}


def _ready_summary_text(sub: dict[str, Any]) -> str:
    unknown_count = int(sub.get("PriceBaselineUnknownCount") or sub.get("unknown_count") or 0)
    inferred_count = int(sub.get("PriceBaselineInferredCount") or sub.get("inferred_count") or 0)
    total_count = sub.get("PriceBaselineTotalCount") or sub.get("BaselineListingsCollected") or sub.get("price_baseline_total_count")
    lines = ["✅ Monitoring is active", str(sub.get("AreaLabel") or "Search area"), ""]
    if total_count is not None:
        lines.append(f"I found {total_count} active listings.")
    lines.append(f"Price ranges were inferred for {inferred_count} listings.")
    if unknown_count:
        lines.append(f"{unknown_count} listings currently have Unknown price and will be retried automatically.")
    else:
        lines.append("All listing price ranges were inferred successfully.")
    lines.extend(["", "You will now receive notifications for new listings and changes."])
    return "\n".join(lines)


SETUP_SUMMARY_COLUMNS = {
    "baseline": "BaselineSummarySentAt",
    "detail_started": "DetailBaselineStartedSummarySentAt",
    "ready": "ReadySummarySentAt",
}


async def _send_setup_summary(chat_id: str, text: str) -> None:
    from telegram import Bot

    await Bot(config.TELEGRAM_BOT_TOKEN).send_message(chat_id=str(chat_id), text=text, disable_web_page_preview=True)


def _load_baseline_summary(subscription: dict[str, Any]) -> dict[str, Any]:
    conn = db_layer.connect(config.DB_PATH)
    try:
        return db_layer.get_user_area_baseline_summary(conn, int(subscription["UserAreaID"]))
    finally:
        conn.close()


def _baseline_listings_found(subscription: dict[str, Any]) -> int | str:
    persisted = subscription.get("BaselineListingsCollected")
    if persisted is not None:
        return int(persisted)
    try:
        summary = _load_baseline_summary(subscription)
    except Exception:
        summary = {}
    persisted = summary.get("baseline_listings_collected")
    if persisted is not None:
        return int(persisted)
    computed = summary.get("computed_listing_count")
    return int(computed) if computed is not None else "Unknown"


def _send_setup_summary_once(subscription: dict[str, Any], summary_type: str) -> dict[str, Any]:
    column_name = SETUP_SUMMARY_COLUMNS[summary_type]
    base = {"user_area_id": int(subscription["UserAreaID"]), "summary_type": summary_type, "chat_id": str(subscription.get("ChatID"))}
    if subscription.get(column_name):
        return {**base, "status": "already_sent"}
    if not config.TELEGRAM_BOT_TOKEN:
        return {**base, "status": "warning", "warning": "telegram token not set; setup summary was not sent"}
    listings_found = _baseline_listings_found(subscription) if summary_type == "baseline" else None
    try:
        asyncio.run(_send_setup_summary(str(subscription.get("ChatID")), SETUP_SUMMARY_TEXT[summary_type](subscription, listings_found)))
        conn = db_layer.connect(config.DB_PATH)
        try:
            db_layer.mark_subscription_setup_summary_sent(conn, int(subscription["UserAreaID"]), column_name)
        finally:
            conn.close()
        subscription[column_name] = _utcnow()
        return {**base, "status": "sent"}
    except Exception as exc:
        return {**base, "status": "failed", "error": config.mask_sensitive_text(exc)}


def _setup_progress(subscription: dict[str, Any], progress: dict[str, Any] | None = None, setup_state: str | None = None) -> dict[str, Any]:
    values = dict(progress or {})
    values.setdefault("baseline_listings_collected", subscription.get("BaselineListingsCollected"))
    values.setdefault("baseline_new_count", subscription.get("BaselineNewCount"))
    values.setdefault("baseline_pages_checked", subscription.get("BaselinePagesChecked"))
    values.setdefault("baseline_total_pages_detected", subscription.get("BaselineTotalPagesDetected"))
    values.setdefault("baseline_stop_reason", subscription.get("BaselineStopReason"))
    values.setdefault("detail_baseline_total_count", 0)
    values.setdefault("detail_baseline_completed_count", 0)
    values.setdefault("detail_baseline_remaining_count", 0)
    values.setdefault("detail_baseline_attempt_count", subscription.get("DetailBaselineAttemptCount", 0))
    values.setdefault("next_retry_at", subscription.get("DetailBaselineNextRetryAt"))
    values.setdefault("detail_baseline_last_error", subscription.get("DetailBaselineLastError"))
    values.setdefault("price_baseline_status", subscription.get("PriceBaselineStatus", "pending"))
    values.setdefault("price_baseline_total_count", 0)
    values.setdefault("price_baseline_completed_count", 0)
    values.setdefault("price_baseline_remaining_count", 0)
    values.setdefault("price_baseline_last_error", subscription.get("PriceBaselineLastError"))
    values["notification_ready_at"] = values.get("notification_ready_at", subscription.get("NotificationReadyAt"))
    values["setup_state"] = setup_state or ("ready" if values["notification_ready_at"] else "detail_baseline_running")
    return values


def _record_setup_state(result: dict[str, Any], subscription: dict[str, Any], setup_state: str | None = None, progress: dict[str, Any] | None = None) -> None:
    if progress is None:
        try:
            progress = _load_baseline_summary(subscription)
        except Exception:
            progress = None
    result["setup_states"].append({"user_area_id": int(subscription["UserAreaID"]), **_setup_progress(subscription, progress, setup_state)})


def run_monitoring_tick(dry_run: bool = False, send_telegram: bool = False, notification_limit: int = 100, send_limit: int | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"dry_run": bool(dry_run), "subscriptions": 0, "baseline": [], "setup_summaries": [], "setup_states": [], "light_checks": [], "detail_refreshes": [], "notifications": [], "sender": None, "errors": []}
    conn = db_layer.connect(config.DB_PATH)
    try:
        db_layer.ensure_telegram_bot_tables(conn)
        subs = db_layer.get_active_user_area_subscriptions(conn)
    finally:
        conn.close()
    result["subscriptions"] = len(subs)
    now = _utcnow()
    light_interval = timedelta(minutes=config.LIGHT_CHECK_INTERVAL_MINUTES)
    detail_interval = timedelta(hours=config.DETAIL_REFRESH_INTERVAL_HOURS)

    setup_search_ids_seen: set[int] = set()
    operational_search_ids_seen: set[int] = set()
    for sub in subs:
        user_area_id = int(sub["UserAreaID"])
        search_id = int(sub.get("SearchID") or 0)
        try:
            status = str(sub.get("BaselineStatus") or "pending").lower()
            if status != "completed":
                if search_id in setup_search_ids_seen:
                    result["baseline"].append({"user_area_id": user_area_id, "search_id": search_id, "status": "shared_setup_already_scheduled"})
                    _record_setup_state(result, sub, "shared_setup_running")
                    continue
                setup_search_ids_seen.add(search_id)
                baseline_result = run_initial_baseline_for_subscription(user_area_id, dry_run=dry_run)
                result["baseline"].append(baseline_result)
                if not dry_run and baseline_result.get("status") == "completed":
                    sub["BaselineStatus"] = "completed"
                    sub["BaselineListingsCollected"] = baseline_result.get("listings_collected")
                    sub["BaselineNewCount"] = baseline_result.get("new_count")
                    sub["BaselinePagesChecked"] = baseline_result.get("pages_checked")
                    sub["BaselineTotalPagesDetected"] = baseline_result.get("total_pages_detected")
                    sub["BaselineStopReason"] = baseline_result.get("stop_reason")
                    if send_telegram:
                        result["setup_summaries"].append(_send_setup_summary_once(sub, "baseline"))
                elif not dry_run and baseline_result.get("status") == "incomplete":
                    sub["BaselineListingsCollected"] = baseline_result.get("listings_collected")
                    sub["BaselinePagesChecked"] = baseline_result.get("pages_checked")
                    sub["BaselineTotalPagesDetected"] = baseline_result.get("total_pages_detected")
                    sub["BaselineStopReason"] = baseline_result.get("stop_reason")
                    if send_telegram:
                        result["setup_summaries"].append(_send_baseline_incomplete_warning(sub))
                _record_setup_state(result, sub, "detail_baseline_pending" if baseline_result.get("status") == "completed" else f"baseline_{baseline_result.get('status')}")
                continue
            if send_telegram:
                result["setup_summaries"].append(_send_setup_summary_once(sub, "baseline"))
            detail_baseline_status = str(sub.get("DetailBaselineStatus") or "pending").lower()
            if detail_baseline_status == "retry_wait":
                next_retry_at = sub.get("DetailBaselineNextRetryAt")
                if not _detail_retry_is_due(next_retry_at, now):
                    result["notifications"].append({"user_area_id": user_area_id, "skipped_reason": "detail_baseline_retry_wait"})
                    _record_setup_state(result, sub, "detail_baseline_retry_wait")
                    continue
                if not dry_run:
                    conn = db_layer.connect(config.DB_PATH)
                    try:
                        db_layer.mark_subscription_detail_baseline_retry_started(conn, user_area_id)
                        sub = db_layer.get_user_area_subscription(conn, user_area_id) or sub
                    finally:
                        conn.close()
                detail_baseline_status = "running"
            if detail_baseline_status in {"pending", "running"}:
                if search_id in setup_search_ids_seen:
                    result["detail_refreshes"].append({"user_area_id": user_area_id, "search_id": search_id, "status": "shared_setup_already_scheduled"})
                    _record_setup_state(result, sub, "shared_setup_running")
                    continue
                setup_search_ids_seen.add(search_id)
                if dry_run:
                    preview = {"user_area_id": user_area_id, "status": "detail_baseline_due", "search_url": sub.get("SearchURL")}
                    preview.update(_setup_progress(sub, setup_state="detail_baseline_running"))
                    result["detail_refreshes"].append(preview)
                    _record_setup_state(result, sub, "detail_baseline_running")
                else:
                    conn = db_layer.connect(config.DB_PATH)
                    try:
                        if detail_baseline_status == "pending":
                            db_layer.mark_subscription_detail_baseline_started(conn, user_area_id)
                        sub = db_layer.get_user_area_subscription(conn, user_area_id) or sub
                    finally:
                        conn.close()
                    if send_telegram:
                        result["setup_summaries"].append(_send_setup_summary_once(sub, "detail_started"))
                    detail_result = refresh_active_listings(sub["SearchURL"], limit=config.DETAIL_REFRESH_BATCH_LIMIT, stale_hours=0, dry_run=False, context="initial_detail_baseline", suppress_notifications=True, subscription=sub)
                    conn = db_layer.connect(config.DB_PATH)
                    try:
                        refreshed_sub = db_layer.get_user_area_subscription(conn, user_area_id) or sub
                        progress = db_layer.get_detail_baseline_progress(conn, refreshed_sub)
                        if is_retryable_detail_batch_failure(detail_result):
                            attempt = int(refreshed_sub.get("DetailBaselineAttemptCount") or 0)
                            backoffs = config.DETAIL_BASELINE_RETRY_BACKOFF_SECONDS
                            backoff_seconds = backoffs[min(attempt, len(backoffs) - 1)]
                            error = config.mask_sensitive_text(detail_result.get("errors") or "retryable detail baseline batch failure")
                            retry_status = db_layer.mark_subscription_detail_baseline_retry_wait(conn, user_area_id, error, now + timedelta(seconds=backoff_seconds), config.DETAIL_BASELINE_MAX_ATTEMPTS)
                            refreshed_sub = db_layer.get_user_area_subscription(conn, user_area_id) or refreshed_sub
                            progress = db_layer.get_detail_baseline_progress(conn, refreshed_sub)
                            detail_status = f"detail_baseline_{retry_status}"
                        elif detail_result.get("errors") and int(detail_result.get("refreshed_count") or 0) == 0:
                            db_layer.mark_subscription_detail_baseline_failed(conn, user_area_id, str(detail_result.get("errors")))
                            refreshed_sub = db_layer.get_user_area_subscription(conn, user_area_id) or refreshed_sub
                            detail_status = "detail_baseline_failed"
                        elif progress["detail_baseline_remaining_count"] == 0:
                            db_layer.mark_subscription_detail_baseline_completed(conn, user_area_id)
                            refreshed_sub = db_layer.get_user_area_subscription(conn, user_area_id) or refreshed_sub
                            progress = db_layer.get_detail_baseline_progress(conn, refreshed_sub)
                            detail_status = "price_baseline_pending"
                        else:
                            db_layer.mark_subscription_detail_baseline_batch_succeeded(conn, user_area_id)
                            refreshed_sub = db_layer.get_user_area_subscription(conn, user_area_id) or refreshed_sub
                            detail_status = "detail_baseline_running"
                    finally:
                        conn.close()
                    detail_result["user_area_id"] = user_area_id
                    detail_result["status"] = detail_status
                    detail_result.update(_setup_progress(refreshed_sub, progress, detail_status))
                    result["detail_refreshes"].append(detail_result)
                    _record_setup_state(result, refreshed_sub, detail_status, progress)
                continue
            price_baseline_status = str(sub.get("PriceBaselineStatus") or "pending").lower()
            if detail_baseline_status == "completed" and price_baseline_status in {"pending", "running"}:
                if search_id in setup_search_ids_seen:
                    _record_setup_state(result, sub, "shared_price_setup_running")
                    continue
                setup_search_ids_seen.add(search_id)
                if dry_run:
                    price_result = {"user_area_id": user_area_id, "search_id": search_id, "status": "price_baseline_due"}
                else:
                    conn = db_layer.connect(config.DB_PATH)
                    try:
                        if price_baseline_status == "pending":
                            db_layer.mark_subscription_price_baseline_started(conn, user_area_id)
                    finally:
                        conn.close()
                    price_result = run_price_baseline_for_search(search_id, dry_run=False, setup=True)
                    conn = db_layer.connect(config.DB_PATH)
                    try:
                        refreshed_sub = db_layer.get_user_area_subscription(conn, user_area_id) or sub
                        progress = db_layer.get_price_baseline_progress(conn, refreshed_sub)
                        if price_result.get("status") == "failed":
                            db_layer.mark_subscription_price_baseline_failed(conn, user_area_id, str(price_result.get("errors")))
                            setup_status = "price_baseline_failed"
                        elif progress["price_baseline_remaining_count"] == 0:
                            db_layer.mark_subscription_price_baseline_completed(conn, user_area_id)
                            refreshed_sub = db_layer.get_user_area_subscription(conn, user_area_id) or refreshed_sub
                            refreshed_sub["PriceBaselineUnknownCount"] = price_result.get("unknown_count", 0)
                            refreshed_sub["PriceBaselineInferredCount"] = price_result.get("inferred_count", 0)
                            refreshed_sub["PriceBaselineTotalCount"] = price_result.get("processed_count", progress.get("price_baseline_total_count"))
                            progress = db_layer.get_price_baseline_progress(conn, refreshed_sub)
                            setup_status = "ready"
                        else:
                            setup_status = "price_baseline_running"
                    finally:
                        conn.close()
                    price_result.update(_setup_progress(refreshed_sub, progress, setup_status))
                    if send_telegram and setup_status == "ready":
                        result["setup_summaries"].append(_send_setup_summary_once(refreshed_sub, "ready"))
                result.setdefault("price_refreshes", []).append(price_result)
                _record_setup_state(result, refreshed_sub if not dry_run else sub, price_result.get("setup_state", price_result.get("status")))
                continue
            if detail_baseline_status != "completed" or price_baseline_status != "completed" or not sub.get("NotificationReadyAt"):
                result["notifications"].append({"user_area_id": user_area_id, "skipped_reason": f"detail_baseline_{detail_baseline_status}"})
                _record_setup_state(result, sub, f"detail_baseline_{detail_baseline_status}")
                continue
            _record_setup_state(result, sub, "ready")
            if send_telegram:
                result["setup_summaries"].append(_send_setup_summary_once(sub, "ready"))
            if search_id not in operational_search_ids_seen:
                operational_search_ids_seen.add(search_id)
                if _is_due(sub.get("LastLightCheckAt"), light_interval, now):
                    if dry_run:
                        light_result = {"user_area_id": user_area_id, "search_id": search_id, "status": "due", "search_url": sub.get("SearchURL")}
                    else:
                        light_result = light_check_area(config.DB_PATH, sub["SearchURL"], max_pages=config.LIGHT_CHECK_PAGES, timeout=config.PIPELINE_TIMEOUT, dry_run=False)
                        if light_result.get("scan_status") == "blocked_rate_limited":
                            result["errors"].append({"user_area_id": user_area_id, "stage": "light_check", "errors": light_result.get("blocked_reason") or light_result.get("stop_reason")})
                            result["light_checks"].append(light_result | {"user_area_id": user_area_id, "search_id": search_id})
                            continue
                        if light_result.get("scan_status") == "technical_failure":
                            result["errors"].append({"user_area_id": user_area_id, "stage": "light_check", "errors": light_result.get("errors") or light_result.get("stop_reason")})
                            result["light_checks"].append(light_result | {"user_area_id": user_area_id, "search_id": search_id})
                            continue
                        conn = db_layer.connect(config.DB_PATH)
                        try:
                            db_layer.mark_subscription_light_checked(conn, user_area_id)
                        finally:
                            conn.close()
                        light_result["user_area_id"] = user_area_id
                        light_result["search_id"] = search_id
                    result["light_checks"].append(light_result)
                if _is_due(sub.get("LastDetailRefreshAt"), detail_interval, now):
                    if dry_run:
                        detail_result = {"user_area_id": user_area_id, "search_id": search_id, "status": "due", "search_url": sub.get("SearchURL")}
                    else:
                        detail_result = refresh_active_listings(sub["SearchURL"], limit=config.DETAIL_REFRESH_BATCH_LIMIT, dry_run=False, subscription=sub)
                        conn = db_layer.connect(config.DB_PATH)
                        try:
                            db_layer.mark_subscription_detail_refreshed(conn, user_area_id)
                        finally:
                            conn.close()
                        detail_result["user_area_id"] = user_area_id
                        detail_result["search_id"] = search_id
                    result["detail_refreshes"].append(detail_result)
            else:
                result["light_checks"].append({"user_area_id": user_area_id, "search_id": search_id, "status": "shared_search_already_checked"})
            conn = db_layer.connect(config.DB_PATH)
            try:
                result["notifications"].append(db_layer.queue_notifications_for_user_area(conn, user_area_id, dry_run=dry_run, limit=notification_limit))
            finally:
                conn.close()
        except Exception as exc:
            result["errors"].append({"user_area_id": user_area_id, "error": config.mask_sensitive_text(exc)})
            _record_setup_state(result, sub, "error")
    if send_telegram:
        if not dry_run and not config.TELEGRAM_BOT_TOKEN:
            result["sender"] = {"processed": 0, "sent": 0, "failed": 0, "warning": "telegram token not set; queued notifications were not sent"}
        else:
            import telegram_sender
            result["sender"] = telegram_sender.send_queued_notifications_once(limit=send_limit or config.TELEGRAM_SENDER_MAX_PER_TICK, dry_run=dry_run)
    return result


def run_monitoring_loop(sleep_seconds: int | None = None, send_telegram: bool = False) -> None:
    delay = sleep_seconds or config.MONITORING_TICK_SLEEP_SECONDS
    while True:
        print(run_monitoring_tick(dry_run=False, send_telegram=send_telegram))
        time.sleep(delay)

# Phase 2B priority Job queue foundation. These helpers enqueue and run SearchID-scoped
# units of work without replacing the existing run_monitoring_tick runtime yet.

def _group_active_subscriptions_by_search(subscriptions: list[dict[str, Any]]) -> dict[int, list[dict[str, Any]]]:
    grouped: dict[int, list[dict[str, Any]]] = {}
    for sub in subscriptions:
        search_id = int(sub.get("SearchID") or 0)
        if search_id <= 0:
            continue
        grouped.setdefault(search_id, []).append(sub)
    return grouped


def _representative_subscription(subscriptions: list[dict[str, Any]]) -> dict[str, Any]:
    return sorted(subscriptions, key=lambda row: int(row.get("UserAreaID") or 0))[0]


def _subscription_group_is_ready(subscriptions: list[dict[str, Any]]) -> bool:
    def subscription_lifecycle_active(sub: dict[str, Any]) -> bool:
        area_status = str(sub.get("AreaSetupStatus") or "").lower()
        if area_status == "inactive":
            return False
        status = str(sub.get("SubscriptionStatus") or sub.get("UserAreaSubscriptionStatus") or "active").lower()
        notify_enabled = int(sub.get("SubscriptionNotifyEnabled") if sub.get("SubscriptionNotifyEnabled") is not None else sub.get("UserAreaNotifyEnabled", 1) if sub.get("UserAreaNotifyEnabled") is not None else 1)
        return status == "active" and notify_enabled == 1

    if any(
        str(sub.get("AreaSetupStatus") or "").lower() == "ready"
        and subscription_lifecycle_active(sub)
        for sub in subscriptions
    ):
        return True
    return any(
        subscription_lifecycle_active(sub)
        and str(sub.get("BaselineStatus") or "").lower() == "completed"
        and str(sub.get("DetailBaselineStatus") or "").lower() == "completed"
        and str(sub.get("PriceBaselineStatus") or "pending").lower() == "completed"
        and bool(sub.get("NotificationReadyAt"))
        for sub in subscriptions
    )


def _subscription_group_baseline_completed(subscriptions: list[dict[str, Any]]) -> bool:
    return any(str(sub.get("BaselineStatus") or "").lower() == "completed" for sub in subscriptions)


def _subscription_group_detail_completed(subscriptions: list[dict[str, Any]]) -> bool:
    return any(str(sub.get("DetailBaselineStatus") or "").lower() == "completed" for sub in subscriptions)


def _subscription_group_price_completed(subscriptions: list[dict[str, Any]]) -> bool:
    return any(str(sub.get("PriceBaselineStatus") or "pending").lower() == "completed" for sub in subscriptions)


def _oldest_or_none(values):
    vals = [value for value in values if value is not None]
    return min(vals) if vals else None


def _effective_search_last_at(subscriptions: list[dict[str, Any]], column_name: str):
    values = [sub.get(column_name) for sub in subscriptions]
    if any(value is None for value in values):
        return None
    return min(values) if values else None


def _effective_ready_at(subscriptions: list[dict[str, Any]]):
    values = [
        sub.get("AreaReadyAt") or sub.get("NotificationReadyAt")
        for sub in subscriptions
        if sub.get("AreaReadyAt") or sub.get("NotificationReadyAt")
    ]
    return max(values) if values else None


def _effective_scheduled_last_at(subscriptions: list[dict[str, Any]], column_name: str):
    last_at = _effective_search_last_at(subscriptions, column_name)
    return last_at if last_at is not None else _effective_ready_at(subscriptions)


def _parse_schedule_times(value: str) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for part in str(value or "").split(","):
        text = part.strip()
        if not text:
            continue
        hour_text, minute_text = text.split(":", 1)
        out.append((int(hour_text), int(minute_text)))
    return sorted(set(out))


def _as_schedule_time(value: datetime, tz: ZoneInfo) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(tz)


def _latest_scheduled_occurrence(now: datetime, times_text: str, timezone_name: str | None = None) -> datetime | None:
    tz = ZoneInfo(timezone_name or getattr(config, "SCHEDULE_TIMEZONE", "Australia/Sydney"))
    local_now = _as_schedule_time(now, tz)
    times = _parse_schedule_times(times_text)
    candidates = []
    for days_back in (0, 1):
        day = (local_now - timedelta(days=days_back)).date()
        for hour, minute in times:
            candidate = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)
            if candidate <= local_now:
                candidates.append(candidate)
    return max(candidates) if candidates else None


def _scheduled_time_due(last_at, now: datetime, times_text: str, timezone_name: str | None = None) -> dict[str, Any]:
    latest = _latest_scheduled_occurrence(now, times_text, timezone_name)
    if latest is None:
        return {"is_due": False, "reason": "no_schedule_time", "latest_scheduled_at": None}
    if last_at is None:
        return {"is_due": True, "reason": "due_null_last_at", "latest_scheduled_at": latest}
    tz = ZoneInfo(timezone_name or getattr(config, "SCHEDULE_TIMEZONE", "Australia/Sydney"))
    last_local = _as_schedule_time(last_at, tz)
    due = last_local < latest
    return {"is_due": due, "reason": "due" if due else "not_due", "latest_scheduled_at": latest, "last_at_local": last_local}


def _scheduled_due_decision(search_id: int, job_type: str, last_at, current_time_used: datetime, times_text: str) -> dict[str, Any]:
    decision = _scheduled_time_due(last_at, current_time_used, times_text, getattr(config, "SCHEDULE_TIMEZONE", "Australia/Sydney"))
    return {"search_id": search_id, "job_type": job_type, "last_at": last_at, "current_time_used": current_time_used, "times": times_text, **decision}


def _job_payload(job: dict[str, Any]) -> dict[str, Any]:
    payload = job.get("PayloadJson") or job.get("payload") or job.get("Payload")
    if not payload:
        return {}
    if isinstance(payload, dict):
        return payload
    try:
        parsed = json.loads(payload)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _active_price_job_exists(search_id: int) -> bool:
    import job_queue

    price_job_types = {
        job_queue.JOB_TYPE_PRICE_REFRESH_EXISTING,
        job_queue.JOB_TYPE_MODULE2_PRICE_REFRESH_AREA,
    }
    for job in job_queue.get_active_jobs():
        if int(job.get("SearchID") or 0) == int(search_id) and str(job.get("JobType") or "") in price_job_types:
            return True
    return False


def _listing_ids_from_light(light_result: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    for row in light_result.get("new_listings") or []:
        listing_id = str(row.get("listing_id") or row.get("external_id") or "").strip()
        if listing_id and listing_id not in ids:
            ids.append(listing_id)
    return ids


def _enqueue_new_listing_processing(search_id: int, user_area_id: int | None, listing_ids: list[str], run_after=None) -> list[dict[str, Any]]:
    import job_queue

    safe_ids = [str(value).strip() for value in listing_ids if str(value).strip()]
    batch_size = max(1, int(getattr(config, "PROCESS_NEW_LISTING_BATCH_SIZE", 5)))
    jobs = []
    for idx in range(0, len(safe_ids), batch_size):
        batch = safe_ids[idx: idx + batch_size]
        dedupe = f"{job_queue.JOB_TYPE_PROCESS_NEW_LISTING}:search_id={int(search_id)}:ids={','.join(batch)}"
        jobs.append(job_queue.enqueue_job_once(
            job_queue.JOB_TYPE_PROCESS_NEW_LISTING,
            search_id=int(search_id),
            user_area_id=user_area_id,
            priority=job_queue.PRIORITY_NEW_LISTING_ENRICHMENT,
            run_after=run_after or _utcnow(),
            payload={"listing_ids": batch},
            dedupe_key=dedupe,
            max_attempts=3,
        ))
    return jobs


def _due_decision(search_id: int, job_type: str, last_at, current_time_used: datetime, interval_seconds: int) -> dict[str, Any]:
    due_before = current_time_used - timedelta(seconds=int(interval_seconds))
    seconds_since_last = None
    if last_at is None:
        due = True
        reason = "due_null_last_at"
    else:
        seconds_since_last = int((current_time_used - last_at).total_seconds())
        due = last_at <= due_before
        reason = "due" if due else "not_due"
    return {
        "search_id": search_id,
        "job_type": job_type,
        "last_at": last_at,
        "current_time_used": current_time_used,
        "interval_seconds": int(interval_seconds),
        "due_before": due_before,
        "seconds_since_last": seconds_since_last,
        "reason": reason,
        "is_due": due,
    }


def enqueue_due_monitoring_jobs(now: datetime | None = None) -> dict[str, Any]:
    import job_queue

    result: dict[str, Any] = {
        "created": [],
        "skipped_duplicates": [],
        "not_due": [],
        "due_checks": [],
        "ready_search_ids_considered": [],
        "not_ready_search_ids_considered": [],
        "errors": [],
    }
    conn = db_layer.connect(config.DB_PATH)
    try:
        db_layer.ensure_telegram_bot_tables(conn)
        job_queue.ensure_job_tables(conn)
        current_time_used = now or _fetch_sql_server_local_time(conn)
        result["current_time_used"] = current_time_used
        subscriptions = db_layer.get_active_user_area_subscriptions(conn)
    finally:
        conn.close()

    light_interval_seconds = int(getattr(config, "NEW_LISTING_CHECK_INTERVAL_SECONDS", 2700))
    detail_interval_seconds = int(getattr(config, "DETAIL_REFRESH_INTERVAL_SECONDS", 3600))
    grouped = _group_active_subscriptions_by_search(subscriptions)
    for search_id, group in sorted(grouped.items()):
        rep = _representative_subscription(group)
        try:
            if not _subscription_group_is_ready(group):
                result["not_ready_search_ids_considered"].append(search_id)
                job = job_queue.enqueue_job_once(
                    job_queue.JOB_TYPE_BASELINE_SETUP_AREA,
                    search_id=search_id,
                    user_area_id=int(rep["UserAreaID"]),
                    priority=job_queue.PRIORITY_SETUP,
                    run_after=current_time_used,
                    payload={"area_id": search_id, "search_url": rep["SearchURL"]},
                    dedupe_key=f"{job_queue.JOB_TYPE_BASELINE_SETUP_AREA}:area_id={search_id}",
                    max_attempts=3,
                )
                (result["created"] if job.get("created") else result["skipped_duplicates"]).append(job)
                continue

            result["ready_search_ids_considered"].append(search_id)
            light_check = _due_decision(
                search_id,
                job_queue.JOB_TYPE_LIGHT_CHECK_NEW_LISTINGS,
                _effective_search_last_at(group, "LastLightCheckAt"),
                current_time_used,
                light_interval_seconds,
            )
            result["due_checks"].append(light_check)
            if light_check["is_due"]:
                job = job_queue.enqueue_job_once(
                    job_queue.JOB_TYPE_LIGHT_CHECK_NEW_LISTINGS,
                    search_id=search_id,
                    user_area_id=int(rep["UserAreaID"]),
                    priority=job_queue.PRIORITY_LIGHT_CHECK,
                    run_after=current_time_used,
                )
                (result["created"] if job.get("created") else result["skipped_duplicates"]).append(job)
            else:
                result["not_due"].append(light_check)

            detail_check = _due_decision(
                search_id,
                job_queue.JOB_TYPE_DETAIL_REFRESH_EXISTING,
                _effective_search_last_at(group, "LastDetailRefreshAt"),
                current_time_used,
                detail_interval_seconds,
            )
            result["due_checks"].append(detail_check)
            if detail_check["is_due"]:
                job = job_queue.enqueue_job_once(
                    job_queue.JOB_TYPE_DETAIL_REFRESH_EXISTING,
                    search_id=search_id,
                    user_area_id=int(rep["UserAreaID"]),
                    priority=job_queue.PRIORITY_DETAIL_REFRESH,
                    run_after=current_time_used,
                )
                (result["created"] if job.get("created") else result["skipped_duplicates"]).append(job)
            else:
                result["not_due"].append(detail_check)

            if getattr(config, "PRICE_INFERENCE_ENABLED", True):
                due_unknown_ids: list[str] = []
                retry_conn = db_layer.connect(config.DB_PATH)
                try:
                    due_unknown_ids = db_layer.get_due_price_retry_listing_ids(retry_conn, search_id, now_value=current_time_used, limit=int(getattr(config, "PRICE_REFRESH_BATCH_SIZE", 10)))
                finally:
                    retry_conn.close()
                if due_unknown_ids:
                    retry_job = job_queue.enqueue_job_once(
                        job_queue.JOB_TYPE_PRICE_RETRY_UNKNOWNS,
                        search_id=search_id,
                        user_area_id=int(rep["UserAreaID"]),
                        priority=job_queue.PRIORITY_PRICE_RETRY_UNKNOWNS,
                        run_after=current_time_used,
                        payload={"listing_external_ids": due_unknown_ids},
                        dedupe_key=f"{job_queue.JOB_TYPE_PRICE_RETRY_UNKNOWNS}:search_id={search_id}:listing_ids={','.join(sorted(due_unknown_ids))}",
                        max_attempts=3,
                    )
                    (result["created"] if retry_job.get("created") else result["skipped_duplicates"]).append(retry_job)
                price_check = _scheduled_due_decision(
                    search_id,
                    job_queue.JOB_TYPE_MODULE2_PRICE_REFRESH_AREA,
                    _effective_scheduled_last_at(group, "LastPriceRefreshAt"),
                    current_time_used,
                    getattr(config, "PRICE_REFRESH_TIMES", "00:00,12:00"),
                )
                result["due_checks"].append(price_check)
                if price_check["is_due"]:
                    if _active_price_job_exists(search_id):
                        job = {
                            "created": False,
                            "duplicate": True,
                            "reason": "active_price_refresh_exists",
                            "search_id": search_id,
                            "job_type": job_queue.JOB_TYPE_MODULE2_PRICE_REFRESH_AREA,
                        }
                    else:
                        run_id = f"price-refresh-{search_id}-{price_check['latest_scheduled_at'].isoformat()}"
                        job = job_queue.enqueue_job_once(
                            job_queue.JOB_TYPE_MODULE2_PRICE_REFRESH_AREA,
                            search_id=search_id,
                            user_area_id=int(rep["UserAreaID"]),
                            priority=getattr(job_queue, "PRIORITY_PRICE_REFRESH", job_queue.PRIORITY_DETAIL_REFRESH),
                            run_after=current_time_used,
                            payload={"run_id": run_id, "run_started_at": current_time_used.isoformat(), "full_refresh": True},
                        )
                    (result["created"] if job.get("created") else result["skipped_duplicates"]).append(job)
                else:
                    result["not_due"].append(price_check)

            sweep_check = _scheduled_due_decision(
                search_id,
                job_queue.JOB_TYPE_MODULE1_FULL_SAFETY_SWEEP,
                _effective_scheduled_last_at(group, "LastFullListingSweepAt"),
                current_time_used,
                getattr(config, "DAILY_FULL_LISTING_SWEEP_TIME", "04:00"),
            )
            result["due_checks"].append(sweep_check)
            if sweep_check["is_due"]:
                job = job_queue.enqueue_job_once(
                    job_queue.JOB_TYPE_MODULE1_FULL_SAFETY_SWEEP,
                    search_id=search_id,
                    user_area_id=int(rep["UserAreaID"]),
                    priority=getattr(job_queue, "PRIORITY_DAILY_SWEEP", job_queue.PRIORITY_MAINTENANCE),
                    run_after=current_time_used,
                    payload={"scheduled_at": sweep_check["latest_scheduled_at"].isoformat() if sweep_check.get("latest_scheduled_at") else None},
                )
                (result["created"] if job.get("created") else result["skipped_duplicates"]).append(job)
            else:
                result["not_due"].append(sweep_check)
        except Exception as exc:
            result["errors"].append({"search_id": search_id, "error": config.mask_sensitive_text(exc)})
    result["next_due_jobs"] = job_queue.get_next_due_jobs(limit=10)
    return result


def _load_search_subscription(search_id: int, preferred_user_area_id: int | None = None) -> dict[str, Any] | None:
    conn = db_layer.connect(config.DB_PATH)
    try:
        if preferred_user_area_id:
            sub = db_layer.get_user_area_subscription(conn, int(preferred_user_area_id))
            if sub:
                return sub
        subs = db_layer.get_active_user_area_subscriptions_for_search(conn, int(search_id))
        return _representative_subscription(subs) if subs else None
    finally:
        conn.close()


def _search_is_active_for_monitoring(search_id: int | None) -> bool:
    if search_id is None:
        return True
    conn = db_layer.connect(config.DB_PATH)
    try:
        return db_layer.is_area_active_for_monitoring(conn, search_id=int(search_id))
    finally:
        conn.close()


def _inactive_job_result(search_id: int | None) -> dict[str, Any]:
    return {
        "status": "cancelled",
        "reason": "area_inactive_or_no_active_subscriptions",
        "search_id": search_id,
    }


def _queue_notifications_for_search(search_id: int, dry_run: bool = False, limit: int = 100) -> list[dict[str, Any]]:
    conn = db_layer.connect(config.DB_PATH)
    try:
        subs = db_layer.get_active_user_area_subscriptions_for_search(conn, int(search_id))
        return [db_layer.queue_notifications_for_user_area(conn, int(sub["UserAreaID"]), dry_run=dry_run, limit=limit) for sub in subs]
    finally:
        conn.close()


def _run_setup_detail_batch(job: dict[str, Any], send_telegram: bool) -> dict[str, Any]:
    import job_queue

    search_id = int(job["SearchID"])
    if not _search_is_active_for_monitoring(search_id):
        return _inactive_job_result(search_id)
    sub = _load_search_subscription(search_id, job.get("UserAreaID"))
    if not sub:
        return {"status": "skipped", "reason": "subscription_not_found"}
    user_area_id = int(sub["UserAreaID"])
    conn = db_layer.connect(config.DB_PATH)
    try:
        if str(sub.get("DetailBaselineStatus") or "pending").lower() == "pending":
            db_layer.mark_subscription_detail_baseline_started(conn, user_area_id)
            sub = db_layer.get_user_area_subscription(conn, user_area_id) or sub
    finally:
        conn.close()
    if send_telegram:
        _send_setup_summary_once(sub, "detail_started")
    if not _search_is_active_for_monitoring(search_id):
        return _inactive_job_result(search_id)
    detail_result = refresh_active_listings(
        sub["SearchURL"],
        limit=int(getattr(config, "SETUP_DETAIL_BATCH_SIZE", 10)),
        stale_hours=0,
        dry_run=False,
        context="initial_detail_baseline",
        suppress_notifications=True,
        subscription=sub,
    )
    if not _search_is_active_for_monitoring(search_id):
        return _inactive_job_result(search_id)
    conn = db_layer.connect(config.DB_PATH)
    try:
        refreshed_sub = db_layer.get_user_area_subscription(conn, user_area_id) or sub
        progress = db_layer.get_detail_baseline_progress(conn, refreshed_sub)
        now = _utcnow()
        if is_retryable_detail_batch_failure(detail_result):
            attempt = int(refreshed_sub.get("DetailBaselineAttemptCount") or 0)
            backoffs = config.DETAIL_BASELINE_RETRY_BACKOFF_SECONDS
            backoff_seconds = backoffs[min(attempt, len(backoffs) - 1)]
            error = config.mask_sensitive_text(detail_result.get("errors") or "retryable detail baseline batch failure")
            retry_status = db_layer.mark_subscription_detail_baseline_retry_wait(conn, user_area_id, error, now + timedelta(seconds=backoff_seconds), config.DETAIL_BASELINE_MAX_ATTEMPTS)
            status = f"detail_baseline_{retry_status}"
            if retry_status == "retry_wait":
                job_queue.enqueue_job_once(job_queue.JOB_TYPE_SETUP_DETAIL_BASELINE, search_id=int(job["SearchID"]), user_area_id=user_area_id, priority=job_queue.PRIORITY_SETUP, run_after=now + timedelta(seconds=backoff_seconds))
        elif detail_result.get("errors") and int(detail_result.get("refreshed_count") or 0) == 0:
            db_layer.mark_subscription_detail_baseline_failed(conn, user_area_id, str(detail_result.get("errors")))
            status = "detail_baseline_failed"
        elif progress["detail_baseline_remaining_count"] == 0:
            db_layer.mark_subscription_detail_baseline_completed(conn, user_area_id)
            refreshed_sub = db_layer.get_user_area_subscription(conn, user_area_id) or refreshed_sub
            progress = db_layer.get_detail_baseline_progress(conn, refreshed_sub)
            status = "price_baseline_pending"
            job_queue.enqueue_job_once(job_queue.JOB_TYPE_SETUP_PRICE_BASELINE, search_id=int(job["SearchID"]), user_area_id=user_area_id, priority=job_queue.PRIORITY_SETUP, run_after=now + timedelta(seconds=5))
        else:
            db_layer.mark_subscription_detail_baseline_batch_succeeded(conn, user_area_id)
            status = "detail_baseline_running"
            job_queue.enqueue_job_once(job_queue.JOB_TYPE_SETUP_DETAIL_BASELINE, search_id=int(job["SearchID"]), user_area_id=user_area_id, priority=job_queue.PRIORITY_SETUP, run_after=now + timedelta(seconds=5))
    finally:
        conn.close()
    if send_telegram and status == "ready":
        _send_setup_summary_once(refreshed_sub, "ready")
    return {"status": status, "detail_result": detail_result, **progress}


def _write_price_inference_input(rows: list[dict[str, Any]], search_id: int) -> str:
    import json
    import os
    import tempfile

    os.makedirs(config.OUTPUT_DIR, exist_ok=True)
    fd, path = tempfile.mkstemp(prefix=f"module2_search_{int(search_id)}_", suffix=".json", dir=config.OUTPUT_DIR, text=True)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(json_safe([
            {
                "listing_id": str(row.get("listing_id") or row.get("external_id") or ""),
                "price": str(row.get("price_display") or ""),
                "price_display": str(row.get("price_display") or ""),
                "url": row.get("url"),
                "address": row.get("address"),
            }
            for row in rows
        ]), f, ensure_ascii=False, indent=2)
    return path


def _load_module2_output_json(path: str | None) -> dict[str, dict[str, Any]]:
    import json
    import os

    if not path or not os.path.isfile(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        rows = json.load(f)
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        listing_id = str(row.get("listing_id") or "").strip()
        if listing_id:
            out[listing_id] = row
    return out


class Module2RetryableInterruption(RuntimeError):
    pass


def _module2_interrupted(metadata: dict[str, Any]) -> bool:
    status = str((metadata or {}).get("status") or "").lower()
    return status in {"retry_wait_network_interrupted", "interrupted_checkpoint_saved", "timeout_limit", "429", "429_retry_same_window", "render_timeout", "blank_render", "unknown"}


def _unknown_price_debug(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "unknown_listing_ids": [int(row["db_listing_id"]) for row in candidates if row.get("db_listing_id") is not None],
        "unknown_external_ids": [str(row.get("listing_id") or row.get("external_id") or "") for row in candidates if str(row.get("listing_id") or row.get("external_id") or "").strip()],
        "unknown_current_price_display": [str(row.get("price_display") or "") for row in candidates],
        "unknown_listing_urls": [str(row.get("url") or "") for row in candidates],
    }


def _price_retry_dedupe_key(search_id: int, listing_external_ids: list[str]) -> str:
    safe_ids = sorted(str(value).strip() for value in listing_external_ids if str(value).strip())
    return f"price_retry_unknowns:search_id={int(search_id)}:listing_ids={','.join(safe_ids)}"


def _enqueue_price_retry_unknowns(search_id: int, listing_external_ids: list[str], run_after=None, dedupe_suffix: str = "") -> dict[str, Any] | None:
    safe_ids = sorted(str(value).strip() for value in listing_external_ids if str(value).strip())
    if not safe_ids:
        return None
    import job_queue

    base_key = _price_retry_dedupe_key(search_id, safe_ids)
    dedupe_key = f"{base_key}:{dedupe_suffix}" if dedupe_suffix else base_key
    return job_queue.enqueue_job_once(
        job_queue.JOB_TYPE_PRICE_RETRY_UNKNOWNS,
        search_id=int(search_id),
        priority=job_queue.PRIORITY_PRICE_RETRY_UNKNOWNS,
        run_after=run_after or (_utcnow() + timedelta(seconds=5)),
        payload={"listing_external_ids": safe_ids},
        dedupe_key=dedupe_key,
        max_attempts=3,
    )


def _price_sweep_history(conn, search_id: int) -> dict[str, Any]:
    summary = db_layer.get_price_inference_history_summary(conn, search_id, sample_size=10)
    minimum = int(getattr(config, "MIN_SMART_PRICE_HISTORY_COUNT", 10))
    summary["has_enough_history"] = int(summary.get("completed_count") or 0) >= minimum
    summary["minimum_required"] = minimum
    return summary


def _default_price_sweep_mode(setup: bool, requested: str | None, history: dict[str, Any]) -> str:
    if requested:
        return requested
    if setup:
        return "setup_full_sweep"
    return "smart_refresh" if history.get("has_enough_history") else "setup_full_sweep"


def run_price_baseline_for_search(search_id: int, limit: int | None = None, dry_run: bool = False, setup: bool = False, listing_external_ids: list[str] | None = None, run_started_at=None, mark_search_complete: bool = True, sweep_mode: str | None = None, enqueue_unknown_retry: bool = True) -> dict[str, Any]:
    search_id = int(search_id)
    if not getattr(config, "PRICE_INFERENCE_ENABLED", True):
        return json_safe({"status": "skipped", "reason": "price_inference_disabled", "search_id": search_id})
    sub = _load_search_subscription(search_id, None)
    if not sub:
        return json_safe({"status": "skipped", "reason": "subscription_not_found", "search_id": search_id})
    conn = db_layer.connect(config.DB_PATH)
    try:
        history = _price_sweep_history(conn, search_id)
        mode = _default_price_sweep_mode(setup, sweep_mode, history)
        setup_full_sweep_all_targets = bool(setup and mode == "setup_full_sweep" and limit is None and not listing_external_ids)
        safe_limit = None if setup_full_sweep_all_targets else int(limit or (getattr(config, "SETUP_PRICE_BASELINE_BATCH_SIZE", 10) if setup else getattr(config, "PRICE_REFRESH_BATCH_SIZE", 10)))
        raw_candidates = db_layer.get_active_listings_for_price_inference(
            conn,
            search_id,
            limit=safe_limit,
            only_due=False,
            interval_seconds=0,
            listing_external_ids=listing_external_ids,
            before_time=run_started_at,
        )
        if setup:
            candidates = list(raw_candidates)
            skipped_direct = []
        else:
            candidates = []
            skipped_direct = []
            for candidate in raw_candidates:
                low, high = db_layer.parse_price_bounds_from_text(candidate.get("price_display"))
                if low is not None or high is not None:
                    skipped_direct.append((candidate, low, high))
                else:
                    candidates.append(candidate)
            for candidate, low, high in skipped_direct:
                db_layer.update_listing_price_inference(
                    conn,
                    search_id,
                    int(candidate["db_listing_id"]),
                    low,
                    high,
                    "direct",
                    "direct",
                    "skipped_direct_price",
                    create_event=False,
                )
            if skipped_direct:
                conn.commit()
    finally:
        conn.close()
    batch_size_used = "all" if safe_limit is None else safe_limit
    if mode == "setup_full_sweep":
        print(f"Rows: {len(candidates)} | Target price inference count: {len(candidates)} | target_mode=all")
        print("Sweep mode: setup_full_sweep")
        print(f"batch_size_used: {batch_size_used}")
    result: dict[str, Any] = {
        "status": "completed",
        "search_id": search_id,
        "setup": bool(setup),
        "sweep_mode": mode,
        "history": history,
        "candidates_count": len(candidates),
        "target_count": len(candidates),
        "setup_full_sweep_all_targets": bool(setup_full_sweep_all_targets),
        "batch_size_used": batch_size_used,
        "processed_count": 0,
        "inferred_count": 0,
        "skipped_count": len(skipped_direct),
        "unknown_count": 0,
        "unknown_listing_ids": [],
        "unknown_external_ids": [],
        "unknown_current_price_display": [],
        "unknown_listing_urls": [],
        "price_retry_job_enqueued": False,
        "price_retry_job": None,
        "listing_external_ids": listing_external_ids or [],
        "run_started_at": run_started_at,
        "module2_runs": [],
        "errors": [],
    }
    if dry_run or not candidates:
        return json_safe(result)

    remaining_by_external = {str(row.get("listing_id") or row.get("external_id") or "").strip(): row for row in candidates if str(row.get("listing_id") or row.get("external_id") or "").strip()}
    if not result["listing_external_ids"]:
        result["listing_external_ids"] = sorted(remaining_by_external.keys())
    inferred_by_external: dict[str, dict[str, Any]] = {}

    def run_sweep(current_rows: list[dict[str, Any]], current_mode: str) -> dict[str, Any]:
        input_file = _write_price_inference_input(current_rows, search_id)
        from module2_infer_prices import module2_run
        current_target_ids = {
            str(row.get("listing_id") or row.get("external_id") or row.get("ExternalID") or "").strip()
            for row in current_rows
            if str(row.get("listing_id") or row.get("external_id") or row.get("ExternalID") or "").strip()
        }

        out_csv, out_json = module2_run(
            base_list_url=sub["SearchURL"],
            input_file=input_file,
            out_dir=config.OUTPUT_DIR,
            window_width=config.MODULE2_WINDOW_WIDTH,
            step=config.MODULE2_STEP,
            max_high=config.MODULE2_MAX_HIGH,
            max_pages_per_window=config.MODULE2_MAX_PAGES_PER_WINDOW,
            only_overwrite_na=False,
            smart_start=False,
            sweep_mode=current_mode,
            low_anchor=history.get("low_anchor"),
            high_anchor=history.get("high_anchor"),
            checkpoint_search_id=search_id,
            target_mode="all" if setup else "missing_only",
            target_listing_ids=current_target_ids if setup else None,
            preserve_existing_price_display=True,
        )
        metadata = dict(getattr(module2_run, "last_result", {}) or {})
        metadata.update({"output_csv": out_csv, "output_json": out_json})
        if not out_json:
            result["module2_runs"].append(metadata)
            if _module2_interrupted(metadata):
                raise Module2RetryableInterruption(metadata.get("status") or "interrupted_checkpoint_saved")
            raise RuntimeError("module2 did not produce an output JSON")
        rows_by_id = _load_module2_output_json(out_json)
        for external_id, row in rows_by_id.items():
            if row.get("price_inferred_low") is not None or row.get("price_inferred_high") is not None:
                inferred_by_external[external_id] = row
                remaining_by_external.pop(external_id, None)
        return metadata

    modes_to_run: list[str]
    if mode == "smart_refresh":
        modes_to_run = ["smart_refresh", "smart_retry_expanded", "fallback_full_for_missing"]
    elif mode == "smart_retry_expanded":
        modes_to_run = ["smart_retry_expanded", "fallback_full_for_missing"]
    elif mode == "fallback_full_for_missing":
        modes_to_run = ["fallback_full_for_missing"]
    else:
        modes_to_run = ["setup_full_sweep"]

    try:
        for current_mode in modes_to_run:
            if not remaining_by_external:
                break
            # Smart modes are unsafe without anchors; fall back to full coverage instead.
            if current_mode.startswith("smart") and not history.get("has_enough_history"):
                continue
            metadata = run_sweep(list(remaining_by_external.values()), current_mode)
            result["module2_runs"].append(metadata)
            if not remaining_by_external:
                break
    except Module2RetryableInterruption as exc:
        result["status"] = "retry_wait_network_interrupted"
        result["errors"].append(config.mask_sensitive_text(exc))
        if result["module2_runs"]:
            result["module2_runs"][-1]["skipped_reason"] = None
        return json_safe(result)
    except Exception as exc:
        result["status"] = "failed"
        result["errors"].append(config.mask_sensitive_text(exc))
        conn = db_layer.connect(config.DB_PATH)
        try:
            for candidate in remaining_by_external.values():
                db_layer.mark_price_inference_technical_failed(conn, search_id, int(candidate["db_listing_id"]), config.mask_sensitive_text(exc))
            conn.commit()
        finally:
            conn.close()
        return json_safe(result)

    unknown_candidates = list(remaining_by_external.values())
    unknown_debug = _unknown_price_debug(unknown_candidates)
    result["processed_count"] = len(candidates)
    result["inferred_count"] = len(inferred_by_external)
    result["remaining_count"] = len(remaining_by_external)
    result["unknown_count"] = len(unknown_candidates)
    result.update(unknown_debug)
    result["missing_listing_ids"] = sorted(remaining_by_external.keys())
    result["used_fallback_full_sweep"] = any(run.get("sweep_mode") == "fallback_full_for_missing" for run in result["module2_runs"])
    result["stopped_early_all_targets_found"] = not remaining_by_external
    # Flatten the latest Module 2 metadata for CLI/operator readability.
    if result["module2_runs"]:
        latest = result["module2_runs"][-1]
        for key in ("low_anchor", "high_anchor", "min_low", "max_high", "start_low", "end_high", "step_profile", "windows_checked", "skipped_reason"):
            result[key] = latest.get(key)

    conn = db_layer.connect(config.DB_PATH)
    try:
        for external_id, row in inferred_by_external.items():
            candidate = next((item for item in candidates if str(item.get("listing_id") or item.get("external_id")) == external_id), None)
            if not candidate:
                continue
            low = row.get("price_inferred_low")
            high = row.get("price_inferred_high")
            method = row.get("price_inferred_method") or "sliding_between_window"
            db_layer.update_listing_price_inference(conn, search_id, int(candidate["db_listing_id"]), low, high, method, "module2", "completed", create_event=not setup)
        for external_id, candidate in remaining_by_external.items():
            db_layer.mark_price_inference_unknown_pending_retry(conn, search_id, int(candidate["db_listing_id"]), "price_not_inferred_after_sweep")
        if remaining_by_external:
            result["status"] = "completed_with_unknowns"
            if enqueue_unknown_retry:
                retry_job = _enqueue_price_retry_unknowns(search_id, result["unknown_external_ids"])
                result["price_retry_job"] = retry_job
                result["price_retry_job_enqueued"] = bool(retry_job)
        if mark_search_complete and result["processed_count"] > 0:
            db_layer.mark_search_price_refreshed(conn, search_id)
        conn.commit()
    finally:
        conn.close()
    return json_safe(result)


def _run_setup_price_batch(job: dict[str, Any], send_telegram: bool) -> dict[str, Any]:
    import job_queue

    search_id = int(job["SearchID"])
    if not _search_is_active_for_monitoring(search_id):
        return _inactive_job_result(search_id)
    sub = _load_search_subscription(search_id, job.get("UserAreaID"))
    if not sub:
        return {"status": "skipped", "reason": "subscription_not_found"}
    user_area_id = int(sub["UserAreaID"])
    conn = db_layer.connect(config.DB_PATH)
    try:
        if str(sub.get("PriceBaselineStatus") or "pending").lower() == "pending":
            db_layer.mark_subscription_price_baseline_started(conn, user_area_id)
            sub = db_layer.get_user_area_subscription(conn, user_area_id) or sub
    finally:
        conn.close()
    price_result = run_price_baseline_for_search(
        search_id,
        dry_run=False,
        setup=True,
        mark_search_complete=False,
    )
    if not _search_is_active_for_monitoring(search_id):
        return _inactive_job_result(search_id)
    conn = db_layer.connect(config.DB_PATH)
    try:
        refreshed_sub = db_layer.get_user_area_subscription(conn, user_area_id) or sub
        progress = db_layer.get_price_baseline_progress(conn, refreshed_sub)
        now = _utcnow()
        if str(price_result.get("status") or "").startswith("retry_wait"):
            raise Module2RetryableInterruption(price_result.get("errors") or price_result.get("status"))
        if price_result.get("status") == "failed":
            db_layer.mark_subscription_price_baseline_failed(conn, user_area_id, str(price_result.get("errors")))
            status = "price_baseline_failed"
        elif progress["price_baseline_remaining_count"] == 0:
            db_layer.mark_subscription_price_baseline_completed(conn, user_area_id)
            refreshed_sub = db_layer.get_user_area_subscription(conn, user_area_id) or refreshed_sub
            refreshed_sub["PriceBaselineUnknownCount"] = price_result.get("unknown_count", 0)
            refreshed_sub["PriceBaselineInferredCount"] = price_result.get("inferred_count", 0)
            refreshed_sub["PriceBaselineTotalCount"] = price_result.get("processed_count", progress.get("price_baseline_total_count"))
            progress = db_layer.get_price_baseline_progress(conn, refreshed_sub)
            status = "ready"
        else:
            status = "price_baseline_running"
            job_queue.enqueue_job_once(job_queue.JOB_TYPE_SETUP_PRICE_BASELINE, search_id=int(job["SearchID"]), user_area_id=user_area_id, priority=job_queue.PRIORITY_SETUP, run_after=now + timedelta(seconds=5))
    finally:
        conn.close()
    if send_telegram and status == "ready":
        _send_setup_summary_once(refreshed_sub, "ready")
    return {"status": status, "price_result": price_result, **progress}


def run_detail_refresh_existing_for_search(search_id: int, limit: int | None = None, dry_run: bool = False, send_telegram: bool = True) -> dict[str, Any]:
    search_id = int(search_id)
    if not _search_is_active_for_monitoring(search_id):
        return _inactive_job_result(search_id)
    sub = _load_search_subscription(search_id, None)
    if not sub:
        return {"status": "skipped", "reason": "subscription_not_found", "search_id": search_id}
    sub = dict(sub)
    sub["SearchID"] = search_id
    safe_limit = int(limit or getattr(config, "DETAIL_REFRESH_BATCH_SIZE", 35))
    conn = db_layer.connect(config.DB_PATH)
    try:
        before_counts = db_layer.get_detail_refresh_candidate_debug_counts(conn, search_id)
    finally:
        conn.close()
    detail = refresh_active_listings(
        sub["SearchURL"],
        limit=safe_limit,
        stale_hours=0,
        dry_run=dry_run,
        subscription=sub,
    )
    if not _search_is_active_for_monitoring(search_id):
        return _inactive_job_result(search_id)
    detail["search_id"] = search_id
    detail["total_state_rows"] = before_counts.get("total_state_rows", 0)
    detail["active_state_rows"] = before_counts.get("active_state_rows", 0)
    detail["valid_url_rows"] = before_counts.get("valid_url_rows", 0)
    detail["selection_strategy"] = "oldest_first"
    detail["stale_filter_enabled"] = False
    notifications = []
    processed_count = int(detail.get("processed_count") or 0)
    active_state_rows = int(detail.get("active_state_rows") or 0)
    if not dry_run:
        conn = db_layer.connect(config.DB_PATH)
        try:
            if processed_count > 0 or active_state_rows == 0:
                db_layer.mark_search_detail_refreshed(conn, search_id)
        finally:
            conn.close()
        notifications = _queue_notifications_for_search(search_id, dry_run=False) if send_telegram else []
    return {"status": "completed", "detail_refresh": detail, "notifications": notifications}


def run_listing_status_recheck_job(job: dict[str, Any], send_telegram: bool = True) -> dict[str, Any]:
    import listing_detail_refresher

    payload = _job_payload(job)
    search_id = int(job.get("SearchID") or payload.get("search_id") or 0)
    listing_id = int(payload.get("recheck_listing_id") or payload.get("listing_id") or 0)
    external_id = str(payload.get("listing_external_id") or "").strip()
    if not search_id or not listing_id:
        return {"status": "skipped", "reason": "missing_search_or_listing_id"}
    sub = _load_search_subscription(search_id, job.get("UserAreaID"))
    if not sub:
        return {"status": "skipped", "reason": "subscription_not_found"}
    if not external_id:
        conn = db_layer.connect(config.DB_PATH)
        try:
            row = db_layer._one(conn.cursor(), "SELECT CAST(ExternalID AS NVARCHAR(80)) FROM dbo.Listing WHERE listingID=?", listing_id)
            external_id = str(row[0]) if row and row[0] is not None else ""
        finally:
            conn.close()
    if not external_id:
        return {"status": "skipped", "reason": "missing_listing_external_id"}
    detail = listing_detail_refresher.refresh_active_listings(
        sub["SearchURL"],
        limit=1,
        stale_hours=None,
        dry_run=False,
        listing_external_id=external_id,
        context="listing_status_recheck",
        suppress_notifications=False,
        subscription=sub,
    )
    errors = detail.get("errors") or []
    if detail.get("failed_count"):
        technical = any(listing_detail_refresher.is_retryable_detail_error(err.get("error") if isinstance(err, dict) else err) for err in errors)
        if technical:
            return {"status": "retry_wait", "reason": "technical_failure", "detail_refresh": detail, "skip_module2": True}
        conn = db_layer.connect(config.DB_PATH)
        try:
            transition = db_layer.apply_listing_lifecycle_signal(conn, search_id, listing_id, "not_found", "status_recheck_not_found", str(errors[:1] or "not_found"), create_event=True)
            conn.commit()
        finally:
            conn.close()
        notifications = _queue_notifications_for_search(search_id, dry_run=False) if send_telegram and transition.get("should_notify") else []
        return {"status": "completed", "transition": transition, "detail_refresh": detail, "notifications": notifications, "skip_module2": True}
    conn = db_layer.connect(config.DB_PATH)
    try:
        status_row = db_layer._one(conn.cursor(), "SELECT COALESCE(ListingLifecycleStatus, Status, 'active') FROM dbo.ListingSearchState WHERE SearchID=? AND ListingID=?", search_id, listing_id)
        lifecycle = db_layer.normalize_listing_lifecycle_status(status_row[0] if status_row else "active")
        if lifecycle == "sold":
            transition = {"new_status": "sold", "should_notify": True}
        else:
            transition = db_layer.apply_listing_lifecycle_signal(conn, search_id, listing_id, "active", "status_recheck_valid", None, create_event=False)
        conn.commit()
    finally:
        conn.close()
    notifications = _queue_notifications_for_search(search_id, dry_run=False) if send_telegram and transition.get("should_notify") else []
    return {"status": "completed", "transition": transition, "detail_refresh": detail, "notifications": notifications, "skip_module2": True}


def _parse_run_started_at(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if value:
        try:
            return datetime.fromisoformat(str(value))
        except Exception:
            pass
    return _utcnow()


def run_process_new_listing_for_search(search_id: int, listing_ids: list[str], dry_run: bool = False, send_telegram: bool = True) -> dict[str, Any]:
    search_id = int(search_id)
    if not _search_is_active_for_monitoring(search_id):
        return _inactive_job_result(search_id)
    safe_ids = [str(value).strip() for value in listing_ids if str(value).strip()]
    sub = _load_search_subscription(search_id, None)
    if not sub:
        return {"status": "skipped", "reason": "subscription_not_found", "search_id": search_id}
    result: dict[str, Any] = {"status": "completed", "search_id": search_id, "listing_ids": safe_ids, "detail_results": [], "price_results": [], "notifications": [], "errors": []}
    for listing_id in safe_ids:
        if not _search_is_active_for_monitoring(search_id):
            return _inactive_job_result(search_id)
        detail = refresh_active_listings(
            sub["SearchURL"],
            limit=1,
            stale_hours=0,
            dry_run=dry_run,
            listing_external_id=listing_id,
            context="new_listing_enrichment",
            suppress_notifications=True,
            subscription=sub,
        )
        if not _search_is_active_for_monitoring(search_id):
            return _inactive_job_result(search_id)
        result["detail_results"].append(detail)
        if detail.get("errors") and int(detail.get("refreshed_count") or 0) == 0:
            result["status"] = "failed"
            result["errors"].append({"listing_id": listing_id, "stage": "detail", "errors": detail.get("errors")})
            continue
        price = run_price_baseline_for_search(
            search_id,
            limit=1,
            dry_run=dry_run,
            setup=False,
            listing_external_ids=[listing_id],
            mark_search_complete=False,
        )
        if not _search_is_active_for_monitoring(search_id):
            return _inactive_job_result(search_id)
        result["price_results"].append(price)
        if str(price.get("status") or "").startswith("retry_wait"):
            result["status"] = "retry_wait_network_interrupted"
            result["errors"].append({"listing_id": listing_id, "stage": "price", "errors": price.get("errors")})
        elif price.get("status") == "failed":
            result["status"] = "failed"
            result["errors"].append({"listing_id": listing_id, "stage": "price", "errors": price.get("errors")})
    if str(result["status"]).startswith("retry_wait"):
        raise Module2RetryableInterruption(config.mask_sensitive_text(result["errors"]))
    if result["status"] == "failed":
        raise RuntimeError(config.mask_sensitive_text(result["errors"]))
    if not dry_run and send_telegram:
        result["notifications"] = _queue_notifications_for_search(search_id, dry_run=False)
    return result


def run_price_retry_unknowns_for_search(search_id: int, payload: dict[str, Any] | None = None, dry_run: bool = False) -> dict[str, Any]:
    import job_queue

    search_id = int(search_id)
    if not _search_is_active_for_monitoring(search_id):
        return _inactive_job_result(search_id)
    payload = dict(payload or {})
    listing_ids = [str(value).strip() for value in payload.get("listing_external_ids") or payload.get("listing_ids") or [] if str(value).strip()]
    if not listing_ids:
        conn = db_layer.connect(config.DB_PATH)
        try:
            listing_ids = db_layer.get_due_price_retry_listing_ids(conn, search_id, now_value=_utcnow(), limit=int(getattr(config, "PRICE_REFRESH_BATCH_SIZE", 10)))
        finally:
            conn.close()
    if not listing_ids:
        return {"status": "completed", "search_id": search_id, "price_retry": {"status": "skipped", "reason": "no_due_unknown_prices"}, "next_retry_job": None}
    price = run_price_baseline_for_search(
        search_id,
        limit=max(1, len(listing_ids)),
        dry_run=dry_run,
        setup=False,
        listing_external_ids=listing_ids,
        mark_search_complete=False,
        enqueue_unknown_retry=False,
    )
    if str(price.get("status") or "").startswith("retry_wait"):
        raise Module2RetryableInterruption(config.mask_sensitive_text(price.get("errors")))
    if price.get("status") == "failed":
        raise RuntimeError(config.mask_sensitive_text(price.get("errors")))
    next_job = None
    if int(price.get("unknown_count") or 0) > 0 and not dry_run:
        retry_after = _utcnow() + timedelta(seconds=int(getattr(config, "PRICE_UNKNOWN_RETRY_INTERVAL_SECONDS", 3600)))
        next_job = _enqueue_price_retry_unknowns(
            search_id,
            price.get("unknown_external_ids") or [],
            run_after=retry_after,
            dedupe_suffix=f"retry_after={retry_after.isoformat()}",
        )
    return {"status": price.get("status", "completed"), "search_id": search_id, "price_retry": price, "next_retry_job": next_job}


def run_price_refresh_existing_for_search(search_id: int, payload: dict[str, Any] | None = None, dry_run: bool = False) -> dict[str, Any]:
    import job_queue

    search_id = int(search_id)
    if not _search_is_active_for_monitoring(search_id):
        return _inactive_job_result(search_id)
    payload = dict(payload or {})
    run_id = payload.get("run_id") or f"price-refresh-{search_id}-{uuid.uuid4().hex}"
    run_started_at = _parse_run_started_at(payload.get("run_started_at"))
    price = run_price_baseline_for_search(
        search_id,
        limit=int(getattr(config, "PRICE_REFRESH_BATCH_SIZE", 10)),
        dry_run=dry_run,
        setup=False,
        run_started_at=run_started_at,
        mark_search_complete=False,
    )
    if not _search_is_active_for_monitoring(search_id):
        return _inactive_job_result(search_id)
    conn = db_layer.connect(config.DB_PATH)
    try:
        remaining = len(db_layer.get_active_listings_for_price_inference(conn, search_id, limit=1, before_time=run_started_at))
        if remaining == 0 and not dry_run and price.get("status") != "failed":
            db_layer.mark_search_price_refreshed(conn, search_id)
    finally:
        conn.close()
    if str(price.get("status") or "").startswith("retry_wait"):
        raise Module2RetryableInterruption(config.mask_sensitive_text(price.get("errors")))
    if price.get("status") == "failed":
        raise RuntimeError(config.mask_sensitive_text(price.get("errors")))
    enqueued = None
    if remaining > 0 and not dry_run:
        if _active_price_job_exists(search_id):
            enqueued = {"created": False, "duplicate": True, "reason": "active_price_refresh_exists", "search_id": search_id}
        else:
            enqueued = job_queue.enqueue_job_once(
                job_queue.JOB_TYPE_PRICE_REFRESH_EXISTING,
                search_id=search_id,
                priority=job_queue.PRIORITY_PRICE_REFRESH,
                run_after=_utcnow() + timedelta(seconds=5),
                payload={"run_id": run_id, "run_started_at": run_started_at.isoformat(), "full_refresh": True},
                dedupe_key=f"{job_queue.JOB_TYPE_PRICE_REFRESH_EXISTING}:search_id={search_id}",
                max_attempts=3,
            )
    status = "completed" if remaining == 0 else "running"
    return {"status": status, "run_id": run_id, "run_started_at": run_started_at, "remaining_count": remaining, "price_refresh": price, "enqueued_next_batch": enqueued}


def run_daily_full_listing_sweep_for_search(search_id: int, dry_run: bool = False) -> dict[str, Any]:
    search_id = int(search_id)
    if not _search_is_active_for_monitoring(search_id):
        return _inactive_job_result(search_id)
    sub = _load_search_subscription(search_id, None)
    if not sub:
        return {"status": "skipped", "reason": "subscription_not_found", "search_id": search_id}
    if dry_run:
        return {"status": "dry_run", "search_id": search_id, "search_url": sub.get("SearchURL")}
    light = light_check_area(config.DB_PATH, sub["SearchURL"], max_pages=getattr(config, "INITIAL_BASELINE_MAX_PAGES", None), timeout=config.PIPELINE_TIMEOUT, full_scan=True, dry_run=False)
    if light.get("scan_status") == "blocked_rate_limited":
        return {
            "status": "retry_wait",
            "reason": light.get("blocked_reason") or light.get("stop_reason") or "blocked_rate_limited",
            "retry_after_seconds": int(light.get("retry_after_seconds") or getattr(config, "REA_RATE_LIMIT_BACKOFF_SECONDS", 21600)),
            "full_sweep": light,
        }
    if light.get("scan_status") == "technical_failure":
        raise RuntimeError(config.mask_sensitive_text(light.get("errors") or light.get("stop_reason") or "light_check_technical_failure"))
    if not _search_is_active_for_monitoring(search_id):
        return _inactive_job_result(search_id)
    listing_ids = _listing_ids_from_light(light)
    enqueued = _enqueue_new_listing_processing(search_id, int(sub["UserAreaID"]), listing_ids)
    conn = db_layer.connect(config.DB_PATH)
    try:
        db_layer.mark_search_full_listing_swept(conn, search_id)
    finally:
        conn.close()
    return {"status": "completed", "search_id": search_id, "full_sweep": light, "new_listing_jobs": enqueued}


def _notify_baseline_setup_area_ready(search_id: int, baseline_result: dict[str, Any], send_telegram: bool = True) -> list[dict[str, Any]]:
    conn = db_layer.connect(config.DB_PATH)
    try:
        db_layer.activate_area_subscriptions(conn, int(search_id))
        conn.commit()
        subscriptions = db_layer.get_active_user_area_subscriptions_for_search(conn, int(search_id))
    finally:
        conn.close()

    summaries: list[dict[str, Any]] = []
    for sub in subscriptions:
        enriched = dict(sub)
        enriched["PriceBaselineTotalCount"] = baseline_result.get("rows_full") or baseline_result.get("active_listing_count")
        enriched["PriceBaselineInferredCount"] = baseline_result.get("inferred_price_count")
        enriched["PriceBaselineUnknownCount"] = baseline_result.get("unknown_price_count")
        if send_telegram:
            summaries.append(_send_setup_summary_once(enriched, "ready"))
        else:
            summaries.append({"user_area_id": int(enriched["UserAreaID"]), "summary_type": "ready", "status": "skipped", "reason": "send_telegram_false"})
    return summaries


def execute_job(job: dict[str, Any], send_telegram: bool = True) -> dict[str, Any]:
    import job_queue

    job_type = job.get("JobType")
    search_id = int(job.get("SearchID") or 0) if job.get("SearchID") is not None else None
    user_area_id = int(job.get("UserAreaID") or 0) if job.get("UserAreaID") is not None else None
    if search_id is not None and not _search_is_active_for_monitoring(search_id):
        return _inactive_job_result(search_id)
    if job_type == job_queue.JOB_TYPE_BASELINE_SETUP_AREA:
        payload = _job_payload(job)
        area_url = payload.get("search_url")
        if not area_url and search_id:
            sub = _load_search_subscription(search_id, user_area_id)
            area_url = sub.get("SearchURL") if sub else None
        if not area_url:
            return {"status": "skipped", "reason": "missing_search_url"}
        try:
            result = baseline_setup_area(area_url)
            if search_id and not _search_is_active_for_monitoring(search_id):
                return _inactive_job_result(search_id)
            if result.get("status") == "ready" and search_id:
                result["setup_summaries"] = _notify_baseline_setup_area_ready(search_id, result, send_telegram=send_telegram)
            return result
        except RealEstateBlockedError as exc:
            reason = config.mask_sensitive_text(getattr(exc, "reason", str(exc)))
            if search_id:
                conn = db_layer.connect(config.DB_PATH)
                try:
                    db_layer.upsert_area_monitoring_state(conn, int(search_id), setup_status="retry_wait", module1_status="retry_wait", last_error=reason)
                    conn.commit()
                finally:
                    conn.close()
            return {
                "status": "retry_wait",
                "reason": reason,
                "retry_after_seconds": int(getattr(exc, "retry_after_seconds", None) or getattr(config, "REA_RATE_LIMIT_BACKOFF_SECONDS", 21600)),
                "source": "realestate.com.au",
            }
        except Exception as exc:
            if search_id:
                conn = db_layer.connect(config.DB_PATH)
                try:
                    db_layer.upsert_area_monitoring_state(conn, int(search_id), setup_status="failed", last_error=config.mask_sensitive_text(exc))
                    conn.commit()
                finally:
                    conn.close()
            raise
    if job_type == job_queue.JOB_TYPE_SETUP_FULL_BASELINE:
        sub = _load_search_subscription(search_id, user_area_id)
        if not sub:
            return {"status": "skipped", "reason": "subscription_not_found"}
        baseline = run_initial_baseline_for_subscription(int(sub["UserAreaID"]), dry_run=False)
        if baseline.get("status") == "completed":
            job_queue.enqueue_job_once(job_queue.JOB_TYPE_SETUP_DETAIL_BASELINE, search_id=search_id, user_area_id=int(sub["UserAreaID"]), priority=job_queue.PRIORITY_SETUP, run_after=_utcnow())
        return {"status": baseline.get("status"), "baseline": baseline}
    if job_type == job_queue.JOB_TYPE_SETUP_DETAIL_BASELINE:
        return _run_setup_detail_batch(job, send_telegram=send_telegram)
    if job_type == job_queue.JOB_TYPE_SETUP_PRICE_BASELINE:
        return _run_setup_price_batch(job, send_telegram=send_telegram)
    if job_type == job_queue.JOB_TYPE_LIGHT_CHECK_NEW_LISTINGS:
        sub = _load_search_subscription(search_id, user_area_id)
        if not sub:
            return {"status": "skipped", "reason": "subscription_not_found"}
        light = light_check_area(config.DB_PATH, sub["SearchURL"], max_pages=config.LIGHT_CHECK_PAGES, timeout=config.PIPELINE_TIMEOUT, dry_run=False)
        if light.get("scan_status") == "blocked_rate_limited":
            return {
                "status": "retry_wait",
                "reason": light.get("blocked_reason") or light.get("stop_reason") or "blocked_rate_limited",
                "retry_after_seconds": int(light.get("retry_after_seconds") or getattr(config, "REA_RATE_LIMIT_BACKOFF_SECONDS", 21600)),
                "light_check": light,
            }
        if light.get("scan_status") == "technical_failure":
            raise RuntimeError(config.mask_sensitive_text(light.get("errors") or light.get("stop_reason") or "light_check_technical_failure"))
        new_listing_jobs = _enqueue_new_listing_processing(search_id, user_area_id, _listing_ids_from_light(light))
        conn = db_layer.connect(config.DB_PATH)
        try:
            db_layer.mark_search_light_checked(conn, search_id)
        finally:
            conn.close()
        return {"status": "completed", "light_check": light, "new_listing_jobs": new_listing_jobs, "notifications": []}
    if job_type == job_queue.JOB_TYPE_PROCESS_NEW_LISTING:
        payload = _job_payload(job)
        return run_process_new_listing_for_search(search_id, payload.get("listing_ids") or [], dry_run=False, send_telegram=send_telegram)
    if job_type in {job_queue.JOB_TYPE_DETAIL_REFRESH_EXISTING, job_queue.JOB_TYPE_MODULE3_REFRESH_AREA}:
        return run_detail_refresh_existing_for_search(search_id, limit=int(getattr(config, "DETAIL_REFRESH_BATCH_SIZE", 35)), dry_run=False, send_telegram=send_telegram)
    if job_type == job_queue.JOB_TYPE_LISTING_STATUS_RECHECK:
        return run_listing_status_recheck_job(job, send_telegram=send_telegram)
    if job_type in {job_queue.JOB_TYPE_PRICE_REFRESH_EXISTING, job_queue.JOB_TYPE_MODULE2_PRICE_REFRESH_AREA}:
        return run_price_refresh_existing_for_search(search_id, payload=_job_payload(job), dry_run=False)
    if job_type == job_queue.JOB_TYPE_PRICE_RETRY_UNKNOWNS:
        return run_price_retry_unknowns_for_search(search_id, payload=_job_payload(job), dry_run=False)
    if job_type in {job_queue.JOB_TYPE_DAILY_FULL_LISTING_SWEEP, job_queue.JOB_TYPE_MODULE1_FULL_SAFETY_SWEEP}:
        return run_daily_full_listing_sweep_for_search(search_id, dry_run=False)
    if job_type == job_queue.JOB_TYPE_MANUAL_CHECK_NOW:
        queued = job_queue.enqueue_job_once(job_queue.JOB_TYPE_LIGHT_CHECK_NEW_LISTINGS, search_id=search_id, user_area_id=user_area_id, priority=job_queue.PRIORITY_MANUAL_OR_NEW_DETAIL, run_after=_utcnow())
        return {"status": "enqueued_light_check", "job": queued}
    if job_type == job_queue.JOB_TYPE_NOTIFICATION_DISPATCH:
        return {"status": "completed", "notifications": _queue_notifications_for_search(search_id, dry_run=False) if search_id else []}
    if job_type == job_queue.JOB_TYPE_TELEGRAM_SEND:
        import telegram_sender
        return {"status": "completed", "sender": telegram_sender.send_queued_notifications_once(limit=config.TELEGRAM_SENDER_MAX_PER_TICK, dry_run=not send_telegram)}
    return {"status": "skipped", "reason": f"unsupported_job_type:{job_type}"}


def run_next_job_once(worker_id: str | None = None, send_telegram: bool = True) -> dict[str, Any]:
    import job_queue

    worker_id = worker_id or "one-shot-worker"
    job = job_queue.claim_next_job(worker_id=worker_id)
    if not job:
        return {"status": "idle", "reason": "no_due_jobs", "claimed_job": None}
    try:
        result = execute_job(job, send_telegram=send_telegram)
        if result.get("status") == "cancelled":
            job_queue.mark_job_cancelled(int(job["JobID"]), result.get("reason") or "area inactive / no active subscriptions")
        elif result.get("status") == "skipped":
            job_queue.mark_job_succeeded(int(job["JobID"]), result)
        elif str(result.get("status") or "").startswith("retry_wait"):
            retry_after_seconds = int(result.get("retry_after_seconds") or int(getattr(config, "DETAIL_REFRESH_INTERVAL_HOURS", 1)) * 3600)
            retry_reason = str(result.get("reason") or "")
            if retry_reason.startswith("realestate_rate_limited_or_blocked") or retry_reason in {"blocked_http_429", "blocked_kpsdk", "blocked_access_denied", "blocked_rate_limited"}:
                job_queue.mark_job_retry_wait(int(job["JobID"]), result.get("reason") or result, retry_after_seconds=retry_after_seconds)
            else:
                job_queue.mark_job_failed(int(job["JobID"]), result.get("reason") or result, retryable=True, retry_after_seconds=retry_after_seconds)
        else:
            job_queue.mark_job_succeeded(int(job["JobID"]), result)
        return {"status": "completed", "claimed_job": job, "job_result": result}
    except RealEstateBlockedError as exc:
        reason = config.mask_sensitive_text(getattr(exc, "reason", str(exc)))
        retry_after_seconds = int(getattr(exc, "retry_after_seconds", None) or getattr(config, "REA_RATE_LIMIT_BACKOFF_SECONDS", 21600))
        failure = job_queue.mark_job_retry_wait(int(job["JobID"]), reason, retry_after_seconds=retry_after_seconds)
        return {"status": "retry_wait", "claimed_job": job, "error": reason, "failure": failure}
    except Exception as exc:
        failure = job_queue.mark_job_failed(int(job["JobID"]), config.mask_sensitive_text(exc), retryable=True)
        return {"status": "failed", "claimed_job": job, "error": config.mask_sensitive_text(exc), "failure": failure}
