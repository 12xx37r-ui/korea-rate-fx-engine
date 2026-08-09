from __future__ import annotations

import json
from pathlib import Path

import requests

from src.collectors import global_market


class _FakeResponse:
    def __init__(self, *, text: str = "", payload=None, status_code: int = 200):
        self.text = text
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status={self.status_code}")

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.headers = {}

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        pass


def _fred_csv() -> str:
    ids = list(global_market.FRED.values())
    header = "observation_date," + ",".join(ids)
    row = "2026-08-07," + ",".join(str(100 + i) for i in range(len(ids)))
    return header + "\n" + row + "\n"


def _yahoo_payload():
    return {
        "chart": {
            "result": [
                {
                    "timestamp": [1786233600],
                    "indicators": {"quote": [{"close": [1407.5]}]},
                }
            ]
        }
    }


def test_global_market_uses_at_most_two_requests(monkeypatch, tmp_path: Path):
    fake = _FakeSession([
        _FakeResponse(text=_fred_csv()),
        _FakeResponse(payload=_yahoo_payload()),
    ])
    monkeypatch.setattr(global_market.requests, "Session", lambda: fake)

    result = global_market.collect(tmp_path, timeout=30, retries=3)
    assert len(fake.calls) == 2
    assert result.metadata["request_count"] == 2
    assert result.status == "ok"

    data = json.loads((tmp_path / "raw_global_market.json").read_text(encoding="utf-8"))
    assert data["broad_dollar"][-1]["date"] == "20260807"
    assert data["us_2y"][-1]["value"] == 101.0
    assert data["usd_krw_yahoo"][-1]["value"] == 1407.5


def test_fred_failure_is_single_attempt_and_reuses_last_good(monkeypatch, tmp_path: Path):
    previous = {
        key: [{"date": "20260806", "value": 1.0, "source": "FRED", "series_id": sid}]
        for key, sid in global_market.FRED.items()
    }
    previous["usd_krw_yahoo"] = [{"date": "20260806", "value": 1410.0, "source": "Yahoo Finance"}]
    (tmp_path / "raw_global_market.json").write_text(json.dumps(previous), encoding="utf-8")

    fake = _FakeSession([
        requests.Timeout("fred timeout"),
        _FakeResponse(payload=_yahoo_payload()),
    ])
    monkeypatch.setattr(global_market.requests, "Session", lambda: fake)

    result = global_market.collect(tmp_path, timeout=30, retries=3)
    assert len(fake.calls) == 2
    assert result.status == "degraded"
    assert len(result.metadata["last_good_reused"]) >= len(global_market.FRED)

    data = json.loads((tmp_path / "raw_global_market.json").read_text(encoding="utf-8"))
    assert data["broad_dollar"][-1]["date"] == "20260806"
    assert data["usd_krw_yahoo"][-1]["value"] == 1407.5
