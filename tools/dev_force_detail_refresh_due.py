import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import config
import db_layer


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--search-id", type=int, required=True)
    parser.add_argument("--hours", type=int, default=3)
    parser.add_argument("--null", action="store_true", help="Set per-listing LastDetailRefreshAt to NULL instead of an old timestamp.")
    args = parser.parse_args()
    conn = None
    try:
        conn = db_layer.connect(config.DB_PATH)
        result = db_layer.force_listing_search_state_detail_refresh_due(conn, args.search_id, hours=args.hours, set_null=args.null)
    except Exception as exc:
        result = {"status": "error", "error": config.mask_sensitive_text(exc)}
    finally:
        if conn is not None:
            conn.close()
    print(json.dumps(result, ensure_ascii=False, default=str, indent=2))


if __name__ == "__main__":
    main()
