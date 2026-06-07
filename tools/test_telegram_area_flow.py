import asyncio
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from area_url_builder import build_realestate_buy_url, normalize_area_input
from notification_engine import build_notification_key
import telegram_sender


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.rows = []
        self.description = []
        self.rowcount = 0

    def execute(self, sql, *params):
        if len(params) == 1 and isinstance(params[0], (tuple, list)):
            params = tuple(params[0])
        sql_upper = " ".join(sql.upper().split())
        self.conn.executed.append((sql, params))
        if "COUNT(1) FROM DBO.USERAREASUBSCRIPTION" in sql_upper:
            user_id = params[0]
            count = sum(1 for s in self.conn.subscriptions if s["TelegramUserID"] == user_id and s["IsActive"])
            self.rows = [(count,)]
            self.description = [("COUNT",)]
        elif "SELECT USERAREAID FROM DBO.USERAREASUBSCRIPTION" in sql_upper:
            user_id, url = params[0], params[1]
            found = [s for s in self.conn.subscriptions if s["TelegramUserID"] == user_id and s["SearchURL"] == url and s["IsActive"]]
            self.rows = [(found[0]["UserAreaID"],)] if found else []
            self.description = [("UserAreaID",)]
        elif "INSERT INTO DBO.USERAREASUBSCRIPTION" in sql_upper:
            self.conn.next_user_area_id += 1
            rec = {
                "UserAreaID": self.conn.next_user_area_id,
                "TelegramUserID": params[0],
                "SearchID": params[1],
                "SearchURL": params[2],
                "AreaLabel": params[3],
                "Suburb": params[4],
                "StateCode": params[5],
                "Postcode": params[6],
                "IsActive": True,
                "BaselineStatus": "pending",
            }
            self.conn.subscriptions.append(rec)
            self.rows = [(self.conn.next_user_area_id,)]
            self.description = [("UserAreaID",)]
        elif sql_upper.startswith("SELECT 1 FROM DBO.NOTIFICATIONOUTBOX"):
            key = params[0]
            self.rows = [(1,)] if key in self.conn.notification_keys else []
            self.description = [("exists",)]
        elif "INSERT INTO DBO.NOTIFICATIONOUTBOX" in sql_upper:
            self.conn.notification_keys.add(params[6])
            self.conn.notifications.append({"NotificationKey": params[6], "ChatID": params[5], "EventID": params[0]})
            self.rows = []
        elif sql_upper.startswith("SAVE TRAN"):
            self.rows = []
        elif "UPDATE DBO.NOTIFICATIONOUTBOX" in sql_upper and "STATUS='FAILED'" in sql_upper:
            error, nid = params[0], params[1]
            for n in self.conn.notifications:
                if n.get("NotificationID") == nid:
                    n["Status"] = "failed"
                    n["LastError"] = error
        else:
            raise AssertionError(f"unexpected SQL: {sql}")
        return self

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class FakeConn:
    def __init__(self):
        self.subscriptions = []
        self.notification_keys = set()
        self.notifications = []
        self.next_user_area_id = 0
        self.executed = []
        self.commits = 0

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass


class FakeBot:
    async def send_message(self, chat_id, text, disable_web_page_preview=True):
        raise RuntimeError("telegram failure")


def patch_db_for_subscription_tests(db_layer):
    db_layer.ensure_telegram_bot_tables = lambda conn: None
    db_layer.ensure_notification_tables = lambda conn: None
    db_layer.get_or_create_suburb_search = lambda conn, url, **kwargs: (abs(hash(url)) % 100000, not any(s.get("SearchURL") == url for s in conn.subscriptions))
    db_layer.user_already_subscribed = lambda conn, telegram_user_id, search_id: any(s["TelegramUserID"] == telegram_user_id and s["SearchID"] == search_id and s["IsActive"] for s in conn.subscriptions)
    db_layer.get_search_setup_state = lambda conn, search_id: {"state": "not_started", "is_ready": False, "is_running": False}
    def create_or_reactivate(conn, telegram_user_id, search_id, search_url, area_label, suburb=None, state_code=None, postcode=None, setup_state=None):
        for sub in conn.subscriptions:
            if sub["TelegramUserID"] == telegram_user_id and sub["SearchID"] == search_id:
                if sub["IsActive"]:
                    return "already_active", {"user_area_id": sub["UserAreaID"], "search_id": search_id, "search_url": search_url, "area_label": area_label}
                sub["IsActive"] = True
                sub["NotificationStartAt"] = "now"
                return "reactivated", {"user_area_id": sub["UserAreaID"], "search_id": search_id, "search_url": search_url, "area_label": area_label}
        conn.next_user_area_id += 1
        rec = {"UserAreaID": conn.next_user_area_id, "TelegramUserID": telegram_user_id, "SearchID": search_id, "SearchURL": search_url, "AreaLabel": area_label, "Suburb": suburb, "StateCode": state_code, "Postcode": postcode, "IsActive": True, "BaselineStatus": "pending"}
        conn.subscriptions.append(rec)
        return "created", {"user_area_id": rec["UserAreaID"], "search_id": search_id, "search_url": search_url, "area_label": area_label}
    db_layer.create_or_reactivate_subscription = create_or_reactivate


def test_build_url():
    data = normalize_area_input("Petersham NSW 2049")
    assert data["search_url"] == "https://www.realestate.com.au/buy/in-petersham,+nsw+2049/list-1?activeSort=list-date"
    assert build_realestate_buy_url("Petersham", "NSW", "2049") == data["search_url"]


def test_max_3_area_rule_and_duplicate():
    import db_layer
    patch_db_for_subscription_tests(db_layer)
    conn = FakeConn()
    for name, postcode in [("A", "2000"), ("B", "2001"), ("C", "2002")]:
        url = build_realestate_buy_url(name, "NSW", postcode)
        ok, _ = db_layer.add_user_area_subscription(conn, 1, url, name, max_active=3)
        assert ok
    ok, payload = db_layer.add_user_area_subscription(conn, 1, build_realestate_buy_url("D", "NSW", "2003"), "D", max_active=3)
    assert not ok and payload["reason"] == "max_areas"

    conn2 = FakeConn()
    url = build_realestate_buy_url("Petersham", "NSW", "2049")
    ok, _ = db_layer.add_user_area_subscription(conn2, 1, url, "Petersham", max_active=3)
    assert ok
    ok, payload = db_layer.add_user_area_subscription(conn2, 1, url, "Petersham", max_active=3)
    assert not ok and payload["reason"] == "duplicate"


def test_initial_baseline_suppresses_old_events_and_key_per_chat():
    import db_layer
    captured = {}
    baseline_time = datetime(2026, 5, 28, 12, 0, 0)
    sub = {"UserAreaID": 1, "TelegramUserID": 7, "ChatID": "chat_a", "SearchURL": build_realestate_buy_url("Petersham", "NSW", "2049"), "Suburb": "Petersham", "StateCode": "NSW", "Postcode": "2049", "IsActive": True, "BaselineStatus": "completed", "BaselineCompletedAt": baseline_time, "DetailBaselineStatus": "completed", "NotificationReadyAt": baseline_time, "NotificationStartAt": baseline_time - timedelta(hours=1)}
    db_layer.get_user_area_subscription = lambda conn, user_area_id: sub
    def fake_events(conn, **kwargs):
        captured["kwargs"] = kwargs
        return [{"EventID": 99, "EventType": "price_changed", "ListingID": 1, "SearchID": 2, "address": "10 Test St, Petersham, NSW 2049", "OldValueJson": '{"value":"$1"}', "NewValueJson": '{"value":"$2"}'}]
    db_layer.get_notifyable_listing_events = fake_events
    db_layer.build_notifications_for_events = lambda conn, events, chat_id=None, **kwargs: {"queued_count": len(events), "chat_id": chat_id, "notifications": events}
    db_layer.mark_subscription_notifications_queued = lambda conn, user_area_id: None
    out = db_layer.queue_notifications_for_user_area(object(), 1)
    assert out["queued_count"] == 1
    assert captured["kwargs"]["created_after"] == baseline_time
    assert captured["kwargs"]["chat_id"] == "chat_a"
    assert build_notification_key(99, "chat_a") != build_notification_key(99, "chat_b")


def test_notification_ready_gate_uses_search_id_scope_not_address_filter():
    import db_layer

    ready = datetime(2026, 6, 2, 12, 0, 0)
    sub = {"UserAreaID": 1, "TelegramUserID": 7, "ChatID": "chat", "SearchURL": build_realestate_buy_url("Petersham", "NSW", "2049"), "Suburb": "Petersham", "StateCode": "NSW", "Postcode": "2049", "IsActive": True, "BaselineStatus": "completed", "BaselineCompletedAt": ready - timedelta(hours=1), "DetailBaselineStatus": "completed", "NotificationReadyAt": None}
    old_get_sub = db_layer.get_user_area_subscription
    old_get_events = db_layer.get_notifyable_listing_events
    old_build = db_layer.build_notifications_for_events
    old_mark = db_layer.mark_subscription_notifications_queued
    try:
        db_layer.get_user_area_subscription = lambda conn, user_area_id: sub
        assert db_layer.queue_notifications_for_user_area(object(), 1)["skipped_reason"] == "notification_not_ready"
        sub["NotificationReadyAt"] = ready
        captured = {}
        def fake_events(conn, **kwargs):
            captured.update(kwargs)
            return [
                {"EventID": 1, "address": "1 Road, Annandale, NSW 2038"},
                {"EventID": 2, "address": "2 Road, Leichhardt, NSW 2040"},
                {"EventID": 3, "address": "3 Road, Marrickville, NSW 2204"},
                {"EventID": 4, "address": "4 Road, Dulwich Hill, NSW 2203"},
                {"EventID": 5, "address": "5 Road, Petersham, NSW 2049"},
            ]
        db_layer.get_notifyable_listing_events = fake_events
        db_layer.build_notifications_for_events = lambda conn, events, **kwargs: {"queued_count": len(events), "events": events}
        db_layer.mark_subscription_notifications_queued = lambda conn, user_area_id: None
        result = db_layer.queue_notifications_for_user_area(object(), 1)
        assert captured["created_after"] == ready
        assert captured["search_url"] == sub["SearchURL"]
        assert result["queued_count"] == 5
        assert [event["EventID"] for event in result["events"]] == [1, 2, 3, 4, 5]
    finally:
        db_layer.get_user_area_subscription = old_get_sub
        db_layer.get_notifyable_listing_events = old_get_events
        db_layer.build_notifications_for_events = old_build
        db_layer.mark_subscription_notifications_queued = old_mark


def test_setup_summaries_are_idempotent_and_marked_once():
    import monitoring_scheduler

    sub = {"UserAreaID": 9, "ChatID": "chat", "AreaLabel": "Petersham, NSW 2049", "BaselineSummarySentAt": None, "DetailBaselineStartedSummarySentAt": None, "ReadySummarySentAt": None, "BaselineListingsCollected": 12}
    calls = []
    class Conn:
        def close(self):
            calls.append("close")
    old_token = monitoring_scheduler.config.TELEGRAM_BOT_TOKEN
    old_send = monitoring_scheduler._send_setup_summary
    old_connect = monitoring_scheduler.db_layer.connect
    old_mark = monitoring_scheduler.db_layer.mark_subscription_setup_summary_sent
    try:
        monitoring_scheduler.config.TELEGRAM_BOT_TOKEN = "secret-test-token"
        async def fake_send(chat_id, text):
            calls.append((chat_id, text))
        monitoring_scheduler._send_setup_summary = fake_send
        monitoring_scheduler.db_layer.connect = lambda path: Conn()
        monitoring_scheduler.db_layer.mark_subscription_setup_summary_sent = lambda conn, uid, col: calls.append((uid, col))
        for summary_type, column in monitoring_scheduler.SETUP_SUMMARY_COLUMNS.items():
            first = monitoring_scheduler._send_setup_summary_once(sub, summary_type)
            second = monitoring_scheduler._send_setup_summary_once(sub, summary_type)
            assert first["status"] == "sent"
            assert second["status"] == "already_sent"
            assert sum(1 for call in calls if call == (9, column)) == 1
        assert "secret-test-token" not in repr(calls)
    finally:
        monitoring_scheduler.config.TELEGRAM_BOT_TOKEN = old_token
        monitoring_scheduler._send_setup_summary = old_send
        monitoring_scheduler.db_layer.connect = old_connect
        monitoring_scheduler.db_layer.mark_subscription_setup_summary_sent = old_mark



def test_baseline_metrics_are_persisted_on_completion():
    import db_layer

    class Cursor:
        def __init__(self, conn):
            self.conn = conn
        def execute(self, sql, *params):
            self.conn.sql = sql
            self.conn.params = params
            return self
    class Conn:
        def __init__(self):
            self.sql = ""
            self.params = ()
            self.commits = 0
        def cursor(self):
            return Cursor(self)
        def commit(self):
            self.commits += 1

    conn = Conn()
    db_layer.mark_subscription_baseline_completed(conn, 9, listings_collected=25, new_count=25, pages_checked=3)
    assert "BaselineListingsCollected=?" in conn.sql
    assert "BaselineLastError=NULL" in conn.sql
    assert conn.params == (25, 25, 3, None, None, 9)
    assert conn.commits == 1


def test_delayed_baseline_summary_uses_persisted_count():
    import monitoring_scheduler

    messages = []
    marks = []
    class Conn:
        def close(self):
            pass
    sub = {"UserAreaID": 9, "ChatID": "chat", "AreaLabel": "Petersham, NSW 2049", "BaselineSummarySentAt": None, "BaselineListingsCollected": 25}
    old_token = monitoring_scheduler.config.TELEGRAM_BOT_TOKEN
    old_send = monitoring_scheduler._send_setup_summary
    old_connect = monitoring_scheduler.db_layer.connect
    old_mark = monitoring_scheduler.db_layer.mark_subscription_setup_summary_sent
    try:
        monitoring_scheduler.config.TELEGRAM_BOT_TOKEN = "secret-test-token"
        async def fake_send(chat_id, text):
            messages.append(text)
        monitoring_scheduler._send_setup_summary = fake_send
        monitoring_scheduler.db_layer.connect = lambda path: Conn()
        monitoring_scheduler.db_layer.mark_subscription_setup_summary_sent = lambda conn, uid, col: marks.append((uid, col))
        result = monitoring_scheduler._send_setup_summary_once(sub, "baseline")
        assert result["status"] == "sent"
        assert "Listings found: 25" in messages[0]
        assert marks == [(9, "BaselineSummarySentAt")]
    finally:
        monitoring_scheduler.config.TELEGRAM_BOT_TOKEN = old_token
        monitoring_scheduler._send_setup_summary = old_send
        monitoring_scheduler.db_layer.connect = old_connect
        monitoring_scheduler.db_layer.mark_subscription_setup_summary_sent = old_mark


def test_baseline_summary_falls_back_to_search_id_computed_count():
    import monitoring_scheduler

    messages = []
    class Conn:
        def close(self):
            pass
    sub = {"UserAreaID": 9, "ChatID": "chat", "AreaLabel": "Petersham, NSW 2049", "BaselineSummarySentAt": None, "BaselineListingsCollected": None}
    old_token = monitoring_scheduler.config.TELEGRAM_BOT_TOKEN
    old_send = monitoring_scheduler._send_setup_summary
    old_connect = monitoring_scheduler.db_layer.connect
    old_load = monitoring_scheduler._load_baseline_summary
    old_mark = monitoring_scheduler.db_layer.mark_subscription_setup_summary_sent
    try:
        monitoring_scheduler.config.TELEGRAM_BOT_TOKEN = "secret-test-token"
        async def fake_send(chat_id, text):
            messages.append(text)
        monitoring_scheduler._send_setup_summary = fake_send
        monitoring_scheduler._load_baseline_summary = lambda subscription: {"baseline_listings_collected": None, "computed_listing_count": 3}
        monitoring_scheduler.db_layer.connect = lambda path: Conn()
        monitoring_scheduler.db_layer.mark_subscription_setup_summary_sent = lambda conn, uid, col: None
        assert monitoring_scheduler._send_setup_summary_once(sub, "baseline")["status"] == "sent"
        assert "Listings found: 3" in messages[0]
    finally:
        monitoring_scheduler.config.TELEGRAM_BOT_TOKEN = old_token
        monitoring_scheduler._send_setup_summary = old_send
        monitoring_scheduler._load_baseline_summary = old_load
        monitoring_scheduler.db_layer.connect = old_connect
        monitoring_scheduler.db_layer.mark_subscription_setup_summary_sent = old_mark


def test_baseline_summary_uses_unknown_when_count_is_unavailable():
    import monitoring_scheduler

    messages = []
    class Conn:
        def close(self):
            pass
    sub = {"UserAreaID": 9, "ChatID": "chat", "AreaLabel": "Petersham, NSW 2049", "BaselineSummarySentAt": None, "BaselineListingsCollected": None}
    old_token = monitoring_scheduler.config.TELEGRAM_BOT_TOKEN
    old_send = monitoring_scheduler._send_setup_summary
    old_connect = monitoring_scheduler.db_layer.connect
    old_load = monitoring_scheduler._load_baseline_summary
    old_mark = monitoring_scheduler.db_layer.mark_subscription_setup_summary_sent
    try:
        monitoring_scheduler.config.TELEGRAM_BOT_TOKEN = "secret-test-token"
        async def fake_send(chat_id, text):
            messages.append(text)
        monitoring_scheduler._send_setup_summary = fake_send
        monitoring_scheduler._load_baseline_summary = lambda subscription: {"baseline_listings_collected": None, "computed_listing_count": None}
        monitoring_scheduler.db_layer.connect = lambda path: Conn()
        monitoring_scheduler.db_layer.mark_subscription_setup_summary_sent = lambda conn, uid, col: None
        assert monitoring_scheduler._send_setup_summary_once(sub, "baseline")["status"] == "sent"
        assert "Listings found: Unknown" in messages[0]
        assert "Listings found: 0" not in messages[0]
    finally:
        monitoring_scheduler.config.TELEGRAM_BOT_TOKEN = old_token
        monitoring_scheduler._send_setup_summary = old_send
        monitoring_scheduler._load_baseline_summary = old_load
        monitoring_scheduler.db_layer.connect = old_connect
        monitoring_scheduler.db_layer.mark_subscription_setup_summary_sent = old_mark


def test_baseline_summary_helper_uses_search_id_fallback_count():
    import db_layer

    captured = {}
    class Cursor:
        def execute(self, sql, *params):
            captured["sql"] = sql
            captured["params"] = params
            return self
        def fetchone(self):
            return (3,)
    class Conn:
        def cursor(self):
            return Cursor()
    sub = {"UserAreaID": 9, "ChatID": "chat", "SearchID": 2, "AreaLabel": "Petersham, NSW 2049", "Suburb": "Petersham", "StateCode": "NSW", "Postcode": "2049", "BaselineListingsCollected": None, "BaselineNewCount": None, "BaselinePagesChecked": None, "NotificationReadyAt": None}
    old_get = db_layer.get_user_area_subscription
    old_progress = db_layer.get_detail_baseline_progress
    try:
        db_layer.get_user_area_subscription = lambda conn, user_area_id: sub
        db_layer.get_detail_baseline_progress = lambda conn, subscription: {"detail_baseline_total_count": 3, "detail_baseline_completed_count": 0, "detail_baseline_remaining_count": 3, "notification_ready_at": None}
        summary = db_layer.get_user_area_baseline_summary(Conn(), 9)
        assert summary["computed_listing_count"] == 3
        assert captured["params"] == (2,)
        assert "LOWER(COALESCE" not in captured["sql"]
    finally:
        db_layer.get_user_area_subscription = old_get
        db_layer.get_detail_baseline_progress = old_progress


def test_missing_token_keeps_persisted_baseline_metrics_unsent():
    import monitoring_scheduler

    sub = {"UserAreaID": 9, "ChatID": "chat", "AreaLabel": "Petersham, NSW 2049", "BaselineSummarySentAt": None, "BaselineListingsCollected": 25}
    old_token = monitoring_scheduler.config.TELEGRAM_BOT_TOKEN
    try:
        monitoring_scheduler.config.TELEGRAM_BOT_TOKEN = ""
        result = monitoring_scheduler._send_setup_summary_once(sub, "baseline")
        assert result["status"] == "warning"
        assert sub["BaselineSummarySentAt"] is None
        assert sub["BaselineListingsCollected"] == 25
    finally:
        monitoring_scheduler.config.TELEGRAM_BOT_TOKEN = old_token

def test_scheduler_without_token_returns_warning_not_crash():
    import monitoring_scheduler

    class Conn:
        def close(self):
            pass
    old_token = monitoring_scheduler.config.TELEGRAM_BOT_TOKEN
    old_connect = monitoring_scheduler.db_layer.connect
    old_ensure = monitoring_scheduler.db_layer.ensure_telegram_bot_tables
    old_subs = monitoring_scheduler.db_layer.get_active_user_area_subscriptions
    try:
        monitoring_scheduler.config.TELEGRAM_BOT_TOKEN = ""
        monitoring_scheduler.db_layer.connect = lambda path: Conn()
        monitoring_scheduler.db_layer.ensure_telegram_bot_tables = lambda conn: None
        monitoring_scheduler.db_layer.get_active_user_area_subscriptions = lambda conn: []
        result = monitoring_scheduler.run_monitoring_tick(send_telegram=True)
        assert result["errors"] == []
        assert result["sender"]["processed"] == 0
        assert result["sender"]["warning"] == "telegram token not set; queued notifications were not sent"
    finally:
        monitoring_scheduler.config.TELEGRAM_BOT_TOKEN = old_token
        monitoring_scheduler.db_layer.connect = old_connect
        monitoring_scheduler.db_layer.ensure_telegram_bot_tables = old_ensure
        monitoring_scheduler.db_layer.get_active_user_area_subscriptions = old_subs


def test_setup_summary_without_token_is_warning_not_crash():
    import monitoring_scheduler

    sub = {"UserAreaID": 9, "ChatID": "chat", "AreaLabel": "Petersham, NSW 2049", "BaselineSummarySentAt": None}
    old_token = monitoring_scheduler.config.TELEGRAM_BOT_TOKEN
    try:
        monitoring_scheduler.config.TELEGRAM_BOT_TOKEN = ""
        result = monitoring_scheduler._send_setup_summary_once(sub, "baseline")
        assert result["status"] == "warning"
        assert result["warning"] == "telegram token not set; setup summary was not sent"
    finally:
        monitoring_scheduler.config.TELEGRAM_BOT_TOKEN = old_token


def test_initial_detail_baseline_audit_events_disabled_by_default():
    import config
    import db_layer

    old_value = config.CREATE_AUDIT_EVENTS_DURING_INITIAL_BASELINE
    try:
        config.CREATE_AUDIT_EVENTS_DURING_INITIAL_BASELINE = False
        assert db_layer.should_create_listing_events_for_context("initial_detail_baseline", True) is False
        assert db_layer.should_create_listing_events_for_context(None, False) is True
        config.CREATE_AUDIT_EVENTS_DURING_INITIAL_BASELINE = True
        assert db_layer.should_create_listing_events_for_context("initial_detail_baseline", True) is True
    finally:
        config.CREATE_AUDIT_EVENTS_DURING_INITIAL_BASELINE = old_value


def test_setup_progress_stays_not_ready_until_completion():
    import monitoring_scheduler

    sub = {"NotificationReadyAt": None}
    running = monitoring_scheduler._setup_progress(sub, {"baseline_listings_collected": 25, "baseline_new_count": 25, "baseline_pages_checked": 3, "detail_baseline_total_count": 25, "detail_baseline_completed_count": 20, "detail_baseline_remaining_count": 5}, "detail_baseline_running")
    assert running["setup_state"] == "detail_baseline_running"
    assert running["baseline_listings_collected"] == 25
    assert running["baseline_new_count"] == 25
    assert running["baseline_pages_checked"] == 3
    assert running["detail_baseline_remaining_count"] == 5
    assert running["notification_ready_at"] is None
    sub["NotificationReadyAt"] = datetime(2026, 6, 2, 14, 0, 0)
    ready = monitoring_scheduler._setup_progress(sub, {"detail_baseline_total_count": 25, "detail_baseline_completed_count": 25, "detail_baseline_remaining_count": 0}, "ready")
    assert ready["setup_state"] == "ready"
    assert ready["detail_baseline_remaining_count"] == 0
    assert ready["notification_ready_at"] == sub["NotificationReadyAt"]


def test_sender_dry_run_keeps_status():
    class Conn:
        def __init__(self):
            self.rows = [{"NotificationID": 1, "ChatID": "chat", "MessageText": "hello", "EventID": 2}]
            self.commits = 0
        def close(self):
            pass
    conn = Conn()
    import db_layer
    old_get = db_layer.get_queued_notifications
    db_layer.get_queued_notifications = lambda conn, limit=20, channel="telegram": conn.rows
    try:
        out = asyncio.run(telegram_sender.send_queued_notifications(FakeBot(), dry_run=True, conn=conn))
        assert out["processed"] == 1
        assert "Status" not in conn.rows[0]
        assert conn.commits == 0
    finally:
        db_layer.get_queued_notifications = old_get


def test_send_fail_missing_chat_id_marks_failed():
    class Conn:
        def __init__(self):
            self.rows = [{"NotificationID": 1, "ChatID": None, "MessageText": "hello", "EventID": 2}]
            self.commits = 0
        def close(self):
            pass
        def commit(self):
            self.commits += 1
    conn = Conn()
    import db_layer
    old_get = db_layer.get_queued_notifications
    old_fail = db_layer.mark_notification_failed
    db_layer.get_queued_notifications = lambda conn, limit=20, channel="telegram": conn.rows
    db_layer.mark_notification_failed = lambda conn, nid, error: conn.rows[0].update({"Status": "failed", "LastError": error})
    try:
        out = asyncio.run(telegram_sender.send_queued_notifications(FakeBot(), dry_run=False, conn=conn))
        assert out["failed"] == 1
        assert conn.rows[0]["Status"] == "failed"
        assert "Missing ChatID" in conn.rows[0]["LastError"]
    finally:
        db_layer.get_queued_notifications = old_get
        db_layer.mark_notification_failed = old_fail


def run_all():
    for name, func in sorted(globals().items()):
        if name.startswith("test_"):
            func()
            print(f"PASS {name}")


def test_telegram_bot_main_bootstraps_event_loop_before_polling():
    import telegram_bot

    calls = []

    class FakeConn:
        def close(self):
            calls.append("close")

    class FakeApp:
        def run_polling(self):
            calls.append("polling")

    old_token = telegram_bot.config.TELEGRAM_BOT_TOKEN
    old_ensure_loop = telegram_bot.ensure_main_event_loop
    old_connect = telegram_bot._connect
    old_ensure_tables = telegram_bot.db_layer.ensure_telegram_bot_tables
    old_build = telegram_bot.build_application
    try:
        telegram_bot.config.TELEGRAM_BOT_TOKEN = "test-token"
        telegram_bot.ensure_main_event_loop = lambda: calls.append("loop")
        telegram_bot._connect = lambda: FakeConn()
        telegram_bot.db_layer.ensure_telegram_bot_tables = lambda conn: calls.append("tables")
        telegram_bot.build_application = lambda token: calls.append("build") or FakeApp()
        telegram_bot.main()
    finally:
        telegram_bot.config.TELEGRAM_BOT_TOKEN = old_token
        telegram_bot.ensure_main_event_loop = old_ensure_loop
        telegram_bot._connect = old_connect
        telegram_bot.db_layer.ensure_telegram_bot_tables = old_ensure_tables
        telegram_bot.build_application = old_build

    assert calls == ["loop", "tables", "close", "build", "polling"]



# Detail-baseline retry hardening regressions.
def test_retryable_detail_error_classification():
    from listing_detail_refresher import is_retryable_detail_error
    for error in ["get_failed", "HTTP_ERROR_429", "timeout waiting renderer", "net::ERR_NETWORK_CHANGED", "temporarily blocked", "page_not_ready"]:
        assert is_retryable_detail_error(error)
    assert not is_retryable_detail_error("invalid url")
    assert not is_retryable_detail_error("listing id missing")


def test_retryable_detail_batch_failure_threshold():
    from listing_detail_refresher import is_retryable_detail_batch_failure
    assert is_retryable_detail_batch_failure({"processed_count": 10, "failed_count": 10, "errors": [{"error": "get_failed"}]})
    assert is_retryable_detail_batch_failure({"processed_count": 10, "failed_count": 8, "errors": [{"error": "http_error_429"}]})
    assert not is_retryable_detail_batch_failure({"processed_count": 10, "failed_count": 7, "errors": [{"error": "get_failed"}]})
    assert not is_retryable_detail_batch_failure({"processed_count": 10, "failed_count": 10, "errors": [{"error": "invalid url"}]})


def test_detail_baseline_retry_wait_then_terminal_failed():
    import db_layer

    class Cursor:
        def __init__(self, conn): self.conn = conn
        def execute(self, sql, *params):
            if sql.strip().startswith("SELECT COALESCE"):
                self.row = (self.conn.attempts,)
            else:
                self.conn.status, self.conn.attempts, self.conn.next_retry, self.conn.error, _ = params
                self.conn.notification_ready_at = None
            return self
        def fetchone(self): return self.row
    class Conn:
        def __init__(self):
            self.attempts = 0; self.status = "running"; self.row = None; self.commits = 0; self.notification_ready_at = "old"
        def cursor(self): return Cursor(self)
        def commit(self): self.commits += 1
    conn = Conn()
    status = db_layer.mark_subscription_detail_baseline_retry_wait(conn, 4, "get_failed", datetime(2026, 6, 2, 12), 2)
    assert status == "retry_wait" and conn.status == "retry_wait" and conn.attempts == 1 and conn.notification_ready_at is None
    status = db_layer.mark_subscription_detail_baseline_retry_wait(conn, 4, "429", datetime(2026, 6, 2, 13), 2)
    assert status == "failed" and conn.status == "failed" and conn.attempts == 2 and conn.next_retry is None


def test_detail_retry_wait_due_gate():
    import monitoring_scheduler
    now = datetime(2026, 6, 2, 12, 0, 0)
    assert not monitoring_scheduler._detail_retry_is_due(now + timedelta(seconds=1), now)
    assert monitoring_scheduler._detail_retry_is_due(now, now)
    assert monitoring_scheduler._detail_retry_is_due(now - timedelta(seconds=1), now)


def test_detail_refresher_retries_transient_batch_once():
    import listing_detail_refresher
    import db_layer
    calls = []
    candidate = {"external_id": "1", "url": "https://example.test/1", "address": "1 Road, Petersham, NSW 2049"}
    failed = {**candidate, "detail_refresh_success": False, "detail_error": "get_failed"}
    success = {**candidate, "detail_refresh_success": True}
    class Conn:
        def close(self): pass
    old_connect = db_layer.connect
    old_get = db_layer.get_active_listings_for_detail_refresh
    old_ingest = db_layer.ingest_detail_refresh_rows
    old_enrich = listing_detail_refresher.ENRICH_DETAIL_ROWS_FUNC
    try:
        db_layer.connect = lambda path: Conn()
        db_layer.get_active_listings_for_detail_refresh = lambda conn, **kwargs: [candidate]
        db_layer.ingest_detail_refresh_rows = lambda *args, **kwargs: {"events_created": 0, "items": []}
        def enrich(rows, **kwargs):
            calls.append(1)
            return [failed] if len(calls) == 1 else [success]
        listing_detail_refresher.ENRICH_DETAIL_ROWS_FUNC = enrich
        out = listing_detail_refresher.refresh_active_listings("url")
        assert len(calls) == 2
        assert out["immediate_retry_performed"] is True
        assert out["failed_count"] == 0
        assert out["refreshed_count"] == 1
    finally:
        db_layer.connect = old_connect
        db_layer.get_active_listings_for_detail_refresh = old_get
        db_layer.ingest_detail_refresh_rows = old_ingest
        listing_detail_refresher.ENRICH_DETAIL_ROWS_FUNC = old_enrich


# Phase 7 button-driven NSW suburb UX regressions.
def _run(coro):
    return asyncio.run(coro)


class _BotMessage:
    def __init__(self, text=""):
        self.text = text
        self.replies = []

    async def reply_text(self, text, reply_markup=None):
        self.replies.append({"text": text, "reply_markup": reply_markup})


class _BotChat:
    id = 77

    def __init__(self):
        self.messages = []
        self.documents = []

    async def send_message(self, text, reply_markup=None):
        self.messages.append({"text": text, "reply_markup": reply_markup})

    async def send_document(self, document, filename=None, caption=None):
        self.documents.append({"document": document, "filename": filename, "caption": caption})


class _BotQuery:
    def __init__(self, data):
        self.data = data
        self.edits = []
        self.answered = False

    async def answer(self):
        self.answered = True

    async def edit_message_text(self, text, reply_markup=None):
        self.edits.append({"text": text, "reply_markup": reply_markup})


class _BotUpdate:
    def __init__(self, text="", callback_data=None):
        self.message = _BotMessage(text)
        self.effective_chat = _BotChat()
        self.effective_user = type("User", (), {"username": "tester", "first_name": "Test", "last_name": "User"})()
        self.callback_query = _BotQuery(callback_data) if callback_data else None


class _BotContext:
    def __init__(self, args=None):
        self.args = args or []
        self.user_data = {}


def _button_texts(markup):
    return [button.text for row in markup.keyboard for button in row]


def _inline_texts(markup):
    return [button.text for row in markup.inline_keyboard for button in row]


def _with_bot_fakes(test):
    import telegram_bot
    sessions = {}
    added = []
    subscriptions = []
    old = {name: getattr(telegram_bot.db_layer, name) for name in ("upsert_telegram_user", "get_user_session", "set_user_session", "clear_user_session", "add_user_area_subscription", "list_user_area_subscriptions")}
    old_get_export_areas = telegram_bot.excel_exporter.get_user_export_areas
    old_connect = telegram_bot._connect
    old_resolve = telegram_bot.resolve_nsw_area_query

    class Conn:
        def close(self): pass

    def resolve(conn, query):
        petersham = {"suburb_name": "Petersham", "postcode": "2049", "state_code": "NSW", "label": "Petersham, NSW 2049", "search_url": "https://www.realestate.com.au/buy/in-petersham,+nsw+2049/list-1?activeSort=list-date"}
        lewisham = {"suburb_name": "Lewisham", "postcode": "2049", "state_code": "NSW", "label": "Lewisham, NSW 2049", "search_url": "https://www.realestate.com.au/buy/in-lewisham,+nsw+2049/list-1?activeSort=list-date"}
        return {"status": "multiple", "matches": [petersham, lewisham]} if query == "2049" else {"status": "exact", "matches": [petersham]}

    try:
        telegram_bot._connect = lambda: Conn()
        telegram_bot.resolve_nsw_area_query = resolve
        telegram_bot.db_layer.upsert_telegram_user = lambda conn, chat_id, **kwargs: 7
        telegram_bot.db_layer.get_user_session = lambda conn, user_id: sessions.get(user_id, {"state": "idle", "payload": {}})
        telegram_bot.db_layer.set_user_session = lambda conn, user_id, state, payload=None: sessions.__setitem__(user_id, {"state": state, "payload": payload or {}})
        telegram_bot.db_layer.clear_user_session = lambda conn, user_id: sessions.__setitem__(user_id, {"state": "idle", "payload": {}})
        telegram_bot.db_layer.list_user_area_subscriptions = lambda conn, user_id: list(subscriptions)
        telegram_bot.excel_exporter.get_user_export_areas = lambda user_id: list(subscriptions)
        def add(conn, user_id, search_url, area_label, **kwargs):
            added.append({"user_id": user_id, "search_url": search_url, "area_label": area_label, **kwargs})
            return True, {"area_label": area_label}
        telegram_bot.db_layer.add_user_area_subscription = add
        test(telegram_bot, sessions, added, subscriptions)
    finally:
        telegram_bot._connect = old_connect
        telegram_bot.resolve_nsw_area_query = old_resolve
        telegram_bot.excel_exporter.get_user_export_areas = old_get_export_areas
        for name, value in old.items():
            setattr(telegram_bot.db_layer, name, value)


def test_phase7_start_help_and_add_button_flow():
    def scenario(bot, sessions, added, subscriptions):
        update = _BotUpdate()
        _run(bot.start(update, _BotContext()))
        assert set(bot.MAIN_MENU_BUTTONS) <= set(_button_texts(update.message.replies[-1]["reply_markup"]))
        help_update = _BotUpdate()
        _run(bot.help_command(help_update, _BotContext()))
        assert set(bot.MAIN_MENU_BUTTONS) <= set(_button_texts(help_update.message.replies[-1]["reply_markup"]))
        add_update = _BotUpdate(bot.BUTTON_ADD)
        _run(bot.handle_text(add_update, _BotContext()))
        assert sessions[7]["state"] == "waiting_for_area_input"
        assert "Send a NSW suburb name or postcode" in add_update.message.replies[-1]["text"]
        suburb = _BotUpdate("Petersham")
        _run(bot.handle_text(suburb, _BotContext()))
        assert sessions[7]["state"] == "confirming_area"
        assert "Petersham, NSW 2049" in suburb.message.replies[-1]["text"]
        assert "✅ Add this search area" in _inline_texts(suburb.message.replies[-1]["reply_markup"])
        confirm = _BotUpdate(callback_data="area_confirm:add")
        _run(bot.handle_area_callback(confirm, _BotContext()))
        assert added[0]["search_url"] == "https://www.realestate.com.au/buy/in-petersham,+nsw+2049/list-1?activeSort=list-date"
        assert added[0]["suburb"] == "Petersham" and added[0]["state_code"] == "NSW" and added[0]["postcode"] == "2049"
        assert "initial data is collected" in confirm.callback_query.edits[-1]["text"]
        assert "search area" in confirm.callback_query.edits[-1]["text"]
    _with_bot_fakes(scenario)


def test_phase7_postcode_candidates_and_addarea_command_resolve():
    def scenario(bot, sessions, added, subscriptions):
        postcode = _BotUpdate("2049")
        sessions[7] = {"state": "waiting_for_area_input", "payload": {}}
        _run(bot.handle_text(postcode, _BotContext()))
        assert sessions[7]["state"] == "choosing_area_candidate"
        assert "Petersham, NSW 2049" in _inline_texts(postcode.message.replies[-1]["reply_markup"])
        for args in (["Petersham"], ["2049"]):
            command = _BotUpdate()
            _run(bot.addarea(command, _BotContext(args)))
            assert command.message.replies[-1]["reply_markup"] is not None
    _with_bot_fakes(scenario)


def test_phase7_duplicate_message_is_friendly():
    def scenario(bot, sessions, added, subscriptions):
        area = {"suburb_name": "Petersham", "postcode": "2049", "state_code": "NSW", "label": "Petersham, NSW 2049", "search_url": "https://www.realestate.com.au/buy/in-petersham,+nsw+2049/list-1?activeSort=list-date"}
        sessions[7] = {"state": "confirming_area", "payload": {"pending_area": area}}
        bot.db_layer.add_user_area_subscription = lambda *args, **kwargs: (False, {"reason": "duplicate"})
        confirm = _BotUpdate(callback_data="area_confirm:add")
        _run(bot.handle_area_callback(confirm, _BotContext()))
        assert confirm.callback_query.edits[-1]["text"] == "You're already monitoring this search area."
    _with_bot_fakes(scenario)


def test_phase7_remove_flow_requires_confirmation_and_soft_deactivates():
    def scenario(bot, sessions, added, subscriptions):
        subscriptions.append({"UserAreaID": 4, "AreaLabel": "Petersham, NSW 2049", "SearchURL": "generated", "BaselineStatus": "completed", "DetailBaselineStatus": "completed"})
        deactivated = []
        bot.db_layer.deactivate_user_area_subscription = lambda conn, user_id, user_area_id: deactivated.append((user_id, user_area_id)) or True
        choose = _BotUpdate(bot.BUTTON_REMOVE)
        _run(bot.handle_text(choose, _BotContext()))
        assert sessions[7]["state"] == "removing_area"
        assert "Petersham, NSW 2049" in _inline_texts(choose.message.replies[-1]["reply_markup"])
        select = _BotUpdate(callback_data="remove_select:4")
        _run(bot.handle_remove_callback(select, _BotContext()))
        assert "Stop monitoring Petersham, NSW 2049?" in select.callback_query.edits[-1]["text"]
        assert not deactivated
        confirm = _BotUpdate(callback_data="remove_confirm:yes")
        _run(bot.handle_remove_callback(confirm, _BotContext()))
        assert deactivated == [(7, 4)]
        assert "Stopped monitoring Petersham, NSW 2049." in confirm.callback_query.edits[-1]["text"]
    _with_bot_fakes(scenario)


def test_phase7_persisted_session_helpers():
    import db_layer
    class Cursor:
        description = [("TelegramUserID",), ("State",), ("PayloadJson",), ("UpdatedAt",)]
        def __init__(self, conn): self.conn = conn; self.rows = []
        def execute(self, sql, *params):
            if sql.lstrip().startswith("SELECT"):
                self.rows = list(self.conn.rows)
            else:
                self.conn.executed.append((sql, params))
            return self
        def fetchall(self): return self.rows
    class Conn:
        def __init__(self, rows=()): self.rows = rows; self.executed = []; self.commits = 0
        def cursor(self): return Cursor(self)
        def commit(self): self.commits += 1
    old_ensure = db_layer.ensure_telegram_bot_tables
    try:
        db_layer.ensure_telegram_bot_tables = lambda conn: None
        assert db_layer.get_user_session(Conn(), 7) == {"telegram_user_id": 7, "state": "idle", "payload": {}}
        loaded = db_layer.get_user_session(Conn([(7, "confirming_area", '{"pending_area":{"postcode":"2049"}}', "now")]), 7)
        assert loaded["state"] == "confirming_area" and loaded["payload"]["pending_area"]["postcode"] == "2049"
        conn = Conn()
        db_layer.set_user_session(conn, 7, "waiting_for_area_input", {"query": "Petersham"})
        assert conn.commits == 1 and conn.executed[-1][1][1] == "waiting_for_area_input"
        db_layer.clear_user_session(conn, 7)
        assert conn.executed[-1][1][1] == "idle"
        try:
            db_layer.set_user_session(conn, 7, "unsupported", {})
            raise AssertionError("unsupported session state must fail")
        except ValueError:
            pass
    finally:
        db_layer.ensure_telegram_bot_tables = old_ensure


def test_phase7_max_area_message_is_friendly():
    def scenario(bot, sessions, added, subscriptions):
        area = {"suburb_name": "Petersham", "postcode": "2049", "state_code": "NSW", "label": "Petersham, NSW 2049", "search_url": "https://www.realestate.com.au/buy/in-petersham,+nsw+2049/list-1?activeSort=list-date"}
        sessions[7] = {"state": "confirming_area", "payload": {"pending_area": area}}
        bot.db_layer.add_user_area_subscription = lambda *args, **kwargs: (False, {"reason": "max_areas"})
        confirm = _BotUpdate(callback_data="area_confirm:add")
        _run(bot.handle_area_callback(confirm, _BotContext()))
        assert "monitor up to 3 suburbs" in confirm.callback_query.edits[-1]["text"]
    _with_bot_fakes(scenario)


def test_phase7_my_suburbs_and_export_menu():
    def scenario(bot, sessions, added, subscriptions):
        subscriptions.append({"UserAreaID": 4, "SearchID": 22, "AreaLabel": "Petersham, NSW 2049", "SearchURL": "generated", "IsActive": True, "BaselineStatus": "completed", "DetailBaselineStatus": "completed", "PriceBaselineStatus": "completed", "NotificationReadyAt": "now"})
        listed = _BotUpdate(bot.BUTTON_AREAS)
        _run(bot.handle_text(listed, _BotContext()))
        assert "Petersham, NSW 2049 — Ready" in listed.message.replies[-1]["text"]
        actions = _inline_texts(listed.message.replies[-1]["reply_markup"])
        assert bot.BUTTON_ADD in actions and bot.BUTTON_REMOVE in actions and bot.BUTTON_EXPORT in actions and bot.BUTTON_BACK in actions
        exported = _BotUpdate(bot.BUTTON_EXPORT)
        _run(bot.handle_text(exported, _BotContext()))
        assert exported.message.replies[-1]["text"] == "Choose a search area to export:"
        assert "Petersham, NSW 2049" in _inline_texts(exported.message.replies[-1]["reply_markup"])
    _with_bot_fakes(scenario)


def test_initial_baseline_uses_initial_baseline_page_limit_not_light_limit():
    import monitoring_scheduler
    calls = []
    class Conn:
        def close(self): pass
    sub = {"UserAreaID": 4, "SearchURL": "url", "AreaLabel": "Tanglewood, NSW 2488"}
    old = (monitoring_scheduler.db_layer.connect, monitoring_scheduler.db_layer.get_user_area_subscription, monitoring_scheduler.db_layer.mark_subscription_baseline_started, monitoring_scheduler.db_layer.mark_subscription_baseline_completed, monitoring_scheduler.light_check_area, monitoring_scheduler.config.INITIAL_BASELINE_MAX_PAGES, monitoring_scheduler.config.LIGHT_CHECK_PAGES)
    try:
        monitoring_scheduler.db_layer.connect = lambda path: Conn()
        monitoring_scheduler.db_layer.get_user_area_subscription = lambda conn, user_area_id: sub
        monitoring_scheduler.db_layer.mark_subscription_baseline_started = lambda conn, user_area_id: calls.append(("started", user_area_id))
        monitoring_scheduler.db_layer.mark_subscription_baseline_completed = lambda conn, user_area_id, **kwargs: calls.append(("completed", kwargs))
        monitoring_scheduler.config.INITIAL_BASELINE_MAX_PAGES = 50
        monitoring_scheduler.config.LIGHT_CHECK_PAGES = 2
        def baseline(*args, **kwargs):
            calls.append(("scan", kwargs))
            return {"rows_scraped": 240, "new_count": 240, "pages_checked": 10, "total_pages_detected": 10, "stop_reason": "reached_total_pages", "errors": []}
        monitoring_scheduler.light_check_area = baseline
        out = monitoring_scheduler.run_initial_baseline_for_subscription(4)
        scan = next(value for key, value in calls if key == "scan")
        assert scan["max_pages"] == 50 and scan["full_scan"] is True
        assert out["status"] == "completed" and out["pages_checked"] == 10
    finally:
        monitoring_scheduler.db_layer.connect, monitoring_scheduler.db_layer.get_user_area_subscription, monitoring_scheduler.db_layer.mark_subscription_baseline_started, monitoring_scheduler.db_layer.mark_subscription_baseline_completed, monitoring_scheduler.light_check_area, monitoring_scheduler.config.INITIAL_BASELINE_MAX_PAGES, monitoring_scheduler.config.LIGHT_CHECK_PAGES = old


def test_known_baseline_page_cap_is_incomplete_and_summary_warns():
    import monitoring_scheduler
    calls = []
    class Conn:
        def close(self): pass
    sub = {"UserAreaID": 4, "SearchURL": "url", "AreaLabel": "Tanglewood, NSW 2488"}
    old = (monitoring_scheduler.db_layer.connect, monitoring_scheduler.db_layer.get_user_area_subscription, monitoring_scheduler.db_layer.mark_subscription_baseline_started, monitoring_scheduler.db_layer.mark_subscription_baseline_failed, monitoring_scheduler.light_check_area)
    try:
        monitoring_scheduler.db_layer.connect = lambda path: Conn()
        monitoring_scheduler.db_layer.get_user_area_subscription = lambda conn, user_area_id: sub
        monitoring_scheduler.db_layer.mark_subscription_baseline_started = lambda conn, user_area_id: None
        monitoring_scheduler.db_layer.mark_subscription_baseline_failed = lambda conn, user_area_id, error, **kwargs: calls.append(kwargs)
        monitoring_scheduler.light_check_area = lambda *args, **kwargs: {"rows_scraped": 73, "new_count": 73, "pages_checked": 3, "total_pages_detected": 10, "stop_reason": "max_pages_reached", "errors": []}
        out = monitoring_scheduler.run_initial_baseline_for_subscription(4)
        assert out["status"] == "incomplete" and calls[0]["pages_checked"] == 3
        text = monitoring_scheduler._baseline_incomplete_text({"AreaLabel": sub["AreaLabel"], "BaselineListingsCollected": 73, "BaselinePagesChecked": 3, "BaselineTotalPagesDetected": 10, "BaselineStopReason": "max_pages_reached"})
        assert "⚠️ Baseline may be incomplete" in text and "Pages checked: 3/10" in text and "Reason: max_pages_reached" in text
    finally:
        monitoring_scheduler.db_layer.connect, monitoring_scheduler.db_layer.get_user_area_subscription, monitoring_scheduler.db_layer.mark_subscription_baseline_started, monitoring_scheduler.db_layer.mark_subscription_baseline_failed, monitoring_scheduler.light_check_area = old


def test_completed_baseline_recurring_check_uses_light_page_limit():
    import monitoring_scheduler
    calls = []
    now = datetime(2026, 6, 2, 12)
    sub = {"UserAreaID": 4, "SearchURL": "url", "AreaLabel": "Tanglewood, NSW 2488", "BaselineStatus": "completed", "BaselineSummarySentAt": "sent", "DetailBaselineStatus": "completed", "NotificationReadyAt": now, "ReadySummarySentAt": "sent", "LastLightCheckAt": None, "LastDetailRefreshAt": now}
    class Conn:
        def close(self): pass
    names = ("connect", "ensure_telegram_bot_tables", "get_active_user_area_subscriptions", "mark_subscription_light_checked", "queue_notifications_for_user_area", "get_user_area_baseline_summary")
    old_db = tuple(getattr(monitoring_scheduler.db_layer, name) for name in names)
    old = (monitoring_scheduler.light_check_area, monitoring_scheduler._utcnow, monitoring_scheduler.config.LIGHT_CHECK_PAGES)
    try:
        monitoring_scheduler.db_layer.connect = lambda path: Conn()
        monitoring_scheduler.db_layer.ensure_telegram_bot_tables = lambda conn: None
        monitoring_scheduler.db_layer.get_active_user_area_subscriptions = lambda conn: [sub]
        monitoring_scheduler.db_layer.mark_subscription_light_checked = lambda conn, user_area_id: None
        monitoring_scheduler.db_layer.queue_notifications_for_user_area = lambda *args, **kwargs: {"queued_count": 0}
        monitoring_scheduler.db_layer.get_user_area_baseline_summary = lambda conn, subscription_id: {}
        monitoring_scheduler.config.LIGHT_CHECK_PAGES = 2
        monitoring_scheduler._utcnow = lambda: now
        monitoring_scheduler.light_check_area = lambda *args, **kwargs: calls.append(kwargs) or {"rows_scraped": 2}
        monitoring_scheduler.run_monitoring_tick()
        assert calls and calls[0]["max_pages"] == 2 and not calls[0].get("full_scan")
    finally:
        for name, value in zip(names, old_db): setattr(monitoring_scheduler.db_layer, name, value)
        monitoring_scheduler.light_check_area, monitoring_scheduler._utcnow, monitoring_scheduler.config.LIGHT_CHECK_PAGES = old


def test_baseline_summary_includes_pages_checked_and_detail_total_matches_full_set():
    import monitoring_scheduler
    import db_layer
    text = monitoring_scheduler._baseline_summary_text({"AreaLabel": "Tanglewood, NSW 2488", "BaselinePagesChecked": 10, "BaselineTotalPagesDetected": 10}, 240)
    assert "Listings found: 240" in text and "Pages checked: 10/10" in text
    class Cursor:
        def execute(self, sql, *params): self.params = params; return self
        def fetchone(self): return (240, 0)
    class Conn:
        def cursor(self): return Cursor()
    progress = db_layer.get_detail_baseline_progress(Conn(), {"SearchID": 9, "DetailBaselineStartedAt": None, "Suburb": "Tanglewood", "StateCode": "NSW", "Postcode": "2488", "NotificationReadyAt": None})
    assert progress["detail_baseline_total_count"] == 240


if __name__ == "__main__":
    run_all()
