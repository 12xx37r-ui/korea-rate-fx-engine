from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from src.core.http import get_json
from src.core.io import read_json, write_json
from src.core.kosis_resolver import DATA_URL, resolve_series
from src.core.result import SourceResult


def _validate_payload(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict) and payload.get("err"):
        raise RuntimeError(f"KOSIS 오류 {payload.get('err')}: {payload.get('errMsg', '')}")
    if not isinstance(payload, list):
        raise RuntimeError("KOSIS 응답 형식이 배열이 아닙니다.")
    return [row for row in payload if isinstance(row, dict)]


def _latest(rows: list[dict[str, Any]]) -> str | None:
    values = [str(row.get("PRD_DE", "")).strip() for row in rows]
    values = [value for value in values if value]
    return max(values) if values else None


def collect(output_dir: Path, timeout: int, retries: int) -> SourceResult:
    print("[KOSIS] collector started")
    key = os.getenv("KOSIS_API_KEY", "").strip()
    config = read_json("config/kosis_series.json")
    cache_version = int(config.get("cache_version", 2))
    series = {name: spec for name, spec in config.get("series", {}).items() if spec.get("enabled")}

    if not key:
        return SourceResult("kosis", "missing_secret", "GitHub Secret KOSIS_API_KEY가 없습니다.")
    if not series:
        return SourceResult("kosis", "not_configured", "config/kosis_series.json에서 지표를 enabled=true로 설정해야 합니다.")

    payloads: dict[str, Any] = {}
    resolutions: dict[str, Any] = {}
    warnings: list[str] = []

    for name, spec in series.items():
        print(f"[KOSIS] {name}: resolving and fetching")
        try:
            resolved = resolve_series(name, spec, key, timeout, retries, cache_version=cache_version)
            params = {
                "method": "getList", "apiKey": key, "orgId": resolved.orgId,
                "tblId": resolved.tblId, "itmId": resolved.itmId,
                "objL1": resolved.objL1, "prdSe": resolved.prdSe,
                "newEstPrdCnt": str(spec.get("newEstPrdCnt", 24)),
                "format": "json", "jsonVD": "Y",
            }
            for level in range(2, 5):
                value = getattr(resolved, f"objL{level}")
                if value:
                    params[f"objL{level}"] = value
            rows = _validate_payload(get_json(DATA_URL, params=params, timeout=timeout, retries=retries))
            if not rows:
                raise RuntimeError("조회 결과가 0건입니다.")
            payloads[name] = rows
            resolutions[name] = resolved.to_dict()
            print(f"[KOSIS] {name}: table={resolved.tblId} item={resolved.item_name} rows={len(rows)} latest={_latest(rows)}")
        except Exception as exc:
            warnings.append(f"{name}: {exc}")
            print(f"[KOSIS] {name}: failed: {exc}")

    data_path = output_dir / "raw_kosis.json"
    resolution_path = output_dir / "kosis_resolution.json"
    write_json(data_path, payloads)
    write_json(resolution_path, resolutions)

    all_rows = [row for rows in payloads.values() for row in rows]
    latest = _latest(all_rows)
    if not payloads:
        return SourceResult("kosis", "error", "KOSIS 데이터 수집 실패", payload_path=str(data_path), warnings=warnings)
    return SourceResult(
        "kosis", "ok" if not warnings else "degraded",
        "KOSIS 국내 공식 지표 수집 완료",
        rows=len(all_rows), latest_observation=latest,
        payload_path=str(data_path), warnings=warnings,
        metadata={"resolution_path": str(resolution_path), "cache_version": cache_version},
    )
