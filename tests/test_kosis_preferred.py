from pathlib import Path
from src.core import kosis_resolver as resolver


def test_preferred_core_cpi_uses_category_name(monkeypatch, tmp_path: Path):
    rows = [
        {"ITM_ID": "T10", "ITM_NM": "지수", "C1": "0", "C1_NM": "총지수", "PRD_SE": "M", "TBL_NM": "소비자물가지수"},
        {"ITM_ID": "T10", "ITM_NM": "지수", "C1": "3", "C1_NM": "농산물 및 석유류 제외지수", "PRD_SE": "M", "TBL_NM": "소비자물가지수"},
    ]
    monkeypatch.setattr(resolver, "_probe_table", lambda *args, **kwargs: rows)
    spec = {
        "preferred_tables": [{"orgId": "101", "tblId": "DT_1J22112"}],
        "item_keywords": ["농산물 및 석유류 제외지수"],
        "exclude_keywords": ["OECD"],
        "prdSe": "M",
    }
    result = resolver.resolve_series("cpi_core", spec, "key", 1, 1, tmp_path / "cache.json", 2)
    assert result.tblId == "DT_1J22112"
    assert result.objL1 == "3"


def test_preferred_industrial_rejects_raw_index(monkeypatch, tmp_path: Path):
    rows = [
        {"ITM_ID": "T1", "ITM_NM": "전산업생산지수(원지수)", "C1": "00", "C1_NM": "총지수", "PRD_SE": "M"},
        {"ITM_ID": "T2", "ITM_NM": "전산업생산지수(계절조정지수)", "C1": "00", "C1_NM": "총지수", "PRD_SE": "M"},
    ]
    monkeypatch.setattr(resolver, "_probe_table", lambda *args, **kwargs: rows)
    spec = {
        "preferred_tables": [{"orgId": "101", "tblId": "DT_1JH20202"}],
        "item_keywords": ["전산업생산지수", "계절조정지수"],
        "exclude_keywords": ["원지수", "광공업"],
        "prdSe": "M",
    }
    result = resolver.resolve_series("industrial_production", spec, "key", 1, 1, tmp_path / "cache.json", 2)
    assert result.tblId == "DT_1JH20202"
    assert result.itmId == "T2"
