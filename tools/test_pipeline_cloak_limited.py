import argparse
import json
import os
from datetime import datetime

import module1_list_scraper as module1
import module2_infer_prices as module2
import module3_enrich_details as module3
from tools.cloak_smoke_common import add_checkpoint_args, add_profile_args, apply_profile_dir, resolve_profile_dir, smoke_checkpoint_path


DEFAULT_URL = "https://www.realestate.com.au/buy/in-petersham,+nsw+2049/list-1?activeSort=list-date"


def _load_json(path: str) -> list[dict]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a limited CloakBrowser Module1 -> Module2 -> Module3 smoke pipeline.")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--out-dir", default=os.path.join("output", "cloak_tests"))
    parser.add_argument("--module3-limit", type=int, default=3)
    parser.add_argument("--module2-max-high", type=int, default=1500000)
    parser.add_argument("--module2-max-windows", type=int, default=3)
    add_profile_args(parser)
    add_checkpoint_args(parser)
    args = parser.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    effective_profile_dir = apply_profile_dir(resolve_profile_dir(args, args.out_dir, "pipeline_profile"), module2=True)
    checkpoint_mode = "resume" if args.resume else "fresh"
    checkpoint_path = None if args.resume else smoke_checkpoint_path(args.out_dir, "pipeline_module2_checkpoint_smoke")
    summary: dict = {"url": args.url, "out_dir": args.out_dir, "db_ingestion": "skipped_by_default", "effective_profile_dir": effective_profile_dir, "checkpoint_mode": checkpoint_mode, "checkpoint_path": checkpoint_path}

    module1_rows = module1.scrape_search(args.url, max_pages=1, timeout=25)
    m1_csv, m1_json = module1.save_results(module1_rows, out_dir=args.out_dir)
    summary["module1"] = {"rows": len(module1_rows), "csv": m1_csv, "json": m1_json}

    m2_csv, m2_json = module2.module2_run(
        args.url,
        input_file=m1_json,
        out_dir=args.out_dir,
        max_high=args.module2_max_high,
        max_pages_per_window=1,
        target_mode="all",
        sweep_mode="setup_full_sweep",
        test_max_windows=args.module2_max_windows,
        checkpoint_path_override=checkpoint_path,
        resume_checkpoint=args.resume,
    )
    module2_rows = _load_json(m2_json) if m2_json else []
    summary["module2"] = {
        "rows": len(module2_rows),
        "csv": m2_csv,
        "json": m2_json,
        "last_result": getattr(module2.module2_run, "last_result", {}),
        "module2_max_windows_requested": args.module2_max_windows,
        "module2_windows_checked": (getattr(module2.module2_run, "last_result", {}) or {}).get("windows_checked"),
        "module2_stopped_reason": (getattr(module2.module2_run, "last_result", {}) or {}).get("stopped_reason"),
    }
    summary["module2_max_windows_requested"] = args.module2_max_windows
    summary["module2_windows_checked"] = summary["module2"]["module2_windows_checked"]
    summary["module2_stopped_reason"] = summary["module2"]["module2_stopped_reason"]

    limited_rows = [row for row in module2_rows if str(row.get("url") or "").strip().upper() != "N/A"][: max(1, args.module3_limit)]
    m3_input = os.path.join(args.out_dir, f"pipeline_module3_input_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    with open(m3_input, "w", encoding="utf-8") as f:
        json.dump(limited_rows, f, ensure_ascii=False, indent=2)

    m3_csv, m3_json = module3.module3_run(
        area_search_url=args.url,
        input_file=m3_input,
        out_dir=args.out_dir,
        only_if_missing=False,
        wait_timeout=25,
        sleep_between=0,
        empty_retry=1,
    )
    module3_rows = _load_json(m3_json) if m3_json else []
    summary["module3"] = {"rows": len(module3_rows), "csv": m3_csv, "json": m3_json, "input": m3_input}
    summary["excel_export"] = "not_run_without_test_db"

    summary_path = os.path.join(args.out_dir, "pipeline_cloak_limited_summary.json")
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if module1_rows and module2_rows and module3_rows else 2


if __name__ == "__main__":
    raise SystemExit(main())
