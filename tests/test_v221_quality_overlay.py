from src.models.krw_strength import _apply_live_usdkrw_overlay
from src.models.krw_liquidity import build_krw_liquidity_forecast


def _monthly(prefix, start=100.0, months=48):
    rows=[]
    y,m=2022,1
    for i in range(months):
        rows.append({"TIME": f"{y}{m:02d}", "DATA_VALUE": start + i})
        m += 1
        if m == 13:
            y += 1; m = 1
    return rows


def test_live_usdkrw_overlay_replaces_current_only():
    hist=[("20260813", 1410.0), ("20260814", 1412.0)]
    meta={"date":"20260814","source":"Yahoo","value":1412.0}
    global_data={"usd_krw_market_snapshot": {"price":1418.5,"source":"Naver MarketIndex FX_USDKRW","market_time_utc":"2026-08-14T20:44:55+00:00"}}
    live, out_meta, anchor = _apply_live_usdkrw_overlay(hist, meta, global_data)
    assert hist[-1][1] == 1412.0
    assert anchor == 1412.0
    assert live[-1][1] == 1418.5
    assert out_meta["source"] == "Naver MarketIndex FX_USDKRW"
    assert out_meta["live_overlay_applied"] is True


def test_liquidity_exposes_korean_source_provenance():
    ecos={
        "kr_m1": _monthly("m1",100),
        "kr_m2": _monthly("m2",200),
        "kr_lf": _monthly("lf",300),
        "kr_base_rate": _monthly("base",2.0),
        "kr_gov_2y": _monthly("y2",3.0),
    }
    out=build_krw_liquidity_forecast(ecos, {"current":{"kr_base_rate_pct":2.75},"meeting_path":[]})
    assert out["input_freshness"]["kr_m2"]["source"] == "ECOS"
    assert out["foreign_core_dependency"]["uses_fred_in_core_liquidity_score"] is False
    assert out["quality_improvement"]["score_inflation_forbidden"] is True
