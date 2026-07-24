from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Callable

SEARCH_URL = "https://kosis.kr/openapi/statisticsSearch.do"
DATA_URL = "https://kosis.kr/openapi/Param/statisticsParameterData.do"


@dataclass
class ResolvedSeries:
    orgId: str
    tblId: str
    itmId: str
    prdSe: str
    objL1: str = "ALL"
    objL2: str = ""
    objL3: str = ""
    table_name: str = ""
    item_name: str = ""
    search_term: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in ("result", "data", "rows"):
            value = payload.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
    return []


def _is_error(payload: Any) -> bool:
    if isinstance(payload, dict):
        keys = {str(k).lower() for k in payload}
        if {"err", "errmsg"} & keys or "error" in keys:
            return True
    rows = _rows(payload)
    return bool(rows and any("ERR" in {str(k).upper() for k in row} for row in rows))


def _score(text: str, include: list[str], exclude: list[str]) -> int:
    normalized = text.replace(" ", "").lower()
    score = sum(10 for word in include if word.replace(" ", "").lower() in normalized)
    score -= sum(20 for word in exclude if word.replace(" ", "").lower() in normalized)
    return score


def search_tables(
    api_key: str,
    search_term: str,
    get_json: Callable[..., Any],
    timeout: int,
    retries: int,
    result_count: int = 20,
) -> list[dict[str, Any]]:
    payload = get_json(
        SEARCH_URL,
        params={
            "method": "getList",
            "apiKey": api_key,
            "searchNm": search_term,
            "sort": "RANK",
            "startCount": "1",
            "resultCount": str(result_count),
            "format": "json",
            "content": "json",
        },
        timeout=timeout,
        retries=retries,
    )
    return _rows(payload)


def probe_table(
    api_key: str,
    org_id: str,
    tbl_id: str,
    get_json: Callable[..., Any],
    timeout: int,
    retries: int,
    periods: list[str],
    max_periods: int = 24,
) -> tuple[list[dict[str, Any]], dict[str, str]] | None:
    # KOSIS tables differ in classification depth. Start with objL1=ALL and
    # progressively add objL2/objL3 only when required.
    for prd_se in periods:
        for depth in (1, 2, 3):
            params = {
                "method": "getList",
                "apiKey": api_key,
                "orgId": org_id,
                "tblId": tbl_id,
                "itmId": "ALL",
                "objL1": "ALL",
                "prdSe": prd_se,
                "newEstPrdCnt": str(max_periods),
                "format": "json",
                "jsonVD": "Y",
            }
            if depth >= 2:
                params["objL2"] = "ALL"
            if depth >= 3:
                params["objL3"] = "ALL"
            try:
                payload = get_json(DATA_URL, params=params, timeout=timeout, retries=retries)
            except Exception:
                continue
            rows = _rows(payload)
            if rows and not _is_error(payload) and any(row.get("DT") not in (None, "") for row in rows):
                return rows, {
                    "prdSe": prd_se,
                    "objL1": "ALL",
                    "objL2": "ALL" if depth >= 2 else "",
                    "objL3": "ALL" if depth >= 3 else "",
                }
    return None


def resolve_series(
    api_key: str,
    spec: dict[str, Any],
    get_json: Callable[..., Any],
    timeout: int,
    retries: int,
) -> tuple[ResolvedSeries, list[dict[str, Any]]] | None:
    include = list(spec.get("include_terms", []))
    exclude = list(spec.get("exclude_terms", []))
    periods = list(spec.get("period_candidates", [spec.get("prdSe", "M"), "Q", "Y"]))
    seen: set[tuple[str, str]] = set()
    candidates: list[tuple[int, dict[str, Any], str]] = []

    for search_term in spec.get("search_terms", []):
        for row in search_tables(api_key, search_term, get_json, timeout, retries):
            org_id = str(row.get("ORG_ID", "")).strip()
            tbl_id = str(row.get("TBL_ID", "")).strip()
            if not org_id or not tbl_id or (org_id, tbl_id) in seen:
                continue
            seen.add((org_id, tbl_id))
            text = " ".join(str(row.get(k, "")) for k in ("TBL_NM", "STAT_NM", "CONTENTS", "MT_ATITLE"))
            candidates.append((_score(text, include, exclude), row, search_term))

    for _, row, search_term in sorted(candidates, key=lambda x: x[0], reverse=True):
        org_id = str(row.get("ORG_ID", "")).strip()
        tbl_id = str(row.get("TBL_ID", "")).strip()
        probed = probe_table(api_key, org_id, tbl_id, get_json, timeout, retries, periods)
        if not probed:
            continue
        rows, selection = probed

        item_names: dict[str, str] = {}
        for data_row in rows:
            item_id = str(data_row.get("ITM_ID", "")).strip()
            item_name = str(data_row.get("ITM_NM", "")).strip()
            if item_id:
                item_names[item_id] = item_name
        if not item_names:
            continue

        ranked_items = sorted(
            item_names.items(),
            key=lambda pair: _score(pair[1], include, exclude),
            reverse=True,
        )
        best_item_id, best_item_name = ranked_items[0]
        if include and _score(best_item_name, include, exclude) <= 0:
            # The table can still be exact even when the item itself is named "지수".
            table_text = str(row.get("TBL_NM", ""))
            if _score(table_text, include, exclude) <= 0:
                continue

        resolved = ResolvedSeries(
            orgId=org_id,
            tblId=tbl_id,
            itmId=best_item_id,
            prdSe=selection["prdSe"],
            objL1=selection["objL1"],
            objL2=selection["objL2"],
            objL3=selection["objL3"],
            table_name=str(row.get("TBL_NM", "")),
            item_name=best_item_name,
            search_term=search_term,
        )
        selected_rows = [r for r in rows if str(r.get("ITM_ID", "")) == best_item_id]
        return resolved, selected_rows
    return None
