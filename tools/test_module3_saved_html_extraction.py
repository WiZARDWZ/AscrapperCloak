from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from module3_enrich_details import extract_detail_data_from_html


SAMPLE_HTML = """
<html><head>
<meta property="og:description" content="Set within the boutique 'Casa' development, this apartment offers style and space.">
<meta name="description" content="2 bedroom apartment for sale at 347-349 Trafalgar Street, Petersham, NSW 2049, Auction - Contact Agent. View 7 property photos...">
</head><body>
<div>These properties from nearby properties recommended</div>
<div class="contact-agent-panel">
  <div class="agent-card">
    <a href="https://www.realestate.com.au/agent/chris-keane-3166276">Chris Keane</a>
    <a href="tel:0497102114">Call</a>
  </div>
  <div class="agent-card">
    <a href="https://www.realestate.com.au/agent/desiree-hough-3538748">Desiree Hough</a>
    <a href="tel:0404860898">Call</a>
  </div>
  <a href="https://www.realestate.com.au/agency/stone-real-estate-greenwich-FFYBBK?cid=x">Stone Real Estate - Greenwich</a>
</div>
</body></html>
"""


def run():
    d = extract_detail_data_from_html(SAMPLE_HTML)
    assert "Set within the boutique 'Casa' development" in d.get("description", "")
    assert "These properties from" not in d.get("description", "")
    assert "Auction - Contact Agent" in (d.get("detail_price_display") or "")

    agents = d.get("agents") or []
    ids = {a.get("agent_id") for a in agents}
    names = {a.get("name") for a in agents}
    phones = {a.get("phone") for a in agents}
    assert "3166276" in ids and "Chris Keane" in names
    assert "3538748" in ids and "Desiree Hough" in names
    assert "0497102114" in phones
    assert "0404860898" in phones
    assert d.get("agency_name") == "Stone Real Estate - Greenwich"
    assert d.get("agency_code") == "FFYBBK"
    print("Module3 saved HTML extraction test passed")


if __name__ == "__main__":
    run()
