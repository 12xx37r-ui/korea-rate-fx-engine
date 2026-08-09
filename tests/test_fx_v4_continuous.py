from src.models.fx_forecast_v4 import build_fx_forecast_v4
from src.models.krw_liquidity import build_krw_liquidity_forecast


def _daily(n=950):
    rows=[]
    for i in range(n):
        # Smooth but nontrivial synthetic path with alternating medium-term regimes.
        value=1250 + i*0.08 + ((i % 90)-45)*0.45
        rows.append({'TIME':f'{2018+i//252:04d}{(i%12)+1:02d}{(i%27)+1:02d}','DATA_VALUE':str(value)})
    return rows


def test_fx_v4_always_forecasts_and_never_random_walk_operational_model():
    out=build_fx_forecast_v4({'usdkrw':_daily()}, {}, {})
    assert out['forecast_operational'] is True
    assert len(out['forecast_path']) == 4
    assert out['production_model'] == 'continuous_oos_weighted_ensemble_v4'
    assert all(row['prediction_status']=='forecast' for row in out['forecast_path'])
    assert all(row['production_model']!='random_walk_fallback' for row in out['forecast_path'])
    assert all(row['signal_active'] is True for row in out['forecast_path'])


def test_liquidity_forecast_runs_with_policy_market_proxy_only():
    ecos={
        'kr_base_rate':[{'TIME':'20260807','DATA_VALUE':'2.50'}],
        'kr_gov_2y':[{'TIME':'20260807','DATA_VALUE':'2.35'}],
    }
    rate={'current':{'kr_base_rate_pct':2.5},'meeting_path':[{'expected_rate_pct':2.4},{'expected_rate_pct':2.3}]}
    out=build_krw_liquidity_forecast(ecos, rate)
    assert out['forecast_operational'] is True
    assert out['data_mode']=='policy_market_proxy'
    assert len(out['forecast_path'])==3
    assert all(row['prediction_status']=='forecast' for row in out['forecast_path'])


def test_fx_v4_uses_public_macro_candidate_when_factor_coverage_is_sufficient():
    rows = _daily(950)
    # Reuse the synthetic FX dates so every historical origin can resolve factor values.
    factor_rows = [{"date": r["TIME"], "value": 100.0 + i * 0.01} for i, r in enumerate(rows)]
    rate_rows = [{"TIME": r["TIME"], "DATA_VALUE": str(2.5 + (i % 30) * 0.001)} for i, r in enumerate(rows)]
    global_data = {
        "broad_dollar": factor_rows,
        "usd_cny": factor_rows,
        "usd_jpy": factor_rows,
        "vix": [{"date": r["TIME"], "value": 20.0} for r in rows],
        "hy_oas": [{"date": r["TIME"], "value": 4.0} for r in rows],
    }
    out = build_fx_forecast_v4({"usdkrw": rows, "kr_gov_2y": rate_rows}, global_data, {})
    assert out["factor_panel"]["3m"]["macro"]["coverage"] >= 3
    assert "macro_public_factors" in out["factor_panel"]["3m"]["candidate_returns_pct"]
