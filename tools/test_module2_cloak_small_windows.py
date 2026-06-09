from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

import module2_infer_prices

DEFAULT_URL = "https://www.realestate.com.au/buy/in-noona,+nsw+2835/list-1?activeSort=list-date"


def _count_logs(logs: list[str], needle: str) -> int:
    needle_l = needle.lower()
    return sum(1 for item in logs if needle_l in str(item).lower())


def build_summary(*, csv_path=None, json_path=None, logs: list[str], crash: bool = False, error: str | None = None) -> dict:
    last_result = getattr(module2_infer_prices.module2_run, "last_result", {}) or {}
    status = "crash" if crash else (last_result.get("status") or "unknown")
    target_count = int(last_result.get("target_count") or 0)
    remaining_count = int(last_result.get("remaining_count") or 0)
    inferred_count = max(0, target_count - remaining_count) if target_count else 0
    return {
        "status": status,
        "csv_path": csv_path,
        "json_path": json_path,
        "last_result": last_result,
        "windows_attempted": _count_logs(logs, "Window "),
        "trusted_windows": _count_logs(logs, "Module2 trusted_window=True"),
        "chrome_error_logged": any("chrome-error" in item or "chrome_error" in item for item in logs),
        "crash": bool(crash),
        "error": error,
        "inferred_count": inferred_count,
        "session_failure_count": int(last_result.get("session_failure_windows") or 0),
        "retry_wait_logged": any("retry_wait" in item.lower() for item in logs),
        "rotation_logged": any("rotate" in item.lower() or "recovery" in item.lower() for item in logs),
        "delay_logged": any("inter-navigation delay" in item for item in logs),
        "recent_logs": logs[-80:],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Module2 CloakBrowser small-window pacing smoke test.")
    parser.add_argument("--base-url", default=os.getenv("TEST_REA_URL", DEFAULT_URL))
    parser.add_argument("--target-id", action="append", default=[])
    parser.add_argument("--windows", type=int, default=int(os.getenv("TEST_WINDOWS", "3")))
    parser.add_argument("--max-pages-per-window", type=int, default=int(os.getenv("TEST_MAX_PAGES_PER_WINDOW", "2")))
    parser.add_argument("--out-dir", default=os.getenv("TEST_OUT_DIR", "output/cloak_tests"))
    args = parser.parse_args()

    target_ids = set(args.target_id or os.getenv("TEST_TARGET_IDS", "").split(","))
    target_ids = {item.strip() for item in target_ids if item.strip()}
    if not target_ids:
        raise SystemExit("Provide --target-id or TEST_TARGET_IDS")
    rows = [{"listing_id": lid, "url": "", "price": "Contact agent"} for lid in sorted(target_ids)]
    fd, input_path = tempfile.mkstemp(prefix="module2_small_windows_", suffix=".json")
    os.close(fd)
    Path(input_path).write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    logs: list[str] = []
    csv_path = None
    json_path = None
    crash = False
    error = None
    try:
        try:
            csv_path, json_path = module2_infer_prices.module2_run(
                base_list_url=args.base_url,
                input_file=input_path,
                out_dir=args.out_dir,
                window_width=200_000,
                step=50_000,
                max_pages_per_window=args.max_pages_per_window,
                target_mode="all",
                target_listing_ids=target_ids,
                test_max_windows=args.windows,
                on_log=logs.append,
            )
        except Exception as exc:
            crash = True
            error = f"{type(exc).__name__}: {module2_infer_prices.config.mask_sensitive_text(exc)}"
    finally:
        try:
            os.remove(input_path)
        except OSError:
            pass
    summary = build_summary(csv_path=csv_path, json_path=json_path, logs=logs, crash=crash, error=error)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if crash:
        return 1
    return 0 if summary["status"] in {"done", "partial_test_limit", "skipped_no_range_after_full_sweep"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
