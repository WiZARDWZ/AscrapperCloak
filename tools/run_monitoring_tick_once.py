import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import monitoring_scheduler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--send-telegram", action="store_true")
    parser.add_argument("--notification-limit", type=int, default=100)
    parser.add_argument("--send-limit", type=int, default=None)
    args = parser.parse_args()
    result = monitoring_scheduler.run_monitoring_tick(dry_run=args.dry_run, send_telegram=args.send_telegram, notification_limit=args.notification_limit, send_limit=args.send_limit)
    print(json.dumps(result, ensure_ascii=False, default=str, indent=2))


if __name__ == "__main__":
    main()
