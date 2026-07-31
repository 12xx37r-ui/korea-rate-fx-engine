from src.collectors.korea_asset_fundamentals import (
    _extract_distributions,
    _merge_index_sources,
    _num,
    _positive_num,
)


def test_num():
    assert _num("1,234.5%") == 1234.5
    assert _num("-") is None


def test_positive_num_rejects_zero():
    assert _positive_num("0") is None
    assert _positive_num("14.8") == 14.8


def test_distribution_parser_prefers_cash_amount():
    html = """
    <table>
      <tr><td>2026.06.29</td><td>0.83</td><td>33</td></tr>
      <tr><td>2026.05.28</td><td>0.77</td><td>33</td></tr>
    </table>
    """
    rows = _extract_distributions(html)
    assert rows == [
        {"date": "2026-05-28", "amount": 33.0},
        {"date": "2026-06-29", "amount": 33.0},
    ]


def test_merge_index_sources_keeps_official_and_proxy_separate():
    out = _merge_index_sources(
        {
            "per": 14.8,
            "pbr": 1.82,
            "dividend_yield": 1.04,
            "available": True,
            "as_of": "2026-07-30",
            "source": "KRX",
            "source_method": "pykrx_authenticated",
            "diagnostics": [],
        },
        {
            "forward_per": 12.5,
            "eps_growth_pct": 8.0,
            "available": True,
            "source": "ETF proxy",
            "source_method": "representative_etf_proxy",
            "proxy_symbols": ["069500.KS"],
            "diagnostics": [],
        },
    )
    assert out["per"] == 14.8
    assert out["forward_per"] == 12.5
    assert out["eps_growth_pct"] == 8.0
    assert out["growth_adjusted_per"] == 1.5625
    assert out["coverage"] == 1.0
    assert out["forward_data_available"] is True
