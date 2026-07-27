from src.models.korea_policy_v2 import build_fx_forecast_v2, build_rate_forecast_v2


def _rows(values, key="DATA_VALUE"):
    result = []
    for i, value in enumerate(values):
        year = 2010 + i // 12
        month = i % 12 + 1
        result.append({"TIME": f"{year}{month:02d}", key: str(value)})
    return result


def test_v2_additive_outputs_and_probability_sum():
    n = 180
    ecos = {
        "kr_base_rate": _rows([2.5] * n),
        "kr_gov_2y": _rows([2.7] * n),
        "usdkrw": _rows([1100 + i for i in range(n)]),
    }
    cpi = []
    for i in range(n):
        year = 2010 + i // 12
        month = i % 12 + 1
        cpi.append({"PRD_DE": f"{year}{month:02d}", "DT": "2.2", "ITM_NM": "농산물 및 석유류 제외 전년동월비"})
    kosis = {"cpi_core": cpi, "industrial_production": _rows([100 + i * 0.1 for i in range(n)], "DT")}
    us = {"current_effective_rate": 4.0, "meeting_path": [{"expected_rate": 3.75}] * 3}
    out = build_rate_forecast_v2(ecos, kosis, us)
    assert len(out["meeting_path"]) == 3
    assert abs(sum(out["meeting_path"][0]["probabilities"].values()) - 1.0) < 0.01
    assert out["engine_scope"] == "korea_only_us_engine_read_only"


def test_fx_v2_uses_legacy_without_mutating_it():
    legacy = {
        "status": "ok",
        "current": {"usdkrw": 1400.0},
        "forecast": {"usdkrw_mid": 1380.0, "usdkrw_range": [1320.0, 1460.0]},
        "methodology": {"fx_backtest_samples": 240, "fx_backtest_rmse_pct": 4.7},
    }
    before = repr(legacy)
    out = build_fx_forecast_v2(legacy, {"regime": {"name": "growth_support"}})
    assert len(out["forecast_path"]) == 4
    assert out["validation"]["quality_gate"]["passed"] is True
    assert repr(legacy) == before
