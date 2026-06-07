from config import AREA_SEARCH_URL

# ماژول ۱
from module1_list_scraper import scrape_search, save_results

# ماژول ۲
from module2_infer_prices import module2_run

# ماژول ۳
from module3_enrich_details import module3_run


if __name__ == "__main__":
    # 1) module1
    rows = scrape_search(AREA_SEARCH_URL, max_pages=3, timeout=25)
    csv1, json1 = save_results(rows, out_dir="output")

    # 2) module2
    csv2, json2 = module2_run(
        base_list_url=AREA_SEARCH_URL,
        input_file=json1,  # یا csv1
        out_dir="output",
        window_width=200_000,
        step=50_000,
        max_high=5_000_000,
        max_pages_per_window=5,
        only_overwrite_na=True,
    )

    # 3) module3
    module3_run(
        area_search_url=AREA_SEARCH_URL,
        input_file=json2,  # خروجی ماژول ۲
        out_dir="output",
        only_if_missing=True,
    )

    print("\n🎉 Pipeline complete.")
