from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from area_resolver import build_realestate_buy_url, compact_area_text, extract_postcode_tokens, normalize_area_text, normalize_postcode, resolve_nsw_area_query


class Cursor:
    description = [("SuburbName",), ("Postcode",), ("StateCode",)]

    def execute(self, sql, *params):
        assert "dbo.NSWSuburbDirectory" in sql
        return self

    def fetchall(self):
        return [
            ("Petersham", "2049", "NSW"),
            ("Lewisham", "2049", "NSW"),
            ("Annandale", "2038", "NSW"),
            ("Melbourne", "3000", "VIC"),
        ]


class Conn:
    def cursor(self):
        return Cursor()


def resolve(query):
    return resolve_nsw_area_query(Conn(), query)


def test_normalizers():
    assert normalize_area_text("Petersham, N.S.W. ۲۰۴۹ Australia") == "petersham 2049"
    assert compact_area_text("peter sham") == "petersham"
    assert normalize_postcode("٢٠٤٩") == "2049"
    assert extract_postcode_tokens("Petersham ۲۰۴۹") == ["2049"]


def test_exact_queries():
    for query in ("Petersham", "petersham", "PETERSHAM", "Petersham 2049", "2049 Petersham", "Petersham, NSW 2049", "petersham.nsw.2049", "peter sham", "Petersham ۲۰۴۹"):
        out = resolve(query)
        assert out["status"] == "exact", (query, out)
        assert out["matches"][0]["label"] == "Petersham, NSW 2049"
        assert out["matches"][0]["search_url"] == build_realestate_buy_url("Petersham", "2049")


def test_postcode_multiple_unknown_and_non_nsw():
    postcode = resolve("2049")
    assert postcode["status"] == "multiple" and len(postcode["matches"]) == 2
    assert resolve("Peterrsham")["status"] == "suggestions"
    assert resolve("Unknown Place")["status"] == "not_found"
    assert resolve("Melbourne VIC 3000")["status"] == "invalid"
    assert resolve("Petersham VIC 2049")["status"] == "invalid"
    assert resolve("https://example.test/")["status"] == "invalid"


def run_tests():
    test_normalizers()
    test_exact_queries()
    test_postcode_multiple_unknown_and_non_nsw()


if __name__ == "__main__":
    run_tests()
    print("OK")
