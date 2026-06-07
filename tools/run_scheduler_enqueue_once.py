import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import config

import monitoring_scheduler


def main() -> None:
    try:
        result = monitoring_scheduler.enqueue_due_monitoring_jobs()
    except Exception as exc:
        result = {"status": "error", "error": config.mask_sensitive_text(exc)}
    print(json.dumps(result, ensure_ascii=False, default=str, indent=2))


if __name__ == "__main__":
    main()
