from src.models.krw_strength import build_snapshot


def rows(values):
    return [{"TIME": str(i), "DATA_VALUE": str(v)} for i, v in enumerate(values)]


def test_snapshot_generates_scenarios_and_caps_confidence_without_us_engine():
    ecos = {
        "usdkrw": rows([1400 - i for i in range(70)]),
        "kr_base_rate": rows([2.5] * 5),
        "kr_gov_2y": rows([2.2] * 5),
    }
    kosis = {"cpi_core": [{"DT": "0.2"}], "industrial_production": [{"DT": "100"}, {"DT": "99"}]}
    result = build_snapshot(ecos, kosis)
    assert result["current"]["usdkrw"] == 1331.0
    assert "kr_base_rate_scenarios" in result["forecast"]
    assert result["forecast"]["confidence"] <= 0.55
    assert result["forecast"]["kr_base_rate_scenarios"]["hold"]["probability"] > 0


def test_high_fx_level_is_not_mislabeled_strong_only_due_to_recent_decline():
    # 대부분 기간보다 현재 환율이 높은 상태지만 최근 20일은 하락한 사례
    fx = [1200 + i * 0.8 for i in range(200)] + [1510 - i * 1.0 for i in range(70)]
    ecos = {
        "usdkrw": rows(fx),
        "kr_base_rate": rows([2.75] * 10),
        "kr_gov_2y": rows([3.8] * 10),
    }
    kosis = {"cpi_core": [{"DT": "0.2"}] * 6, "industrial_production": [{"DT": str(100 + i)} for i in range(6)]}
    result = build_snapshot(ecos, kosis)
    assert result["current"]["krw_absolute_level_score"] < 0
    assert result["current"]["krw_short_term_momentum_score"] > 0
    assert result["current"]["krw_strength_score"] < result["current"]["krw_short_term_momentum_score"]


def test_strength_current_spot_uses_same_fresh_overlay_as_fx_v4():
    from datetime import date, timedelta

    start = date(2025, 1, 1)
    fx_rows = []
    for i in range(320):
        d = start + timedelta(days=i)
        fx_rows.append({"TIME": d.strftime("%Y%m%d"), "DATA_VALUE": str(1400 + i * 0.05)})
    last = start + timedelta(days=321)
    global_data = {
        "usd_krw_yahoo": [{"date": last.strftime("%Y%m%d"), "value": 1392.5}],
    }
    ecos = {
        "usdkrw": fx_rows,
        "kr_base_rate": [{"TIME": "20251118", "DATA_VALUE": "2.5"}],
        "kr_gov_2y": [{"TIME": "20251118", "DATA_VALUE": "2.4"}],
    }
    result = build_snapshot(ecos, {}, global_data=global_data)
    assert result["current"]["usdkrw"] == 1392.5
    assert result["forecast"]["usdkrw_mid"] is not None
