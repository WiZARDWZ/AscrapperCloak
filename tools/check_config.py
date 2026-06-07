from __future__ import annotations

import json
import sys

import config


def main() -> int:
    summary = config.safe_runtime_summary()
    missing = config.validate_runtime_config(require_token=True, require_db=True)
    payload = {
        "ok": not missing,
        "missing": missing,
        "production": config.is_production(),
        "config": summary,
        "odbc_connection_string": config.build_sqlserver_connection_string(include_password=False),
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
