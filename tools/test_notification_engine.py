import contextlib
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import db_layer

from notification_engine import (
    build_notification_key,
    build_notification_message,
    canonical_event_type,
    format_agent_list,
    format_money,
    is_event_notifyable,
    normalize_event_for_notification,
    truncate_text,
)


def base_event(event_type="price_changed", old="Before", new="After"):
    return {
        "event_id": 123,
        "event_type": event_type,
        "old": old,
        "new": new,
        "listing": {
            "address": "10 Test Street, Petersham NSW 2049",
            "url": "https://example.test/listing/123",
            "price_display": "$1,000,000",
            "status": "active",
            "bedrooms": 3,
            "bathrooms": 2,
            "parking": 1,
        },
        "agency": {"name": "Example Agency"},
        "agents": [{"name": "Agent One", "phone": "0400000000"}],
    }


def test_is_event_notifyable_positive_types():
    for event_type in ["price_changed", "sold", "agent_changed", "description_changed"]:
        assert is_event_notifyable({"event_type": event_type}) is True


def test_is_event_notifyable_rejects_internal_and_explicit_false():
    assert is_event_notifyable({"event_type": "agent_changed", "reason": "initial_agent_enrichment"}) is False
    assert is_event_notifyable({"event_type": "price_changed", "should_notify": False}) is False
    assert is_event_notifyable({"event_type": "detail_refresh_failed_skip_change_detection"}) is False


def test_notification_key_idempotency():
    k1 = build_notification_key(123, "chat_a")
    k2 = build_notification_key(123, "chat_a")
    k3 = build_notification_key(123, "chat_b")
    assert k1 == k2
    assert k1 != k3


def test_new_listing_message_contains_address_url_price():
    message = build_notification_message(base_event("new_listing", new={"price_display": "$1,000,000"}))
    assert "New listing" in message
    assert "10 Test Street" in message
    assert "$1,000,000" in message
    assert "https://example.test/listing/123" in message


def test_sold_message_contains_sold_and_price():
    event = base_event("sold", old="active", new={"status": "sold", "sold_price": "$1,200,000", "sold_date": "2026-05-27"})
    message = build_notification_message(event)
    assert "Sold" in message
    assert "Previous status:\nactive" in message
    assert "Current status:\nsold" in message


def test_price_changed_message_contains_before_after():
    message = build_notification_message(base_event("price_changed", old="$900,000", new="$950,000"))
    assert "Price changed" in message
    assert "Previous price:\n$900,000" in message
    assert "Current price:\n$950,000" in message


def test_agent_changed_message_contains_before_after_agents():
    old_agents = [{"name": "Old Agent", "phone": "0411111111"}]
    new_agents = [{"name": "New Agent", "phone": "0422222222"}]
    message = build_notification_message(base_event("agent_changed", old=old_agents, new=new_agents))
    assert "Agent changed" in message
    assert "Previous:" in message
    assert "Old Agent" in message
    assert "Current:" in message
    assert "New Agent" in message


def test_long_text_truncated_under_4000():
    message = build_notification_message(base_event("price_changed", old="x" * 6000, new="y" * 6000))
    assert len(message) < 4000
    assert "truncated" in message
    assert len(truncate_text("z" * 6000, 3500)) <= 3500


def test_legacy_event_type_mapping():
    assert canonical_event_type("agent_change") == "agent_changed"
    normalized = normalize_event_for_notification({"EventID": 1, "EventType": "agent_change"})
    assert normalized["event_type"] == "agent_changed"



class FakeDdlCursor:
    def __init__(self, fail=False):
        self.fail = fail
        self.sql = None

    def execute(self, sql):
        self.sql = sql
        if self.fail:
            raise RuntimeError("ddl failed")


class FakeDdlConn:
    def __init__(self, fail=False):
        self.fail = fail
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return FakeDdlCursor(fail=self.fail)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def test_execute_ddl_safely_commits_success():
    conn = FakeDdlConn()
    assert db_layer._execute_ddl_safely(conn, "CREATE TABLE dbo.X(ID INT)", "test", required=True) is True
    assert conn.commits == 1
    assert conn.rollbacks == 0


def test_execute_ddl_safely_rolls_back_optional_failure():
    conn = FakeDdlConn(fail=True)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        ok = db_layer._execute_ddl_safely(conn, "ALTER TABLE dbo.X ADD bad INT", "optional", required=False)
    assert ok is False
    assert conn.commits == 0
    assert conn.rollbacks == 1
    assert "[migration warning] optional" in out.getvalue()


class FakeOutboxCursor:
    def __init__(self, conn):
        self.conn = conn
        self._result = None
        self._last_was_query = False

    def execute(self, sql, *params):
        self.conn.executed.append(sql.strip())
        upper = sql.strip().upper()
        if upper.startswith("SELECT 1 FROM DBO.NOTIFICATIONOUTBOX"):
            key = params[0]
            self._result = (1,) if key in self.conn.keys else None
            self._last_was_query = True
            return
        if upper.startswith("SAVE TRAN"):
            self._result = None
            self._last_was_query = False
            return
        if "INSERT INTO DBO.NOTIFICATIONOUTBOX" in upper:
            key = params[6]
            self.conn.keys.add(key)
            self._result = None
            self._last_was_query = False
            return
        raise AssertionError(f"unexpected SQL: {sql}")

    def fetchone(self):
        if not self._last_was_query:
            raise AssertionError("fetchone called after non-query")
        return self._result


class FakeOutboxConn:
    def __init__(self):
        self.keys = set()
        self.executed = []

    def cursor(self):
        return FakeOutboxCursor(self)

    def rollback(self):
        raise AssertionError("rollback should not be needed")


def test_insert_notification_outbox_if_new_select_then_insert_no_nonquery_fetch():
    conn = FakeOutboxConn()
    assert db_layer.insert_notification_outbox_if_new(conn, 1, "price_changed", "msg", "key1", chat_id="chat-1") is True
    assert db_layer.insert_notification_outbox_if_new(conn, 1, "price_changed", "msg", "key1", chat_id="chat-1") is False
    inserts = [sql for sql in conn.executed if "INSERT INTO dbo.NotificationOutbox" in sql]
    assert len(inserts) == 1


def test_build_notifications_for_events_counters():
    original_ensure = db_layer.ensure_notification_tables
    original_insert = db_layer.insert_notification_outbox_if_new
    outcomes = iter([True, True, False])
    try:
        db_layer.ensure_notification_tables = lambda conn: None
        db_layer.insert_notification_outbox_if_new = lambda *args, **kwargs: next(outcomes)
        events = [
            {"EventID": 1, "EventType": "price_changed", "OldValueJson": '{"field":"price","value":"$1"}', "NewValueJson": '{"field":"price","value":"$2"}'},
            {"EventID": 2, "EventType": "sold", "OldValueJson": "active", "NewValueJson": '{"status":"sold"}'},
            {"EventID": 3, "EventType": "agent_changed", "OldValueJson": "[]", "NewValueJson": '[{"name":"A"}]'},
            {"EventID": 4, "EventType": "removed_or_missing"},
        ]
        result = db_layer.build_notifications_for_events(object(), events, chat_id="chat-1", user_id=7, dry_run=False)
    finally:
        db_layer.ensure_notification_tables = original_ensure
        db_layer.insert_notification_outbox_if_new = original_insert
    assert result["queued_count"] == 2
    assert result["duplicates_count"] == 1
    assert result["skipped_count"] == 1
    assert result["errors"] == []


def test_build_notifications_for_events_missing_chat_id_skips_without_insert():
    original_ensure = db_layer.ensure_notification_tables
    original_insert = db_layer.insert_notification_outbox_if_new
    calls = []
    try:
        db_layer.ensure_notification_tables = lambda conn: None
        db_layer.insert_notification_outbox_if_new = lambda *args, **kwargs: calls.append(kwargs)
        result = db_layer.build_notifications_for_events(
            object(),
            [{"EventID": 1, "EventType": "price_changed"}],
            chat_id=None,
            user_id=7,
        )
    finally:
        db_layer.ensure_notification_tables = original_ensure
        db_layer.insert_notification_outbox_if_new = original_insert
    assert result["queued_count"] == 0
    assert result["skipped_count"] == 1
    assert result["skipped_reason"] == "missing_chat_id"
    assert result["notifications"][0]["skipped_reason"] == "missing_chat_id"
    assert calls == []


def test_insert_notification_outbox_if_new_rejects_missing_chat_id():
    try:
        db_layer.insert_notification_outbox_if_new(FakeOutboxConn(), 1, "price_changed", "msg", "unsafe-key")
    except ValueError as exc:
        assert str(exc) == "missing_chat_id"
    else:
        raise AssertionError("queued outbox insert accepted a missing ChatID")


class FakeSchemaCursor:
    def __init__(self, conn):
        self.conn = conn
        self._result = None

    def execute(self, sql, *params):
        if len(params) == 1 and isinstance(params[0], tuple):
            params = params[0]
        self.conn.sql.append(sql)
        if "INFORMATION_SCHEMA.TABLES" in sql:
            self._result = (1,)
            return
        if "INFORMATION_SCHEMA.COLUMNS" in sql and "DATA_TYPE" not in sql:
            self._result = (1,)
            return
        if "INFORMATION_SCHEMA.COLUMNS" in sql and "DATA_TYPE" in sql:
            _, table, column = params
            type_name = self.conn.types.get((table, column))
            if type_name is None:
                self._result = None
            else:
                nullable = "NO" if column in {"EventID", "listingID"} else "YES"
                self._result = (type_name, None, None, None, nullable)
            return
        self._result = None

    def fetchone(self):
        return self._result


class FakeSchemaConn:
    def __init__(self):
        self.sql = []
        self.commits = 0
        self.rollbacks = 0
        self.types = {
            ("ListingEvent", "EventID"): "bigint",
            ("NotificationOutbox", "EventID"): "int",
            ("SuburbSearch", "SearchID"): "int",
            ("NotificationOutbox", "SearchID"): "int",
            ("Listing", "listingID"): "int",
            ("NotificationOutbox", "ListingID"): "int",
        }

    def cursor(self):
        return FakeSchemaCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def test_ensure_notification_tables_skips_fk_type_mismatch_once():
    old_ensured = db_layer._NOTIFICATION_TABLES_ENSURED
    old_warnings = set(db_layer._MIGRATION_WARNINGS_EMITTED)
    conn = FakeSchemaConn()
    out = io.StringIO()
    try:
        db_layer._NOTIFICATION_TABLES_ENSURED = False
        db_layer._MIGRATION_WARNINGS_EMITTED.clear()
        with contextlib.redirect_stdout(out):
            db_layer.ensure_notification_tables(conn)
            db_layer._NOTIFICATION_TABLES_ENSURED = False
            db_layer.ensure_notification_tables(conn)
    finally:
        db_layer._NOTIFICATION_TABLES_ENSURED = old_ensured
        db_layer._MIGRATION_WARNINGS_EMITTED.clear()
        db_layer._MIGRATION_WARNINGS_EMITTED.update(old_warnings)
    text = out.getvalue()
    assert text.count("Skipping FK_NotificationOutbox_Event") == 1
    event_fk_sql = [sql for sql in conn.sql if "ADD CONSTRAINT FK_NotificationOutbox_Event" in sql]
    assert event_fk_sql == []


def test_format_price_dict_range():
    assert format_money({"price_low": "1550000.00", "price_high": "1580000.00"}) == "$1,550,000 - $1,580,000"
    assert format_money({"price_low": "1550000.00", "price_high": "1550000.00"}) == "$1,550,000"


def test_format_agent_list_includes_name_phone_url():
    message = format_agent_list([{"name": "Agent One", "phone": "0400", "profile_url": "https://agent.test/profile"}])
    assert "Agent One" in message
    assert "0400" in message
    assert "https://agent.test/profile" in message


def test_listing_event_metadata_suppression():
    assert is_event_notifyable({"EventType": "price_changed", "ShouldNotify": 0}) is False
    assert is_event_notifyable({"EventType": "price_changed", "Reason": "initial_detail_baseline"}) is False
    assert is_event_notifyable({"EventType": "price_changed", "EventPayloadJson": '{"reason":"initial_detail_baseline","should_notify":false}'}) is False
    normalized = normalize_event_for_notification({"EventID": 5, "EventType": "price_changed", "EventPayloadJson": '{"old_value":"$100","new_value":"$95","reason":"initial_detail_baseline","should_notify":false}'})
    assert normalized["old"] == "$100"
    assert normalized["new"] == "$95"
    assert normalized["should_notify"] is False


def run_tests():
    test_is_event_notifyable_positive_types()
    test_is_event_notifyable_rejects_internal_and_explicit_false()
    test_notification_key_idempotency()
    test_new_listing_message_contains_address_url_price()
    test_sold_message_contains_sold_and_price()
    test_price_changed_message_contains_before_after()
    test_agent_changed_message_contains_before_after_agents()
    test_long_text_truncated_under_4000()
    test_legacy_event_type_mapping()
    test_execute_ddl_safely_commits_success()
    test_execute_ddl_safely_rolls_back_optional_failure()
    test_insert_notification_outbox_if_new_select_then_insert_no_nonquery_fetch()
    test_build_notifications_for_events_counters()
    test_build_notifications_for_events_missing_chat_id_skips_without_insert()
    test_insert_notification_outbox_if_new_rejects_missing_chat_id()
    test_ensure_notification_tables_skips_fk_type_mismatch_once()
    test_format_price_dict_range()
    test_format_agent_list_includes_name_phone_url()
    test_listing_event_metadata_suppression()
    print("notification_engine tests passed")


if __name__ == "__main__":
    run_tests()
