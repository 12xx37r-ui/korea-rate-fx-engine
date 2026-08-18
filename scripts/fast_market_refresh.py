from __future__ import annotations
import json, math, random, time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import requests

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output" / "korea_rate_fx_outlook_v3.json"
STATUS = ROOT / "output" / "fast_market_refresh_status.json"
YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/KRW=X"
TIMEOUT = (3, 8)
FULL_GUARD_MINUTES = 120

def now_iso():
    return datetime.now(timezone.utc).isoformat()

def read_json(path: Path) -> dict[str, Any]:
    try:
        x = json.loads(path.read_text(encoding="utf-8"))
        return x if isinstance(x, dict) else {}
    except Exception:
        return {}

def write_json(path: Path, obj: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)

def num(x):
    try:
        v=float(x)
        return v if math.isfinite(v) else None
    except Exception:
        return None

def parse_dt(v):
    try:
        d=datetime.fromisoformat(str(v).replace("Z","+00:00"))
        if d.tzinfo is None: d=d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc)
    except Exception:
        return None

def yahoo_spot(session: requests.Session) -> dict[str, Any]:
    last = None
    for attempt in range(3):
        try:
            r = session.get(YAHOO, params={"range":"1d","interval":"1m","includePrePost":"true","events":"history","_ts":int(time.time())},
                            timeout=TIMEOUT, headers={"Cache-Control":"no-cache","Pragma":"no-cache"})
            if r.status_code == 429:
                ra = r.headers.get("Retry-After")
                wait = min(12.0, float(ra)) if ra and str(ra).replace(".","",1).isdigit() else min(12.0, 1.5*(2**attempt)+random.random())
                time.sleep(wait); continue
            r.raise_for_status()
            node = (((r.json() or {}).get("chart") or {}).get("result") or [None])[0] or {}
            meta = node.get("meta") or {}
            price = num(meta.get("regularMarketPrice"))
            ts = meta.get("regularMarketTime")
            if price is None or ts is None:
                raise ValueError("regularMarketPrice/Time unavailable")
            obs = datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
            return {"price":price,"market_time_utc":obs,"retrieved_at_utc":now_iso(),
                    "source":"Yahoo Finance KRW=X chart metadata","exchange":meta.get("exchangeName"),
                    "market_state":meta.get("marketState")}
        except Exception as e:
            last=e
            if attempt < 2: time.sleep(min(8.0, 1.0*(2**attempt)+random.random()))
    raise RuntimeError(f"KRW=X refresh failed: {type(last).__name__}: {last}")

def rebase_fx(payload: dict[str, Any], spot: float, obs: str) -> None:
    fx = payload.get("fx")
    if not isinstance(fx, dict):
        return
    fx["current_usdkrw"] = round(spot, 4)
    fx["current_date"] = obs[:10].replace("-","")
    fx["current_source"] = "Yahoo Finance KRW=X chart metadata"
    fx["market_spot"] = round(spot, 4)
    fx["market_spot_as_of_utc"] = obs
    fx["market_spot_source"] = "Yahoo Finance KRW=X chart metadata"
    fx["market_spot_status"] = "LIVE"
    fx["market_spot_retrieved_at_utc"] = now_iso()
    age=(datetime.now(timezone.utc)-parse_dt(obs)).total_seconds()/60 if parse_dt(obs) else None
    fx["market_spot_age_minutes"] = round(max(0,age),2) if age is not None else None

    source_path = fx.get("model_forecast_path")
    if not isinstance(source_path, list):
        source_path = fx.get("forecast_path")
    rebased=[]
    if isinstance(source_path,list):
        for row in source_path:
            if not isinstance(row,dict): continue
            chg = num(row.get("model_change_pct"))
            if chg is None: chg = num(row.get("change_pct"))
            if chg is None: continue
            nr=dict(row)
            factor=1.0+chg/100.0
            point=spot*factor
            nr["point_forecast"]=point; nr["mid"]=point
            nr["model_change_pct"]=chg; nr["change_pct"]=chg
            nr["market_spot_anchor"]=spot
            for key in ("range_50","range_80"):
                rg=row.get(key)
                old_anchor=num(fx.get("model_anchor_usdkrw"))
                if isinstance(rg,list) and len(rg)==2 and old_anchor:
                    nr[key]=[spot*(num(rg[0])/old_anchor), spot*(num(rg[1])/old_anchor)]
            rebased.append(nr)
    if rebased:
        fx["rebased_forecast_path"]=rebased
        fx["forecast_path"]=rebased

def main():
    payload=read_json(OUT)
    if not payload:
        raise SystemExit("canonical output missing: output/korea_rate_fx_outlook_v3.json")
    full_dt=parse_dt(payload.get("generated_at"))
    if full_dt is not None:
        full_age=(datetime.now(timezone.utc)-full_dt).total_seconds()/60.0
        if 0 <= full_age <= FULL_GUARD_MINUTES:
            skipped={"schema_version":"1.0","generated_at_utc":now_iso(),"status":"SKIP_RECENT_FULL",
                     "reason":"full output is recent","full_generated_at_utc":full_dt.isoformat(),
                     "full_age_minutes":round(full_age,2),"guard_minutes":FULL_GUARD_MINUTES,
                     "network_calls":0,"canonical_file_changed":False}
            write_json(STATUS,skipped)
            print(json.dumps(skipped,ensure_ascii=False))
            return
    old_obs=parse_dt(((payload.get("fx") or {}).get("market_spot_as_of_utc")))
    session=requests.Session()
    session.headers.update({"User-Agent":"korea-rate-fx-engine-fast-refresh/1.0","Accept":"application/json"})
    try:
        snap=yahoo_spot(session)
        new_obs=parse_dt(snap["market_time_utc"])
        if old_obs and new_obs and new_obs <= old_obs:
            print(json.dumps({"status":"NO_CHANGE","reason":"provider observation not newer","network_calls":1}, ensure_ascii=False))
            return
        rebase_fx(payload, snap["price"], snap["market_time_utc"])
        payload.setdefault("fast_market_refresh",{})
        payload["fast_market_refresh"].update({"version":"V230","updated_at_utc":now_iso(),
            "scope":["USD/KRW"],"full_engine_recomputed":False,"model_formulas_changed":False})
        write_json(OUT,payload)
        write_json(STATUS, {"schema_version":"1.0","generated_at_utc":now_iso(),"status":"UPDATED",
                            "network_calls":1,"canonical_file_changed":True,"snapshot":snap})
    except Exception as e:
        print(json.dumps({"status":"LKG","canonical_file_changed":False,
                          "error":f"{type(e).__name__}: {str(e)[:300]}"}, ensure_ascii=False))
        raise

if __name__=="__main__":
    main()
