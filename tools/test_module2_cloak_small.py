import argparse
import json
import os

import module1_list_scraper as module1
import module2_infer_prices as module2
from tools.cloak_smoke_common import add_checkpoint_args, add_profile_args, apply_profile_dir, resolve_profile_dir, smoke_checkpoint_path


DEFAULT_URL = "https://www.realestate.com.au/buy/in-petersham,+nsw+2049/list-1?activeSort=list-date"
INFERENCE_KEYS = [
    "price_inferred_display",
    "price_inferred_low",
    "price_inferred_high",
    "price_inferred_method",
    "price_inferred_window_low",
    "price_inferred_window_high",
    "price_inferred_found_at_page",
    "InferredPriceLow",
    "InferredPriceHigh",
    "InferredPriceRange",
    "PriceInferenceStatus",
    "PriceInferenceLastError",
    "PriceInferenceLastAttemptAt",
]


def _load_rows(path: str) -> list[dict]:
    with open(path, "r", encoding="utf-8-sig") as f:
        if path.lower().endswith(".json"):
            return json.load(f)
        import csv

        return list(csv.DictReader(f))


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a small CloakBrowser Module2 price inference smoke test.")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--input-file", default=None)
    parser.add_argument("--out-dir", default=os.path.join("output", "cloak_tests"))
    parser.add_argument("--window-width", type=int, default=200000)
    parser.add_argument("--step", type=int, default=50000)
    parser.add_argument("--max-high", type=int, default=1500000)
    parser.add_argument("--max-pages-per-window", type=int, default=1)
    parser.add_argument("--max-windows", type=int, default=3)
    add_profile_args(parser)
    add_checkpoint_args(parser)
    args = parser.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    effective_profile_dir = apply_profile_dir(resolve_profile_dir(args, args.out_dir, "module2_profile"), module2=True)
    checkpoint_mode = "resume" if args.resume else "fresh"
    checkpoint_path = None if args.resume else smoke_checkpoint_path(args.out_dir, "module2_price_checkpoint_smoke")
    input_file = args.input_file
    if not input_file:
        rows = module1.scrape_search(args.url, max_pages=1, timeout=25)
        _csv, input_file = module1.save_results(rows, out_dir=args.out_dir)

    logs = []
    csv_path, json_path = module2.module2_run(
        args.url,
        input_file=input_file,
        out_dir=args.out_dir,
        window_width=args.window_width,
        step=args.step,
        max_high=args.max_high,
        max_pages_per_window=args.max_pages_per_window,
        target_mode="all",
        sweep_mode="setup_full_sweep",
        test_max_windows=args.max_windows,
        checkpoint_path_override=checkpoint_path,
        resume_checkpoint=args.resume,
        on_log=logs.append,
    )
    rows_out = _load_rows(json_path) if json_path else []
    schema_keys = sorted({key for row in rows_out for key in row.keys()})
    inference_key_presence = {key: any(key in row for row in rows_out) for key in INFERENCE_KEYS}
    summary = {
        "input_file": input_file,
        "csv_path": csv_path,
        "json_path": json_path,
        "rows": len(rows_out),
        "schema_keys": schema_keys,
        "inference_key_presence": inference_key_presence,
        "last_result": getattr(module2.module2_run, "last_result", {}),
        "logs_tail": logs[-30:],
        "max_windows": args.max_windows,
        "effective_profile_dir": effective_profile_dir,
        "checkpoint_path": checkpoint_path or getattr(module2.module2_run, "last_result", {}).get("checkpoint_path"),
        "checkpoint_resumed": bool((getattr(module2.module2_run, "last_result", {}) or {}).get("checkpoint_resumed")),
        "checkpoint_mode": checkpoint_mode,
    }
    summary_path = os.path.join(args.out_dir, "module2_cloak_small_summary.json")
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if rows_out and json_path and csv_path else 2


if __name__ == "__main__":
    raise SystemExit(main())
