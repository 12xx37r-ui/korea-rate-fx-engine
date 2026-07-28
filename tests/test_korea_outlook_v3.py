from src.models.korea_outlook_v3 import build_v3

def test_v3_point_forecast_not_spot_copy():
    rate={'current':{'kr_base_rate_pct':2.75,'core_cpi_yoy':0.02},'meeting_path':[{'expected_rate_pct':2.8,'probabilities':{'hold':.5,'hike':.4,'cut':.1},'most_likely_action':'hold'}],'validation':{'quality_gate':{'level':'후보'}}}
    fx={'current_usdkrw':1400,'forecast_path':[{'months':3,'range_80':[1330,1470]}],'validation':{'quality_gate':{'passed':True,'level':'준기관급(3개월)'}}}
    ecos={'usdkrw':[{'date':f'2020{i:04d}','value':1200+i*.2} for i in range(1,1300)],'kr_gov_2y':[{'date':'20260101','value':3.0}],'kr_gov_10y':[{'date':'20260101','value':3.2}]}
    g={'us_2y':[{'date':'20260101','value':4.0}],'us_10y':[{'date':'20260101','value':4.2}],'us_breakeven_10y':[{'date':'20260101','value':2.3}],'vix':[{'date':'20260101','value':18}],'hy_oas':[{'date':'20260101','value':3.5}]}
    out=build_v3(rate,fx,ecos,{}, {},g,{'current_effective_rate':4.0,'meeting_path':[]})
    assert out['fx']['point_forecast_is_not_spot_copy'] is True
    assert out['fx']['forecast_path'][1]['point_forecast'] != 1400
    assert out['rate']['calendar_horizon_estimates']['3m'] is not None
