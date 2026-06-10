from __future__ import annotations

import asyncio
import logging
import socket
import sys
import os
import uuid
from dataclasses import dataclass, field
from typing import Any

import config
import db_layer
import job_queue
import excel_exporter
from monitoring_scheduler import enqueue_due_monitoring_jobs, run_next_job_once
from area_resolver import resolve_nsw_area_query
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

BUTTON_ADD = "➕ Add suburb"
BUTTON_AREAS = "📍 My suburbs"
BUTTON_EXPORT = "📊 Export Excel"
BUTTON_REMOVE = "🗑 Remove suburb"
BUTTON_CHECK = "🔄 Check now"
BUTTON_HELP = "ℹ️ Help"
BUTTON_BACK = "Back to menu"

MAIN_MENU_BUTTONS = (BUTTON_ADD, BUTTON_AREAS, BUTTON_EXPORT, BUTTON_HELP)
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
START_IMAGE_PATH = os.getenv("START_IMAGE_PATH", os.path.join(PROJECT_ROOT, "assets", "start.png"))
WELCOME_TEXT = """🏡 Welcome to OzHome Monitor

Your smart assistant for tracking NSW property listings.

With this bot you can:
• Monitor selected suburbs
• Detect new listings
• Track price, agent and inspection changes
• Export updated property data to Excel

Choose an option below to get started 👇"""
HELP_TEXT = """OzHome Monitor tracks NSW property listings automatically.

What I do:
• Monitor your selected suburbs
• Detect new listings
• Detect ad price and inferred price range changes
• Detect sold or removed listings
• Detect inspection, agent and property detail changes
• Send Telegram updates when something changes
• Export current active listings to Excel

How it works:
1. Add a suburb
2. I prepare the initial data
3. Monitoring starts automatically
4. You receive updates when changes are detected

Use My suburbs to view or remove suburbs.
Use Export Excel to download current active listings."""
AREA_PROMPT = """Send a NSW suburb name or postcode.

Examples:
• Petersham
• 2049
• Petersham 2049
• Petersham, NSW 2049"""
NSW_ONLY_MESSAGE = "I can currently monitor NSW suburbs only. Please send a valid NSW suburb name or postcode."

logger = logging.getLogger(__name__)


@dataclass
class RuntimeState:
    scheduler_loop_running: bool = False
    worker_loop_running: bool = False
    heartbeat_loop_running: bool = False
    last_scheduler_summary: dict[str, Any] = field(default_factory=lambda: {"status": "not_started"})
    last_worker_summary: dict[str, Any] = field(default_factory=lambda: {"status": "not_started"})


RUNTIME_STATE = RuntimeState()


def parse_admin_telegram_ids(value: str | None = None) -> set[str]:
    return config.parse_admin_telegram_ids(value)


def _user_id(update: Update) -> str | None:
    user = update.effective_user
    return str(user.id) if user and user.id is not None else None


def _admin_identity_matches(update: Update) -> bool:
    admin_ids = parse_admin_telegram_ids()
    user_id = _user_id(update)
    chat_id = _chat_id(update) if update.effective_chat else None
    return bool((user_id and user_id in admin_ids) or (chat_id and chat_id in admin_ids))


def _is_admin_chat(update: Update) -> bool:
    return _admin_identity_matches(update)


def _log_admin_command(update: Update, command_name: str, authorized: bool) -> None:
    logger.info(
        "admin command received command=%s user_id=%s chat_id=%s authorized=%s",
        command_name,
        _user_id(update),
        _chat_id(update) if update.effective_chat else None,
        str(bool(authorized)).lower(),
    )


def _safe_register_chat(update: Update) -> None:
    try:
        register_chat(update)
    except Exception as exc:
        logger.exception("failed to register chat for admin command: %s", config.mask_sensitive_text(exc))


def _summarize_scheduler_result(result: dict[str, Any] | None) -> dict[str, Any]:
    result = result or {}
    stale = result.get("stale_recovery") or {}
    return {
        "status": "completed",
        "created": len(result.get("created") or []),
        "skipped_duplicates": len(result.get("skipped_duplicates") or []),
        "blocked_by_active_duplicate": len(result.get("skipped_duplicates") or []),
        "ready_searches": len(result.get("ready_search_ids_considered") or []),
        "not_ready_searches": len(result.get("not_ready_search_ids_considered") or []),
        "not_due": len(result.get("not_due") or []),
        "stale_running_recovered": stale.get("recovered_count", 0),
        "stale_running_failed": stale.get("failed_count", 0),
        "stale_job_ids": stale.get("stale_job_ids") or [],
        "recovered_job_types": stale.get("recovered_job_types") or [],
        "errors": len(result.get("errors") or []),
    }


def _summarize_worker_result(result: dict[str, Any] | None) -> dict[str, Any]:
    result = result or {}
    claimed = result.get("claimed_job") or {}
    summary = {
        "status": result.get("status", "unknown"),
        "job_id": claimed.get("JobID"),
        "job_type": claimed.get("JobType"),
        "search_id": claimed.get("SearchID"),
    }
    if result.get("reason"):
        summary["reason"] = result.get("reason")
    job_result = result.get("job_result") or {}
    if job_result:
        summary["job_result_status"] = job_result.get("status")
    if result.get("error"):
        summary["error"] = config.mask_sensitive_text(result.get("error"))
    return summary


def _format_summary(summary: dict[str, Any]) -> str:
    if not summary:
        return "none"
    return ", ".join(f"{key}={value}" for key, value in summary.items() if value is not None)


def _queue_status_snapshot(next_due_limit: int = 5) -> dict[str, Any]:
    active = job_queue.get_active_jobs()
    failed_by_lifecycle = job_queue.get_failed_job_summary_by_lifecycle(limit=5, include_inactive=True)
    return {
        "summary": job_queue.get_queue_summary(),
        "running_jobs": [row for row in active if str(row.get("Status") or "").lower() == "running"],
        "retry_wait_jobs": [row for row in active if str(row.get("Status") or "").lower() == "retry_wait"],
        "next_due_jobs": job_queue.get_next_due_jobs(limit=next_due_limit),
        "active_failed_jobs": failed_by_lifecycle["active_failed_jobs"],
        "inactive_failed_jobs": failed_by_lifecycle["inactive_failed_jobs"],
        "failed_jobs": failed_by_lifecycle["active_failed_jobs"],
    }


def _queue_counts_text(summary: dict[str, Any]) -> str:
    counts = summary.get("counts") if isinstance(summary, dict) else None
    if isinstance(counts, dict):
        return ", ".join(f"{key}={value}" for key, value in sorted(counts.items())) or "none"
    if isinstance(counts, list):
        return ", ".join(f"P{row.get('Priority')}:{row.get('Status')}={row.get('Count')}" for row in counts) or "none"
    return "none"


def _subscription_counts() -> dict[str, int]:
    conn = _connect()
    try:
        subs = db_layer.get_active_user_area_subscriptions(conn)
    finally:
        conn.close()
    ready_search_ids = {
        int(sub["SearchID"])
        for sub in subs
        if sub.get("SearchID") is not None
        and str(sub.get("BaselineStatus") or "").lower() == "completed"
        and str(sub.get("DetailBaselineStatus") or "").lower() == "completed"
        and str(sub.get("PriceBaselineStatus") or "pending").lower() == "completed"
        and sub.get("NotificationReadyAt")
    }
    return {"active_subscriptions": len(subs), "ready_searches": len(ready_search_ids)}


def _next_due_jobs_text(jobs: list[dict[str, Any]]) -> str:
    if not jobs:
        return "none"
    lines = []
    for job in jobs[:5]:
        label = job.get("AreaLabel") or f"SearchID {job.get('SearchID')}"
        lines.append(f"#{job.get('JobID')} P{job.get('Priority')} {job.get('JobType')} — {label}")
    return "\n".join(lines)


def _area_label_from(area: dict[str, Any] | None, search_id: int | None = None, result: Any = None) -> str:
    result_label = getattr(result, "area_label", None) if result is not None else None
    if result_label:
        return str(result_label)
    area = area or {}
    for key in ("name", "label", "AreaLabel", "area_label", "SearchDisplayName"):
        value = area.get(key)
        if value:
            return str(value)
    if area.get("SearchURL"):
        return str(area["SearchURL"])
    resolved_search_id = search_id if search_id is not None else area.get("SearchID")
    return f"Search #{resolved_search_id}" if resolved_search_id is not None else "Search area"


def _failed_jobs_text(jobs: list[dict[str, Any]]) -> str:
    if not jobs:
        return "none"
    parts = []
    for job in jobs[:5]:
        err = config.mask_sensitive_text(job.get("last_error") or "")
        if len(err) > 160:
            err = err[:157] + "..."
        parts.append(
            f"#{job.get('job_id')} {job.get('job_type')} search_id={job.get('search_id')} "
            f"status={job.get('status')} attempts={job.get('attempts')} updated_at={job.get('updated_at')} last_error={err}"
        )
    return " | ".join(parts)


def ensure_runtime_schema() -> None:
    conn = _connect()
    try:
        db_layer.ensure_telegram_bot_tables(conn)
        job_queue.ensure_job_tables(conn)
        recovery = job_queue.recover_stale_running_jobs(conn=conn)
        logger.info("startup stale job recovery: %s", _format_summary(_summarize_stale_recovery(recovery)))
    finally:
        conn.close()


def _summarize_stale_recovery(recovery: dict[str, Any] | None) -> dict[str, Any]:
    recovery = recovery or {}
    return {
        "stale_running_recovered": recovery.get("recovered_count", 0),
        "stale_running_failed": recovery.get("failed_count", 0),
        "stale_job_ids": recovery.get("stale_job_ids") or [],
        "recovered_job_types": recovery.get("recovered_job_types") or [],
    }


async def scheduler_loop() -> None:
    RUNTIME_STATE.scheduler_loop_running = True
    logger.info("Queue scheduler loop started; interval=%ss", config.SCHEDULER_LOOP_SECONDS)
    try:
        while True:
            try:
                result = await asyncio.to_thread(enqueue_due_monitoring_jobs)
                RUNTIME_STATE.last_scheduler_summary = _summarize_scheduler_result(result)
                logger.info("scheduler tick: %s", _format_summary(RUNTIME_STATE.last_scheduler_summary))
            except Exception as exc:
                RUNTIME_STATE.last_scheduler_summary = {"status": "error", "error": config.mask_sensitive_text(exc)}
                logger.exception("scheduler loop error: %s", config.mask_sensitive_text(exc))
            await asyncio.sleep(config.SCHEDULER_LOOP_SECONDS)
    except asyncio.CancelledError:
        logger.info("Queue scheduler loop stopping")
        raise
    finally:
        RUNTIME_STATE.scheduler_loop_running = False


async def worker_loop() -> None:
    RUNTIME_STATE.worker_loop_running = True
    worker_id = f"telegram-bot-{socket.gethostname()}"
    logger.info("Queue worker loop started; idle_sleep=%ss worker_id=%s", config.WORKER_IDLE_SLEEP_SECONDS, worker_id)
    try:
        while True:
            try:
                result = await asyncio.to_thread(run_next_job_once, worker_id=worker_id, send_telegram=True)
                RUNTIME_STATE.last_worker_summary = _summarize_worker_result(result)
                if result.get("status") != "idle":
                    logger.info("worker tick: %s", _format_summary(RUNTIME_STATE.last_worker_summary))
                await asyncio.sleep(0 if result.get("status") != "idle" else config.WORKER_IDLE_SLEEP_SECONDS)
            except Exception as exc:
                RUNTIME_STATE.last_worker_summary = {"status": "error", "error": config.mask_sensitive_text(exc)}
                logger.exception("worker loop error: %s", config.mask_sensitive_text(exc))
                await asyncio.sleep(config.WORKER_ERROR_SLEEP_SECONDS)
    except asyncio.CancelledError:
        logger.info("Queue worker loop stopping")
        raise
    finally:
        RUNTIME_STATE.worker_loop_running = False


async def heartbeat_loop() -> None:
    RUNTIME_STATE.heartbeat_loop_running = True
    logger.info("Heartbeat loop started; interval=%ss", config.HEARTBEAT_SECONDS)
    try:
        while True:
            try:
                snapshot = await asyncio.to_thread(_queue_status_snapshot, 5)
                logger.info(
                    "heartbeat: queue=%s running=%s retry_wait=%s next_due=%s active_failed_jobs=%s inactive_failed_jobs=%s last_scheduler=(%s) last_worker=(%s)",
                    _queue_counts_text(snapshot["summary"]),
                    len(snapshot["running_jobs"]),
                    len(snapshot["retry_wait_jobs"]),
                    len(snapshot["next_due_jobs"]),
                    _failed_jobs_text(snapshot["active_failed_jobs"]),
                    len(snapshot["inactive_failed_jobs"]),
                    _format_summary(RUNTIME_STATE.last_scheduler_summary),
                    _format_summary(RUNTIME_STATE.last_worker_summary),
                )
            except Exception as exc:
                logger.exception("heartbeat loop error: %s", config.mask_sensitive_text(exc))
            await asyncio.sleep(config.HEARTBEAT_SECONDS)
    except asyncio.CancelledError:
        logger.info("Heartbeat loop stopping")
        raise
    finally:
        RUNTIME_STATE.heartbeat_loop_running = False


async def start_background_runtime(app: Application) -> None:
    tasks = app.bot_data.setdefault("runtime_tasks", set())
    for coro in (scheduler_loop(), worker_loop(), heartbeat_loop()):
        task = asyncio.create_task(coro)
        tasks.add(task)
        task.add_done_callback(tasks.discard)


async def stop_background_runtime(app: Application) -> None:
    tasks = set(app.bot_data.get("runtime_tasks", set()))
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def _connect():
    return db_layer.connect(config.DB_PATH)


def _user_names(update: Update) -> dict[str, Any]:
    user = update.effective_user
    return {"username": user.username if user else None, "first_name": user.first_name if user else None, "last_name": user.last_name if user else None}


def _chat_id(update: Update) -> str:
    return str(update.effective_chat.id)


def register_chat(update: Update) -> int:
    conn = _connect()
    try:
        return db_layer.upsert_telegram_user(conn, _chat_id(update), **_user_names(update))
    finally:
        conn.close()


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton(BUTTON_ADD), KeyboardButton(BUTTON_AREAS)], [KeyboardButton(BUTTON_EXPORT), KeyboardButton(BUTTON_HELP)]],
        resize_keyboard=True,
    )


def _suburb_actions_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(BUTTON_ADD, callback_data="menu:add")], [InlineKeyboardButton(BUTTON_BACK, callback_data="menu:back")]])


def _candidate_keyboard(matches: list[dict]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(match["label"], callback_data=f"area_candidate:{index}")] for index, match in enumerate(matches[:10])]
    rows.append([InlineKeyboardButton("🔍 Search again", callback_data="area_confirm:search"), InlineKeyboardButton("❌ Cancel", callback_data="area_confirm:cancel")])
    return InlineKeyboardMarkup(rows)


def _confirm_area_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("✅ Add this search area", callback_data="area_confirm:add")], [InlineKeyboardButton("🔍 Search again", callback_data="area_confirm:search"), InlineKeyboardButton("❌ Cancel", callback_data="area_confirm:cancel")]])


def _export_area_keyboard(areas: list[dict]) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                str(area.get("AreaLabel") or area.get("SearchURL") or f"Search {area.get('SearchID')}"),
                callback_data=f"export_area:{area['UserAreaID']}",
            )
        ]
        for area in areas
    ]
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="export_area:cancel")])
    return InlineKeyboardMarkup(rows)


def _format_export_timestamp(value) -> str:
    try:
        return value.strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        return str(value)


def _remove_area_keyboard(areas: list[dict]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(str(area.get("AreaLabel") or area.get("SearchURL")), callback_data=f"remove_select:{area['UserAreaID']}")] for area in areas]
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="remove_confirm:cancel")])
    return InlineKeyboardMarkup(rows)


def _my_suburbs_keyboard(areas: list[dict]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(f"Remove {area.get('AreaLabel') or area.get('SearchURL')}", callback_data=f"remove_select:{area['UserAreaID']}")] for area in areas]
    rows.append([InlineKeyboardButton(BUTTON_BACK, callback_data="menu:back")])
    return InlineKeyboardMarkup(rows)


def _remove_confirm_keyboard(user_area_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("✅ Yes, remove", callback_data=f"remove_confirm:{int(user_area_id)}"), InlineKeyboardButton("❌ Cancel", callback_data="remove_confirm:cancel")]])


def _status_label(subscription: dict) -> str:
    baseline = str(subscription.get("BaselineStatus") or "pending").lower()
    detail = str(subscription.get("DetailBaselineStatus") or "pending").lower()
    price = str(subscription.get("PriceBaselineStatus") or "pending").lower()
    if subscription.get("NotificationReadyAt") and detail == "completed" and price == "completed":
        return "Ready"
    if baseline == "failed" or detail == "failed" or price == "failed":
        return "Failed — retry option coming soon"
    if baseline == "retry_wait" or detail == "retry_wait" or price == "retry_wait":
        return "Preparing — retrying setup"
    if baseline == "running" or detail == "running" or price == "running":
        return "Preparing — setup in progress"
    if baseline == "completed" and detail == "completed" and price != "completed":
        return "Preparing — price setup in progress"
    return "Preparing — setup queued"


def _session(telegram_user_id: int) -> dict:
    conn = _connect()
    try:
        return db_layer.get_user_session(conn, telegram_user_id)
    finally:
        conn.close()


def _set_session(telegram_user_id: int, state: str, payload: dict | None = None) -> None:
    conn = _connect()
    try:
        db_layer.set_user_session(conn, telegram_user_id, state, payload)
    finally:
        conn.close()


def _clear_session(telegram_user_id: int) -> None:
    conn = _connect()
    try:
        db_layer.clear_user_session(conn, telegram_user_id)
    finally:
        conn.close()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    telegram_user_id = register_chat(update)
    _clear_session(telegram_user_id)
    if START_IMAGE_PATH and os.path.exists(START_IMAGE_PATH):
        with open(START_IMAGE_PATH, "rb") as photo:
            await update.message.reply_photo(photo=photo, caption=WELCOME_TEXT, reply_markup=main_menu_keyboard())
        return
    await update.message.reply_text(WELCOME_TEXT, reply_markup=main_menu_keyboard())


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    register_chat(update)
    await update.message.reply_text(HELP_TEXT, reply_markup=main_menu_keyboard())


async def _begin_add_area(update: Update) -> None:
    telegram_user_id = register_chat(update)
    _set_session(telegram_user_id, "waiting_for_area_input", {})
    await update.message.reply_text(AREA_PROMPT, reply_markup=main_menu_keyboard())


async def addarea(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if getattr(context, "args", None):
        telegram_user_id = register_chat(update)
        await _resolve_area_text(update, telegram_user_id, " ".join(context.args))
        return
    await _begin_add_area(update)


async def _resolve_area_text(update: Update, telegram_user_id: int, text: str) -> None:
    conn = _connect()
    try:
        result = resolve_nsw_area_query(conn, text)
    finally:
        conn.close()
    if result["status"] == "exact":
        area = result["matches"][0]
        _set_session(telegram_user_id, "confirming_area", {"pending_area": area})
        await update.message.reply_text(f"I found:\n\n📍 {area['label']}\n\nAdd this search area?", reply_markup=_confirm_area_keyboard())
        return
    if result["status"] in {"multiple", "suggestions"}:
        matches = result["matches"][:10]
        _set_session(telegram_user_id, "choosing_area_candidate", {"matches": matches})
        text = "I found several NSW suburbs. Which one do you want to monitor?" if result["status"] == "multiple" else "I could not find an exact match. Did you mean:"
        await update.message.reply_text(text, reply_markup=_candidate_keyboard(matches))
        return
    _set_session(telegram_user_id, "waiting_for_area_input", {})
    await update.message.reply_text(NSW_ONLY_MESSAGE)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.message.text or "").strip()
    if text == BUTTON_ADD:
        await _begin_add_area(update)
        return
    if text == BUTTON_AREAS:
        await areas(update, context)
        return
    if text == BUTTON_EXPORT:
        await export(update, context)
        return
    if text == BUTTON_HELP:
        await help_command(update, context)
        return
    telegram_user_id = register_chat(update)
    if _session(telegram_user_id).get("state") == "waiting_for_area_input":
        await _resolve_area_text(update, telegram_user_id, text)
        return
    await update.message.reply_text("Please choose an option from the menu.", reply_markup=main_menu_keyboard())


async def handle_area_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    telegram_user_id = register_chat(update)
    session = _session(telegram_user_id)
    if query.data == "area_confirm:cancel":
        _clear_session(telegram_user_id)
        await query.edit_message_text("Cancelled.")
        await update.effective_chat.send_message("Choose an option below.", reply_markup=main_menu_keyboard())
        return
    if query.data == "area_confirm:search":
        _set_session(telegram_user_id, "waiting_for_area_input", {})
        await query.edit_message_text(AREA_PROMPT)
        return
    if query.data.startswith("area_candidate:"):
        try:
            area = session.get("payload", {}).get("matches", [])[int(query.data.split(":", 1)[1])]
        except (IndexError, TypeError, ValueError):
            await query.edit_message_text("That suburb choice expired. Please search again.")
            return
        _set_session(telegram_user_id, "confirming_area", {"pending_area": area})
        await query.edit_message_text(f"I found:\n\n📍 {area['label']}\n\nAdd this search area?", reply_markup=_confirm_area_keyboard())
        return
    area = session.get("payload", {}).get("pending_area")
    if query.data != "area_confirm:add" or not area:
        await query.edit_message_text("That suburb choice expired. Please search again.")
        return
    conn = _connect()
    try:
        ok, payload = db_layer.add_user_area_subscription(conn, telegram_user_id, area["search_url"], area["label"], suburb=area["suburb_name"], state_code="NSW", postcode=area["postcode"])
    finally:
        conn.close()
    _clear_session(telegram_user_id)
    if not ok:
        if payload.get("reason") == "duplicate":
            message = payload.get("message") or "You're already monitoring this search area."
        elif payload.get("reason") == "max_areas":
            message = f"You can monitor up to {config.MAX_AREAS_PER_USER} suburbs. Remove one before adding another."
        else:
            message = payload.get("message", "Could not add search area.")
        await query.edit_message_text(message)
    else:
        await query.edit_message_text(payload.get("message") or "I will prepare this search area now. Monitoring will start after the initial data is collected.")
    await update.effective_chat.send_message("Choose an option below.", reply_markup=main_menu_keyboard())


async def areas(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    telegram_user_id = register_chat(update)
    conn = _connect()
    try:
        subs = db_layer.list_user_area_subscriptions(conn, telegram_user_id)
    finally:
        conn.close()
    if not subs:
        await update.message.reply_text("You are not monitoring any search areas yet.", reply_markup=_suburb_actions_keyboard())
        return
    lines = ["Your monitored NSW search areas:", ""] + [f"• {sub.get('AreaLabel')} — {_status_label(sub)}" for sub in subs]
    await update.message.reply_text("\n".join(lines), reply_markup=_my_suburbs_keyboard(subs))


async def removearea(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    telegram_user_id = register_chat(update)
    conn = _connect()
    try:
        subs = db_layer.list_user_area_subscriptions(conn, telegram_user_id)
    finally:
        conn.close()
    if not subs:
        await update.message.reply_text("You are not monitoring any search areas yet.", reply_markup=main_menu_keyboard())
        return
    await update.message.reply_text("Which search area do you want to stop monitoring?", reply_markup=_remove_area_keyboard(subs))


async def handle_remove_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    telegram_user_id = register_chat(update)
    session = _session(telegram_user_id)
    if query.data == "remove_confirm:cancel":
        _clear_session(telegram_user_id)
        await query.edit_message_text("Cancelled.")
        await update.effective_chat.send_message("Choose an option below.", reply_markup=main_menu_keyboard())
        return
    if query.data.startswith("remove_select:"):
        user_area_id = int(query.data.split(":", 1)[1])
        conn = _connect()
        try:
            areas = db_layer.list_user_area_subscriptions(conn, telegram_user_id)
        finally:
            conn.close()
        area = next((item for item in areas if int(item["UserAreaID"]) == user_area_id), None)
        if not area:
            await query.edit_message_text("That suburb is no longer active for your account.")
            return
        await query.edit_message_text(f"Stop monitoring {area['AreaLabel']}?", reply_markup=_remove_confirm_keyboard(user_area_id))
        return
    if not query.data.startswith("remove_confirm:"):
        await query.edit_message_text("That suburb choice expired. Please try again.")
        return
    user_area_id = int(query.data.split(":", 1)[1])
    conn = _connect()
    try:
        areas = db_layer.list_user_area_subscriptions(conn, telegram_user_id)
        area = next((item for item in areas if int(item["UserAreaID"]) == user_area_id), None)
        lifecycle = db_layer.remove_user_area_subscription_lifecycle(conn, telegram_user_id, user_area_id)
    finally:
        conn.close()
    logger.info(
        "remove area requested: telegram_user_id=%s user_area_id=%s resolved_search_id=%s resolved_area_id=%s remaining_active_subscriptions=%s action=%s cancelled_jobs=%s",
        telegram_user_id,
        user_area_id,
        lifecycle.get("resolved_search_id"),
        lifecycle.get("resolved_area_id"),
        lifecycle.get("remaining_active_subscriptions"),
        lifecycle.get("action"),
        lifecycle.get("cancelled_jobs"),
    )
    await query.edit_message_text(f"Stopped monitoring {area.get('AreaLabel') if area else 'that suburb'}." if lifecycle.get("removed") else "That suburb is already inactive.")
    await update.effective_chat.send_message("Choose an option below.", reply_markup=main_menu_keyboard())


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    action = update.callback_query.data.split(":", 1)[1]
    await update.callback_query.answer()
    if action == "back":
        await update.callback_query.edit_message_text("Main menu")
        await update.effective_chat.send_message("Choose an option below.", reply_markup=main_menu_keyboard())
    elif action == "add":
        _set_session(register_chat(update), "waiting_for_area_input", {})
        await update.callback_query.edit_message_text(AREA_PROMPT)
    elif action == "export":
        await update.callback_query.edit_message_text("Use the 📊 Export Excel button below.")
        await update.effective_chat.send_message("Choose an option below.", reply_markup=main_menu_keyboard())


async def check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    register_chat(update)
    if _is_admin_chat(update):
        await update.message.reply_text("Admin refresh commands are handled by the scheduler/worker runtime.", reply_markup=main_menu_keyboard())
        return
    await update.message.reply_text("Monitoring runs automatically. You’ll receive updates when changes are detected.", reply_markup=main_menu_keyboard())


async def refresh(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await check(update, context)


async def handle_export_excel_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    telegram_user_id = register_chat(update)
    try:
        areas = await asyncio.to_thread(excel_exporter.get_user_export_areas, telegram_user_id)
    except Exception as exc:
        logger.exception("export area lookup failed for telegram_user_id=%s: %s", telegram_user_id, config.mask_sensitive_text(exc))
        await update.message.reply_text("Could not load your export areas right now.", reply_markup=main_menu_keyboard())
        return
    if not areas:
        await update.message.reply_text("You have no active search areas to export.", reply_markup=main_menu_keyboard())
        return
    await update.message.reply_text("Choose a search area to export:", reply_markup=_export_area_keyboard(areas))


async def handle_export_area_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    telegram_user_id = register_chat(update)
    if query.data == "export_area:cancel":
        await query.edit_message_text("Cancelled.")
        await update.effective_chat.send_message("Choose an option below.", reply_markup=main_menu_keyboard())
        return
    try:
        user_area_id = int(query.data.split(":", 1)[1])
    except (IndexError, TypeError, ValueError):
        await query.edit_message_text("That export choice is invalid. Please try again.")
        return
    area = await asyncio.to_thread(excel_exporter.get_authorized_export_area, telegram_user_id, user_area_id)
    if not area:
        await query.edit_message_text("That export area is not available for your account.")
        return
    area_label = _area_label_from(area, area.get("SearchID"))
    search_id = int(area["SearchID"])
    await query.edit_message_text(f"Generating Excel for {area_label}...")
    try:
        export_mode = "debug" if config.EXCEL_EXPORT_MODE == "debug" and _is_admin_chat(update) else "normal"
        result = await asyncio.to_thread(excel_exporter.build_active_listings_excel, search_id, area_label, mode=export_mode)
        result_area_label = _area_label_from(area, search_id, result)
        logger.info(
            "Excel export: mode=%s telegram_user_id=%s user_area_id=%s resolved_search_id=%s rows=%s file=%s",
            export_mode,
            telegram_user_id,
            user_area_id,
            search_id,
            result.active_listing_count,
            result.file_path,
        )
        if int(result.active_listing_count or 0) == 0:
            diagnostics = await asyncio.to_thread(excel_exporter.get_zero_row_diagnostics, telegram_user_id, user_area_id, search_id)
            logger.warning(
                "Excel export generated zero rows telegram_user_id=%s user_area_id=%s resolved_search_id=%s diagnostics=%s",
                telegram_user_id,
                user_area_id,
                search_id,
                config.mask_sensitive_text(diagnostics),
            )
            await update.effective_chat.send_message("Excel export generated no rows for this area. Please try again after monitoring refreshes.", reply_markup=main_menu_keyboard())
            return
        caption = (
            f"Area: {result_area_label}\n"
            f"Active listings: {result.active_listing_count}\n"
            f"Generated: {_format_export_timestamp(result.generated_at)}"
        )
        with open(result.file_path, "rb") as document:
            await update.effective_chat.send_document(document=document, filename=getattr(result, "filename", None) or os.path.basename(result.file_path), caption=caption)
        await update.effective_chat.send_message("Choose an option below.", reply_markup=main_menu_keyboard())
    except Exception as exc:
        debug_id = f"EXPORT-{uuid.uuid4().hex[:8].upper()}"
        logger.exception("excel export failed debug_id=%s telegram_user_id=%s user_area_id=%s search_id=%s: %s", debug_id, telegram_user_id, user_area_id, search_id, config.mask_sensitive_text(exc))
        await update.effective_chat.send_message(f"Could not generate the Excel export right now. Debug ID: {debug_id}", reply_markup=main_menu_keyboard())


async def export(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await handle_export_excel_menu(update, context)


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    authorized = _is_admin_chat(update)
    _log_admin_command(update, "status", authorized)
    _safe_register_chat(update)
    if not authorized:
        await update.message.reply_text("Unauthorized.", reply_markup=main_menu_keyboard())
        return
    try:
        snapshot, subscriptions = await asyncio.gather(asyncio.to_thread(_queue_status_snapshot, 5), asyncio.to_thread(_subscription_counts))
        text = (
            "Status\n"
            "Bot: running\n"
            f"Queue loop: {'running' if RUNTIME_STATE.scheduler_loop_running else 'stopped'}\n"
            f"Worker loop: {'running' if RUNTIME_STATE.worker_loop_running else 'stopped'}\n"
            f"Queue: {_queue_counts_text(snapshot['summary'])}\n"
            f"Running jobs: {len(snapshot['running_jobs'])}\n"
            f"Retry-wait jobs: {len(snapshot['retry_wait_jobs'])}\n"
            f"Next due jobs: {len(snapshot['next_due_jobs'])}\n"
            f"Active failed jobs: {len(snapshot['active_failed_jobs'])}\n"
            f"Inactive failed jobs: {len(snapshot['inactive_failed_jobs'])}\n"
            f"Active subscriptions: {subscriptions['active_subscriptions']}\n"
            f"Ready searches: {subscriptions['ready_searches']}\n"
            f"Last scheduler: {_format_summary(RUNTIME_STATE.last_scheduler_summary)}\n"
            f"Last worker: {_format_summary(RUNTIME_STATE.last_worker_summary)}"
        )
    except Exception as exc:
        logger.exception("admin /status error: %s", config.mask_sensitive_text(exc))
        text = "Status unavailable right now."
    await update.message.reply_text(text, reply_markup=main_menu_keyboard())


async def queue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    authorized = _is_admin_chat(update)
    _log_admin_command(update, "queue", authorized)
    _safe_register_chat(update)
    if not authorized:
        await update.message.reply_text("Unauthorized.", reply_markup=main_menu_keyboard())
        return
    try:
        snapshot = await asyncio.to_thread(_queue_status_snapshot, 5)
        text = (
            "Queue\n"
            f"Counts: {_queue_counts_text(snapshot['summary'])}\n"
            f"Running jobs: {len(snapshot['running_jobs'])}\n"
            f"Retry-wait jobs: {len(snapshot['retry_wait_jobs'])}\n"
            "Next due jobs:\n"
            f"{_next_due_jobs_text(snapshot['next_due_jobs'])}"
        )
    except Exception as exc:
        logger.exception("admin /queue error: %s", config.mask_sensitive_text(exc))
        text = "Queue status unavailable right now."
    await update.message.reply_text(text, reply_markup=main_menu_keyboard())


def build_application(token: str) -> Application:
    app = Application.builder().token(token).post_init(start_background_runtime).post_shutdown(stop_background_runtime).build()
    app.add_handler(CommandHandler("status", status), group=-1)
    app.add_handler(CommandHandler("queue", queue), group=-1)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("addarea", addarea))
    app.add_handler(CommandHandler("areas", areas))
    app.add_handler(CommandHandler("removearea", removearea))
    app.add_handler(CommandHandler("check", check))
    app.add_handler(CommandHandler("refresh", refresh))
    app.add_handler(CommandHandler("export", export))
    app.add_handler(CallbackQueryHandler(handle_area_callback, pattern="^(area_candidate:|area_confirm:).+"))
    app.add_handler(CallbackQueryHandler(handle_remove_callback, pattern="^(remove_select:|remove_confirm:).+"))
    app.add_handler(CallbackQueryHandler(handle_export_area_selection, pattern="^export_area:"))
    app.add_handler(CallbackQueryHandler(menu_callback, pattern="^menu:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    return app


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for logger_name in ("httpx", "httpcore", "telegram.request", "telegram.vendor.ptb_urllib3"):
        logging.getLogger(logger_name).setLevel(logging.WARNING)


def ensure_main_event_loop() -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())


def main() -> None:
    configure_logging()
    ensure_main_event_loop()
    for key, value in config.safe_runtime_summary().items():
        logger.info("runtime config %s=%s", key, value)
    if not config.TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
    missing = config.validate_runtime_config(require_db=config.is_production())
    if missing:
        raise RuntimeError(f"Missing required production configuration: {', '.join(missing)}")
    ensure_runtime_schema()
    logger.info("Starting Telegram bot polling with queue scheduler/worker runtime")
    build_application(config.TELEGRAM_BOT_TOKEN).run_polling()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {config.mask_sensitive_text(exc)}", file=sys.stderr)
        raise
