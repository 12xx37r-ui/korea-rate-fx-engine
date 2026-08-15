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


def test_global_market_uses_three_parallel_fred_groups_plus_yahoo(monkeypatch, tmp_path: Path):
    calls = []

    def fake_fred(group, start):
        calls.append((tuple(group), start))
        return _fresh_for(group)

    monkeypatch.setattr(global_market, "_fred_batch", fake_fred)
    monkeypatch.setattr(global_market, "_yahoo_usdkrw", lambda: [{"date": "20260809", "value": 1407.5, "source": "Yahoo Finance"}])
    monkeypatch.setattr(global_market, "_bis_eer_api", lambda previous: ({"krw_neer":[{"date":"20260701","value":100.0,"source":"BIS"}], "krw_reer":[{"date":"20260701","value":101.0,"source":"BIS"}]}, "2000-01", "bootstrap"))
    monkeypatch.setattr(global_market, "_collect_yahoo_equity", _fake_semiconductor)

    result = global_market.collect(tmp_path, timeout=30, retries=3)
    assert len(calls) == 3
    assert result.metadata["request_count"] == 3 + 1 + 1 + len(global_market.YAHOO_EQUITY)
    assert result.metadata["fred_groups_parallel"] is True
    assert result.status == "ok"

    data = json.loads((tmp_path / "raw_global_market.json").read_text(encoding="utf-8"))
    assert data["broad_dollar"][-1]["date"] == "20260807"
    assert data["usd_krw_yahoo"][-1]["value"] == 1407.5


def test_fred_group_failures_do_not_fan_out_and_reuse_last_good(monkeypatch, tmp_path: Path):
    previous = {
        key: [{"date": "20260806", "value": 1.0, "source": "FRED", "series_id": sid}]
        for key, sid in global_market.FRED.items()
    }
    previous["usd_krw_yahoo"] = [{"date": "20260806", "value": 1410.0, "source": "Yahoo Finance"}]
    (tmp_path / "raw_global_market.json").write_text(json.dumps(previous), encoding="utf-8")

    calls = []
    def fail(group, start):
        calls.append((tuple(group), start))
        raise requests.Timeout("fred timeout")

    monkeypatch.setattr(global_market, "_fred_batch", fail)
    monkeypatch.setattr(global_market, "_yahoo_usdkrw", lambda: [{"date": "20260809", "value": 1407.5, "source": "Yahoo Finance"}])
    monkeypatch.setattr(global_market, "_bis_eer_api", lambda previous: (_ for _ in ()).throw(AssertionError("fresh EER cache should skip BIS")))
    monkeypatch.setattr(global_market, "_collect_yahoo_equity", _fake_semiconductor)

    result = global_market.collect(tmp_path, timeout=30, retries=3)
    assert len(calls) == 3
    assert result.metadata["request_count"] == 3 + 1 + len(global_market.YAHOO_EQUITY)
    assert len(result.metadata["last_good_reused"]) >= sum(len(g) for g in global_market.FRED_GROUPS.values())

    data = json.loads((tmp_path / "raw_global_market.json").read_text(encoding="utf-8"))
    assert data["broad_dollar"][-1]["date"] == "20260806"
    assert data["usd_krw_yahoo"][-1]["value"] == 1407.5


def test_fred_first_bootstrap_is_recent_not_2018():
    start, mode = global_market._group_start({}, global_market.FRED_GROUPS["currency"])
    assert mode == "bootstrap_recent"
    assert start > "2023-01-01"


def test_fred_existing_history_uses_incremental_overlap():
    group = global_market.FRED_GROUPS["currency"]
    previous = {key: [{"date": "20260801", "value": 1.0}] for key in group}
    start, mode = global_market._group_start(previous, group)
    assert mode == "incremental"
    assert "2026" in start or "2025" in start



def test_bis_eer_csv_parser_combined_nominal_real():
    text="FREQ,EER_TYPE,EER_BASKET,REF_AREA,TIME_PERIOD,OBS_VALUE\nM,N,B,KR,2026-06,101.2\nM,R,B,KR,2026-06,98.7\n"
    out=global_market._parse_bis_eer_csv(text)
    assert out["krw_neer"][-1]["value"] == 101.2
    assert out["krw_reer"][-1]["value"] == 98.7
