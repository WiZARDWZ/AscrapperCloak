from __future__ import annotations

import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import config
import db_layer


def main() -> int:
    missing = config.validate_runtime_config(require_db=True)
    if missing:
        print(f"Missing required DB configuration: {', '.join(missing)}", file=sys.stderr)
        return 2
    print("Connecting to SQL Server...")
    print(config.build_sqlserver_connection_string(include_password=False))
    conn = db_layer.connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1")
        row = cur.fetchone()
        value = row[0] if row else None
    finally:
        conn.close()
    if value != 1:
        print(f"Unexpected SELECT 1 result: {value!r}", file=sys.stderr)
        return 1
    print("SQL Server connection OK (SELECT 1).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
