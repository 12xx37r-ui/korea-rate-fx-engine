from __future__ import annotations

from datetime import date, timedelta

import pytest

from src.collectors import korea_asset_fundamentals as kaf


def test_num_and_positive_num():
    assert kaf._num("1,234.5%") == 1234.5
    assert kaf._num("-") is None
    assert kaf._num("NaN") is None
    assert kaf._positive_num("0") is None
    assert kaf._positive_num("-2") is None
    assert kaf._positive_num("3.5") == 3.5


def test_extract_naver_total_infos_is_case_tolerant():
    payload = {
        "totalInfos": [
            {"code": "cnsPer", "value": "12.5"},
            {"code": "cnsEps", "value": "8,000"},
            {"code": "dividendYieldRatio", "value": "3.20%"},
        ]
    }
    infos = kaf._extract_naver_total_infos(payload)
    assert kaf._info_value(infos, "cnsPer") == "12.5"
    assert kaf._info_value(infos, "CNSPER") == "12.5"
    assert kaf._info_value(infos, "dividendYieldRatio") == "3.20%"


def test_aggregate_forward_proxy_uses_harmonic_earnings_weighting():
    ranked = [("000001", 600.0), ("000002", 400.0)]
    metrics = {
        "000001": {"per": 12.0, "forward_per": 10.0, "diagnostics": []},
        "000002": {"per": 24.0, "forward_per": 20.0, "diagnostics": []},
    }
    out = kaf._aggregate_forward_proxy(
        ranked,
        total_market_cap=1000.0,
        metrics_by_code=metrics,
        minimum_coverage=0.5,
        minimum_samples=2,
    )
    assert out["available"] is True
    assert out["sample_size"] == 2
    assert out["market_cap_coverage"] == 1.0
    assert out["forward_per"] == 12.5
    assert out["eps_growth_pct"] == pytest.approx(20.0, abs=1e-3)


def test_aggregate_forward_proxy_abstains_when_coverage_is_low():
    ranked = [("000001", 200.0), ("000002", 100.0)]
    metrics = {
        "000001": {"per": 10.0, "forward_per": 8.0, "diagnostics": []},
        "000002": {"per": None, "forward_per": None, "diagnostics": []},
    }
    out = kaf._aggregate_forward_proxy(
        ranked,
        total_market_cap=1000.0,
        metrics_by_code=metrics,
        minimum_coverage=0.3,
        minimum_samples=2,
    )
    assert out["available"] is False
    assert out["forward_per"] is None
    assert any("forward_proxy_insufficient" in x for x in out["diagnostics"])


def test_merge_index_sources_preserves_official_current_and_adds_proxy():
    current = {
        "per": 14.8,
        "official_forward_per": None,
        "pbr": 1.82,
        "dividend_yield": 1.04,
        "available": True,
        "as_of": "2026-07-30",
        "source": "KRX",
        "source_url": "https://data.krx.co.kr/",
        "source_method": "pykrx_authenticated",
        "diagnostics": [],
    }
    proxy = {
        "forward_per": 12.5,
        "eps_growth_pct": 18.0,
        "available": True,
        "sample_size": 12,
        "market_cap_coverage": 0.71,
        "growth_market_cap_coverage": 0.69,
        "proxy_symbols": ["005930", "000660"],
        "source": "NAVER proxy",
        "source_method": "constituent_market_cap_weighted_consensus",
        "diagnostics": [],
    }
    out = kaf._merge_index_sources(current, proxy)
    assert out["per"] == 14.8
    assert out["forward_per"] == 12.5
    assert out["eps_growth_pct"] == 18.0
    assert out["growth_adjusted_per"] == pytest.approx(12.5 / 18.0, abs=1e-4)
    assert out["forward_data_available"] is True
    assert out["forward_proxy_sample_size"] == 12


def test_distribution_parser_handles_official_six_column_table():
    html = """
    <table>
      <tr><th>분배락일</th><th>지급기준일</th><th>실지급일</th><th>분배율(%)</th><th>분배금액(원)</th><th>과세표준액(원)</th></tr>
      <tr><td>2026.06.27</td><td>2026.06.30</td><td>2026.07.02</td><td>0.82%</td><td>33</td><td>33</td></tr>
      <tr><td>2026.05.29</td><td>2026.05.30</td><td>2026.06.03</td><td>0.80%</td><td>33</td><td>33</td></tr>
    </table>
    """
    rows = kaf._extract_distributions(html)
    assert rows == [
        {"date": "2026-05-29", "amount": 33.0},
        {"date": "2026-06-27", "amount": 33.0},
    ]


def test_extract_public_reit_snapshot_from_korean_text():
    html = """
    <html><body>
      <div>배당금 (최근 12개월) 396.00</div>
      <div>배당수익률 9.863%</div>
    </body></html>
    """
    out = kaf._extract_public_reit_snapshot(html)
    assert out["trailing_12m_distribution"] == 396.0
    assert out["distribution_yield"] == 9.863


def test_collect_reit_prefers_official_history(monkeypatch):
    recent1 = (date.today() - timedelta(days=30)).isoformat()
    recent2 = (date.today() - timedelta(days=60)).isoformat()
    monkeypatch.setattr(
        kaf,
        "_fetch_mirae_distribution_history",
        lambda session, timeout: {
            "available": True,
            "rows": [{"date": recent2, "amount": 33.0}, {"date": recent1, "amount": 33.0}],
            "source": "official",
            "source_url": "https://example.test/official",
            "source_method": "mirae_official_distribution_history",
            "diagnostics": [],
        },
    )
    monkeypatch.setattr(
        kaf,
        "_naver_integration_metrics",
        lambda code, timeout, session=None: {
            "last_close_price": 4000.0,
            "available": True,
            "diagnostics": [],
        },
    )
    monkeypatch.setattr(kaf, "_naver_realtime_price", lambda session, timeout: 4000.0)
    out = kaf._collect_reit(object(), 10)
    assert out["available"] is True
    assert out["official_history_available"] is True
    assert out["trailing_12m_distribution"] == 66.0
    assert out["distribution_yield"] == 1.65
    assert out["source_method"] == "mirae_official_distribution_history"


def test_collect_reit_uses_naver_cross_checked_fallback(monkeypatch):
    monkeypatch.setattr(
        kaf,
        "_fetch_mirae_distribution_history",
        lambda session, timeout: {"available": False, "rows": [], "diagnostics": ["403"]},
    )
    monkeypatch.setattr(
        kaf,
        "_naver_integration_metrics",
        lambda code, timeout, session=None: {
            "last_close_price": 4000.0,
            "dividend_yield": 9.9,
            "dividend": 396.0,
            "available": True,
            "diagnostics": [],
        },
    )
    monkeypatch.setattr(kaf, "_naver_realtime_price", lambda session, timeout: 4000.0)
    monkeypatch.setattr(
        kaf,
        "_fetch_public_reit_snapshot",
        lambda session, timeout: {"available": False, "diagnostics": []},
    )
    out = kaf._collect_reit(object(), 10)
    assert out["available"] is True
    assert out["official_history_available"] is False
    assert out["trailing_12m_distribution"] == 396.0
    assert out["distribution_yield"] == 9.9
    assert out["source_method"] == "naver_public_reit_snapshot_fallback"


def test_collect_reit_uses_public_snapshot_when_official_and_naver_fail(monkeypatch):
    monkeypatch.setattr(
        kaf,
        "_fetch_mirae_distribution_history",
        lambda session, timeout: {"available": False, "rows": [], "diagnostics": ["403"]},
    )
    monkeypatch.setattr(
        kaf,
        "_naver_integration_metrics",
        lambda code, timeout, session=None: {
            "last_close_price": 4000.0,
            "available": False,
            "diagnostics": ["naver failed"],
        },
    )
    monkeypatch.setattr(kaf, "_naver_realtime_price", lambda session, timeout: 4000.0)
    monkeypatch.setattr(
        kaf,
        "_fetch_public_reit_snapshot",
        lambda session, timeout: {
            "available": True,
            "distribution_yield": 9.9,
            "trailing_12m_distribution": 396.0,
            "current_price": 4000.0,
            "source": "public",
            "source_url": "https://example.test/public",
            "source_method": "public_snapshot",
            "diagnostics": [],
        },
    )
    out = kaf._collect_reit(object(), 10)
    assert out["available"] is True
    assert out["distribution_yield"] == 9.9
    assert out["trailing_12m_distribution"] == 396.0
    assert out["source_method"] == "public_snapshot"


def test_collect_writes_schema_13_output(monkeypatch, tmp_path):
    monkeypatch.setattr(
        kaf,
        "_pykrx_index",
        lambda cfg: {
            "per": 15.0,
            "official_forward_per": None,
            "pbr": 1.5,
            "dividend_yield": 1.0,
            "available": True,
            "as_of": "2026-07-30",
            "source": "KRX",
            "source_url": "https://data.krx.co.kr/",
            "source_method": "pykrx_authenticated",
            "diagnostics": [],
        },
    )
    monkeypatch.setattr(
        kaf,
        "_collect_forward_proxy",
        lambda cfg, as_of, timeout: {
            "forward_per": 12.0,
            "eps_growth_pct": 10.0,
            "available": True,
            "sample_size": 10,
            "market_cap_coverage": 0.6,
            "growth_market_cap_coverage": 0.6,
            "proxy_symbols": ["005930"],
            "source": "NAVER proxy",
            "source_method": "constituent_market_cap_weighted_consensus",
            "diagnostics": [],
        },
    )
    monkeypatch.setattr(
        kaf,
        "_collect_reit",
        lambda session, timeout, previous_reit=None: {
            "ticker": "329200",
            "distribution_yield": 9.9,
            "available": True,
            "official_history_available": False,
            "naver_fallback_attempted": True,
            "public_fallback_attempted": False,
            "source_method": "naver_public_reit_snapshot_fallback",
            "stale": False,
            "diagnostics": [],
        },
    )
    result = kaf.collect(tmp_path, timeout=5)
    assert result["schema_version"] == "1.3.0"
    assert result["engine_version"] == "korea-asset-fundamentals-v1.3"
    assert result["errors"] == []
    assert result["indices"]["kospi200"]["forward_data_available"] is True
    assert result["reit"]["available"] is True
    assert (tmp_path / "korea_asset_fundamentals.json").exists()
