import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import db_layer


class Conn:
    def __init__(self):
        self.commits = 0
    def commit(self):
        self.commits += 1


def _patch(monkeypatches):
    originals = []
    for name, value in monkeypatches.items():
        originals.append((name, getattr(db_layer, name)))
        setattr(db_layer, name, value)
    return originals


def _restore(originals):
    for name, value in reversed(originals):
        setattr(db_layer, name, value)


def test_same_user_adds_same_search_area_twice_is_duplicate_without_setup():
    calls = []
    state = {"already": False}
    def get_or_create(conn, search_url, area_label=None, suburb=None, postcode=None):
        calls.append(("get_or_create", search_url))
        return 42, False
    def already(conn, telegram_user_id, search_id):
        return state["already"]
    def create(conn, *args, **kwargs):
        calls.append(("create_subscription", args[1]))
        state["already"] = True
        return "created", {"user_area_id": 1, "search_id": 42, "search_url": args[2], "area_label": args[3]}
    originals = _patch({
        "ensure_telegram_bot_tables": lambda conn: None,
        "get_or_create_suburb_search": get_or_create,
        "user_already_subscribed": already,
        "active_area_count_for_user": lambda conn, telegram_user_id: 0,
        "get_search_setup_state": lambda conn, search_id: {"state": "not_started", "is_ready": False, "is_running": False},
        "create_or_reactivate_subscription": create,
    })
    try:
        conn = Conn()
        ok1, payload1 = db_layer.add_user_area_subscription(conn, 7, "url", "Tanglewood, NSW 2488")
        ok2, payload2 = db_layer.add_user_area_subscription(conn, 7, "url", "Tanglewood, NSW 2488")
    finally:
        _restore(originals)
    assert ok1 is True and payload1["reason"] == "setup_required"
    assert ok2 is False and payload2["reason"] == "duplicate"
    assert calls.count(("create_subscription", 42)) == 1
    assert len([call for call in calls if call[0] == "get_or_create"]) == 2


def test_different_users_share_one_search_id_and_only_first_needs_setup():
    created_for = []
    setup_states = iter([
        {"state": "not_started", "is_ready": False, "is_running": False},
        {"state": "running", "is_ready": False, "is_running": True},
    ])
    def create(conn, telegram_user_id, search_id, *args, **kwargs):
        created_for.append((telegram_user_id, search_id))
        return "created", {"user_area_id": telegram_user_id, "search_id": search_id, "search_url": args[0], "area_label": args[1]}
    originals = _patch({
        "ensure_telegram_bot_tables": lambda conn: None,
        "get_or_create_suburb_search": lambda *args, **kwargs: (42, False),
        "user_already_subscribed": lambda conn, telegram_user_id, search_id: False,
        "active_area_count_for_user": lambda conn, telegram_user_id: 0,
        "get_search_setup_state": lambda conn, search_id: next(setup_states),
        "create_or_reactivate_subscription": create,
    })
    try:
        first = db_layer.add_user_area_subscription(Conn(), 7, "url", "Tanglewood, NSW 2488")
        second = db_layer.add_user_area_subscription(Conn(), 8, "url", "Tanglewood, NSW 2488")
    finally:
        _restore(originals)
    assert first[1]["reason"] == "setup_required"
    assert second[1]["reason"] == "setup_running"
    assert created_for == [(7, 42), (8, 42)]


def test_ready_search_join_uses_future_only_message_and_does_not_request_setup():
    now = datetime(2026, 6, 3, 12, 0, 0)
    create_payloads = []
    def create(conn, telegram_user_id, search_id, *args, **kwargs):
        create_payloads.append(kwargs["setup_state"])
        return "created", {"user_area_id": 9, "search_id": search_id, "search_url": args[0], "area_label": args[1], "notification_start_at": now}
    originals = _patch({
        "ensure_telegram_bot_tables": lambda conn: None,
        "get_or_create_suburb_search": lambda *args, **kwargs: (42, False),
        "user_already_subscribed": lambda conn, telegram_user_id, search_id: False,
        "active_area_count_for_user": lambda conn, telegram_user_id: 0,
        "get_search_setup_state": lambda conn, search_id: {"state": "ready", "is_ready": True, "is_running": False, "ready_at": now - timedelta(hours=1)},
        "create_or_reactivate_subscription": create,
    })
    try:
        ok, payload = db_layer.add_user_area_subscription(Conn(), 9, "url", "Tanglewood, NSW 2488")
    finally:
        _restore(originals)
    assert ok is True
    assert payload["reason"] == "ready"
    assert payload["message"] == "This search area is already ready. I will notify you about future changes from now on."
    assert create_payloads[0]["is_ready"] is True


def test_inactive_subscription_reactivation_resets_notification_start_sql():
    executed = []
    class Cursor:
        def execute(self, sql, *params):
            executed.append((sql, params))
            self.description = [("UserAreaID",), ("IsActive",)]
            self.rows = [(5, 0)] if "SELECT TOP 1" in sql else []
            return self
        def fetchone(self):
            return self.rows.pop(0) if self.rows else None
        def fetchall(self):
            return []
    class SqlConn(Conn):
        def cursor(self): return Cursor()
    originals = _patch({"ensure_telegram_bot_tables": lambda conn: None})
    try:
        action, payload = db_layer.create_or_reactivate_subscription(SqlConn(), 7, 42, "url", "Tanglewood, NSW 2488", setup_state={"state": "running", "is_ready": False})
    finally:
        _restore(originals)
    update_sql = "\n".join(sql for sql, _ in executed if "UPDATE dbo.UserAreaSubscription" in sql)
    assert action == "reactivated" and payload["user_area_id"] == 5
    assert "NotificationStartAt=SYSDATETIME()" in update_sql
    assert "WHERE UserAreaID=?" in update_sql


def test_ready_search_subscription_initializes_summary_suppression_columns():
    executed = []
    class Cursor:
        def execute(self, sql, *params):
            executed.append((sql, params))
            self.description = [("UserAreaID",), ("IsActive",)]
            self.rows = [] if "SELECT TOP 1" in sql else [(10,)]
            return self
        def fetchone(self):
            return self.rows.pop(0) if self.rows else None
        def fetchall(self):
            return []
    class SqlConn(Conn):
        def cursor(self): return Cursor()
    originals = _patch({"ensure_telegram_bot_tables": lambda conn: None})
    try:
        action, payload = db_layer.create_or_reactivate_subscription(
            SqlConn(),
            8,
            42,
            "url",
            "Tanglewood, NSW 2488",
            setup_state={"state": "ready", "is_ready": True, "baseline_completed": True, "detail_started": True},
        )
    finally:
        _restore(originals)
    insert_sql = "\n".join(sql for sql, _ in executed if "INSERT INTO dbo.UserAreaSubscription" in sql)
    assert action == "created" and payload["baseline_status"] == "completed" and payload["detail_baseline_status"] == "completed"
    assert "BaselineSummarySentAt" in insert_sql
    assert "DetailBaselineStartedSummarySentAt" in insert_sql
    assert "ReadySummarySentAt" in insert_sql
    assert insert_sql.count("SYSDATETIME()") >= 7


def test_setup_running_join_after_baseline_suppresses_historical_progress_but_allows_ready_summary():
    executed = []
    class Cursor:
        def execute(self, sql, *params):
            executed.append((sql, params))
            self.description = [("UserAreaID",), ("IsActive",)]
            self.rows = [] if "SELECT TOP 1" in sql else [(11,)]
            return self
        def fetchone(self):
            return self.rows.pop(0) if self.rows else None
        def fetchall(self):
            return []
    class SqlConn(Conn):
        def cursor(self): return Cursor()
    originals = _patch({"ensure_telegram_bot_tables": lambda conn: None})
    try:
        action, payload = db_layer.create_or_reactivate_subscription(
            SqlConn(),
            8,
            42,
            "url",
            "Tanglewood, NSW 2488",
            setup_state={"state": "running", "is_ready": False, "baseline_completed": True, "detail_started": True},
        )
    finally:
        _restore(originals)
    insert_sql = "\n".join(sql for sql, _ in executed if "INSERT INTO dbo.UserAreaSubscription" in sql)
    assert action == "created"
    assert payload["baseline_status"] == "completed"
    assert payload["detail_baseline_status"] == "running"
    assert "BaselineSummarySentAt" in insert_sql and "DetailBaselineStartedSummarySentAt" in insert_sql
    assert "ReadySummarySentAt" in insert_sql
    assert "NULL," in insert_sql.split("ReadySummarySentAt", 1)[1]


def test_notification_start_at_gate_filters_old_events_for_late_joiner():
    gate = datetime(2026, 6, 3, 12, 0, 0)
    old_event = {"EventID": 1, "CreatedAt": gate - timedelta(seconds=1)}
    new_event = {"EventID": 2, "CreatedAt": gate + timedelta(seconds=1)}
    captured = {}
    sub = {"UserAreaID": 5, "TelegramUserID": 7, "ChatID": "chat", "IsActive": True, "BaselineStatus": "completed", "BaselineCompletedAt": gate, "DetailBaselineStatus": "completed", "NotificationReadyAt": gate - timedelta(hours=1), "NotificationStartAt": gate, "SearchURL": "url"}
    def get_events(conn, **kwargs):
        captured.update(kwargs)
        cutoff = kwargs["created_at_or_after"]
        return [event for event in [old_event, new_event] if event["CreatedAt"] >= cutoff]
    originals = _patch({
        "get_user_area_subscription": lambda conn, user_area_id: sub,
        "get_notifyable_listing_events": get_events,
        "build_notifications_for_events": lambda conn, events, **kwargs: {"events_input": len(events), "notifyable_count": len(events), "queued_count": len(events), "skipped_count": 0, "duplicates_count": 0, "notifications": events, "errors": []},
        "mark_subscription_notifications_queued": lambda conn, user_area_id: None,
    })
    try:
        result = db_layer.queue_notifications_for_user_area(Conn(), 5)
    finally:
        _restore(originals)
    assert captured["created_at_or_after"] == gate
    assert result["queued_count"] == 1
    assert result["notifications"] == [new_event]


def run_tests():
    test_same_user_adds_same_search_area_twice_is_duplicate_without_setup()
    test_different_users_share_one_search_id_and_only_first_needs_setup()
    test_ready_search_join_uses_future_only_message_and_does_not_request_setup()
    test_inactive_subscription_reactivation_resets_notification_start_sql()
    test_ready_search_subscription_initializes_summary_suppression_columns()
    test_setup_running_join_after_baseline_suppresses_historical_progress_but_allows_ready_summary()
    test_notification_start_at_gate_filters_old_events_for_late_joiner()
    print("shared_search_subscriptions tests passed")


if __name__ == "__main__":
    run_tests()
