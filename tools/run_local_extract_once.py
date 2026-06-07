import argparse
import json

import config
import module1_list_scraper
import module2_infer_prices
import module3_enrich_details
from db_layer import connect, ensure_sort_list_date, export_latest_to_rows, get_or_create_area, ingest_full_rows
from db_layer import parse_price_range
from monitor import export_area_excel


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--max-pages", type=int, default=1)
    ap.add_argument("--skip-module2", action="store_true")
    ap.add_argument("--light", action="store_true")
    args = ap.parse_args()
    print("Local bandwidth config:")
    print(f"LOW_BANDWIDTH_MODE={config.LOW_BANDWIDTH_MODE}")
    print(f"BLOCK_HEAVY_RESOURCES={config.BLOCK_HEAVY_RESOURCES}")
    print(f"ULTRA_LOW_BANDWIDTH={config.ULTRA_LOW_BANDWIDTH}")
    print(f"BLOCK_IMAGES={config.BLOCK_IMAGES}")
    print(f"BLOCK_MEDIA={config.BLOCK_MEDIA}")
    print(f"BLOCK_FONTS={config.BLOCK_FONTS}")
    print(f"BLOCK_MAPS={config.BLOCK_MAPS}")
    print(f"BLOCK_TRACKERS={config.BLOCK_TRACKERS}")
    print(f"BLOCK_CSS={config.BLOCK_CSS}")
    print(f"BLOCK_JS={config.BLOCK_JS}")

    url = ensure_sort_list_date(args.url)
    max_pages = 1 if args.light else max(1, args.max_pages)
    rows1 = module1_list_scraper.scrape_search(url, max_pages=max_pages, timeout=config.PIPELINE_TIMEOUT)
    _, json1 = module1_list_scraper.save_results(rows1, out_dir=config.OUTPUT_DIR)
    _, json3 = module3_enrich_details.module3_run(url, json1, config.OUTPUT_DIR, only_if_missing=True, wait_timeout=config.MODULE3_WAIT_TIMEOUT, sleep_between=config.MODULE3_SLEEP_BETWEEN, empty_retry=config.MODULE3_EMPTY_RETRY)
    with open(json3, "r", encoding="utf-8") as f:
        rows3 = json.load(f)

    rows_need_module2 = []
    for row in rows3:
        detail_price_display = str(row.get("detail_price_display") or "").strip()
        list_price = str(row.get("price") or "").strip()
        detail_low, detail_high = parse_price_range(detail_price_display)
        list_low, list_high = parse_price_range(list_price)
        if (detail_low is None and detail_high is None) and (list_low is None and list_high is None) and module2_infer_prices.price_needs_inference(list_price):
            rows_need_module2.append(row)

    rows_final = rows3
    if rows_need_module2 and not args.skip_module2:
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump(rows_need_module2, f, ensure_ascii=False)
            json_for_module2 = f.name
        m2_kwargs = {
            "window_width": config.MODULE2_WINDOW_WIDTH,
            "step": config.MODULE2_STEP,
            "max_high": config.MODULE2_MAX_HIGH,
            "max_pages_per_window": config.MODULE2_MAX_PAGES_PER_WINDOW,
        }
        if args.light:
            m2_kwargs.update({"window_width": 500_000, "step": 500_000, "max_high": 2_500_000, "max_pages_per_window": 1})
        _, json2 = module2_infer_prices.module2_run(url, json_for_module2, config.OUTPUT_DIR, only_overwrite_na=True, smart_start=True, **m2_kwargs)
        if json2:
            with open(json2, "r", encoding="utf-8") as f:
                rows2 = json.load(f)
            mapped = {str(r.get("listing_id") or ""): r for r in rows2}
            rows_final = [mapped.get(str(r.get("listing_id") or ""), r) for r in rows3]
    elif args.skip_module2:
        print("--skip-module2 enabled: Module2 skipped.")


    run_id = ingest_full_rows(config.DB_PATH, url, rows_final, full_scan=True)

    conn = connect(config.DB_PATH)
    search_id = get_or_create_area(conn, url)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM dbo.ListingSnapshot WHERE SearchID=? AND RunID=?", search_id, run_id)
    snapshot_count = int(c.fetchone()[0])
    c.execute("SELECT COUNT(*) FROM dbo.ListingEvent WHERE SearchID=? AND RunID=?", search_id, run_id)
    event_count = int(c.fetchone()[0])
    conn.close()

    excel_path = export_area_excel(config.DB_PATH, url, config.OUTPUT_DIR)
    print(f"SearchID: {search_id}")
    print(f"RunID: {run_id}")
    print(f"rows module1: {len(rows1)}")
    print(f"rows final: {len(rows_final)}")
    print(f"snapshots: {snapshot_count}")
    print(f"events: {event_count}")
    print(f"excel: {excel_path}")


if __name__ == "__main__":
    main()
