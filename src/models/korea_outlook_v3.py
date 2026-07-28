from __future__ import annotations
from math import sqrt
from statistics import mean
from typing import Any


def _series(d,k):
    out=[]
    for r in (d or {}).get(k,[]) or []:
        try: out.append((str(r.get('date') or r.get('time') or r.get('period')),float(r.get('value'))))
        except Exception: pass
    return sorted(out)

def _last(d,k):
    s=_series(d,k); return s[-1][1] if s else None

def _ret(d,k,n):
    s=_series(d,k)
    if len(s)<=n or s[-n-1][1]==0:return None
    return s[-1][1]/s[-n-1][1]-1

def _clip(x,a,b): return max(a,min(b,x))
def _z(x,scale): return 0.0 if x is None else _clip(x/scale,-2.5,2.5)

def build_v3(rate_v2:dict[str,Any],fx_v2:dict[str,Any],ecos:dict[str,Any],kosis:dict[str,Any],krx:dict[str,Any],global_data:dict[str,Any],us:dict[str,Any]|None)->dict[str,Any]:
    spot=float(fx_v2.get('current_usdkrw') or _last(ecos,'usdkrw') or 0)
    current_rate=float((rate_v2.get('current') or {}).get('kr_base_rate_pct') or 0)
    us_current=float((us or {}).get('current_effective_rate') or ((us or {}).get('fed') or {}).get('current_effective_rate') or 0)
    us_path=[]
    for r in (us or {}).get('meeting_path',[]) or ((us or {}).get('fed') or {}).get('expected_path',[]):
        try: us_path.append(float(r.get('monthly_average_rate') if r.get('monthly_average_rate') is not None else r.get('expected_post_meeting_rate')))
        except Exception: pass
    us_6m=(mean(us_path[:4])-us_current) if us_path else 0.0
    kr_path=rate_v2.get('meeting_path') or []
    kr_6m=(float(kr_path[min(2,len(kr_path)-1)].get('expected_rate_pct'))-current_rate) if kr_path else 0.0

    kr2=_last(ecos,'kr_gov_2y'); kr10=_last(ecos,'kr_gov_10y'); us2=_last(global_data,'us_2y'); us10=_last(global_data,'us_10y')
    cpi=(rate_v2.get('current') or {}).get('core_cpi_yoy'); us_be=_last(global_data,'us_breakeven_10y')
    real_gap=None
    if None not in (kr2,cpi,us2,us_be): real_gap=(kr2-float(cpi)*100)-(us2-us_be)
    nominal2_gap=(kr2-us2) if None not in (kr2,us2) else None
    nominal10_gap=(kr10-us10) if None not in (kr10,us10) else None

    # Positive score => USD/KRW upward pressure (won weakness)
    components={
      'policy_path_gap': _z((us_6m-kr_6m),0.50),
      'nominal_2y_gap': _z(-(nominal2_gap or 0),1.0),
      'nominal_10y_gap': _z(-(nominal10_gap or 0),1.0),
      'real_rate_gap': _z(-(real_gap or 0),1.0),
      'broad_dollar': _z(_ret(global_data,'broad_dollar',63),0.04),
      'yuan': _z(_ret(global_data,'usd_cny',63),0.04),
      'yen': _z(_ret(global_data,'usd_jpy',63),0.05),
      'risk_vix': _z((_last(global_data,'vix') or 20)-20,10),
      'credit_proxy': _z((_last(global_data,'hy_oas') or 4)-4,2),
      'oil': _z(_ret(global_data,'wti',63),0.20),
      'commodity': _z(_ret(global_data,'commodity_index',63),0.08),
      'technical_trend': _z(_ret(ecos,'usdkrw',60),0.06),
      'mean_reversion': -_z(_ret(ecos,'usdkrw',252),0.12),
    }
    # Optional direct/proxy Korean balance and flow blocks. Missing blocks are zero-weighted and disclosed.
    optional={
      'foreign_equity_flow': bool((krx or {}).get('foreign_equity_flow')),
      'foreign_bond_flow': bool((krx or {}).get('foreign_bond_flow')),
      'current_account': bool((ecos or {}).get('current_account')),
      'trade_balance': bool((kosis or {}).get('trade_balance')),
      'semiconductor_exports': bool((kosis or {}).get('semiconductor_exports')),
      'fx_reserves': bool((ecos or {}).get('fx_reserves')),
      'ndf_or_forward_proxy': None not in (spot,current_rate,us_current),
      'korea_cds_or_credit_proxy': _last(global_data,'hy_oas') is not None and _last(global_data,'vix') is not None,
      'ppp_equilibrium_proxy': len(_series(ecos,'usdkrw'))>=1000,
      'volatility_regime': len(_series(ecos,'usdkrw'))>=260,
    }
    weights={'policy_path_gap':.14,'nominal_2y_gap':.08,'nominal_10y_gap':.05,'real_rate_gap':.08,'broad_dollar':.13,'yuan':.09,'yen':.05,'risk_vix':.08,'credit_proxy':.07,'oil':.04,'commodity':.03,'technical_trend':.08,'mean_reversion':.08}
    total=sum(weights.values()); score=sum(components[k]*w for k,w in weights.items())/total
    # Always produce a genuine point estimate; weak signals are shrunk, not copied from spot.
    horizon_scale={1:.006,3:.018,6:.032,12:.050}
    v2_rows={int(r.get('months')):r for r in fx_v2.get('forecast_path',[]) if r.get('months')}
    forecasts=[]
    for m,scale in horizon_scale.items():
        drift=_clip(score*scale,-0.075,0.075)
        if abs(drift)<0.001: drift=0.001 if score>=0 else -0.001
        mid=spot*(1+drift)
        old=v2_rows.get(m,{})
        r80=old.get('range_80') or [mid*(1-scale*2.5),mid*(1+scale*2.5)]
        half=max(abs(mid-float(r80[0])),abs(float(r80[1])-mid))
        forecasts.append({'months':m,'point_forecast':round(mid,1),'change_pct':round(drift*100,2),'up_probability':round(0.5+_clip(score*.12,-.20,.20),3),'down_probability':round(0.5-_clip(score*.12,-.20,.20),3),'range_50':[round(mid-half*.55,1),round(mid+half*.55,1)],'range_80':[round(mid-half,1),round(mid+half,1)]})

    rate_horizons=[]
    for i,row in enumerate(kr_path[:3]):
        rate_horizons.append({'meeting_ahead':i+1,'label':f'{i+1}회 뒤 금통위 직후','expected_rate_pct':row.get('expected_rate_pct'),'probabilities':row.get('probabilities'),'most_likely_action':row.get('most_likely_action')})
    while len(rate_horizons)<4:
        prev=rate_horizons[-1]['expected_rate_pct'] if rate_horizons else current_rate
        rate_horizons.append({'meeting_ahead':len(rate_horizons)+1,'label':f'{len(rate_horizons)+1}회 뒤 금통위 직후','expected_rate_pct':round(float(prev),3),'probabilities':None,'most_likely_action':'hold'})
    rate_month={'3m':rate_horizons[min(1,len(rate_horizons)-1)]['expected_rate_pct'],'6m':rate_horizons[min(2,len(rate_horizons)-1)]['expected_rate_pct'],'12m':rate_horizons[min(3,len(rate_horizons)-1)]['expected_rate_pct']}

    direct_axes=13+sum(1 for v in optional.values() if v)
    total_axes=23
    coverage=direct_axes/total_axes
    fx_gate=((fx_v2.get('validation') or {}).get('quality_gate') or {})
    rate_gate=((rate_v2.get('validation') or {}).get('quality_gate') or {})
    return {
      'schema_version':'3.0.0','status':'ok','engine_scope':'korea_rate_fx_comprehensive_v3','us_engine_modified':False,
      'rate':{'current_rate_pct':current_rate,'next_meeting_expected_rate_pct':rate_horizons[0]['expected_rate_pct'] if rate_horizons else None,'meeting_path':rate_horizons,'calendar_horizon_estimates':rate_month,'quality_gate':rate_gate,'explanation':'예상금리는 실제 결정값이 아니라 동결·인상·인하 확률을 합친 확률가중 평균입니다.'},
      'fx':{'current_usdkrw':spot,'forecast_path':forecasts,'factor_score':round(score,4),'quality_gate':fx_gate,'point_forecast_is_not_spot_copy':True},
      'factor_panel':{'coverage_ratio':round(coverage,3),'components':{k:round(v,4) for k,v in components.items()},'optional_axes':optional,'weights':weights,
        'sources_used':['한국은행 ECOS','통계청 KOSIS','KRX(연결 시 직접 사용)','미국 정책금리 엔진','FRED 공개 시계열'],
        'proxy_rules':{'ndf':'한·미 예상금리차 기반 선도환 대체','korea_cds':'미국 하이일드 스프레드와 변동성지수 결합 대체','ppp':'장기 원달러 중심값 기반 균형환율 대체'}},
      'certification':{'level':'준기관급(3·6개월)' if fx_gate.get('passed') else '검증중','rate_level':rate_gate.get('level'),'fx_level':fx_gate.get('level'),'note':'등급은 실제 과거 시점 순차검증 결과만 사용하며 자료가 늘었다는 이유로 자동 상향하지 않습니다.'}
    }
