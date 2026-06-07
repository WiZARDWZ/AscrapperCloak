from __future__ import annotations

import json
import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
sys.modules.setdefault("chrome_options_helper", types.SimpleNamespace(build_chrome_driver=lambda *a, **k: None, cleanup_chrome_driver=lambda *a, **k: None))

import db_layer
import monitoring_scheduler
import notification_formatter


class Conn:
    def close(self):
        pass
    def commit(self):
        pass


def _candidate(n: int) -> dict:
    return {
        "db_listing_id": n,
        "listing_id": f"ext-{n}",
        "external_id": f"ext-{n}",
        "price_display": "Contact agent" if n % 2 else "",
        "url": f"https://example.test/property-{n}",
        "address": f"{n} Test Street",
    }


def test_setup_price_baseline_completed_with_unknowns_marks_unknown_and_enqueues_retry():
    import module2_infer_prices

    candidates = [_candidate(i) for i in range(1, 11)]
    calls = {"unknown": [], "completed": [], "retry": None}
    originals = {
        "load": monitoring_scheduler._load_search_subscription,
        "connect": monitoring_scheduler.db_layer.connect,
        "history": monitoring_scheduler._price_sweep_history,
        "get_candidates": monitoring_scheduler.db_layer.get_active_listings_for_price_inference,
        "update": monitoring_scheduler.db_layer.update_listing_price_inference,
        "unknown": monitoring_scheduler.db_layer.mark_price_inference_unknown_pending_retry,
        "retry": monitoring_scheduler._enqueue_price_retry_unknowns,
        "module2_run": module2_infer_prices.module2_run,
    }

    def fake_module2_run(**kwargs):
        rows = []
        for idx in range(1, 8):
            rows.append({
                "listing_id": f"ext-{idx}",
                "price_inferred_low": idx * 100000,
                "price_inferred_high": idx * 100000 + 50000,
                "price_inferred_method": "test",
            })
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(rows, f)
        fake_module2_run.last_result = {"sweep_mode": "setup_full_sweep", "status": "done"}
        return None, path

    fake_module2_run.last_result = {}

    try:
        monitoring_scheduler._load_search_subscription = lambda search_id, preferred_user_area_id=None: {"SearchID": search_id, "SearchURL": "https://example.test", "UserAreaID": 1}
        monitoring_scheduler.db_layer.connect = lambda path=None: Conn()
        monitoring_scheduler._price_sweep_history = lambda conn, search_id: {"has_enough_history": False}
        monitoring_scheduler.db_layer.get_active_listings_for_price_inference = lambda *a, **k: list(candidates)
        monitoring_scheduler.db_layer.update_listing_price_inference = lambda conn, search_id, listing_id, *a, **k: calls["completed"].append(listing_id)
        monitoring_scheduler.db_layer.mark_price_inference_unknown_pending_retry = lambda conn, search_id, listing_id, reason=None: calls["unknown"].append((listing_id, reason))
        monitoring_scheduler._enqueue_price_retry_unknowns = lambda search_id, ids, **kwargs: calls.update({"retry": list(ids)}) or {"JobID": 99, "listing_external_ids": list(ids)}
        module2_infer_prices.module2_run = fake_module2_run

        result = monitoring_scheduler.run_price_baseline_for_search(1, limit=10, setup=True, mark_search_complete=False)
    finally:
        monitoring_scheduler._load_search_subscription = originals["load"]
        monitoring_scheduler.db_layer.connect = originals["connect"]
        monitoring_scheduler._price_sweep_history = originals["history"]
        monitoring_scheduler.db_layer.get_active_listings_for_price_inference = originals["get_candidates"]
        monitoring_scheduler.db_layer.update_listing_price_inference = originals["update"]
        monitoring_scheduler.db_layer.mark_price_inference_unknown_pending_retry = originals["unknown"]
        monitoring_scheduler._enqueue_price_retry_unknowns = originals["retry"]
        module2_infer_prices.module2_run = originals["module2_run"]

    assert result["status"] == "completed_with_unknowns"
    assert result["inferred_count"] == 7
    assert result["unknown_count"] == 3
    assert result["unknown_external_ids"] == ["ext-8", "ext-9", "ext-10"]
    assert calls["unknown"] == [(8, "price_not_inferred_after_sweep"), (9, "price_not_inferred_after_sweep"), (10, "price_not_inferred_after_sweep")]
    assert calls["retry"] == ["ext-8", "ext-9", "ext-10"]
    assert result["price_retry_job_enqueued"] is True



def test_setup_price_baseline_selects_all_active_listings_for_full_sweep():
    import module2_infer_prices

    candidates = [_candidate(i) for i in range(1, 36)]
    calls = {"candidate_limit": "unset", "module2_inputs": [], "updates": []}
    originals = {
        "load": monitoring_scheduler._load_search_subscription,
        "connect": monitoring_scheduler.db_layer.connect,
        "history": monitoring_scheduler._price_sweep_history,
        "get_candidates": monitoring_scheduler.db_layer.get_active_listings_for_price_inference,
        "update": monitoring_scheduler.db_layer.update_listing_price_inference,
        "unknown": monitoring_scheduler.db_layer.mark_price_inference_unknown_pending_retry,
        "mark_search": monitoring_scheduler.db_layer.mark_search_price_refreshed,
        "module2_run": module2_infer_prices.module2_run,
    }

    def fake_get_candidates(conn, search_id, limit=10, **kwargs):
        calls["candidate_limit"] = limit
        return list(candidates)

    def fake_module2_run(**kwargs):
        with open(kwargs["input_file"], "r", encoding="utf-8") as f:
            input_rows = json.load(f)
        calls["module2_inputs"].append({"count": len(input_rows), "sweep_mode": kwargs.get("sweep_mode")})
        rows = []
        for idx in range(1, 36):
            rows.append({
                "listing_id": f"ext-{idx}",
                "price_inferred_low": idx * 100000,
                "price_inferred_high": idx * 100000 + 50000,
                "price_inferred_method": "test",
            })
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(rows, f)
        fake_module2_run.last_result = {"sweep_mode": "setup_full_sweep", "status": "done", "windows_checked": 12}
        return None, path

    fake_module2_run.last_result = {}

    try:
        monitoring_scheduler._load_search_subscription = lambda search_id, preferred_user_area_id=None: {"SearchID": search_id, "SearchURL": "https://example.test", "UserAreaID": 1}
        monitoring_scheduler.db_layer.connect = lambda path=None: Conn()
        monitoring_scheduler._price_sweep_history = lambda conn, search_id: {"has_enough_history": False}
        monitoring_scheduler.db_layer.get_active_listings_for_price_inference = fake_get_candidates
        monitoring_scheduler.db_layer.update_listing_price_inference = lambda conn, search_id, listing_id, *a, **k: calls["updates"].append(listing_id)
        monitoring_scheduler.db_layer.mark_price_inference_unknown_pending_retry = lambda *a, **k: None
        monitoring_scheduler.db_layer.mark_search_price_refreshed = lambda conn, search_id: None
        module2_infer_prices.module2_run = fake_module2_run

        result = monitoring_scheduler.run_price_baseline_for_search(1, setup=True, mark_search_complete=True)
    finally:
        monitoring_scheduler._load_search_subscription = originals["load"]
        monitoring_scheduler.db_layer.connect = originals["connect"]
        monitoring_scheduler._price_sweep_history = originals["history"]
        monitoring_scheduler.db_layer.get_active_listings_for_price_inference = originals["get_candidates"]
        monitoring_scheduler.db_layer.update_listing_price_inference = originals["update"]
        monitoring_scheduler.db_layer.mark_price_inference_unknown_pending_retry = originals["unknown"]
        monitoring_scheduler.db_layer.mark_search_price_refreshed = originals["mark_search"]
        module2_infer_prices.module2_run = originals["module2_run"]

    assert calls["candidate_limit"] is None
    assert result["target_count"] == 35
    assert result["candidates_count"] == 35
    assert result["setup_full_sweep_all_targets"] is True
    assert result["batch_size_used"] == "all"
    assert calls["module2_inputs"] == [{"count": 35, "sweep_mode": "setup_full_sweep"}]
    assert len(calls["updates"]) == 35


def test_setup_full_sweep_debug_limit_still_limits_manual_runs():
    import module2_infer_prices

    candidates = [_candidate(i) for i in range(1, 11)]
    calls = {"candidate_limit": None, "module2_count": None}
    originals = {
        "load": monitoring_scheduler._load_search_subscription,
        "connect": monitoring_scheduler.db_layer.connect,
        "history": monitoring_scheduler._price_sweep_history,
        "get_candidates": monitoring_scheduler.db_layer.get_active_listings_for_price_inference,
        "update": monitoring_scheduler.db_layer.update_listing_price_inference,
        "unknown": monitoring_scheduler.db_layer.mark_price_inference_unknown_pending_retry,
        "module2_run": module2_infer_prices.module2_run,
    }

    def fake_module2_run(**kwargs):
        with open(kwargs["input_file"], "r", encoding="utf-8") as f:
            input_rows = json.load(f)
        calls["module2_count"] = len(input_rows)
        rows = [{"listing_id": f"ext-{idx}", "price_inferred_low": idx, "price_inferred_high": idx + 1, "price_inferred_method": "test"} for idx in range(1, 11)]
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(rows, f)
        fake_module2_run.last_result = {"sweep_mode": "setup_full_sweep", "status": "done"}
        return None, path

    fake_module2_run.last_result = {}

    try:
        monitoring_scheduler._load_search_subscription = lambda search_id, preferred_user_area_id=None: {"SearchID": search_id, "SearchURL": "https://example.test", "UserAreaID": 1}
        monitoring_scheduler.db_layer.connect = lambda path=None: Conn()
        monitoring_scheduler._price_sweep_history = lambda conn, search_id: {"has_enough_history": False}
        monitoring_scheduler.db_layer.get_active_listings_for_price_inference = lambda conn, search_id, limit=10, **kwargs: calls.update({"candidate_limit": limit}) or list(candidates)
        monitoring_scheduler.db_layer.update_listing_price_inference = lambda *a, **k: None
        monitoring_scheduler.db_layer.mark_price_inference_unknown_pending_retry = lambda *a, **k: None
        module2_infer_prices.module2_run = fake_module2_run

        result = monitoring_scheduler.run_price_baseline_for_search(1, limit=10, setup=True, mark_search_complete=False)
    finally:
        monitoring_scheduler._load_search_subscription = originals["load"]
        monitoring_scheduler.db_layer.connect = originals["connect"]
        monitoring_scheduler._price_sweep_history = originals["history"]
        monitoring_scheduler.db_layer.get_active_listings_for_price_inference = originals["get_candidates"]
        monitoring_scheduler.db_layer.update_listing_price_inference = originals["update"]
        monitoring_scheduler.db_layer.mark_price_inference_unknown_pending_retry = originals["unknown"]
        module2_infer_prices.module2_run = originals["module2_run"]

    assert calls["candidate_limit"] == 10
    assert result["status"] != "failed", result
    assert calls["module2_count"] == 10
    assert result["target_count"] == 10
    assert result["setup_full_sweep_all_targets"] is False
    assert result["batch_size_used"] == 10


def test_setup_price_queue_job_does_not_apply_configured_batch_size():
    calls = {"limit": "unset", "setup": None}
    originals = {
        "load": monitoring_scheduler._load_search_subscription,
        "connect": monitoring_scheduler.db_layer.connect,
        "get_sub": monitoring_scheduler.db_layer.get_user_area_subscription,
        "progress": monitoring_scheduler.db_layer.get_price_baseline_progress,
        "mark_started": monitoring_scheduler.db_layer.mark_subscription_price_baseline_started,
        "mark_completed": monitoring_scheduler.db_layer.mark_subscription_price_baseline_completed,
        "run_price": monitoring_scheduler.run_price_baseline_for_search,
        "summary": monitoring_scheduler._send_setup_summary_once,
    }
    sub = {"UserAreaID": 1, "SearchID": 1, "SearchURL": "url", "PriceBaselineStatus": "pending", "AreaLabel": "Noona, NSW 2835"}
    try:
        monitoring_scheduler._load_search_subscription = lambda search_id, user_area_id=None: dict(sub)
        monitoring_scheduler.db_layer.connect = lambda path=None: Conn()
        monitoring_scheduler.db_layer.get_user_area_subscription = lambda conn, user_area_id: dict(sub)
        monitoring_scheduler.db_layer.get_price_baseline_progress = lambda conn, subscription: {"price_baseline_total_count": 35, "price_baseline_completed_count": 35, "price_baseline_remaining_count": 0}
        monitoring_scheduler.db_layer.mark_subscription_price_baseline_started = lambda conn, user_area_id: None
        monitoring_scheduler.db_layer.mark_subscription_price_baseline_completed = lambda conn, user_area_id: None
        def fake_run(search_id, **kwargs):
            calls["limit"] = kwargs.get("limit", None)
            calls["setup"] = kwargs.get("setup")
            return {"status": "completed", "processed_count": 35, "inferred_count": 35, "unknown_count": 0}
        monitoring_scheduler.run_price_baseline_for_search = fake_run
        monitoring_scheduler._send_setup_summary_once = lambda subscription, summary_type: {"status": "sent", "summary_type": summary_type}

        result = monitoring_scheduler._run_setup_price_batch({"SearchID": 1, "UserAreaID": 1}, send_telegram=True)
    finally:
        monitoring_scheduler._load_search_subscription = originals["load"]
        monitoring_scheduler.db_layer.connect = originals["connect"]
        monitoring_scheduler.db_layer.get_user_area_subscription = originals["get_sub"]
        monitoring_scheduler.db_layer.get_price_baseline_progress = originals["progress"]
        monitoring_scheduler.db_layer.mark_subscription_price_baseline_started = originals["mark_started"]
        monitoring_scheduler.db_layer.mark_subscription_price_baseline_completed = originals["mark_completed"]
        monitoring_scheduler.run_price_baseline_for_search = originals["run_price"]
        monitoring_scheduler._send_setup_summary_once = originals["summary"]

    assert result["status"] == "ready"
    assert calls == {"limit": None, "setup": True}

def test_setup_price_batch_becomes_ready_after_completed_with_unknowns():
    calls = {"completed": False}
    originals = {
        "load": monitoring_scheduler._load_search_subscription,
        "connect": monitoring_scheduler.db_layer.connect,
        "get_sub": monitoring_scheduler.db_layer.get_user_area_subscription,
        "progress": monitoring_scheduler.db_layer.get_price_baseline_progress,
        "mark_started": monitoring_scheduler.db_layer.mark_subscription_price_baseline_started,
        "mark_completed": monitoring_scheduler.db_layer.mark_subscription_price_baseline_completed,
        "run_price": monitoring_scheduler.run_price_baseline_for_search,
        "summary": monitoring_scheduler._send_setup_summary_once,
    }
    sub = {"UserAreaID": 1, "SearchID": 1, "SearchURL": "url", "PriceBaselineStatus": "pending", "AreaLabel": "Noona, NSW 2835"}
    try:
        monitoring_scheduler._load_search_subscription = lambda search_id, user_area_id=None: dict(sub)
        monitoring_scheduler.db_layer.connect = lambda path=None: Conn()
        monitoring_scheduler.db_layer.get_user_area_subscription = lambda conn, user_area_id: dict(sub)
        monitoring_scheduler.db_layer.get_price_baseline_progress = lambda conn, subscription: {"price_baseline_total_count": 10, "price_baseline_completed_count": 10, "price_baseline_remaining_count": 0}
        monitoring_scheduler.db_layer.mark_subscription_price_baseline_started = lambda conn, user_area_id: None
        monitoring_scheduler.db_layer.mark_subscription_price_baseline_completed = lambda conn, user_area_id: calls.update({"completed": True})
        monitoring_scheduler.run_price_baseline_for_search = lambda *a, **k: {"status": "completed_with_unknowns", "processed_count": 10, "inferred_count": 7, "unknown_count": 3}
        monitoring_scheduler._send_setup_summary_once = lambda subscription, summary_type: {"status": "sent", "summary_type": summary_type}

        result = monitoring_scheduler._run_setup_price_batch({"SearchID": 1, "UserAreaID": 1}, send_telegram=True)
    finally:
        monitoring_scheduler._load_search_subscription = originals["load"]
        monitoring_scheduler.db_layer.connect = originals["connect"]
        monitoring_scheduler.db_layer.get_user_area_subscription = originals["get_sub"]
        monitoring_scheduler.db_layer.get_price_baseline_progress = originals["progress"]
        monitoring_scheduler.db_layer.mark_subscription_price_baseline_started = originals["mark_started"]
        monitoring_scheduler.db_layer.mark_subscription_price_baseline_completed = originals["mark_completed"]
        monitoring_scheduler.run_price_baseline_for_search = originals["run_price"]
        monitoring_scheduler._send_setup_summary_once = originals["summary"]

    assert result["status"] == "ready"
    assert calls["completed"] is True


def test_ready_summary_does_not_show_preparing_when_unknown_prices_remain():
    text = monitoring_scheduler._ready_summary_text({"AreaLabel": "Noona, NSW 2835", "PriceBaselineTotalCount": 10, "PriceBaselineInferredCount": 7, "PriceBaselineUnknownCount": 3})
    assert "Monitoring is active" in text
    assert "Preparing monitoring" not in text
    assert "3 listings currently have Unknown price" in text


def test_effective_price_display_unknown_fallback():
    assert notification_formatter.effective_price_text({"price_display": "Contact agent"}) == "Unknown"


def test_new_listing_unknown_price_still_notifies():
    originals = {
        "load": monitoring_scheduler._load_search_subscription,
        "refresh": monitoring_scheduler.refresh_active_listings,
        "price": monitoring_scheduler.run_price_baseline_for_search,
        "queue": monitoring_scheduler._queue_notifications_for_search,
    }
    try:
        monitoring_scheduler._load_search_subscription = lambda search_id, preferred_user_area_id=None: {"SearchID": search_id, "SearchURL": "url", "UserAreaID": 1}
        monitoring_scheduler.refresh_active_listings = lambda *a, **k: {"refreshed_count": 1, "errors": []}
        monitoring_scheduler.run_price_baseline_for_search = lambda *a, **k: {"status": "completed_with_unknowns", "unknown_count": 1, "price_retry_job_enqueued": True}
        monitoring_scheduler._queue_notifications_for_search = lambda search_id, dry_run=False: [{"queued": True}]
        result = monitoring_scheduler.run_process_new_listing_for_search(1, ["ext-1"], dry_run=False, send_telegram=True)
    finally:
        monitoring_scheduler._load_search_subscription = originals["load"]
        monitoring_scheduler.refresh_active_listings = originals["refresh"]
        monitoring_scheduler.run_price_baseline_for_search = originals["price"]
        monitoring_scheduler._queue_notifications_for_search = originals["queue"]
    assert result["status"] == "completed"
    assert result["notifications"] == [{"queued": True}]
    assert result["price_results"][0]["price_retry_job_enqueued"] is True


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("price unknown readiness tests passed")
