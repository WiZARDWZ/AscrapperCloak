from __future__ import annotations

from unittest import mock

import pytest

import monitor
from realestate_errors import RealEstateBlockedError


TARGET_URL = "https://www.realestate.com.au/buy/in-yadboro,+nsw+2539/list-1"
WRONG_URL = "https://www.realestate.com.au/buy/in-nowra,+nsw+2541/list-1"


def _row(listing_id: str = "100", suburb: str = "Yadboro", postcode: str = "2539") -> dict:
    slug = suburb.lower().replace(" ", "-")
    return {
        "listing_id": listing_id,
        "url": f"https://www.realestate.com.au/property-house-nsw-{slug}-{listing_id}",
        "address": f"1 Main Street, {suburb}, NSW {postcode}",
        "price": "$800,000",
    }


def _scan(rows, **overrides) -> dict:
    result = {
        "rows": list(rows),
        "scan_status": "ok",
        "trusted_scan": True,
        "stop_reason": "reached_total_pages",
        "pages_checked": 2,
        "total_pages_detected": 2,
        "current_url": TARGET_URL,
        "blocked_reason": None,
        "retry_after_seconds": None,
        "page_state": "listings",
    }
    result.update(overrides)
    return result


class _Conn:
    def __init__(self, calls):
        self.calls = calls

    def commit(self):
        self.calls.append(("commit",))

    def rollback(self):
        self.calls.append(("rollback",))

    def close(self):
        self.calls.append(("close",))


def _patch_baseline(monkeypatch, scan_result, *, detail_results=None):
    calls = []
    detail_results = iter(detail_results or [{"created": True}])
    monkeypatch.setattr(monitor, "init_db", lambda path: None)
    monkeypatch.setattr(monitor, "connect", lambda path: _Conn(calls))
    monkeypatch.setattr(monitor, "get_or_create_area", lambda conn, url: 42)
    monkeypatch.setattr(
        monitor,
        "upsert_area_monitoring_state",
        lambda conn, area_id, **kwargs: calls.append(("state", area_id, kwargs)),
    )
    monkeypatch.setattr(
        monitor,
        "ingest_full_rows",
        lambda *args, **kwargs: calls.append(("ingest", args, kwargs)) or 77,
    )
    monkeypatch.setattr(
        monitor.db_layer,
        "mark_search_baseline_completed",
        lambda conn, search_id, **kwargs: calls.append(("baseline_completed", search_id, kwargs)),
    )
    monkeypatch.setattr(
        monitor.db_layer,
        "enqueue_setup_detail_baseline_job",
        lambda conn, search_id, **kwargs: calls.append(("detail_job", search_id, kwargs)) or next(detail_results),
    )
    monkeypatch.setattr(
        monitor,
        "activate_area_subscriptions",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("Module1 must not activate notifications")),
    )
    monkeypatch.setattr(monitor.module1_list_scraper, "scrape_search_with_result", lambda *args, **kwargs: dict(scan_result))
    return calls


def _calls(calls, name):
    return [call for call in calls if call[0] == name]


def test_non_empty_partial_blocked_baseline_never_ingests(monkeypatch):
    calls = _patch_baseline(
        monkeypatch,
        _scan([_row()], scan_status="partial_blocked", trusted_scan=False, stop_reason="blocked_kpsdk", page_state="blocked_kpsdk", blocked_reason="blocked_kpsdk", retry_after_seconds=90),
    )

    with pytest.raises(RealEstateBlockedError) as exc:
        monitor.baseline_setup_area(TARGET_URL)

    assert exc.value.retry_after_seconds == 90
    assert _calls(calls, "ingest") == []
    assert _calls(calls, "detail_job") == []
    assert _calls(calls, "baseline_completed") == []
    assert _calls(calls, "state")[-1][2]["module1_status"] == "retry_wait"


@pytest.mark.parametrize("blocked_reason", ["blocked_http_429", "blocked_kpsdk"])
def test_block_after_collecting_rows_preserves_backoff(monkeypatch, blocked_reason):
    calls = _patch_baseline(
        monkeypatch,
        _scan([_row()], scan_status="partial_blocked", trusted_scan=False, stop_reason=blocked_reason, page_state=blocked_reason, blocked_reason=blocked_reason, retry_after_seconds=321),
    )

    with pytest.raises(RealEstateBlockedError) as exc:
        monitor.baseline_setup_area(TARGET_URL)

    assert exc.value.reason == blocked_reason
    assert exc.value.retry_after_seconds == 321
    assert _calls(calls, "ingest") == []


def test_wrong_current_url_rejects_valid_looking_rows(monkeypatch):
    calls = _patch_baseline(monkeypatch, _scan([_row()], current_url=WRONG_URL))

    with pytest.raises(RuntimeError, match="wrong_area_current_url"):
        monitor.baseline_setup_area(TARGET_URL)

    assert _calls(calls, "ingest") == []
    assert _calls(calls, "detail_job") == []
    assert _calls(calls, "state")[-1][2]["module1_status"] == "failed"


def test_wrong_area_rows_heavy_mismatch_are_rejected(monkeypatch):
    wrong_rows = [_row("201", "Nowra", "2541"), _row("202", "Nowra", "2541")]
    calls = _patch_baseline(monkeypatch, _scan(wrong_rows))

    with pytest.raises(RuntimeError, match="wrong_area"):
        monitor.baseline_setup_area(TARGET_URL)

    assert _calls(calls, "ingest") == []
    assert _calls(calls, "detail_job") == []


@pytest.mark.parametrize("state", ["blank_render", "render_timeout"])
def test_technical_empty_scan_is_not_trusted_no_results(monkeypatch, state):
    calls = _patch_baseline(
        monkeypatch,
        _scan([], scan_status="technical_failure", trusted_scan=False, stop_reason=state, page_state=state, retry_after_seconds=75),
    )

    with pytest.raises(RealEstateBlockedError):
        monitor.baseline_setup_area(TARGET_URL)

    assert _calls(calls, "ingest") == []
    assert _calls(calls, "detail_job") == []


def test_trusted_success_ingests_once_without_events_and_enqueues_detail(monkeypatch):
    rows = [_row("301"), _row("302")]
    calls = _patch_baseline(monkeypatch, _scan(rows))

    out = monitor.baseline_setup_area(TARGET_URL)

    ingest = _calls(calls, "ingest")
    assert len(ingest) == 1
    assert ingest[0][2]["full_scan"] is True
    assert ingest[0][2]["emit_events"] is False
    assert len(_calls(calls, "detail_job")) == 1
    completed_state = _calls(calls, "state")[-1][2]
    assert completed_state["setup_status"] == "preparing"
    assert completed_state["module1_status"] == "completed"
    assert out["trusted_scan"] is True
    assert out["rows_accepted"] == 2
    assert out["ingest_allowed"] is True
    assert out["detail_job_enqueued"] is True


def test_trusted_explicit_no_results_advances_without_zero_full_scan(monkeypatch):
    calls = _patch_baseline(
        monkeypatch,
        _scan([], scan_status="valid_empty_result", stop_reason="no_results", page_state="no_results", total_pages_detected=None),
    )

    out = monitor.baseline_setup_area(TARGET_URL)

    assert _calls(calls, "ingest") == []
    assert len(_calls(calls, "detail_job")) == 1
    assert out["trusted_scan"] is True
    assert out["completed_empty"] is True
    assert out["ingest_allowed"] is False
    assert _calls(calls, "state")[-1][2]["setup_status"] == "preparing"


def test_trusted_rerun_dedupes_active_detail_job_and_never_emits_events(monkeypatch):
    calls = _patch_baseline(monkeypatch, _scan([_row("401")]), detail_results=[{"created": True}, {"created": False, "duplicate": True}])

    first = monitor.baseline_setup_area(TARGET_URL)
    second = monitor.baseline_setup_area(TARGET_URL)

    assert len(_calls(calls, "ingest")) == 2
    assert all(call[2]["emit_events"] is False for call in _calls(calls, "ingest"))
    assert len(_calls(calls, "detail_job")) == 2
    assert first["detail_job_enqueued"] is True
    assert second["detail_job_enqueued"] is False
    assert all(call[2]["setup_status"] != "ready" for call in _calls(calls, "state"))


def test_untrusted_reruns_preserve_listing_lifecycle(monkeypatch):
    calls = _patch_baseline(
        monkeypatch,
        _scan([_row("501")], scan_status="partial_blocked", trusted_scan=False, stop_reason="blocked_http_429", page_state="blocked_http_429", blocked_reason="blocked_http_429", retry_after_seconds=60),
    )

    for _ in range(2):
        with pytest.raises(RealEstateBlockedError):
            monitor.baseline_setup_area(TARGET_URL)

    assert _calls(calls, "ingest") == []
    assert _calls(calls, "baseline_completed") == []
    assert _calls(calls, "detail_job") == []


def test_scrape_search_with_result_keeps_legacy_list_contract(monkeypatch):
    rows = [_row("601")]
    monkeypatch.setattr(monitor.module1_list_scraper, "scrape_search", mock.Mock(return_value=rows))
    monitor.module1_list_scraper.scrape_search.last_result = {
        "scan_status": "ok",
        "trusted_scan": True,
        "stop_reason": "max_pages_reached",
        "pages_checked": 50,
        "total_pages_detected": 60,
        "current_url": TARGET_URL,
        "page_state": "listings",
    }

    result = monitor.module1_list_scraper.scrape_search_with_result(TARGET_URL, max_pages=50)

    assert result["rows"] == rows
    assert result["trusted_scan"] is True
    assert result["stop_reason"] == "max_pages_reached"
