import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import monitoring_scheduler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sleep-seconds", type=int, default=None)
    parser.add_argument("--send-telegram", action="store_true")
    args = parser.parse_args()
    monitoring_scheduler.run_monitoring_loop(sleep_seconds=args.sleep_seconds, send_telegram=args.send_telegram)


if __name__ == "__main__":
    main()
