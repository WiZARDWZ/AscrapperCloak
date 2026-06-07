import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import config

import job_queue


def main() -> None:
    try:
        active = job_queue.get_active_jobs()
        result = {
            "summary": job_queue.get_queue_summary(),
            "running_jobs": [row for row in active if row.get("Status") == "running"],
            "retry_wait_jobs": [row for row in active if row.get("Status") == "retry_wait"],
            "next_due_jobs": job_queue.get_next_due_jobs(limit=20),
        }
    except Exception as exc:
        result = {"status": "error", "error": config.mask_sensitive_text(exc)}
    print(json.dumps(result, ensure_ascii=False, default=str, indent=2))


if __name__ == "__main__":
    main()
