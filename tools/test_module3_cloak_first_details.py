from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

import module3_enrich_details

DEFAULT_AREA_URL = "https://www.realestate.com.au/buy/in-noona,+nsw+2835/list-1?activeSort=list-date"


def _default_rows() -> list[dict]:
    raw = os.getenv("TEST_DETAIL_URLS", "").strip()
    if not raw:
        return []
    return [
        {"listing_id": str(i + 1), "url": url.strip(), "address": f"Test detail {i + 1}"}
        for i, url in enumerate(raw.split(","))
        if url.strip()
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Module3 CloakBrowser first-detail pacing smoke test.")
    parser.add_argument("--area-url", default=os.getenv("TEST_AREA_URL", DEFAULT_AREA_URL))
    parser.add_argument("--input-json", default=os.getenv("TEST_INPUT_JSON", ""))
    parser.add_argument("--limit", type=int, default=int(os.getenv("TEST_LIMIT", "3")))
    parser.add_argument("--out-dir", default=os.getenv("TEST_OUT_DIR", "output/cloak_tests"))
    parser.add_argument("--timeout", type=int, default=int(os.getenv("TEST_TIMEOUT", "60")))
    args = parser.parse_args()

    input_json = args.input_json
    temp_path = None
    if not input_json:
        rows = _default_rows()
        if not rows:
            raise SystemExit("Provide --input-json or TEST_DETAIL_URLS=url1,url2,url3")
        rows = rows[: args.limit]
        fd, temp_path = tempfile.mkstemp(prefix="module3_first_details_", suffix=".json")
        os.close(fd)
        Path(temp_path).write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        input_json = temp_path

    logs: list[str] = []
    try:
        csv_path, json_path = module3_enrich_details.module3_run(
            area_search_url=args.area_url,
            input_file=input_json,
            out_dir=args.out_dir,
            only_if_missing=False,
            wait_timeout=args.timeout,
            sleep_between=0,
            on_log=logs.append,
        )
    finally:
        if temp_path:
            try:
                os.remove(temp_path)
            except OSError:
                pass

    summary = {
        "csv_path": csv_path,
        "json_path": json_path,
        "last_result": getattr(module3_enrich_details.module3_run, "last_result", {}),
        "detail_delay_logged": any("inter-navigation delay" in item for item in logs),
        "chrome_error_logged": any("chrome-error" in item for item in logs),
        "recent_logs": logs[-80:],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if not json_path:
        return 1
    rows = json.loads(Path(json_path).read_text(encoding="utf-8"))
    fake_success = any(str(row.get("url", "")).startswith("chrome-error://chromewebdata/") for row in rows)
    return 2 if fake_success else 0


if __name__ == "__main__":
    raise SystemExit(main())
