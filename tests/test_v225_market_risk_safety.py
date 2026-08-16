from datetime import date as real_date
from pathlib import Path

from src.collectors import realtime_risk, krx, kosis
from src.core.kosis_resolver import ResolvedSeries


def test_vkospi_legacy_unverified_rows_are_rejected():
    rows = [{"date": "20260814", "value": 5531.96, "source": "pykrx", "ticker": "1024"}]
    out = realtime_risk._collect_vkospi(rows)
    assert out["available"] is False
    assert out["rows"] == []
    assert out["source_validation"] == "unavailable"


def test_vkospi_only_verified_rows_can_be_reused():
    rows = [{
        "date": "20260814",
        "value": 24.5,
        "source": "KRX official",
        "source_validation": "verified",
    }]
    out = realtime_risk._collect_vkospi(rows)
    assert out["available"] is True
    assert out["rows"][0]["value"] == 24.5
    assert out["source_validation"] == "verified"


def test_krx_weekend_basis_skips_network(monkeypatch):
    class FakeDate(real_date):
        @classmethod
        def today(cls):
            return cls(2026, 8, 16)  # Sunday

    class Stock:
        def get_index_ohlcv_by_date(self, *args, **kwargs):
            raise AssertionError("weekend should not call KRX index endpoint")

    monkeypatch.setattr(krx, "date", FakeDate)
    previous = {"available": True, "basis": 0.5, "signal": "cached", "date": "20260814"}
    out = krx._collect_basis(Stock(), previous)
    assert out["status"] == "CACHE"
    assert out["market_state"] == "CLOSED"
    assert out["basis"] == 0.5


def _resolved(name: str):
    if name == "cpi_core":
        return ResolvedSeries("101", "DT_1J22042", "T03", "3", "M", item_name="core")
    return ResolvedSeries("101", "DT_1JH20202", "T1", "1", "M", item_name="industry")


def test_kosis_weekend_reuses_last_good_without_network(monkeypatch, tmp_path: Path):
    class FakeDate(real_date):
        @classmethod
        def today(cls):
            return cls(2026, 8, 16)  # Sunday

    monkeypatch.setenv("KOSIS_API_KEY", "test")
    monkeypatch.setattr(kosis, "date", FakeDate)
    monkeypatch.setattr(kosis, "_cached_resolution", lambda name, version: _resolved(name))

    def should_not_call(*args, **kwargs):
        raise AssertionError("weekend monthly cache skip should avoid KOSIS network")

    monkeypatch.setattr(kosis, "get_json", should_not_call)
    (tmp_path / "raw_kosis.json").write_text(
        '{"cpi_core":[{"PRD_DE":"202607","DT":"2.1"}],'
        '"industrial_production":[{"PRD_DE":"202606","DT":"110"}]}',
        encoding="utf-8",
    )
    result = kosis.collect(tmp_path, timeout=30, retries=3)
    assert result.status == "ok"
    assert result.metadata["external_request_count"] == 0
    assert result.metadata["cadence_skips"] == ["cpi_core", "industrial_production"]
