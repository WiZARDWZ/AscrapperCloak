"""Report operational state invariant violations for AScrapper/OzHome Monitor."""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import config
import db_layer
from cloak_browser_helper import profile_lock_status


def check_invariants(search_id: int | None = None) -> dict:
    conn = db_layer.connect(config.DB_PATH)
    violations = []
    try:
        cur = conn.cursor()
        sid_filter = " AND ams.area_id=?" if search_id is not None else ""
        params = [int(search_id)] if search_id is not None else []
        cur.execute(f"""
            SELECT ams.area_id, ams.setup_status, ams.module1_status, ams.module3_status, ams.module2_status
            FROM dbo.area_monitoring_state ams
            WHERE 1=1 {sid_filter}
        """, *params)
        for row in cur.fetchall():
            area_id, setup, m1, m3, m2 = row
            statuses = {"module1": str(m1 or '').lower(), "module3": str(m3 or '').lower(), "module2": str(m2 or '').lower()}
            if str(setup or '').lower() == 'inactive' and any(v == 'running' for v in statuses.values()):
                violations.append({"type": "inactive_area_with_running_module", "search_id": int(area_id), "modules": statuses})
            if str(setup or '').lower() == 'ready' and not (statuses["module1"] == 'completed' and statuses["module3"] in {'completed','skipped'} and statuses["module2"] in {'completed','completed_with_unknowns','skipped'}):
                violations.append({"type": "ready_area_with_incomplete_module", "search_id": int(area_id), "modules": statuses})
        cur.execute("""
            SELECT SearchID, COUNT(1)
            FROM dbo.Job
            WHERE Status IN ('queued','running','retry_wait','pending','paused','scheduled')
              AND JobType IN ('baseline_setup_area','setup_detail_baseline','setup_price_baseline')
            GROUP BY SearchID
            HAVING COUNT(1) > 1
        """)
        for row in cur.fetchall():
            violations.append({"type": "multiple_active_setup_jobs", "search_id": int(row[0]), "count": int(row[1])})
    finally:
        conn.close()
    lock = profile_lock_status(config.get_effective_browser_profile_dir('module1'))
    if lock.get('locked') and not lock.get('owner_alive'):
        violations.append({"type": "stale_cloak_profile_owner", "lock": lock})
    return {"violations": violations, "violation_count": len(violations), "profile_lock": lock}


def main() -> int:
    parser = argparse.ArgumentParser(description="Check AScrapper state invariants.")
    parser.add_argument("--search-id", type=int, default=None)
    args = parser.parse_args()
    print(check_invariants(args.search_id))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
