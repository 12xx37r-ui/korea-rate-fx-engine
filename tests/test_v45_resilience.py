from __future__ import annotations

from src.models.fx_forecast_v4 import build_fx_forecast_v4
from src.models.krw_liquidity import build_krw_liquidity_forecast
from src.models.krw_strength import build_krw_strength_forecast
from src.models.korea_policy_v2 import build_rate_forecast_v2


def _daily_fx(n=1000):
    rows=[]
    for i in range(n):
        year=2022+i//252
        month=(i//21)%12+1
        day=i%20+1
        rows.append({"TIME":f"{year:04d}{month:02d}{day:02d}","DATA_VALUE":str(1350+0.03*i+8*((i%50)-25)/25)})
    return rows


def _daily_rate(n=1000, start=2.5):
    rows=[]
    for i in range(n):
        year=2022+i//252; month=(i//21)%12+1; day=i%20+1
        rows.append({"TIME":f"{year:04d}{month:02d}{day:02d}","DATA_VALUE":str(start+0.3*((i%80)/80))})
    return rows


def _monthly(n=180, base=1000.0, growth=0.005):
    rows=[]
    y,m=2012,1
    v=base
    for i in range(n):
        rows.append({"TIME":f"{y:04d}{m:02d}","DATA_VALUE":str(v)})
        v*=1+growth+0.001*((i%12)-6)/6
        m+=1
        if m==13: y+=1; m=1
    return rows


def test_fx_macro_has_ecos_fallback_without_fred():
    ecos={
        "usdkrw":_daily_fx(),
        "kr_gov_2y":_daily_rate(start=3.1),
        "kr_base_rate":_daily_rate(start=2.6),
        "current_account":_monthly(base=5000,growth=0.002),
        "fx_reserves":_monthly(base=400000,growth=0.001),
    }
    out=build_fx_forecast_v4(ecos, global_data={})
    assert out["forecast_operational"] is True
    macro=out["factor_panel"]["3m"]["macro"]
    assert macro["coverage"] >= 3
    assert macro["fallback_core_active"] is True
    assert "macro_public_factors" in out["factor_panel"]["3m"]["candidate_returns_pct"]


def test_liquidity_has_separate_oos_quality():
    ecos={
        "kr_m1":_monthly(base=1000,growth=.006),
        "kr_m2":_monthly(base=2000,growth=.005),
        "kr_lf":_monthly(base=3000,growth=.0045),
        "kr_base_rate":_daily_rate(start=2.5),
        "kr_gov_2y":_daily_rate(start=3.0),
    }
    out=build_krw_liquidity_forecast(ecos,{"current":{"kr_base_rate_pct":2.75},"meeting_path":[{"expected_rate_pct":2.75},{"expected_rate_pct":2.75},{"expected_rate_pct":2.75}]})
    assert out["forecast_operational"] is True
    assert out["quality"]["separate_oos_validated"] is True
    assert out["quality"]["forecast_quality_grade"] in {"A","B","C","D"}
    assert out["quality"]["input_data_quality_score"] >= 0
    assert out["validation"]["oos_by_horizon"]["3m"]["samples"] > 60


def test_strength_rate_gap_proxy_when_fred_us2_missing():
    ecos={
        "usdkrw":_daily_fx(),
        "kr_base_rate":[{"TIME":"20260807","DATA_VALUE":"2.75"}],
        "kr_gov_2y":[{"TIME":"20260807","DATA_VALUE":"3.57"}],
        "current_account":_monthly(base=5000,growth=.002),
        "fx_reserves":_monthly(base=400000,growth=.001),
    }
    fx={"forecast_path":[
        {"months":3,"mid":1400,"change_pct":-0.5,"down_probability":.50,"neutral_probability":.05,"up_probability":.45,"range_80":[1320,1480],"quality_grade":"C","model_quality_score":68},
        {"months":6,"mid":1400,"change_pct":-0.5,"down_probability":.50,"neutral_probability":.05,"up_probability":.45,"range_80":[1300,1500],"quality_grade":"C","model_quality_score":67},
        {"months":12,"mid":1400,"change_pct":-0.5,"down_probability":.50,"neutral_probability":.05,"up_probability":.45,"range_80":[1250,1550],"quality_grade":"C","model_quality_score":64},
    ]}
    rate={"current":{"kr_base_rate_pct":2.75,"us_current_effective_rate_pct":3.63}}
    out=build_krw_strength_forecast(ecos,{},fx,rate)
    assert out["forecast_operational"] is True
    assert "rate_gap" in out["factor_panel"]["group_scores"]
    assert out["factor_panel"]["details"]["rate_gap"]["proxy"] is True
    assert out["factor_panel"]["weighted_group_coverage"] >= 0.6


def test_fx_macro_stays_active_with_two_independent_ecos_axes():
    ecos={
        "usdkrw":_daily_fx(),
        "kr_gov_2y":_daily_rate(start=3.1),
        "kr_base_rate":_daily_rate(start=2.6),
        "current_account":_monthly(base=5000,growth=0.002),
        # Deliberately omit reserves/global data: temporary source gaps must not
        # collapse the macro candidate to zero or stop the operational forecast.
    }
    out=build_fx_forecast_v4(ecos, global_data={})
    macro=out["factor_panel"]["3m"]["macro"]
    assert out["forecast_operational"] is True
    assert macro["coverage"] >= 2
    assert macro["fallback_core_active"] is True
    assert "macro_public_factors" in out["factor_panel"]["3m"]["candidate_returns_pct"]
