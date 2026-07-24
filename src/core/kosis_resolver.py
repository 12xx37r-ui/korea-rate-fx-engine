from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.core.http import get_json
from src.core.io import read_json, write_json

SEARCH_URL = "https://kosis.kr/openapi/statisticsSearch.do"
DATA_URL = "https://kosis.kr/openapi/Param/statisticsParameterData.do"
DEFAULT_CACHE_PATH = Path("cache/kosis_resolved.json")


@dataclass(frozen=True)
class ResolvedSeries:
    orgId: str
    tblId: str
    itmId: str
    objL1: str
    prdSe: str
    objL2: str = ""
    table_name: str = ""
    item_name: str = ""
    resolved_from: str = "auto"

    def to_dict(self) -> dict[str, str]:
        return {
            "orgId": self.orgId,
            "tblId": self.tblId,
            "itmId": self.itmId,
            "objL1": self.objL1,
            "objL2": self.objL2,
            "prdSe": self.prdSe,
            "table_name": self.table_name,
            "item_name": self.item_name,
            "resolved_from": self.resolved_from,
        }


def _as_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        if payload.get("err"):
            raise RuntimeError(f"KOSIS 오류 {payload.get('err')}: {payload.get('errMsg', '')}")
        for key in ("result", "data", "rows"):
            value = payload.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
    return []


def _norm(text: Any) -> str:
    return "".join(str(text or "").lower().split())


def _score_candidate(row: dict[str, Any], keywords: list[str], exclude: list[str]) -> int:
    haystack = _norm(" ".join(str(row.get(k, "")) for k in ("TBL_NM", "STAT_NM", "CONTENTS", "MT_ATITLE")))
    score = 0
    for index, keyword in enumerate(keywords):
        token = _norm(keyword)
        if token and token in haystack:
            score += max(10 - index, 2)
    for keyword in exclude:
        token = _norm(keyword)
        if token and token in haystack:
            score -= 20
    if str(row.get("REC_TBL_SE", "")).upper() in {"Y", "1"}:
        score += 2
    return score


def _search_tables(api_key: str, query: str, timeout: int, retries: int, result_count: int) -> list[dict[str, Any]]:
    payload = get_json(
        SEARCH_URL,
        params={
            "method": "getList",
            "apiKey": api_key,
            "searchNm": query,
            "sort": "RANK",
            "startCount": "1",
            "resultCount": str(result_count),
            "format": "json",
            "jsonVD": "Y",
        },
        timeout=timeout,
        retries=retries,
    )
    return _as_rows(payload)


def _probe_table(
    api_key: str,
    org_id: str,
    tbl_id: str,
    prd_se: str,
    timeout: int,
    retries: int,
) -> list[dict[str, Any]]:
    base = {
        "method": "getList",
        "apiKey": api_key,
        "orgId": org_id,
        "tblId": tbl_id,
        "itmId": "ALL",
        "objL1": "ALL",
        "prdSe": prd_se,
        "newEstPrdCnt": "1",
        "format": "json",
        "jsonVD": "Y",
    }
    last_error: Exception | None = None
    # 일부 표는 objL2, objL3도 필수다. ALL을 단계적으로 추가해 탐색한다.
    for depth in range(1, 5):
        params = dict(base)
        for level in range(2, depth + 1):
            params[f"objL{level}"] = "ALL"
        try:
            rows = _as_rows(get_json(DATA_URL, params=params, timeout=timeout, retries=retries))
            if rows:
                return rows
        except Exception as exc:  # 다음 깊이로 재시도
            last_error = exc
    if last_error:
        raise last_error
    return []


def _choose_item(rows: list[dict[str, Any]], item_keywords: list[str], exclude: list[str]) -> dict[str, Any] | None:
    best: tuple[int, dict[str, Any]] | None = None
    for row in rows:
        item_name = _norm(row.get("ITM_NM"))
        score = 0
        for index, keyword in enumerate(item_keywords):
            token = _norm(keyword)
            if token and token in item_name:
                score += max(10 - index, 2)
        for keyword in exclude:
            token = _norm(keyword)
            if token and token in item_name:
                score -= 20
        if not item_keywords:
            score = 1
        if best is None or score > best[0]:
            best = (score, row)
    if best and best[0] > 0:
        return best[1]
    return None


def _dimension_value(row: dict[str, Any], level: int) -> str:
    value = str(row.get(f"C{level}", "")).strip()
    return value or "ALL"


def resolve_series(
    name: str,
    spec: dict[str, Any],
    api_key: str,
    timeout: int,
    retries: int,
    cache_path: Path = DEFAULT_CACHE_PATH,
) -> ResolvedSeries:
    cache = read_json(cache_path) if cache_path.exists() else {}
    cached = cache.get("series", {}).get(name)
    if isinstance(cached, dict) and all(cached.get(k) for k in ("orgId", "tblId", "itmId", "objL1", "prdSe")):
        return ResolvedSeries(**{k: cached.get(k, "") for k in ResolvedSeries.__dataclass_fields__})

    # 수동 코드가 완성되어 있으면 자동탐색보다 우선한다.
    if all(str(spec.get(k, "")).strip() for k in ("orgId", "tblId", "itmId", "objL1", "prdSe")):
        resolved = ResolvedSeries(
            orgId=str(spec["orgId"]), tblId=str(spec["tblId"]), itmId=str(spec["itmId"]),
            objL1=str(spec["objL1"]), objL2=str(spec.get("objL2", "")), prdSe=str(spec["prdSe"]),
            resolved_from="manual",
        )
        _save_cache(cache_path, name, resolved)
        return resolved

    search_terms = [str(v).strip() for v in spec.get("search_terms", []) if str(v).strip()]
    if not search_terms:
        raise ValueError(f"{name}: search_terms가 없습니다.")
    table_keywords = [str(v) for v in spec.get("table_keywords", search_terms)]
    item_keywords = [str(v) for v in spec.get("item_keywords", [])]
    exclude = [str(v) for v in spec.get("exclude_keywords", [])]
    preferred_prd_se = str(spec.get("prdSe", "M") or "M")
    result_count = int(spec.get("result_count", 20))

    candidates: dict[tuple[str, str], dict[str, Any]] = {}
    for term in search_terms:
        for row in _search_tables(api_key, term, timeout, retries, result_count):
            org_id, tbl_id = str(row.get("ORG_ID", "")), str(row.get("TBL_ID", ""))
            if org_id and tbl_id:
                candidates[(org_id, tbl_id)] = row

    ranked = sorted(candidates.values(), key=lambda row: _score_candidate(row, table_keywords, exclude), reverse=True)
    errors: list[str] = []
    for table in ranked[: int(spec.get("probe_limit", 8))]:
        org_id, tbl_id = str(table.get("ORG_ID", "")), str(table.get("TBL_ID", ""))
        try:
            rows = _probe_table(api_key, org_id, tbl_id, preferred_prd_se, timeout, retries)
        except Exception as exc:
            errors.append(f"{tbl_id}: {exc}")
            continue
        selected = _choose_item(rows, item_keywords, exclude)
        if not selected:
            continue
        actual_prd_se = str(selected.get("PRD_SE") or preferred_prd_se)
        resolved = ResolvedSeries(
            orgId=org_id,
            tblId=tbl_id,
            itmId=str(selected.get("ITM_ID", "ALL")) or "ALL",
            objL1=_dimension_value(selected, 1),
            objL2=_dimension_value(selected, 2) if selected.get("C2") is not None else "",
            prdSe=actual_prd_se,
            table_name=str(selected.get("TBL_NM") or table.get("TBL_NM", "")),
            item_name=str(selected.get("ITM_NM", "")),
            resolved_from="auto",
        )
        _save_cache(cache_path, name, resolved)
        return resolved

    detail = "; ".join(errors[-3:])
    raise RuntimeError(f"{name}: 적합한 KOSIS 통계표/항목을 자동 탐색하지 못했습니다. {detail}".strip())


def _save_cache(path: Path, name: str, resolved: ResolvedSeries) -> None:
    cache = read_json(path) if path.exists() else {}
    cache.setdefault("series", {})[name] = resolved.to_dict()
    cache["updated_at"] = datetime.now(timezone.utc).isoformat()
    write_json(path, cache)
