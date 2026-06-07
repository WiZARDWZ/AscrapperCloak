import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import config
import monitoring_scheduler


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--search-id", type=int, required=True)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--send-telegram", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        result = monitoring_scheduler.run_detail_refresh_existing_for_search(
            search_id=args.search_id,
            limit=args.limit,
            dry_run=args.dry_run,
            send_telegram=args.send_telegram,
        )
    except Exception as exc:
        result = {"status": "error", "error": config.mask_sensitive_text(exc)}
    print(json.dumps(result, ensure_ascii=False, default=str, indent=2))


if __name__ == "__main__":
    main()
