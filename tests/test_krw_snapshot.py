from src.models.krw_strength import build_snapshot


def rows(values):
    return [{"TIME": str(i), "DATA_VALUE": str(v)} for i, v in enumerate(values)]


def test_snapshot_generates_current_and_forecast():
    ecos = {
        "usdkrw": rows([1400 - i for i in range(70)]),
        "kr_base_rate": rows([2.5] * 5),
        "kr_gov_2y": rows([2.2] * 5),
    }
    kosis = {"cpi_core": [{"DT": "100"}], "industrial_production": [{"DT": "100"}]}
    result = build_snapshot(ecos, kosis)
    assert result["current"]["usdkrw"] == 1331.0
    assert result["forecast"]["kr_base_rate_direction"] == "인하 우세"
    assert result["forecast"]["usdkrw_range"]
