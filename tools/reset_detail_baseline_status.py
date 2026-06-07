import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import config
import db_layer


def main() -> None:
    parser = argparse.ArgumentParser(description="Reset a Telegram subscription detail-baseline state.")
    parser.add_argument("--user-area-id", type=int, required=True)
    parser.add_argument("--status", choices=["pending", "running", "retry_wait", "failed", "completed"], default="pending")
    parser.add_argument("--clear-attempts", action="store_true")
    args = parser.parse_args()
    conn = db_layer.connect(config.DB_PATH)
    try:
        db_layer.ensure_telegram_bot_tables(conn)
        db_layer.reset_detail_baseline_status(conn, args.user_area_id, args.status, args.clear_attempts)
    finally:
        conn.close()
    print(f"detail baseline reset: user_area_id={args.user_area_id} status={args.status} clear_attempts={args.clear_attempts}")


if __name__ == "__main__":
    main()
