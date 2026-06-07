import ast
import unittest
from pathlib import Path


RUNTIME_FILES = [
    "module1_list_scraper.py",
    "module2_infer_prices.py",
    "module3_enrich_details.py",
    "chrome_options_helper.py",
    "cloak_browser_helper.py",
]


class CloakMigrationStaticTests(unittest.TestCase):
    def test_runtime_files_do_not_import_old_browser_stack(self):
        banned = {"sel" + "enium", "undetected" + "_chromedriver"}
        for filename in RUNTIME_FILES:
            tree = ast.parse(Path(filename).read_text(encoding="utf-8"), filename=filename)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        root = alias.name.split(".", 1)[0]
                        self.assertNotIn(root, banned, f"{filename} imports {alias.name}")
                elif isinstance(node, ast.ImportFrom) and node.module:
                    root = node.module.split(".", 1)[0]
                    self.assertNotIn(root, banned, f"{filename} imports from {node.module}")

    def test_module1_required_output_keys_are_declared(self):
        import module1_list_scraper as module1

        required = [
            "listing_id",
            "price",
            "address",
            "bedrooms",
            "bathrooms",
            "parking",
            "property_type",
            "agency",
            "inspection_short_label",
            "inspection_long_label",
            "inspection",
            "auction_label",
            "auction_time",
            "auction",
            "url",
            "scraped_at",
            "search_url",
        ]
        source = Path(module1.__file__).read_text(encoding="utf-8")
        for key in required:
            self.assertIn(f'"{key}"', source)


if __name__ == "__main__":
    unittest.main()
