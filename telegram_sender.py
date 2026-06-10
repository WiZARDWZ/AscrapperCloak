from __future__ import annotations

import asyncio
from typing import Any

import config
import db_layer
from telegram import Bot


def _telegram_error_is_permanent(exc: Exception) -> bool:
    text = str(exc).lower()
    name = exc.__class__.__name__.lower()
    permanent_markers = (
        "forbidden",
        "bot was blocked",
        "chat not found",
        "user is deactivated",
        "not enough rights",
        "bad request: chat_id is empty",
    )
    return any(marker in text or marker in name for marker in permanent_markers)


def _notification_backoff_seconds(attempt_count: int | None) -> int:
    base = int(getattr(config, "NOTIFICATION_RETRY_BACKOFF_SECONDS", 300) or 300)
    attempt = max(1, int(attempt_count or 1))
    return min(3600, base * (2 ** max(0, attempt - 1)))


async def send_queued_notifications(bot: Bot, limit: int = 20, dry_run: bool = False, conn=None) -> dict[str, Any]:
    own_conn = conn is None
    if conn is None:
        conn = db_layer.connect(config.DB_PATH)
    result = {
        "processed": 0,
        "sent": 0,
        "failed": 0,
        "retried": 0,
        "skipped": 0,
        "notifications_skipped_by_revalidation": 0,
        "dry_run": bool(dry_run),
        "stale_sending_recovered": 0,
        "stale_sending_failed": 0,
        "items": [],
    }
    try:
        recovery = db_layer.recover_stale_sending_notifications(conn)
        result["stale_sending_recovered"] = recovery.get("stale_sending_recovered", 0)
        result["stale_sending_failed"] = recovery.get("stale_sending_failed", 0)
        if recovery.get("stale_sending_recovered") or recovery.get("stale_sending_failed"):
            conn.commit()
        rows = db_layer.get_queued_notifications(conn, limit=limit, channel="telegram")
        for row in rows:
            nid = row.get("NotificationID")
            chat_id = row.get("ChatID")
            text = row.get("MessageText") or ""
            item = {"notification_id": nid, "chat_id": chat_id, "event_id": row.get("EventID")}
            result["processed"] += 1
            if dry_run:
                validation = db_layer.validate_notification_for_send(conn, int(nid))
            else:
                validation = db_layer.cancel_notification_if_unsafe(conn, int(nid), reason_prefix="send_time_revalidation")
            if not validation.get("valid"):
                if not dry_run:
                    conn.commit()
                item["status"] = "skipped"
                item["reason"] = validation.get("reason")
                result["skipped"] += 1
                result["notifications_skipped_by_revalidation"] += 1
                result["items"].append(item)
                continue
            if dry_run:
                print(f"--- notification {nid} chat={chat_id} ---")
                print(text)
                item["status"] = "preview"
                result["items"].append(item)
                continue
            if not chat_id:
                db_layer.mark_notification_cancelled(conn, nid, "missing_chat_id")
                conn.commit()
                item["status"] = "skipped"
                item["error"] = "missing_chat_id"
                result["skipped"] += 1
                result["items"].append(item)
                continue
            try:
                claimed = db_layer.mark_notification_sending(conn, nid)
                conn.commit()
                if claimed is False:
                    item["status"] = "skipped"
                    item["reason"] = "notification_no_longer_queued"
                    result["skipped"] += 1
                    result["items"].append(item)
                    continue
                await bot.send_message(chat_id=str(chat_id), text=text, disable_web_page_preview=True)
                db_layer.mark_notification_sent(conn, nid)
                conn.commit()
                item["status"] = "sent"
                result["sent"] += 1
            except Exception as exc:
                masked = config.mask_sensitive_text(exc)
                if _telegram_error_is_permanent(exc):
                    db_layer.mark_notification_cancelled(conn, nid, f"permanent telegram send failure: {masked}")
                    conn.commit()
                    item["status"] = "skipped"
                    item["error"] = masked
                    result["skipped"] += 1
                else:
                    retry = db_layer.mark_notification_send_error(
                        conn,
                        nid,
                        f"transient telegram send failure: {masked}",
                        max_attempts=config.NOTIFICATION_MAX_ATTEMPTS,
                        backoff_seconds=_notification_backoff_seconds(row.get("AttemptCount")),
                    )
                    conn.commit()
                    item["status"] = retry.get("status")
                    item["error"] = masked
                    item["backoff_seconds"] = retry.get("backoff_seconds")
                    if retry.get("status") == "queued":
                        result["retried"] += 1
                    else:
                        result["failed"] += 1
            result["items"].append(item)
        return result
    finally:
        if own_conn:
            conn.close()


async def send_queued_notifications_once_async(limit: int = 20, dry_run: bool = False, token: str | None = None) -> dict[str, Any]:
    bot_token = token or config.TELEGRAM_BOT_TOKEN
    if not dry_run and not bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
    bot = Bot(bot_token or "0:dry-run-token")
    return await send_queued_notifications(bot, limit=limit, dry_run=dry_run)


def send_queued_notifications_once(limit: int = 20, dry_run: bool = False, token: str | None = None) -> dict[str, Any]:
    return asyncio.run(send_queued_notifications_once_async(limit=limit, dry_run=dry_run, token=token))
