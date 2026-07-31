from src.collectors.korea_asset_fundamentals import (
    _extract_distributions,
    _extract_indexergo_value,
    _merge_sources,
    _num,
)


def test_num():
    assert _num("1,234.5%") == 1234.5
    assert _num("-") is None


def test_indexergo_header_parser():
    value, as_of = _extract_indexergo_value(
        "<title>PER (22.47)</title><div>2026.02.13 | KOSPI 200</div>", "PER"
    )
    assert value == 22.47
    assert as_of == "2026-02-13"


def test_distribution_parser():
    html = """
    <table><tr><td>2026/06/30</td><td>33</td></tr>
    <tr><td>2026/05/30</td><td>33</td></tr></table>
    """
    rows = _extract_distributions(html)
    assert rows == [
        {"date": "2026-05-30", "amount": 33.0},
        {"date": "2026-06-30", "amount": 33.0},
    ]


def test_merge_sources_keeps_primary_and_fills_missing():
    out = _merge_sources(
        {"per": 11.0, "pbr": None, "available": True, "as_of": "2026-07-30", "source": "KRX", "source_method": "primary", "diagnostics": []},
        {"per": 12.0, "pbr": 0.9, "dividend_yield": 2.1, "available": True, "as_of": "2026-07-29", "source": "fallback", "source_method": "secondary", "diagnostics": []},
    )
    assert out["per"] == 11.0
    assert out["pbr"] == 0.9
    assert out["dividend_yield"] == 2.1
    assert out["coverage"] == 1.0
