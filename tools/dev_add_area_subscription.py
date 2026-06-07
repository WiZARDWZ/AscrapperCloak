import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import config
import db_layer
from area_url_builder import area_label_from_url_or_input, normalize_area_input


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--chat-id", required=True)
    parser.add_argument("--area", required=True)
    args = parser.parse_args()
    area = normalize_area_input(args.area)
    conn = db_layer.connect(config.DB_PATH)
    try:
        user_id = db_layer.upsert_telegram_user(conn, args.chat_id)
        ok, payload = db_layer.add_user_area_subscription(
            conn,
            user_id,
            area["search_url"],
            area_label_from_url_or_input(area),
            suburb=area.get("suburb"),
            state_code=area.get("state"),
            postcode=area.get("postcode"),
        )
    finally:
        conn.close()
    print(json.dumps({"ok": ok, "payload": payload}, ensure_ascii=False, default=str, indent=2))


if __name__ == "__main__":
    main()
