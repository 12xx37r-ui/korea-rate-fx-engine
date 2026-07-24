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
    objL3: str = ""
    objL4: str = ""
    table_name: str = ""
    item_name: str = ""
    resolved_from: str = "auto"
    cache_version: int = 2

    def to_dict(self) -> dict[str, Any]:
        return {field: getattr(self, field) for field in self.__dataclass_fields__}


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


def _contains(text: str, keyword: str) -> bool:
    token = _norm(keyword)
    return bool(token and token in _norm(text))


def _row_text(row: dict[str, Any]) -> str:
    keys = ["TBL_NM", "STAT_NM", "CONTENTS", "MT_ATITLE", "ITM_NM"]
    keys += [f"C{i}_NM" for i in range(1, 9)]
    return " ".join(str(row.get(k, "")) for k in keys)


def _score_text(text: str, keywords: list[str], exclude: list[str]) -> int:
    score = 0
    for index, keyword in enumerate(keywords):
        if _contains(text, keyword):
            score += max(30 - index * 3, 8)
    for keyword in exclude:
        if _contains(text, keyword):
            score -= 100
    return score


def _score_candidate(row: dict[str, Any], keywords: list[str], exclude: list[str], preferred_org: str = "") -> int:
    score = _score_text(_row_text(row), keywords, exclude)
    if preferred_org and str(row.get("ORG_ID", "")) == preferred_org:
        score += 100
    return score


def _search_tables(api_key: str, query: str, timeout: int, retries: int, result_count: int) -> list[dict[str, Any]]:
    payload = get_json(
        SEARCH_URL,
        params={
            "method": "getList", "apiKey": api_key, "searchNm": query,
            "sort": "RANK", "startCount": "1", "resultCount": str(result_count),
            "format": "json", "jsonVD": "Y",
        },
        timeout=timeout, retries=retries,
    )
    return _as_rows(payload)


def _probe_table(api_key: str, org_id: str, tbl_id: str, prd_se: str, timeout: int, retries: int) -> list[dict[str, Any]]:
    base = {
        "method": "getList", "apiKey": api_key, "orgId": org_id, "tblId": tbl_id,
        "itmId": "ALL", "objL1": "ALL", "prdSe": prd_se, "newEstPrdCnt": "1",
        "format": "json", "jsonVD": "Y",
    }
    errors: list[str] = []
    for depth in range(1, 6):
        params = dict(base)
        for level in range(2, depth + 1):
            params[f"objL{level}"] = "ALL"
        try:
            rows = _as_rows(get_json(DATA_URL, params=params, timeout=timeout, retries=retries))
            if rows:
                return rows
        except Exception as exc:
            errors.append(str(exc))
    raise RuntimeError(errors[-1] if errors else "조회 결과가 없습니다")


def _choose_row(rows: list[dict[str, Any]], item_keywords: list[str], exclude: list[str]) -> dict[str, Any] | None:
    ranked = sorted(rows, key=lambda row: _score_text(_row_text(row), item_keywords, exclude), reverse=True)
    if not ranked:
        return None
    best = ranked[0]
    return best if _score_text(_row_text(best), item_keywords, exclude) > 0 else None


def _choose_item(rows: list[dict[str, Any]], item_keywords: list[str], exclude: list[str]) -> dict[str, Any] | None:
    return _choose_row(rows, item_keywords, exclude)


def _dimension_value(row: dict[str, Any], level: int) -> str:
    value = str(row.get(f"C{level}", "")).strip()
    return value or "ALL"


def _resolved_from_row(org_id: str, tbl_id: str, row: dict[str, Any], prd_se: str, source: str) -> ResolvedSeries:
    dims: dict[str, str] = {}
    for level in range(1, 5):
        key = f"objL{level}"
        dims[key] = _dimension_value(row, level) if row.get(f"C{level}") is not None else ""
    return ResolvedSeries(
        orgId=org_id,
        tblId=tbl_id,
        itmId=str(row.get("ITM_ID", "ALL")) or "ALL",
        objL1=dims["objL1"] or "ALL",
        objL2=dims["objL2"],
        objL3=dims["objL3"],
        objL4=dims["objL4"],
        prdSe=str(row.get("PRD_SE") or prd_se),
        table_name=str(row.get("TBL_NM", "")),
        item_name=" / ".join(filter(None, [str(row.get("ITM_NM", "")), str(row.get("C1_NM", ""))])),
        resolved_from=source,
    )


def resolve_series(name: str, spec: dict[str, Any], api_key: str, timeout: int, retries: int,
                   cache_path: Path = DEFAULT_CACHE_PATH, cache_version: int = 2) -> ResolvedSeries:
    cache = read_json(cache_path) if cache_path.exists() else {}
    cached = cache.get("series", {}).get(name)
    if isinstance(cached, dict) and int(cached.get("cache_version", 0)) == cache_version:
        required = ("orgId", "tblId", "itmId", "objL1", "prdSe")
        if all(cached.get(k) for k in required):
            values = {k: cached.get(k, field.default if field.default is not None else "")
                      for k, field in ResolvedSeries.__dataclass_fields__.items()}
            return ResolvedSeries(**values)

    preferred_prd_se = str(spec.get("prdSe", "M") or "M")
    item_keywords = [str(v) for v in spec.get("item_keywords", [])]
    exclude = [str(v) for v in spec.get("exclude_keywords", [])]

    errors: list[str] = []
    for preferred in spec.get("preferred_tables", []):
        org_id = str(preferred.get("orgId", "")).strip()
        tbl_id = str(preferred.get("tblId", "")).strip()
        if not org_id or not tbl_id:
            continue
        try:
            rows = _probe_table(api_key, org_id, tbl_id, preferred_prd_se, timeout, retries)
            selected = _choose_row(rows, item_keywords, exclude)
            if selected:
                resolved = _resolved_from_row(org_id, tbl_id, selected, preferred_prd_se, "preferred_table")
                _save_cache(cache_path, name, resolved)
                return resolved
            errors.append(f"{tbl_id}: 원하는 항목을 찾지 못함")
        except Exception as exc:
            errors.append(f"{tbl_id}: {exc}")

    search_terms = [str(v).strip() for v in spec.get("search_terms", []) if str(v).strip()]
    table_keywords = [str(v) for v in spec.get("table_keywords", search_terms)]
    preferred_org = str(spec.get("preferred_org_id", "101"))
    candidates: dict[tuple[str, str], dict[str, Any]] = {}
    for term in search_terms:
        for row in _search_tables(api_key, term, timeout, retries, int(spec.get("result_count", 30))):
            org_id, tbl_id = str(row.get("ORG_ID", "")), str(row.get("TBL_ID", ""))
            if org_id and tbl_id:
                candidates[(org_id, tbl_id)] = row

    ranked = sorted(candidates.values(), key=lambda row: _score_candidate(row, table_keywords, exclude, preferred_org), reverse=True)
    for table in ranked[: int(spec.get("probe_limit", 12))]:
        org_id, tbl_id = str(table.get("ORG_ID", "")), str(table.get("TBL_ID", ""))
        if preferred_org and org_id != preferred_org:
            continue
        try:
            rows = _probe_table(api_key, org_id, tbl_id, preferred_prd_se, timeout, retries)
            selected = _choose_row(rows, item_keywords, exclude)
            if selected:
                resolved = _resolved_from_row(org_id, tbl_id, selected, preferred_prd_se, "auto")
                _save_cache(cache_path, name, resolved)
                return resolved
        except Exception as exc:
            errors.append(f"{tbl_id}: {exc}")

    raise RuntimeError(f"{name}: 적합한 국내 KOSIS 통계표 또는 항목을 찾지 못했습니다. {'; '.join(errors[-4:])}")


def _save_cache(path: Path, name: str, resolved: ResolvedSeries) -> None:
    cache = read_json(path) if path.exists() else {}
    cache.setdefault("series", {})[name] = resolved.to_dict()
    cache["updated_at"] = datetime.now(timezone.utc).isoformat()
    write_json(path, cache)
