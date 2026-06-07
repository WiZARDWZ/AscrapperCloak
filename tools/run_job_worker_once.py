import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import config

import job_queue
import monitoring_scheduler


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker-id", default=None)
    parser.add_argument("--send-telegram", action="store_true")
    args = parser.parse_args()
    try:
        result = monitoring_scheduler.run_next_job_once(worker_id=args.worker_id, send_telegram=args.send_telegram)
        result["queue_summary"] = job_queue.get_queue_summary()
    except Exception as exc:
        result = {"status": "error", "error": config.mask_sensitive_text(exc)}
    print(json.dumps(result, ensure_ascii=False, default=str, indent=2))


if __name__ == "__main__":
    main()
