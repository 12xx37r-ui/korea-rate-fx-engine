from __future__ import annotations

from datetime import date, timedelta

from src.collectors import ecos
from src.core.ecos_resolver import EcosResolver


def test_incremental_daily_start_uses_overlap():
    rows = [{"TIME": "20260807", "ITEM_CODE1": "x", "DATA_VALUE": "1"}]
    start = ecos._incremental_start(rows, "D", date(2010, 1, 1))
    assert start == (date(2026, 8, 7) - timedelta(days=ecos.DAILY_OVERLAP_DAYS)).strftime("%Y%m%d")


def test_merge_revised_period_replaces_value():
    old = [{"TIME": "20260807", "ITEM_CODE1": "x", "ITEM_NAME1": "A", "DATA_VALUE": "1"}]
    fresh = [{"TIME": "20260807", "ITEM_CODE1": "x", "ITEM_NAME1": "A", "DATA_VALUE": "2"}]
    merged = ecos._merge_rows(old, fresh)
    assert len(merged) == 1
    assert merged[0]["DATA_VALUE"] == "2"


def test_foreign_reserve_filter_selects_total():
    rows = [
        {"TIME": "202607", "ITEM_NAME1": "외환", "DATA_VALUE": "1"},
        {"TIME": "202607", "ITEM_NAME1": "합계", "DATA_VALUE": "2"},
        {"TIME": "202607", "ITEM_NAME1": "금", "DATA_VALUE": "3"},
    ]
    assert ecos._filter_item_rows(rows, "합계") == [rows[1]]


def test_explicit_ecos_resolution_does_not_call_metadata(monkeypatch):
    resolver = EcosResolver("dummy", timeout=1, retries=0)
    monkeypatch.setattr(resolver, "_table_rows", lambda: (_ for _ in ()).throw(AssertionError("metadata call")))
    resolved = resolver.resolve(
        "usdkrw",
        {
            "stat_code": "731Y001",
            "stat_name": "환율",
            "frequency": "D",
            "item_code1": "0000001",
            "item_name1": "원/미국달러(매매기준율)",
        },
    )
    assert resolved.stat_code == "731Y001"
    assert resolved.item_code1 == "0000001"
