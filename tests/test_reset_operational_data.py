import inspect

import pytest

import tools.reset_operational_data as reset_tool


def T(name):
    return reset_tool._parse_table(name)


def production_tables():
    names = set(reset_tool.OPERATIONAL_TABLES) | set(reset_tool.REFERENCE_TABLES) | set(reset_tool.USER_PRESERVE_TABLES)
    return {T(name).key: T(name) for name in names}


def edge(fk, child, parent):
    return {"fk": fk, "child": T(child), "parent": T(parent)}


def production_edges():
    return [
        edge("FK_ScrapeRun_SuburbSearch", "dbo.ScrapeRun", "dbo.SuburbSearch"),
        edge("FK_ExportRun_ScrapeRun", "dbo.ExportRun", "dbo.ScrapeRun"),
        edge("FK_RunRequest_ScrapeRun", "dbo.RunRequest", "dbo.ScrapeRun"),
        edge("FK_ListingSnapshot_ScrapeRun", "dbo.ListingSnapshot", "dbo.ScrapeRun"),
        edge("FK_ListingEvent_ScrapeRun", "dbo.ListingEvent", "dbo.ScrapeRun"),
        edge("FK_ListingSnapshotAgent_ListingSnapshot", "dbo.ListingSnapshotAgent", "dbo.ListingSnapshot"),
        edge("FK_EventNotification_ListingEvent", "dbo.EventNotification", "dbo.ListingEvent"),
        edge("FK_TelegramMessage_Listing", "dbo.TelegramMessage", "dbo.Listing"),
        edge("FK_TelegramMessage_SuburbSearch", "dbo.TelegramMessage", "dbo.SuburbSearch"),
        edge("FK_Job_Listing", "dbo.Job", "dbo.Listing"),
        edge("FK_Job_SuburbSearch", "dbo.Job", "dbo.SuburbSearch"),
        edge("FK_PropertyValuationEstimate_Property", "dbo.PropertyValuationEstimate", "dbo.Property"),
        edge("FK_Listing_Property", "dbo.Listing", "dbo.Property"),
        edge("FK_Agent_Agency", "dbo.Agent", "dbo.Agency"),
        edge("FK_Listing_Agent", "dbo.Listing", "dbo.Agent"),
        edge("FK_Listing_Agency", "dbo.Listing", "dbo.Agency"),
        edge("FK_Property_Suburb", "dbo.Property", "dbo.Suburb"),
        edge("FK_Property_PropertyType", "dbo.Property", "dbo.PropertyType"),
        edge("FK_SuburbSearch_Suburb", "dbo.SuburbSearch", "dbo.Suburb"),
        edge("FK_UserSetting_User", "dbo.UserSetting", "dbo.User"),
        edge("FK_UserAccessLog_User", "dbo.UserAccessLog", "dbo.User"),
        edge("FK_TelegramUserSession_TelegramUser", "dbo.TelegramUserSession", "dbo.TelegramUser"),
    ]


def test_production_fk_graph_orders_children_before_parents_and_preserves_references():
    selected, refs, users, missing = reset_tool._selected_table_refs(production_tables(), preserve_users=True)
    edges = production_edges()
    reset_tool.validate_dependency_closure(selected, refs | users, edges)
    order = reset_tool.compute_delete_order(selected, edges)
    pos = {table.display: idx for idx, table in enumerate(order)}

    assert T("dbo.NSWSuburbDirectory") in refs
    assert T("dbo.TelegramUser") in users
    assert T("dbo.User") in users
    assert T("dbo.UserSetting") in users
    assert T("dbo.UserAccessLog") in users
    assert T("dbo.TelegramUserSession") in selected
    assert T("dbo.ScrapeRun") in selected
    assert T("dbo.ExportRun") in selected
    assert T("dbo.RunRequest") in selected
    assert T("dbo.ListingSnapshotAgent") in selected
    assert T("dbo.EventNotification") in selected
    assert T("dbo.PropertyValuationEstimate") in selected

    for e in edges:
        child = e["child"].display
        parent = e["parent"].display
        if child in pos and parent in pos:
            assert pos[child] < pos[parent], f"{child} must be deleted before {parent}"


def test_dependency_closure_fails_for_omitted_dependent_table():
    selected, refs, users, _ = reset_tool._selected_table_refs(production_tables(), preserve_users=True)
    selected.remove(T("dbo.ScrapeRun"))
    with pytest.raises(RuntimeError) as exc:
        reset_tool.validate_dependency_closure(selected, refs | users, production_edges())
    message = str(exc.value)
    assert "dbo.ScrapeRun --[FK_ScrapeRun_SuburbSearch]--> dbo.SuburbSearch" in message
    assert "dbo.ScrapeRun is missing from the operational reset domain" in message


def test_identifier_quoting_handles_dbo_user_safely():
    user = T("dbo.[User]")
    assert user.display == "dbo.User"
    assert user.quoted == "[dbo].[User]"


def test_reset_rolls_back_after_middle_delete_failure_and_preserves_counts(monkeypatch):
    class Conn:
        def __init__(self):
            self.committed = False
            self.rolled_back = False
            self.closed = False
        def cursor(self):
            raise AssertionError("metadata helpers are monkeypatched")
        def commit(self):
            self.committed = True
        def rollback(self):
            self.rolled_back = True
        def close(self):
            self.closed = True

    conn = Conn()
    selected = {T("dbo.EventNotification"), T("dbo.ListingEvent"), T("dbo.ScrapeRun"), T("dbo.SuburbSearch"), T("dbo.TelegramUserSession")}
    refs = {T("dbo.State"), T("dbo.Suburb"), T("dbo.PropertyType"), T("dbo.NSWSuburbDirectory")}
    users = {T("dbo.TelegramUser"), T("dbo.User"), T("dbo.UserSetting"), T("dbo.UserAccessLog")}
    delete_order = [T("dbo.EventNotification"), T("dbo.ListingEvent"), T("dbo.ScrapeRun"), T("dbo.SuburbSearch"), T("dbo.TelegramUserSession")]
    plan = {
        "tables": {t.key: t for t in selected | refs | users},
        "edges": production_edges(),
        "operational": selected,
        "preserved_reference": refs,
        "preserved_user": users,
        "missing_optional": [],
        "delete_order": delete_order,
        "identity_tables": [T("dbo.SuburbSearch")],
    }
    counts = {table.display: 1 for table in selected | refs | users}
    deleted = []

    monkeypatch.setattr(reset_tool.db_layer, "connect", lambda path: conn)
    monkeypatch.setattr(reset_tool, "build_reset_plan", lambda c, preserve_users: plan)
    monkeypatch.setattr(reset_tool, "_active_jobs", lambda c, tables: 0)
    monkeypatch.setattr(reset_tool, "_counts", lambda c, tables: {t.display: counts[t.display] for t in tables})
    def fail_on_scrape_run(c, table):
        deleted.append(table.display)
        if table.display == "dbo.ScrapeRun":
            raise RuntimeError("simulated delete failure")
    monkeypatch.setattr(reset_tool, "_delete_table", fail_on_scrape_run)

    with pytest.raises(RuntimeError, match="simulated delete failure"):
        reset_tool.reset_operational_data(dry_run=False, confirm=reset_tool.CONFIRM_TOKEN, preserve_users=True, maintenance_override=True)
    assert deleted == ["dbo.EventNotification", "dbo.ListingEvent", "dbo.ScrapeRun"]
    assert conn.rolled_back is True
    assert conn.committed is False
    assert conn.closed is True


def test_repeated_reset_is_idempotent_and_verifies_reference_counts(monkeypatch):
    class Conn:
        def __init__(self):
            self.commits = 0
            self.rollbacks = 0
            self.closed = False
        def cursor(self):
            raise AssertionError("metadata helpers are monkeypatched")
        def commit(self):
            self.commits += 1
        def rollback(self):
            self.rollbacks += 1
        def close(self):
            self.closed = True

    selected = {T("dbo.TelegramUserSession"), T("dbo.SuburbSearch")}
    refs = {T("dbo.State"), T("dbo.Suburb"), T("dbo.PropertyType"), T("dbo.NSWSuburbDirectory")}
    users = {T("dbo.TelegramUser"), T("dbo.User"), T("dbo.UserSetting"), T("dbo.UserAccessLog")}
    plan = {
        "tables": {t.key: t for t in selected | refs | users},
        "edges": [],
        "operational": selected,
        "preserved_reference": refs,
        "preserved_user": users,
        "missing_optional": [],
        "delete_order": [T("dbo.TelegramUserSession"), T("dbo.SuburbSearch")],
        "identity_tables": [T("dbo.SuburbSearch")],
    }
    counts = {"dbo.TelegramUserSession": 1, "dbo.SuburbSearch": 2, "dbo.State": 1, "dbo.Suburb": 2, "dbo.PropertyType": 14, "dbo.NSWSuburbDirectory": 4621, "dbo.TelegramUser": 1, "dbo.User": 1, "dbo.UserSetting": 1, "dbo.UserAccessLog": 1}
    reseeded = []

    monkeypatch.setattr(reset_tool, "build_reset_plan", lambda c, preserve_users: plan)
    monkeypatch.setattr(reset_tool, "_active_jobs", lambda c, tables: 0)
    monkeypatch.setattr(reset_tool, "_counts", lambda c, tables: {t.display: counts[t.display] for t in tables})
    def delete_table(c, table):
        counts[table.display] = 0
    monkeypatch.setattr(reset_tool, "_delete_table", delete_table)
    monkeypatch.setattr(reset_tool, "_reseed", lambda c, table: reseeded.append(table.display))

    for _ in range(2):
        conn = Conn()
        monkeypatch.setattr(reset_tool.db_layer, "connect", lambda path, conn=conn: conn)
        out = reset_tool.reset_operational_data(dry_run=False, confirm=reset_tool.CONFIRM_TOKEN, preserve_users=True, maintenance_override=True)
        assert out["after"] == {"dbo.SuburbSearch": 0, "dbo.TelegramUserSession": 0}
        assert conn.commits == 1
        assert conn.rollbacks == 0
    assert reseeded == ["dbo.SuburbSearch"]


def test_show_dependencies_and_source_never_disable_constraints(monkeypatch, capsys):
    class Conn:
        def close(self):
            pass
    selected, refs, users, _ = reset_tool._selected_table_refs(production_tables(), preserve_users=True)
    plan = {
        "tables": production_tables(),
        "edges": production_edges(),
        "operational": selected,
        "preserved_reference": refs,
        "preserved_user": users,
        "missing_optional": [],
        "delete_order": reset_tool.compute_delete_order(selected, production_edges()),
        "identity_tables": [T("dbo.SuburbSearch")],
    }
    monkeypatch.setattr(reset_tool.db_layer, "connect", lambda path: Conn())
    monkeypatch.setattr(reset_tool, "build_reset_plan", lambda c, preserve_users: plan)
    monkeypatch.setattr(reset_tool, "_active_jobs", lambda c, tables: 14)
    monkeypatch.setattr(reset_tool, "_counts", lambda c, tables: {t.display: 0 for t in tables})

    out = reset_tool.reset_operational_data(dry_run=True, confirm=None, preserve_users=True, maintenance_override=True, show_dependencies=True)
    captured = capsys.readouterr().out
    assert out["show_dependencies"] is True
    assert "FK_ScrapeRun_SuburbSearch" in captured
    assert "computed_child_before_parent_delete_order" in captured
    source = inspect.getsource(reset_tool).lower()
    assert "nocheck" not in source
    assert "disable constraint" not in source
    assert "drop constraint" not in source
