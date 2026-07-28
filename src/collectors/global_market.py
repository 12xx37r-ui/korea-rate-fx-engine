from __future__ import annotations

import csv, io, time
from pathlib import Path
from typing import Any
import requests

from src.core.io import write_json
from src.core.result import SourceResult

FRED = {
    'broad_dollar':'DTWEXBGS','us_2y':'DGS2','us_10y':'DGS10','us_breakeven_10y':'T10YIE',
    'vix':'VIXCLS','hy_oas':'BAMLH0A0HYM2','wti':'DCOILWTICO','commodity_index':'PPIACO',
    'usd_cny':'DEXCHUS','usd_jpy':'DEXJPUS','usd_krw_fred':'DEXKOUS'
}

def _fred_csv(series_id:str, timeout:int)->list[dict[str,Any]]:
    url=f'https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}'
    r=requests.get(url,timeout=timeout,headers={'User-Agent':'korea-rate-fx-engine/3.0'})
    r.raise_for_status()
    rows=[]
    for row in csv.DictReader(io.StringIO(r.text)):
        v=row.get(series_id)
        if not v or v=='.': continue
        try: x=float(v)
        except ValueError: continue
        rows.append({'date':row['DATE'].replace('-',''),'value':x,'source':'FRED','series_id':series_id})
    return rows[-5000:]

def collect(output_dir:Path, timeout:int, retries:int)->SourceResult:
    payload={}; errors={}
    for key,sid in FRED.items():
        for attempt in range(max(1,retries+1)):
            try:
                payload[key]=_fred_csv(sid,timeout); break
            except Exception as e:
                if attempt>=retries: errors[key]=f'{type(e).__name__}: {e}'
                else: time.sleep(min(3,attempt+1))
    path=output_dir/'raw_global_market.json'; write_json(path,payload)
    ok=sum(bool(v) for v in payload.values())
    status='ok' if ok>=8 else ('degraded' if ok>=4 else 'error')
    return SourceResult(source='GLOBAL_MARKET',status=status,message=f'{ok}/{len(FRED)} public series collected',payload_path=str(path),metadata={'series_ok':ok,'series_total':len(FRED),'errors':errors,'credential_status':'not_required','action_required':False})
