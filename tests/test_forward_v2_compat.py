from src.models.korea_comprehensive_market import _weighted_signal_score_v2, _build_forward_score_v2

def test_weighted_signal_normalizes_by_weight_sum():
    out=_weighted_signal_score_v2([(1.0,2.0,"a"),(-1.0,1.0,"b")])
    assert out["score"]==67
    assert out["weight_sum"]==3.0

def test_v2_is_shadow_and_confidence_is_capped():
    factors={k:{"available":True,"score_normalized":0.2,"evidence":{}} for k in ["price_trend","breadth","earnings","valuation","flow","rate_liquidity_credit","fx","market_risk","external"]}
    out=_build_forward_score_v2(factors,{}, {}, {})
    assert out["shadow_only"] is True
    assert out["periods"]["12m"]["forecast_confidence_pct"] <= 46
