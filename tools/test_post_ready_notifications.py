"""Deterministic Phase 6 post-ready notification acceptance tests."""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import db_layer
import telegram_sender
from notification_engine import build_notification_key
from tools import dev_cleanup_post_ready_test_events as cleanup
from tools import run_notification_engine_once as engine_runner
from tools import dev_inject_post_ready_event as injector

READY_AT = datetime(2026, 6, 2, 12, 0, 0)


def _subscription(**overrides):
    data = {
        "UserAreaID": 4,
        "TelegramUserID": 7,
        "ChatID": "chat-7",
        "SearchID": 10,
        "SearchURL": "https://example.test/petersham?activeSort=list-date",
        "Suburb": "Petersham",
        "StateCode": "NSW",
        "Postcode": "2049",
        "IsActive": True,
        "BaselineStatus": "completed",
        "BaselineCompletedAt": READY_AT - timedelta(hours=1),
        "DetailBaselineStatus": "completed",
        "NotificationReadyAt": READY_AT,
        "NotificationStartAt": READY_AT - timedelta(hours=2),
    }
    data.update(overrides)
    return data


class InjectionCursor:
    def __init__(self, conn):
        self.conn = conn
        self.row = None

    def execute(self, sql, *params):
        self.conn.executed.append((sql, params))
        if "FROM dbo.Listing l" in sql and "JOIN dbo.ListingSearchState" in sql:
            if "l.listingID=?" in sql:
                listing_id, search_id = params
                self.row = (501, "REA-501") if int(listing_id) == 501 and int(search_id) == 10 else None
            elif "CAST(l.ExternalID" in sql:
                external_id, search_id = params
                self.row = (501, "REA-501") if str(external_id) == "REA-501" and int(search_id) == 10 else None
            else:
                self.row = (501, "REA-501") if int(params[0]) == 10 else None
        elif "SELECT SYSDATETIME()" in sql:
            self.row = (READY_AT + timedelta(seconds=1),)
        elif "INSERT INTO dbo.ListingEvent" in sql:
            self.row = (9001, READY_AT + timedelta(seconds=1))
        return self

    def fetchone(self):
        return self.row


class InjectionConn:
    def __init__(self):
        self.executed = []

    def cursor(self):
        return InjectionCursor(self)


def _with_injector_patches(subscription, callback):
    old_ensure_bot = db_layer.ensure_telegram_bot_tables
    old_ensure_event = db_layer.ensure_listing_event_metadata_columns
    old_get_sub = db_layer.get_user_area_subscription
    old_create_run = db_layer.create_lightweight_scrape_run
    try:
        db_layer.ensure_telegram_bot_tables = lambda conn: None
        db_layer.ensure_listing_event_metadata_columns = lambda conn: None
        db_layer.get_user_area_subscription = lambda conn, user_area_id: subscription
        db_layer.create_lightweight_scrape_run = lambda conn, search_id, source, run_type: 6001
        return callback()
    finally:
        db_layer.ensure_telegram_bot_tables = old_ensure_bot
        db_layer.ensure_listing_event_metadata_columns = old_ensure_event
        db_layer.get_user_area_subscription = old_get_sub
        db_layer.create_lightweight_scrape_run = old_create_run


def test_cannot_inject_when_notification_ready_at_is_null():
    def run():
        try:
            injector.inject_post_ready_event(InjectionConn(), 4)
        except ValueError as exc:
            assert "NotificationReadyAt is NULL" in str(exc)
        else:
            raise AssertionError("injector accepted a subscription before NotificationReadyAt")
    _with_injector_patches(_subscription(NotificationReadyAt=None), run)



def test_injector_rejects_inactive_and_incomplete_detail_baseline_subscriptions():
    for subscription, message in [
        (_subscription(IsActive=False), "not active"),
        (_subscription(DetailBaselineStatus="running"), "not completed"),
    ]:
        try:
            injector.validate_subscription(subscription)
        except ValueError as exc:
            assert message in str(exc)
        else:
            raise AssertionError("injector accepted an unsafe subscription state")


def test_can_inject_after_notification_ready_and_created_at_is_after_gate():
    def run():
        result = injector.inject_post_ready_event(InjectionConn(), 4)
        assert result["event_id"] == 9001
        assert result["listing_id"] == 501
        assert result["external_id"] == "REA-501"
        assert result["search_id"] == 10
        assert result["created_at"] > result["notification_ready_at"]
    _with_injector_patches(_subscription(), run)


def test_dry_run_does_not_create_scrape_run_or_event():
    conn = InjectionConn()
    calls = []
    old_create_run = db_layer.create_lightweight_scrape_run
    try:
        db_layer.create_lightweight_scrape_run = lambda *args, **kwargs: calls.append(1)
        def run():
            result = injector.inject_post_ready_event(conn, 4, dry_run=True)
            assert result["dry_run"] is True
            assert result["event_id"] is None
            assert calls == []
            assert not any("INSERT INTO dbo.ListingEvent" in sql for sql, _ in conn.executed)
        _with_injector_patches(_subscription(), run)
    finally:
        db_layer.create_lightweight_scrape_run = old_create_run


def test_listing_id_from_another_search_id_is_rejected():
    def run():
        try:
            injector.inject_post_ready_event(InjectionConn(), 4, listing_id=999)
        except ValueError as exc:
            assert "does not belong" in str(exc)
        else:
            raise AssertionError("injector accepted a listing from another area")
    _with_injector_patches(_subscription(), run)


def _event(event_id=9001, search_id=10, should_notify=1, reason=injector.TEST_MARKER, address="1 Road, Petersham, NSW 2049"):
    return {
        "EventID": event_id,
        "EventType": "price_changed",
        "ShouldNotify": should_notify,
        "Reason": reason,
        "SearchID": search_id,
        "ListingID": 501,
        "CreatedAt": READY_AT + timedelta(seconds=1),
        "ExternalID": "REA-501",
        "address": address,
        "OldValueJson": '{"field":"price","value":"Guide $1,300,000"}',
        "NewValueJson": '{"field":"price","value":"Guide $1,250,000"}',
        "EventPayloadJson": '{"test_marker":"dev_post_ready_acceptance"}',
        "url": "https://example.test/listing/REA-501",
    }


class QueueState:
    def __init__(self):
        self.notifications = []
        self.keys = set()


def _queue_with_memory_state(state, events):
    old_get_sub = db_layer.get_user_area_subscription
    old_get_events = db_layer.get_notifyable_listing_events
    old_insert = db_layer.insert_notification_outbox_if_new
    old_ensure = db_layer.ensure_notification_tables
    old_mark = db_layer.mark_subscription_notifications_queued
    try:
        db_layer.get_user_area_subscription = lambda conn, user_area_id: _subscription()
        # Model the SQL query's SearchURL/SearchID restriction; address suburb is not a post-filter.
        db_layer.get_notifyable_listing_events = lambda conn, **kwargs: [event for event in events if event.get("SearchID") == 10]
        db_layer.ensure_notification_tables = lambda conn: None
        db_layer.mark_subscription_notifications_queued = lambda conn, user_area_id: None
        def insert(conn, **kwargs):
            key = kwargs["notification_key"]
            if key in state.keys:
                return False
            state.keys.add(key)
            state.notifications.append({"NotificationID": len(state.notifications) + 1, "Status": "queued", **kwargs})
            return True
        db_layer.insert_notification_outbox_if_new = insert
        return db_layer.queue_notifications_for_user_area(object(), 4)
    finally:
        db_layer.get_user_area_subscription = old_get_sub
        db_layer.get_notifyable_listing_events = old_get_events
        db_layer.insert_notification_outbox_if_new = old_insert
        db_layer.ensure_notification_tables = old_ensure
        db_layer.mark_subscription_notifications_queued = old_mark


def test_notification_engine_queues_exactly_once_and_second_run_deduplicates():
    state = QueueState()
    first = _queue_with_memory_state(state, [_event()])
    second = _queue_with_memory_state(state, [_event()])
    assert first["queued_count"] == 1
    assert second["queued_count"] == 0
    assert second["duplicates_count"] == 1
    assert len(state.notifications) == 1
    assert state.notifications[0]["chat_id"] == "chat-7"
    assert state.notifications[0]["user_id"] == 7
    assert state.notifications[0]["notification_key"] == build_notification_key(9001, "chat-7")


def test_missing_subscription_chat_id_skips_without_queued_outbox_row():
    state = QueueState()
    old_get_sub = db_layer.get_user_area_subscription
    old_get_events = db_layer.get_notifyable_listing_events
    try:
        db_layer.get_user_area_subscription = lambda conn, user_area_id: _subscription(ChatID=None)
        db_layer.get_notifyable_listing_events = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("events must not be fetched without ChatID"))
        result = db_layer.queue_notifications_for_user_area(object(), 4)
    finally:
        db_layer.get_user_area_subscription = old_get_sub
        db_layer.get_notifyable_listing_events = old_get_events
    assert result["queued_count"] == 0
    assert result["skipped_reason"] == "missing_chat_id"
    assert state.notifications == []


def test_active_subscription_queue_path_resolves_each_subscription_recipient():
    subscriptions = [_subscription(), _subscription(UserAreaID=5, TelegramUserID=8, ChatID=None)]
    calls = []
    old_get_subs = db_layer.get_active_user_area_subscriptions
    old_queue = db_layer.queue_notifications_for_user_area
    try:
        db_layer.get_active_user_area_subscriptions = lambda conn: subscriptions
        def queue(conn, user_area_id, **kwargs):
            calls.append((user_area_id, kwargs))
            if user_area_id == 4:
                return {"events_input": 1, "notifyable_count": 1, "queued_count": 1, "skipped_count": 0, "duplicates_count": 0, "notifications": [], "errors": []}
            return {"events_input": 0, "notifyable_count": 0, "queued_count": 0, "skipped_count": 0, "duplicates_count": 0, "skipped_reason": "missing_chat_id", "notifications": [], "errors": []}
        db_layer.queue_notifications_for_user_area = queue
        result = db_layer.queue_notifications_for_active_user_areas(object())
    finally:
        db_layer.get_active_user_area_subscriptions = old_get_subs
        db_layer.queue_notifications_for_user_area = old_queue
    assert [item[0] for item in calls] == [4, 5]
    assert result["subscriptions_considered"] == 2
    assert result["queued_count"] == 1
    assert result["subscription_results"][1]["skipped_reason"] == "missing_chat_id"


class RunnerConn:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


def test_notification_engine_runner_queues_per_subscription_and_exercises_duplicates_by_default():
    conn = RunnerConn()
    captured = {}
    old_argv = sys.argv
    old_connect = db_layer.connect
    old_ensure = db_layer.ensure_notification_tables
    old_queue = db_layer.queue_notifications_for_active_user_areas
    try:
        sys.argv = ["run_notification_engine_once.py"]
        db_layer.connect = lambda path: conn
        db_layer.ensure_notification_tables = lambda conn: None
        def queue(conn, **kwargs):
            captured.update(kwargs)
            return {"events_input": 1, "notifyable_count": 1, "queued_count": 0, "skipped_count": 0, "duplicates_count": 1, "dry_run": False, "notifications": [], "errors": [], "subscriptions_considered": 1}
        db_layer.queue_notifications_for_active_user_areas = queue
        engine_runner.main()
    finally:
        sys.argv = old_argv
        db_layer.connect = old_connect
        db_layer.ensure_notification_tables = old_ensure
        db_layer.queue_notifications_for_active_user_areas = old_queue
    assert captured["include_already_queued"] is True
    assert conn.commits == 2
    assert conn.closed is True


def test_search_id_scope_allows_nearby_addresses_and_rejects_other_searches():
    state = QueueState()
    result = _queue_with_memory_state(state, [
        _event(event_id=9002, search_id=11),
        _event(event_id=9003, address="1 Road, Annandale, NSW 2038"),
    ])
    assert result["events_considered"] == 1
    assert result["queued_count"] == 1
    assert state.notifications[0]["event_id"] == 9003


def test_should_notify_zero_and_initial_enrichment_reason_do_not_queue():
    state = QueueState()
    result = _queue_with_memory_state(state, [
        _event(event_id=9004, should_notify=0),
        _event(event_id=9005, reason="initial_price_enrichment"),
    ])
    assert result["queued_count"] == 0
    assert result["skipped_count"] == 2
    assert state.notifications == []



class CleanupCursor:
    def __init__(self):
        self.executed = []
        self.rowcount = 1

    def execute(self, sql, *params):
        self.executed.append((sql, params))
        return self


class CleanupConn:
    def __init__(self):
        self.cur = CleanupCursor()

    def cursor(self):
        return self.cur


def test_cleanup_delete_protects_queued_sending_and_sent_history_by_default():
    conn = CleanupConn()
    old_get_sub = db_layer.get_user_area_subscription
    try:
        db_layer.get_user_area_subscription = lambda conn, user_area_id: _subscription()
        result = cleanup.delete_test_events(conn, 4)
    finally:
        db_layer.get_user_area_subscription = old_get_sub
    select_sql = conn.cur.executed[0][0]
    assert "NOT EXISTS" in select_sql
    assert "('queued','sending','sent')" in select_sql
    assert result == {"events_deleted": 1, "notifications_deleted": 1}


class SenderConn:
    def __init__(self):
        self.rows = [{"NotificationID": 1, "EventID": 9001, "ChatID": "chat-7", "MessageText": "acceptance", "Status": "queued"}]
        self.commits = 0

    def commit(self):
        self.commits += 1


class SuccessBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)


def _with_sender_patches(conn, callback):
    old_get = db_layer.get_queued_notifications
    old_sending = db_layer.mark_notification_sending
    old_sent = db_layer.mark_notification_sent
    old_failed = db_layer.mark_notification_failed
    try:
        db_layer.get_queued_notifications = lambda conn, limit=20, channel="telegram": [row for row in conn.rows if row["Status"] == "queued"]
        db_layer.mark_notification_sending = lambda conn, notification_id: conn.rows[0].update(Status="sending")
        db_layer.mark_notification_sent = lambda conn, notification_id: conn.rows[0].update(Status="sent")
        db_layer.mark_notification_failed = lambda conn, notification_id, error: conn.rows[0].update(Status="failed", LastError=error)
        return callback()
    finally:
        db_layer.get_queued_notifications = old_get
        db_layer.mark_notification_sending = old_sending
        db_layer.mark_notification_sent = old_sent
        db_layer.mark_notification_failed = old_failed


def test_sender_dry_run_does_not_mark_sent_and_real_run_marks_sent():
    conn = SenderConn()
    bot = SuccessBot()
    def run():
        preview = asyncio.run(telegram_sender.send_queued_notifications(bot, dry_run=True, conn=conn))
        assert preview["processed"] == 1 and preview["sent"] == 0
        assert preview["items"][0]["chat_id"] == "chat-7"
        assert conn.rows[0]["Status"] == "queued"
        sent = asyncio.run(telegram_sender.send_queued_notifications(bot, dry_run=False, conn=conn))
        assert sent["processed"] == 1 and sent["sent"] == 1
        assert conn.rows[0]["Status"] == "sent"
        assert len(bot.sent) == 1
    _with_sender_patches(conn, run)


def run_all():
    for name, func in sorted(globals().items()):
        if name.startswith("test_"):
            func()
            print(f"PASS {name}")


if __name__ == "__main__":
    run_all()
