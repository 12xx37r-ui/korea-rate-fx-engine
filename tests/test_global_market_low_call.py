from __future__ import annotations

import json
from pathlib import Path

import requests

from src.collectors import global_market


def _fresh_for(group: dict[str, str]):
    return {
        key: [{"date": "20260807", "value": 100.0 + i, "source": "FRED", "series_id": sid}]
        for i, (key, sid) in enumerate(group.items())
    }


def _fake_semiconductor(previous):
    rows = {
        key: [{"date": "20260809", "value": 100.0, "source": "Yahoo Finance"}]
        for key in global_market.YAHOO_EQUITY
    }
    momentum = {
        key: {"latest": 100.0, "mom_20d_pct": None, "mom_60d_pct": None}
        for key in global_market.YAHOO_EQUITY
    }
    return {"rows": rows, "momentum": momentum, "errors": {}}


def _patch_non_fred(monkeypatch):
    monkeypatch.setattr(global_market, "_yahoo_usdkrw_bundle", lambda: ([{"date": "20260809", "value": 1407.5, "source": "Yahoo Finance"}], {"price": 1418.5, "market_time_utc": "2026-08-09T04:00:00+00:00", "source": "Yahoo Finance chart metadata"}))
    monkeypatch.setattr(global_market, "_naver_usdkrw_snapshot", lambda: {})
    monkeypatch.setattr(global_market, "_bis_eer_api", lambda previous: ({"krw_neer":[{"date":"20260701","value":100.0,"source":"BIS"}], "krw_reer":[{"date":"20260701","value":101.0,"source":"BIS"}]}, "2000-01", "bootstrap"))
    monkeypatch.setattr(global_market, "_collect_yahoo_equity", _fake_semiconductor)


def test_v223_missing_fred_series_bootstrap_individually(monkeypatch, tmp_path: Path):
    calls = []

    def fake_fred(group, start):
        calls.append((tuple(group), start))
        return _fresh_for(group)

    monkeypatch.setattr(global_market, "_fred_batch", fake_fred)
    _patch_non_fred(monkeypatch)

    result = global_market.collect(tmp_path, timeout=30, retries=3)
    # Empty cache: each of the 11 FRED series is bootstrapped independently.
    assert len(calls) == sum(len(g) for g in global_market.FRED_GROUPS.values())
    assert all(len(keys) == 1 for keys, _ in calls)
    assert result.metadata["fred_series_aware_incremental"] is True
    assert result.metadata["fred_read_timeout_seconds"] == 20
    assert result.status == "ok"

    data = json.loads((tmp_path / "raw_global_market.json").read_text(encoding="utf-8"))
    assert data["broad_dollar"][-1]["date"] == "20260807"
    assert data["usd_krw_yahoo"][-1]["value"] == 1407.5


def test_v223_existing_history_batches_short_incremental_windows(monkeypatch, tmp_path: Path):
    previous = {
        key: [{"date": "20260806", "value": 1.0, "source": "FRED", "series_id": sid}]
        for key, sid in global_market.FRED.items()
    }
    previous["krw_neer"] = [{"date": "20260801", "value": 100.0}]
    previous["krw_reer"] = [{"date": "20260801", "value": 101.0}]
    previous["usd_krw_yahoo"] = [{"date": "20260806", "value": 1410.0, "source": "Yahoo Finance"}]
    (tmp_path / "raw_global_market.json").write_text(json.dumps(previous), encoding="utf-8")

    calls = []
    def fake_fred(group, start):
        calls.append((tuple(group), start))
        return _fresh_for(group)

    monkeypatch.setattr(global_market, "_fred_batch", fake_fred)
    monkeypatch.setattr(global_market, "_yahoo_usdkrw_bundle", lambda: ([{"date": "20260809", "value": 1407.5, "source": "Yahoo Finance"}], {"price": 1418.5, "source": "Yahoo Finance chart metadata"}))
    monkeypatch.setattr(global_market, "_naver_usdkrw_snapshot", lambda: {})
    monkeypatch.setattr(global_market, "_bis_eer_api", lambda previous: (_ for _ in ()).throw(AssertionError("fresh EER cache should skip BIS")))
    monkeypatch.setattr(global_market, "_collect_yahoo_equity", _fake_semiconductor)

    result = global_market.collect(tmp_path, timeout=30, retries=3)
    assert len(calls) == 3
    assert sorted(len(keys) for keys, _ in calls) == [2, 4, 5]
    assert all(start > "2026-06-01" for _, start in calls)
    assert result.status == "ok"


def test_v223_one_missing_series_does_not_force_whole_group_bootstrap():
    group = global_market.FRED_GROUPS["currency"]
    previous = {key: [{"date": "20260801", "value": 1.0}] for key in group if key != "usd_krw_fred"}
    plan = global_market._group_plan(previous, group)
    incremental = [p for p in plan if p["mode"] == "incremental"]
    bootstrap = [p for p in plan if p["mode"] == "bootstrap_missing"]
    assert len(incremental) == 1
    assert set(incremental[0]["keys"]) == {"broad_dollar", "usd_cny", "usd_jpy"}
    assert len(bootstrap) == 1
    assert bootstrap[0]["keys"] == ["usd_krw_fred"]
    _, mode = global_market._group_start(previous, group)
    assert mode == "mixed_incremental_bootstrap"


def test_v223_failed_subset_gets_only_one_exact_retry(monkeypatch, tmp_path: Path):
    previous = {
        key: [{"date": "20260806", "value": 1.0, "source": "FRED", "series_id": sid}]
        for key, sid in global_market.FRED.items()
    }
    previous["krw_neer"] = [{"date": "20260801", "value": 100.0}]
    previous["krw_reer"] = [{"date": "20260801", "value": 101.0}]
    previous["usd_krw_yahoo"] = [{"date": "20260806", "value": 1410.0, "source": "Yahoo Finance"}]
    (tmp_path / "raw_global_market.json").write_text(json.dumps(previous), encoding="utf-8")

    calls = []
    def fail(group, start):
        calls.append((tuple(group), start))
        raise requests.Timeout("fred timeout")

    monkeypatch.setattr(global_market, "_fred_batch", fail)
    monkeypatch.setattr(global_market, "_yahoo_usdkrw_bundle", lambda: ([{"date": "20260809", "value": 1407.5, "source": "Yahoo Finance"}], {"price": 1418.5, "source": "Yahoo Finance chart metadata"}))
    monkeypatch.setattr(global_market, "_naver_usdkrw_snapshot", lambda: {})
    monkeypatch.setattr(global_market, "_bis_eer_api", lambda previous: (_ for _ in ()).throw(AssertionError("fresh EER cache should skip BIS")))
    monkeypatch.setattr(global_market, "_collect_yahoo_equity", _fake_semiconductor)
    monkeypatch.setattr(global_market.time, "sleep", lambda *_: None)

    result = global_market.collect(tmp_path, timeout=30, retries=3)
    # 3 incremental group requests + one exact retry for each failed group.
    assert len(calls) == 6
    assert result.metadata["request_count"] == 6 + 2 + len(global_market.YAHOO_EQUITY)
    data = json.loads((tmp_path / "raw_global_market.json").read_text(encoding="utf-8"))
    assert data["broad_dollar"][-1]["date"] == "20260806"


def test_fred_missing_bootstrap_is_recent_not_2018():
    start, mode = global_market._series_start({}, "broad_dollar")
    assert mode == "bootstrap_missing"
    assert start > "2023-01-01"


def test_fred_existing_history_uses_45_day_incremental_overlap():
    previous = {"broad_dollar": [{"date": "20260801", "value": 1.0}]}
    start, mode = global_market._series_start(previous, "broad_dollar")
    assert mode == "incremental"
    assert start == "2026-06-17"


def test_bis_eer_csv_parser_combined_nominal_real():
    text="FREQ,EER_TYPE,EER_BASKET,REF_AREA,TIME_PERIOD,OBS_VALUE\nM,N,B,KR,2026-06,101.2\nM,R,B,KR,2026-06,98.7\n"
    out=global_market._parse_bis_eer_csv(text)
    assert out["krw_neer"][-1]["value"] == 101.2
    assert out["krw_reer"][-1]["value"] == 98.7


def test_v219_naver_current_quote_is_preferred(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(global_market, "_fred_batch", lambda group,start: _fresh_for(group))
    monkeypatch.setattr(global_market, "_yahoo_usdkrw_bundle", lambda: ([{"date":"20260814","value":1412.0,"source":"Yahoo Finance"}], {"price":1412.0,"source":"Yahoo Finance chart metadata"}))
    monkeypatch.setattr(global_market, "_naver_usdkrw_snapshot", lambda: {"price":1418.5,"market_state":"CLOSE","source":"Naver MarketIndex FX_USDKRW","retrieved_at_utc":"2026-08-16T05:00:00+00:00"})
    monkeypatch.setattr(global_market, "_bis_eer_api", lambda previous: ({"krw_neer":[{"date":"20260701","value":100.0}],"krw_reer":[{"date":"20260701","value":101.0}]},"2000-01","bootstrap"))
    monkeypatch.setattr(global_market, "_collect_yahoo_equity", _fake_semiconductor)
    global_market.collect(tmp_path, timeout=30, retries=3)
    data=json.loads((tmp_path/"raw_global_market.json").read_text(encoding="utf-8"))
    assert data["usd_krw_market_snapshot"]["price"] == 1418.5
    assert data["usd_krw_market_snapshot"]["source"].startswith("Naver")


def test_v219_fx_weekend_calendar_guardrail():
    from datetime import datetime, timezone
    assert global_market._fx_weekend_state(datetime(2026,8,16,5,0,tzinfo=timezone.utc)) == "CLOSED"
    assert global_market._fx_weekend_state(datetime(2026,8,17,5,0,tzinfo=timezone.utc)) == "OPEN"


def test_v224_official_fred_api_series_observations(monkeypatch):
    calls = []
    class Resp:
        def raise_for_status(self): pass
        def json(self):
            return {"observations": [{"date": "2026-08-14", "value": "3.75"}]}
    class Session:
        headers = {}
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, params, timeout):
            calls.append((url, dict(params), timeout))
            return Resp()
    monkeypatch.setenv("FRED_API_KEY", "test-key")
    monkeypatch.setattr(global_market.requests, "Session", Session)
    rows = global_market._fred_series_api("DGS2", "2026-07-01")
    assert calls[0][0] == "https://api.stlouisfed.org/fred/series/observations"
    assert calls[0][1]["series_id"] == "DGS2"
    assert calls[0][1]["observation_start"] == "2026-07-01"
    assert calls[0][1]["file_type"] == "json"
    assert rows[-1]["date"] == "20260814"
    assert rows[-1]["value"] == 3.75


def test_v224_fred_api_requires_secret(monkeypatch):
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    import pytest
    with pytest.raises(RuntimeError, match="FRED_API_KEY"):
        global_market._fred_series_api("DGS2", "2026-07-01")
