import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import telegram_sender


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    print(telegram_sender.send_queued_notifications_once(limit=args.limit, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
