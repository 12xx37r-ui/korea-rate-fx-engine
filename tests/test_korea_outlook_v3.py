from src.models.korea_outlook_v3 import build_v3


def _sample():
    rate={'current':{'kr_base_rate_pct':2.75,'core_cpi_yoy':0.02},'meeting_path':[{'expected_rate_pct':2.8,'probabilities':{'hold':.5,'hike':.4,'cut':.1},'most_likely_action':'hold'}],'validation':{'quality_gate':{'level':'후보'}}}
    fx={'current_usdkrw':1400,'forecast_path':[{'months':3,'range_80':[1330,1470]}],'validation':{'quality_gate':{'passed':True,'level':'준기관급(3개월)'}}}
    ecos={'usdkrw':[{'date':f'2020{i:04d}','value':1200+i*.2} for i in range(1,1300)],'kr_gov_2y':[{'date':'20260101','value':3.0}],'kr_gov_10y':[{'date':'20260101','value':3.2}]}
    g={'us_2y':[{'date':'20260101','value':4.0}],'us_10y':[{'date':'20260101','value':4.2}],'us_breakeven_10y':[{'date':'20260101','value':2.3}],'vix':[{'date':'20260101','value':18}],'hy_oas':[{'date':'20260101','value':3.5}]}
    return rate,fx,ecos,g


def test_v3_is_unified_production_layer():
    rate,fx,ecos,g=_sample()
    out=build_v3(rate,fx,ecos,{}, {},g,{'current_effective_rate':4.0,'meeting_path':[]})
    assert out['fx']['production_use'] is True
    assert out['fx']['quality_gate']['passed'] is True
    assert out['certification']['v3_production_enabled'] is True


def test_v3_does_not_force_minimum_drift():
    rate,fx,ecos,g=_sample()
    out=build_v3(rate,fx,ecos,{}, {},g,{'current_effective_rate':4.0,'meeting_path':[]})
    # 점예측은 계산되지만 현재값과 다르게 보이기 위한 강제 ±0.1% 이동은 없다.
    assert all(abs(float(x['change_pct'])) < 7.6 for x in out['fx']['forecast_path'])


def test_v219_public_current_prefers_market_overlay_and_preserves_model_anchor():
    rate,fx,ecos,g=_sample()
    g=dict(g)
    g['usd_krw_market_snapshot']={
        'price':1418.5,
        'market_time_utc':'2099-08-16T04:30:00+00:00',
        'source':'Yahoo Finance chart metadata',
    }
    # Provide a direct point forecast so the scaling rule is deterministic.
    fx=dict(fx)
    fx['forecast_path']=[{'months':3,'point_forecast':1414.0,'mid':1414.0,'change_pct':1.0,'range_80':[1330,1470]}]
    out=build_v3(rate,fx,ecos,{}, {},g,{'current_effective_rate':4.0,'meeting_path':[]})
    assert out['fx']['current_usdkrw'] == 1418.5
    assert out['fx']['model_anchor_usdkrw'] == 1400
    assert out['fx']['market_spot'] == 1418.5
    assert round(out['fx']['forecast_path'][0]['point_forecast'],2) == round(1414.0*(1418.5/1400.0),2)
    assert out['fx']['model_forecast_path'][0]['point_forecast'] == 1414.0
    assert round(out['fx']['rebased_forecast_path'][0]['point_forecast'], 2) == round(1414.0*(1418.5/1400.0),2)
    assert out['source_freshness']['fx_market']['model_anchor_preserved'] is True


def test_v219_open_flag_with_old_timestamp_is_cache():
    rate,fx,ecos,g=_sample()
    g=dict(g)
    g['usd_krw_market_snapshot']={
        'price':1418.5,
        'market_time_utc':'2020-01-01T00:00:00+00:00',
        'market_state':'OPEN',
        'source':'Naver MarketIndex FX_USDKRW',
    }
    out=build_v3(rate,fx,ecos,{}, {},g,{'current_effective_rate':4.0,'meeting_path':[]})
    assert out['fx']['current_usdkrw'] == 1418.5
    assert out['fx']['market_spot_status'] == 'CACHE'
    assert out['source_freshness']['fx_market']['status'] == 'CACHE'
