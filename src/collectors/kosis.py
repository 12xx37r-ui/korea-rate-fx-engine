from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from src.core.http import get_json
from src.core.io import read_json, write_json
from src.core.kosis_resolver import DATA_URL, resolve_series
from src.core.result import SourceResult

CACHE_PATH = Path("cache/kosis_resolved.json")


def _fetch_resolved(key: str, resolved: dict[str, Any], timeout: int, retries: int, count: int) -> list[dict[str, Any]]:
    params = {
        "method": "getList",
        "apiKey": key,
        "orgId": resolved["orgId"],
        "tblId": resolved["tblId"],
        "itmId": resolved["itmId"],
        "objL1": resolved.get("objL1") or "ALL",
        "prdSe": resolved["prdSe"],
        "newEstPrdCnt": str(count),
        "format": "json",
        "jsonVD": "Y",
    }
    for field in ("objL2", "objL3", "objL4", "objL5", "objL6", "objL7", "objL8"):
        if resolved.get(field):
            params[field] = resolved[field]
    payload = get_json(DATA_URL, params=params, timeout=timeout, retries=retries)
    return payload if isinstance(payload, list) else []


def collect(output_dir: Path, timeout: int, retries: int) -> SourceResult:
    key = os.getenv("KOSIS_API_KEY", "").strip()
    config = read_json("config/kosis_series.json")
    series = {name: spec for name, spec in config.get("series", {}).items() if spec.get("enabled")}

    print("[KOSIS] collector started")
    if not key:
        print("[KOSIS] skipped: KOSIS_API_KEY is missing")
        return SourceResult("kosis", "missing_secret", "KOSIS_API_KEY가 없습니다.")
    if not series:
        print("[KOSIS] skipped: no enabled series")
        return SourceResult("kosis", "not_configured", "활성화된 KOSIS 지표가 없습니다.")

    cache: dict[str, Any] = {}
    if CACHE_PATH.exists():
        try:
            cache = read_json(CACHE_PATH)
        except Exception:
            cache = {}

    payloads: dict[str, Any] = {}
    resolved_cache: dict[str, Any] = dict(cache.get("series", {}))
    warnings: list[str] = []
    total_rows = 0

    for name, spec in series.items():
        print(f"[KOSIS] {name}: resolving/fetching")
        count = int(spec.get("newEstPrdCnt", 24))
        resolved = resolved_cache.get(name)

        # Explicit manual codes always take priority.
        if all(str(spec.get(field, "")).strip() for field in ("orgId", "tblId", "itmId")):
            resolved = {
                "orgId": spec["orgId"], "tblId": spec["tblId"], "itmId": spec["itmId"],
                "objL1": spec.get("objL1") or "ALL", "objL2": spec.get("objL2", ""),
                "objL3": spec.get("objL3", ""), "prdSe": spec.get("prdSe", "M"),
                "table_name": "manual configuration", "item_name": "manual configuration",
            }

        rows: list[dict[str, Any]] = []
        if resolved:
            try:
                rows = _fetch_resolved(key, resolved, timeout, retries, count)
                if rows:
                    print(f"[KOSIS] {name}: cache/manual hit {resolved['orgId']}/{resolved['tblId']}/{resolved['itmId']} rows={len(rows)}")
            except Exception as exc:
                print(f"[KOSIS] {name}: cached mapping failed, re-resolving: {exc}")
                rows = []

        if not rows and spec.get("auto_resolve", True):
            try:
                found = resolve_series(key, spec, get_json, timeout, retries)
                if found:
                    resolved_obj, preview_rows = found
                    resolved = resolved_obj.to_dict()
                    resolved_cache[name] = resolved
                    rows = preview_rows
                    print(
                        f"[KOSIS] {name}: resolved {resolved['orgId']}/{resolved['tblId']}/{resolved['itmId']} "
                        f"table={resolved.get('table_name')} item={resolved.get('item_name')} rows={len(rows)}"
                    )
            except Exception as exc:
                warnings.append(f"{name}: 자동탐색 오류: {exc}")

        if not rows:
            warnings.append(f"{name}: 적합한 KOSIS 통계표 또는 데이터를 찾지 못했습니다.")
            print(f"[KOSIS] {name}: no data")
            continue

        payloads[name] = {
            "resolved": resolved,
            "rows": rows,
        }
        total_rows += len(rows)

    write_json(CACHE_PATH, {"schema_version": 1, "series": resolved_cache})
    path = output_dir / "raw_kosis.json"
    write_json(path, payloads)
    print(f"[KOSIS] wrote {path} series={len(payloads)} rows={total_rows}")
    print(f"[KOSIS] wrote {CACHE_PATH}")

    if not payloads:
        return SourceResult(
            "kosis", "error", "KOSIS 데이터 수집 실패",
            payload_path=str(path), warnings=warnings,
            metadata={"cache_path": str(CACHE_PATH)},
        )
    return SourceResult(
        "kosis", "ok" if not warnings else "degraded",
        "KOSIS 지표를 자동 탐색하여 수집했습니다.",
        rows=total_rows, payload_path=str(path), warnings=warnings,
        metadata={"series_count": len(payloads), "cache_path": str(CACHE_PATH)},
    )
