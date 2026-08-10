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
    assert out["quality"]["separate_oos_validated"] is False



def _monthly_series(start_year=2008, months=180, base=100.0, drift=0.08):
    rows=[]
    for i in range(months):
        y=start_year+i//12; m=i%12+1
        rows.append({"date":f"{y:04d}{m:02d}01","value":base+drift*i+0.8*((i%12)-6)/6})
    return rows


def test_independent_krw_strength_oos_with_bis_history():
    months=180
    neer=_monthly_series(base=95, drift=.09)
    reer=_monthly_series(base=96, drift=.075)
    # USD/KRW gradually falls as EER rises, creating a learnable but nontrivial strength target.
    fxrows=[]
    for i in range(months):
        y=2008+i//12; m=i%12+1
        fxrows.append({"TIME":f"{y:04d}{m:02d}01","DATA_VALUE":str(1450-0.9*i+5*((i%12)-6)/6)})
    global_data={
        "krw_neer":neer,"krw_reer":reer,
        "usd_krw_yahoo": [{"date":"20260810","value":1290.0}],
        "broad_dollar":_monthly_series(base=105,drift=-.02),
        "usd_cny":_monthly_series(base=7.2,drift=-.001),
        "usd_jpy":_monthly_series(base=150,drift=-.03),
        "us_2y":[{"date":"20260801","value":3.4}],
    }
    ecos={
        "usdkrw":fxrows,
        "kr_gov_2y":[{"TIME":"20260801","DATA_VALUE":"2.8"}],
        "current_account":[{"TIME":f"{2012+i//12:04d}{i%12+1:02d}","DATA_VALUE":str(50+i*.2)} for i in range(120)],
        "fx_reserves":[{"TIME":f"{2012+i//12:04d}{i%12+1:02d}","DATA_VALUE":str(3500+i*2)} for i in range(120)],
    }
    fx={"forecast_path":[
        {"months":3,"mid":1285,"change_pct":-.4,"down_probability":.55,"neutral_probability":.1,"up_probability":.35,"range_80":[1220,1360],"quality_grade":"C","model_quality_score":65},
        {"months":6,"mid":1280,"change_pct":-.8,"down_probability":.57,"neutral_probability":.08,"up_probability":.35,"range_80":[1180,1400],"quality_grade":"C","model_quality_score":64},
        {"months":12,"mid":1275,"change_pct":-1.2,"down_probability":.58,"neutral_probability":.07,"up_probability":.35,"range_80":[1120,1450],"quality_grade":"C","model_quality_score":62},
    ]}
    out=build_krw_strength_forecast(ecos,global_data,fx,{})
    q=out["quality"]
    assert q["separate_oos_validated"] is True
    assert q["independent_oos_validation"]["no_lookahead"] is True
    assert q["independent_oos_validation"]["oos_by_horizon"]["3m"]["samples"] >= 60
    assert out["current"]["neer"] is not None and out["current"]["reer"] is not None
