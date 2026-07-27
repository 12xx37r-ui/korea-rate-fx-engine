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
    assert out["validation"]["quality_gate"]["passed"] is False
    assert out["validation"]["quality_gate"]["candidate"] is True
    assert out["validation"]["quality_gate"]["level"] == "준기관급 후보"
    assert repr(legacy) == before


def test_us_meeting_spike_is_filtered_by_monthly_curve():
    n = 180
    ecos = {
        "kr_base_rate": _rows([2.5] * n),
        "kr_gov_2y": _rows([2.7] * n),
        "usdkrw": _rows([1400 + i * 0.1 for i in range(n)]),
    }
    cpi = [{"PRD_DE": f"{2010+i//12}{i%12+1:02d}", "DT": "2.2", "ITM_NM": "농산물 및 석유류 제외 전년동월비"} for i in range(n)]
    kosis = {"cpi_core": cpi, "industrial_production": _rows([100 + i * 0.1 for i in range(n)], "DT")}
    us = {
        "current_effective_rate": 3.63,
        "meeting_path": [
            {"monthly_average_rate": 3.6325, "expected_post_meeting_rate": 3.6558},
            {"monthly_average_rate": 3.795, "expected_post_meeting_rate": 3.88},
            {"monthly_average_rate": 3.895, "expected_post_meeting_rate": 4.57},
        ],
    }
    out = build_rate_forecast_v2(ecos, kosis, us, source_status={"krx": "not_configured", "reb": "not_configured"})
    assert out["current"]["us_3meeting_rate_change_pctp"] < 0.25
    assert out["current"]["us_path_filter"]["rejected"] >= 1
    assert out["validation"]["quality_gate"]["observed"]["data_coverage"] == 0.8


def test_fx_oos_metrics_are_computed_from_ecos_history():
    n = 900
    ecos = {"usdkrw": _rows([1200 + 0.15*i + 18*((i % 40)/40.0) for i in range(n)])}
    legacy = {
        "status": "ok",
        "current": {"usdkrw": ecos["usdkrw"][-1]["DATA_VALUE"]},
        "forecast": {"usdkrw_mid": 1340.0, "usdkrw_range": [1250.0, 1450.0]},
        "methodology": {"fx_backtest_samples": 0, "fx_backtest_rmse_pct": None},
    }
    legacy["current"]["usdkrw"] = float(legacy["current"]["usdkrw"])
    out = build_fx_forecast_v2(legacy, {"regime": {"name": "growth_support"}}, ecos)
    v = out["validation"]
    assert v["samples"] > 0
    assert v["direction_accuracy"] is not None
    assert v["persistence_skill_pct"] is not None
    assert v["interval_80_coverage"] is not None
    assert set(v["oos_by_horizon"]) == {"1m", "3m", "6m", "12m"}
