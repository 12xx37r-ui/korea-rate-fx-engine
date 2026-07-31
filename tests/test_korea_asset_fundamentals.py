from src.collectors.korea_asset_fundamentals import _merge, _num, _rows


def test_num_parses_commas_and_percent():
    assert _num("1,234.5") == 1234.5
    assert _num("3.25%") == 3.25
    assert _num("-") is None


def test_rows_extracts_list_blocks():
    payload = {"OutBlock_1": [{"IDX_NM": "코스피 200"}], "other": "x"}
    assert _rows(payload)[0]["IDX_NM"] == "코스피 200"


def test_merge_uses_fallback_per_field():
    out = _merge(
        {"available": True, "per": 12.0, "pbr": None, "dividend_yield": None, "source": "KRX"},
        {"available": True, "per": 13.0, "pbr": 1.2, "dividend_yield": 2.1, "source": "NAVER"},
    )
    assert out["per"] == 12.0
    assert out["pbr"] == 1.2
    assert out["dividend_yield"] == 2.1
    assert out["available"] is True
