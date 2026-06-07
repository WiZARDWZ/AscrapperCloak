from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from module2_price_utils import price_needs_inference


def run():
    assert price_needs_inference("N/A") is True
    assert price_needs_inference("") is True
    assert price_needs_inference("Auction") is True
    assert price_needs_inference("Contact Agent") is True
    assert price_needs_inference("Price on request") is True
    assert price_needs_inference("Expressions of Interest") is True
    assert price_needs_inference("EOI") is True
    assert price_needs_inference("Offers invited") is True

    assert price_needs_inference("Guide $950,000") is False
    assert price_needs_inference("For Sale $1,150,000") is False
    assert price_needs_inference("Auction Guide $700k") is False
    assert price_needs_inference("Auction Guide $1,500,000") is False
    assert price_needs_inference("Auction Guide $500,000 - $550,000") is False

    print("Module2 missing detection test passed")


if __name__ == "__main__":
    run()
