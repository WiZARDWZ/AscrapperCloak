from __future__ import annotations

import asyncio
from typing import Any

import config
import db_layer
from telegram import Bot


async def send_queued_notifications(bot: Bot, limit: int = 20, dry_run: bool = False, conn=None) -> dict[str, Any]:
    own_conn = conn is None
    if conn is None:
        conn = db_layer.connect(config.DB_PATH)
    result = {"processed": 0, "sent": 0, "failed": 0, "skipped": 0, "dry_run": bool(dry_run), "items": []}
    try:
        rows = db_layer.get_queued_notifications(conn, limit=limit, channel="telegram")
        for row in rows:
            nid = row.get("NotificationID")
            chat_id = row.get("ChatID")
            text = row.get("MessageText") or ""
            item = {"notification_id": nid, "chat_id": chat_id, "event_id": row.get("EventID")}
            result["processed"] += 1
            if dry_run:
                print(f"--- notification {nid} chat={chat_id} ---")
                print(text)
                item["status"] = "preview"
                result["items"].append(item)
                continue
            if not chat_id:
                db_layer.mark_notification_failed(conn, nid, "Missing ChatID for telegram notification")
                conn.commit()
                item["status"] = "failed"
                item["error"] = "missing_chat_id"
                result["failed"] += 1
                result["items"].append(item)
                continue
            try:
                db_layer.mark_notification_sending(conn, nid)
                conn.commit()
                await bot.send_message(chat_id=str(chat_id), text=text, disable_web_page_preview=True)
                db_layer.mark_notification_sent(conn, nid)
                conn.commit()
                item["status"] = "sent"
                result["sent"] += 1
            except Exception as exc:
                db_layer.mark_notification_failed(conn, nid, config.mask_sensitive_text(exc))
                conn.commit()
                item["status"] = "failed"
                item["error"] = config.mask_sensitive_text(exc)
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
