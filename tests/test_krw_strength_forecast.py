from src.models.krw_strength import build_krw_strength_forecast


def _daily(n=900):
    return [
        {"TIME": f"{2020+i//252:04d}{(i%12)+1:02d}{(i%27)+1:02d}", "DATA_VALUE": str(1300 + i*0.05 + ((i%80)-40)*0.4)}
        for i in range(n)
    ]


def test_krw_strength_forecast_has_3_6_12_month_paths():
    fx = {
        "forecast_path": [
            {"months": 3, "mid": 1390.0, "change_pct": -1.0, "down_probability": .56, "neutral_probability": .08, "up_probability": .36, "range_80": [1320, 1470], "quality_grade": "C", "model_quality_score": 68},
            {"months": 6, "mid": 1385.0, "change_pct": -1.4, "down_probability": .57, "neutral_probability": .08, "up_probability": .35, "range_80": [1290, 1500], "quality_grade": "C", "model_quality_score": 67},
            {"months": 12, "mid": 1400.0, "change_pct": -0.4, "down_probability": .52, "neutral_probability": .08, "up_probability": .40, "range_80": [1250, 1550], "quality_grade": "C", "model_quality_score": 64},
        ]
    }
    global_data = {
        "usd_krw_yahoo": [{"date": "20260809", "value": 1407.45}],
        "krw_neer": [{"date": f"2024{i:02d}01", "value": 95+i*.1} for i in range(1,13)],
        "krw_reer": [{"date": f"2024{i:02d}01", "value": 96+i*.1} for i in range(1,13)],
        "broad_dollar": [{"date": f"2025{i%12+1:02d}{i%27+1:02d}", "value": 105-i*.02} for i in range(100)],
        "usd_cny": [{"date": f"2025{i%12+1:02d}{i%27+1:02d}", "value": 7.2-i*.0005} for i in range(100)],
        "usd_jpy": [{"date": f"2025{i%12+1:02d}{i%27+1:02d}", "value": 150-i*.02} for i in range(100)],
        "us_2y": [{"date": "20260808", "value": 3.4}],
    }
    ecos = {
        "usdkrw": _daily(),
        "kr_gov_2y": [{"TIME": "20260808", "DATA_VALUE": "2.8"}],
        "current_account": [{"TIME": f"2024{i:02d}", "DATA_VALUE": str(i)} for i in range(1,13)],
        "fx_reserves": [{"TIME": f"2024{i:02d}", "DATA_VALUE": str(4000+i*5)} for i in range(1,13)],
    }
    out = build_krw_strength_forecast(ecos, global_data, fx, {})
    assert out["forecast_operational"] is True
    assert [row["months"] for row in out["forecast_path"]] == [3, 6, 12]
    assert 0 <= out["current"]["strength_score"] <= 100
    assert out["factor_panel"]["active_group_count"] >= 3
    assert "separate KRW-strength target OOS not claimed" in out["quality"]["validation_basis"]
