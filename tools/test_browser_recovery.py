import os
import tempfile
import sys
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from browser_recovery import is_429_html, rotate_chrome_profile_safely


def run():
    assert is_429_html("HTTP ERROR 429", "...") is True
    assert is_429_html("Too Many Requests", "...") is True
    assert is_429_html("Normal page", "<html>property cards</html>") is False
    normal_with_script_token = """
    <html>
      <head>
        <meta property="og:title" content="2 Bed Unit in Petersham"/>
        <meta property="og:description" content="Great property details"/>
        <script>var s='rate limit';</script>
      </head>
      <body><a href="/property-unit-nsw-petersham-123456">listing</a></body>
    </html>
    """
    assert is_429_html("Normal listing", normal_with_script_token) is False
    chrome_429 = "<html><body><h1>This page isn't working</h1><p>HTTP ERROR 429</p></body></html>"
    assert is_429_html("This page isn't working", chrome_429) is True
    kpsdk_shell = "<html><head><script>window.KPSDK={};</script><script src='/ips.js'></script></head><body></body></html>"
    assert is_429_html("", kpsdk_shell) is True
    property_with_script_rate_limit = """
    <html>
      <head>
        <meta property="og:title" content="Auction guide"/>
        <meta property="og:description" content="3 bed, 2 bath, parking"/>
      </head>
      <body>
        <a href="/agent/john-smith-123">Agent</a>
        <script>console.log('rate limit');</script>
      </body>
    </html>
    """
    assert is_429_html("Property page", property_with_script_rate_limit) is False

    with tempfile.TemporaryDirectory() as td:
        profile = os.path.join(td, "rea_profile")
        out = rotate_chrome_profile_safely(profile, log_func=lambda *_: None)
        assert os.path.isdir(out)

    with tempfile.TemporaryDirectory() as td:
        profile = os.path.join(td, "rea_profile")
        os.makedirs(profile, exist_ok=True)
        with mock.patch("browser_recovery.os.rename", side_effect=PermissionError("locked")):
            out = rotate_chrome_profile_safely(profile, log_func=lambda *_: None)
            assert "rea_profile_gen_" in os.path.basename(out)
            assert os.path.isdir(out)

    print("OK: browser_recovery tests passed")


if __name__ == "__main__":
    run()
