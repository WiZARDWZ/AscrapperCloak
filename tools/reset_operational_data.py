"""Safely clear operational AScrapper data while preserving reference tables.

The reset is intentionally conservative:
- dry-run by default for operator review
- destructive mode requires --confirm RESET_OPERATIONAL_DATA
- FK dependencies are discovered from SQL Server metadata and deleted child-first
- constraints are never disabled or dropped
"""
from __future__ import annotations

import argparse
from collections import defaultdict, deque
from dataclasses import dataclass
import os
import sys
from typing import Iterable, Mapping, Sequence

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import config
import db_layer

CONFIRM_TOKEN = "RESET_OPERATIONAL_DATA"

# Required production reference/locality tables. Optional legacy names may exist in
# developer databases, but NSWSuburbDirectory is the production NSW locality source.
REFERENCE_TABLES = {
    "dbo.State",
    "dbo.Suburb",
    "dbo.PropertyType",
    "dbo.NSWSuburbDirectory",
    "dbo.NSWSuburb",       # optional legacy
    "dbo.SuburbAlias",     # optional legacy
}

USER_PRESERVE_TABLES = {
    "dbo.TelegramUser",
    "dbo.User",
    "dbo.UserSetting",
    "dbo.UserAccessLog",
}

# Operational/derived-data reset domain. Missing tables are reported as optional.
OPERATIONAL_TABLES = {
    "dbo.NotificationOutbox",
    "dbo.NotificationLog",
    "dbo.EventNotification",
    "dbo.TelegramMessage",
    "dbo.ListingSnapshotAgent",
    "dbo.ListingAgentAssignment",
    "dbo.ListingEvent",
    "dbo.ListingPriceHistory",
    "dbo.ListingStatusHistory",
    "dbo.ListingMedia",
    "dbo.ListingSnapshot",
    "dbo.ListingSearchState",
    "dbo.listing_price_inference_state",
    "dbo.PropertyValuationEstimate",
    "dbo.ExportRun",
    "dbo.RunRequest",
    "dbo.ScrapeRun",
    "dbo.Job",
    "dbo.UserSuburbMonitor",
    "dbo.UserAreaSubscription",
    "dbo.TelegramUserSession",
    "dbo.user_area_subscription_state",
    "dbo.area_monitoring_state",
    "dbo.SuburbSearch",
    "dbo.Listing",
    "dbo.Property",
    "dbo.Agent",
    "dbo.Agency",
    # optional legacy operational tables
    "dbo.CrawlRun",
    "dbo.CrawlError",
    "dbo.NotificationLogs",
    "dbo.TelegramAccessRule",
    "dbo.TelegramRole",
    "dbo.ExportJob",
}

ACTIVE_JOB_STATUSES = ("queued", "running", "retry_wait", "pending", "paused", "scheduled")


@dataclass(frozen=True, order=True)
class TableRef:
    schema: str
    name: str

    @property
    def key(self) -> str:
        return f"{self.schema.lower()}.{self.name.lower()}"

    @property
    def display(self) -> str:
        return f"{self.schema}.{self.name}"

    @property
    def quoted(self) -> str:
        return f"{_quote_ident(self.schema)}.{_quote_ident(self.name)}"


def _quote_ident(value: str) -> str:
    return "[" + str(value).replace("]", "]]") + "]"


def _parse_table(name: str) -> TableRef:
    raw = name.strip()
    if "." in raw:
        schema, table = raw.split(".", 1)
    else:
        schema, table = "dbo", raw
    table = table.strip()
    if table.startswith("[") and table.endswith("]"):
        table = table[1:-1].replace("]]", "]")
    return TableRef(schema.strip("[]") or "dbo", table)


def _rows(cursor) -> list[tuple]:
    rows = cursor.fetchall()
    return [tuple(row) for row in rows]


def discover_tables(conn) -> dict[str, TableRef]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT s.name AS schema_name, t.name AS table_name
        FROM sys.tables AS t
        JOIN sys.schemas AS s ON s.schema_id = t.schema_id
        WHERE t.is_ms_shipped = 0
        """
    )
    return {TableRef(str(schema), str(table)).key: TableRef(str(schema), str(table)) for schema, table in _rows(cur)}


def discover_foreign_keys(conn, tables: Mapping[str, TableRef] | None = None) -> list[dict[str, TableRef | str]]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT fk.name,
               child_schema.name AS child_schema,
               child_table.name AS child_table,
               parent_schema.name AS parent_schema,
               parent_table.name AS parent_table
        FROM sys.foreign_keys AS fk
        JOIN sys.tables AS child_table ON child_table.object_id = fk.parent_object_id
        JOIN sys.schemas AS child_schema ON child_schema.schema_id = child_table.schema_id
        JOIN sys.tables AS parent_table ON parent_table.object_id = fk.referenced_object_id
        JOIN sys.schemas AS parent_schema ON parent_schema.schema_id = parent_table.schema_id
        WHERE child_table.is_ms_shipped = 0 AND parent_table.is_ms_shipped = 0
        """
    )
    edges = []
    table_map = tables or {}
    for fk, cs, ct, ps, pt in _rows(cur):
        child = table_map.get(TableRef(str(cs), str(ct)).key, TableRef(str(cs), str(ct)))
        parent = table_map.get(TableRef(str(ps), str(pt)).key, TableRef(str(ps), str(pt)))
        edges.append({"fk": str(fk), "child": child, "parent": parent})
    return edges


def _count(conn, table: TableRef) -> int:
    cur = conn.cursor()
    cur.execute(f"SELECT COUNT(1) FROM {table.quoted}")
    row = cur.fetchone()
    return int(row[0] or 0) if row else 0


def _has_identity(conn, table: TableRef) -> bool:
    cur = conn.cursor()
    cur.execute("SELECT OBJECTPROPERTY(OBJECT_ID(?), 'TableHasIdentity')", table.display)
    row = cur.fetchone()
    return bool(row and int(row[0] or 0) == 1)


def _active_jobs(conn, table_map: Mapping[str, TableRef]) -> int:
    job = table_map.get(_parse_table("dbo.Job").key)
    if not job:
        return 0
    placeholders = ",".join("?" for _ in ACTIVE_JOB_STATUSES)
    cur = conn.cursor()
    cur.execute(f"SELECT COUNT(1) FROM {job.quoted} WHERE Status IN ({placeholders})", *ACTIVE_JOB_STATUSES)
    row = cur.fetchone()
    return int(row[0] or 0) if row else 0


def _selected_table_refs(table_map: Mapping[str, TableRef], preserve_users: bool) -> tuple[set[TableRef], set[TableRef], set[TableRef], list[str]]:
    operational_keys = {_parse_table(name).key for name in OPERATIONAL_TABLES}
    if not preserve_users:
        operational_keys.update(_parse_table(name).key for name in USER_PRESERVE_TABLES)
    preserved_reference_keys = {_parse_table(name).key for name in REFERENCE_TABLES}
    preserved_user_keys = {_parse_table(name).key for name in USER_PRESERVE_TABLES} if preserve_users else set()

    operational = {table for key, table in table_map.items() if key in operational_keys}
    preserved_reference = {table for key, table in table_map.items() if key in preserved_reference_keys}
    preserved_user = {table for key, table in table_map.items() if key in preserved_user_keys}
    known_keys = operational_keys | preserved_reference_keys | preserved_user_keys
    missing_optional = sorted(name for name in sorted(OPERATIONAL_TABLES | REFERENCE_TABLES | USER_PRESERVE_TABLES) if _parse_table(name).key not in table_map and _parse_table(name).key in known_keys)
    return operational, preserved_reference, preserved_user, missing_optional


def _edges_touching(edges: Sequence[dict[str, TableRef | str]], tables: set[TableRef]) -> list[dict[str, TableRef | str]]:
    keys = {table.key for table in tables}
    return [edge for edge in edges if edge["child"].key in keys or edge["parent"].key in keys]  # type: ignore[index,union-attr]


def validate_dependency_closure(selected: set[TableRef], preserved: set[TableRef], edges: Sequence[dict[str, TableRef | str]]) -> None:
    selected_keys = {t.key for t in selected}
    preserved_keys = {t.key for t in preserved}
    parent_to_edges: dict[str, list[dict[str, TableRef | str]]] = defaultdict(list)
    for edge in edges:
        parent_to_edges[edge["parent"].key].append(edge)  # type: ignore[index,union-attr]

    omitted: list[dict[str, TableRef | str]] = []
    seen: set[tuple[str, str, str]] = set()
    queue = deque(selected)
    visited_parents: set[str] = set()
    while queue:
        parent = queue.popleft()
        if parent.key in visited_parents:
            continue
        visited_parents.add(parent.key)
        for edge in parent_to_edges.get(parent.key, []):
            child = edge["child"]  # type: ignore[assignment]
            marker = (str(edge["fk"]), child.key, parent.key)  # type: ignore[union-attr]
            if marker in seen:
                continue
            seen.add(marker)
            if child.key in selected_keys:
                queue.append(child)
            elif child.key not in preserved_keys:
                omitted.append(edge)
    if omitted:
        lines = ["Dependency validation failed:"]
        for edge in omitted:
            child = edge["child"]
            parent = edge["parent"]
            lines.append(f"{child.display} --[{edge['fk']}]--> {parent.display}")  # type: ignore[union-attr]
            lines.append(f"{child.display} is missing from the operational reset domain.")  # type: ignore[union-attr]
        raise RuntimeError("\n".join(lines))


def compute_delete_order(selected: set[TableRef], edges: Sequence[dict[str, TableRef | str]]) -> list[TableRef]:
    selected_keys = {t.key for t in selected}
    by_key = {t.key: t for t in selected}
    outgoing: dict[str, set[str]] = {t.key: set() for t in selected}
    indegree: dict[str, int] = {t.key: 0 for t in selected}
    for edge in edges:
        child = edge["child"]  # type: ignore[assignment]
        parent = edge["parent"]  # type: ignore[assignment]
        if child.key in selected_keys and parent.key in selected_keys and parent.key not in outgoing[child.key]:
            outgoing[child.key].add(parent.key)
            indegree[parent.key] += 1
    queue = deque(sorted((key for key, degree in indegree.items() if degree == 0), key=lambda key: by_key[key].display.lower()))
    order: list[TableRef] = []
    while queue:
        key = queue.popleft()
        order.append(by_key[key])
        for parent_key in sorted(outgoing[key], key=lambda k: by_key[k].display.lower()):
            indegree[parent_key] -= 1
            if indegree[parent_key] == 0:
                queue.append(parent_key)
    if len(order) != len(selected):
        cycle = [by_key[key].display for key, degree in indegree.items() if degree > 0]
        raise RuntimeError("Cycle detected in FK delete graph: " + ", ".join(sorted(cycle)))
    return order


def build_reset_plan(conn, *, preserve_users: bool) -> dict:
    table_map = discover_tables(conn)
    edges = discover_foreign_keys(conn, table_map)
    operational, preserved_reference, preserved_user, missing_optional = _selected_table_refs(table_map, preserve_users)
    preserved = preserved_reference | preserved_user
    validate_dependency_closure(operational, preserved, edges)
    delete_order = compute_delete_order(operational, edges)
    identity_tables = [table for table in delete_order if _has_identity(conn, table)]
    return {
        "tables": table_map,
        "edges": edges,
        "operational": operational,
        "preserved_reference": preserved_reference,
        "preserved_user": preserved_user,
        "missing_optional": missing_optional,
        "delete_order": delete_order,
        "identity_tables": identity_tables,
    }


def _counts(conn, tables: Iterable[TableRef]) -> dict[str, int]:
    return {table.display: _count(conn, table) for table in sorted(tables)}


def _print_plan(
    plan: dict,
    before: Mapping[str, int],
    expected_after: Mapping[str, int],
    active_jobs: int,
    *,
    preserve_users: bool,
    reference_counts: Mapping[str, int] | None = None,
    user_counts: Mapping[str, int] | None = None,
) -> None:
    print(f"active_job_count={active_jobs}")
    print("warning: --maintenance-override is service-independent and does not prove the ascrapper process is stopped.")
    print("dependency_closure_status=ok")
    print("operational_tables:")
    for name, count in sorted(before.items()):
        print(f"  {name}: {count}")
    print("missing_optional_operational_or_legacy_tables:")
    for name in plan["missing_optional"]:
        print(f"  {name}")
    reference_counts = reference_counts or {}
    user_counts = user_counts or {}
    print("preserved_reference_tables:")
    for table in sorted(plan["preserved_reference"]):
        print(f"  {table.display}: {reference_counts.get(table.display, 0)}")
    print("preserved_user_tables:" if preserve_users else "preserved_user_tables: none (--preserve-users not supplied)")
    for table in sorted(plan["preserved_user"]):
        print(f"  {table.display}: {user_counts.get(table.display, 0)}")
    print("fk_edges_involving_operational_or_preserved_tables:")
    touched = plan["operational"] | plan["preserved_reference"] | plan["preserved_user"]
    for edge in _edges_touching(plan["edges"], touched):
        print(f"  {edge['child'].display} --[{edge['fk']}]--> {edge['parent'].display}")  # type: ignore[union-attr]
    print("computed_child_before_parent_delete_order:")
    for table in plan["delete_order"]:
        print(f"  {table.display}")
    print("before_counts:")
    for name, count in sorted(before.items()):
        print(f"  {name}: {count}")
    print("expected_after_counts:")
    for name, count in sorted(expected_after.items()):
        print(f"  {name}: {count}")
    print("identity_tables_to_reseed:")
    for table in plan["identity_tables"]:
        print(f"  {table.display}")


def _delete_table(conn, table: TableRef) -> None:
    conn.cursor().execute(f"DELETE FROM {table.quoted}")


def _reseed(conn, table: TableRef) -> None:
    conn.cursor().execute(f"DBCC CHECKIDENT ('{table.display.replace("'", "''")}', RESEED, 0) WITH NO_INFOMSGS")


def reset_operational_data(*, dry_run: bool, confirm: str | None, preserve_users: bool, maintenance_override: bool = False, show_dependencies: bool = False) -> dict:
    conn = db_layer.connect(config.DB_PATH)
    try:
        plan = build_reset_plan(conn, preserve_users=preserve_users)
        active_jobs = _active_jobs(conn, plan["tables"])
        before = _counts(conn, plan["operational"])
        expected_after = {name: 0 for name in before}
        reference_before = _counts(conn, plan["preserved_reference"])
        user_before = _counts(conn, plan["preserved_user"])
        if show_dependencies:
            _print_plan(plan, before, expected_after, active_jobs, preserve_users=preserve_users, reference_counts=reference_before, user_counts=user_before)
            return {"show_dependencies": True, "active_jobs": active_jobs, "delete_order": [t.display for t in plan["delete_order"]]}
        if active_jobs and not maintenance_override:
            raise SystemExit(f"Refusing reset while {active_jobs} active jobs exist. Stop service/workers or pass --maintenance-override.")
        _print_plan(plan, before, expected_after if dry_run else expected_after, active_jobs, preserve_users=preserve_users, reference_counts=reference_before, user_counts=user_before)
        if dry_run:
            return {"dry_run": True, "before": before, "after": before, "active_jobs": active_jobs, "delete_order": [t.display for t in plan["delete_order"]]}
        if confirm != CONFIRM_TOKEN:
            raise SystemExit(f"Refusing destructive reset without --confirm {CONFIRM_TOKEN}")
        try:
            for table in plan["delete_order"]:
                _delete_table(conn, table)
            after = _counts(conn, plan["operational"])
            nonzero = {name: count for name, count in after.items() if count != 0}
            if nonzero:
                raise RuntimeError(f"Operational tables not empty after reset: {nonzero}")
            reference_after = _counts(conn, plan["preserved_reference"])
            if reference_after != reference_before:
                raise RuntimeError(f"Reference counts changed: before={reference_before} after={reference_after}")
            if preserve_users:
                user_after = _counts(conn, plan["preserved_user"])
                if user_after != user_before:
                    raise RuntimeError(f"Preserved user counts changed: before={user_before} after={user_after}")
            for table in plan["identity_tables"]:
                if int(before.get(table.display, 0) or 0) > 0:
                    _reseed(conn, table)
            conn.commit()
            return {"dry_run": False, "before": before, "after": after, "active_jobs": _active_jobs(conn, plan["tables"]), "delete_order": [t.display for t in plan["delete_order"]]}
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reset operational AScrapper data safely.")
    parser.add_argument("--dry-run", action="store_true", help="Print counts without deleting anything.")
    parser.add_argument("--confirm", default=None, help=f"Required token for destructive mode: {CONFIRM_TOKEN}")
    parser.add_argument("--preserve-users", action="store_true", help="Keep Telegram users/access rows.")
    parser.add_argument("--maintenance-override", action="store_true", help="Allow reset despite active jobs after operator stopped service externally.")
    parser.add_argument("--show-dependencies", action="store_true", help="Print FK dependency plan and exit without deleting anything.")
    args = parser.parse_args(list(argv) if argv is not None else None)
    result = reset_operational_data(
        dry_run=args.dry_run,
        confirm=args.confirm,
        preserve_users=args.preserve_users,
        maintenance_override=args.maintenance_override,
        show_dependencies=args.show_dependencies,
    )
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
