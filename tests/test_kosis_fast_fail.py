from datetime import date as real_date
from pathlib import Path

from src.collectors import kosis
from src.core.kosis_resolver import ResolvedSeries
from src.core.credentials import credential_issue


def _resolved(name: str):
    if name == "cpi_core":
        return ResolvedSeries("101", "DT_1J22042", "T03", "3", "M", item_name="core")
    return ResolvedSeries("101", "DT_1JH20202", "T1", "1", "M", item_name="industry")


def test_network_timeout_is_not_credential_error():
    text = "HTTPSConnectionPool host kosis.kr apiKey=*** ConnectTimeoutError max retries exceeded"
    assert credential_issue(text) is False


def test_kosis_isolates_network_failure_and_attempts_each_series(monkeypatch, tmp_path: Path):
    class FakeWeekday(real_date):
        @classmethod
        def today(cls):
            return cls(2026, 8, 17)

    monkeypatch.setattr(kosis, "date", FakeWeekday)
    monkeypatch.setenv("KOSIS_API_KEY", "test")
    monkeypatch.setattr(kosis, "_cached_resolution", lambda name, version: _resolved(name))
    calls = {"n": 0}

    def fail_once(*args, **kwargs):
        calls["n"] += 1
        raise RuntimeError("ConnectTimeoutError: Connection to kosis.kr timed out")

    monkeypatch.setattr(kosis, "get_json", fail_once)
    # Keep last-good for both series so the forecast pipeline remains usable.
    (tmp_path / "raw_kosis.json").write_text(
        '{"cpi_core":[{"PRD_DE":"202607","DT":"2.1"}],"industrial_production":[{"PRD_DE":"202606","DT":"110"}]}',
        encoding="utf-8",
    )
    result = kosis.collect(tmp_path, timeout=30, retries=3)
    assert calls["n"] == 2
    assert result.status == "degraded"
    assert result.metadata["circuit_open"] is False
    assert result.metadata["network_failures"] == ["cpi_core", "industrial_production"]
    assert len(result.metadata["last_good_reused"]) == 2
