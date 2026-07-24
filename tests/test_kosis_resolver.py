from src.core.kosis_resolver import resolve_series, SEARCH_URL, DATA_URL


def test_resolve_series_selects_matching_item():
    def fake_get(url, *, params, timeout, retries):
        if url == SEARCH_URL:
            return [{"ORG_ID": "101", "TBL_ID": "T1", "TBL_NM": "근원 소비자물가지수"}]
        if url == DATA_URL:
            return [
                {"ITM_ID": "A", "ITM_NM": "총지수", "DT": "120", "PRD_DE": "202601"},
                {"ITM_ID": "B", "ITM_NM": "농산물 및 석유류 제외지수", "DT": "118", "PRD_DE": "202601"},
            ]
        raise AssertionError(url)

    spec = {
        "search_terms": ["근원물가지수"],
        "include_terms": ["농산물", "석유류", "제외"],
        "exclude_terms": [],
        "period_candidates": ["M"],
    }
    result = resolve_series("key", spec, fake_get, 1, 1)
    assert result is not None
    resolved, rows = result
    assert resolved.itmId == "B"
    assert len(rows) == 1
