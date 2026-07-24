from __future__ import annotations

import os
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

from src.core.ecos_resolver import EcosResolver
from src.core.http import get_json
from src.core.io import read_json, write_json
from src.core.result import SourceResult

BASE = "https://ecos.bok.or.kr/api/StatisticSearch"


def _rows(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    node = payload.get("StatisticSearch")
    if not isinstance(node, dict):
        result = payload.get("RESULT")
        if isinstance(result, dict):
            raise RuntimeError(f"ECOS 오류 {result.get('CODE')}: {result.get('MESSAGE')}")
        return []
    result = node.get("RESULT")
    if isinstance(result, dict) and str(result.get("CODE", "")).upper() not in {"INFO-000", ""}:
        raise RuntimeError(f"ECOS 오류 {result.get('CODE')}: {result.get('MESSAGE')}")
    rows = node.get("row", [])
    return rows if isinstance(rows, list) else []


def _fetch(key: str, resolution, start_text: str, end_text: str, timeout: int, retries: int) -> list[dict[str, Any]]:
    parts = [
        BASE, key, "json", "kr", "1", "1000", resolution.stat_code,
        resolution.cycle, start_text, end_text, resolution.item_code1,
        resolution.item_code2 or "?", resolution.item_code3 or "?",
    ]
    url = "/".join(quote(str(x).strip("/"), safe="?:") for x in parts)
    return _rows(get_json(url, timeout=timeout, retries=retries))


def collect(output_dir: Path, timeout: int, retries: int) -> SourceResult:
    print("[ECOS] collector started")
    key = os.getenv("ECOS_API_KEY", "").strip()
    config = read_json("config/ecos_series.json")
    series = {n: v for n, v in config.get("series", {}).items() if v.get("enabled")}

    if not key:
        return SourceResult("ecos", "missing_secret", "ECOS_API_KEY가 없습니다.")
    if not series:
        return SourceResult("ecos", "not_configured", "config/ecos_series.json에 활성 지표가 없습니다.")

    resolver = EcosResolver(key, timeout, retries)
    payloads: dict[str, list[dict[str, Any]]] = {}
    resolution_log: dict[str, Any] = {}
    warnings: list[str] = []
    latest_observation: str | None = None

    for name, item in series.items():
        print(f"[ECOS] {name}: resolving and fetching")
        try:
            resolved = resolver.resolve(name, item)
            end = date.today()
            start = end - timedelta(days=int(item.get("lookback_days", 400)))
            if resolved.cycle == "D":
                start_text, end_text = start.strftime("%Y%m%d"), end.strftime("%Y%m%d")
            elif resolved.cycle == "M":
                start_text, end_text = start.strftime("%Y%m"), end.strftime("%Y%m")
            else:
                start_text, end_text = start.strftime("%Y"), end.strftime("%Y")
            rows = _fetch(key, resolved, start_text, end_text, timeout, retries)
            if not rows:
                # Refresh stale cache once.
                resolved = resolver.resolve(name, item, force=True)
                rows = _fetch(key, resolved, start_text, end_text, timeout, retries)
            if not rows:
                raise RuntimeError("데이터가 없습니다.")
            rows.sort(key=lambda r: str(r.get("TIME", "")))
            payloads[name] = rows
            resolution_log[name] = resolved.to_dict()
            latest = str(rows[-1].get("TIME", "")) or None
            if latest and (latest_observation is None or latest > latest_observation):
                latest_observation = latest
            print(f"[ECOS] {name}: table={resolved.stat_code} item={resolved.item_name1} rows={len(rows)} latest={latest}")
        except Exception as exc:
            warnings.append(f"{name}: {exc}")
            print(f"[ECOS] {name}: failed: {exc}")

    resolver.save()
    path = output_dir / "raw_ecos.json"
    resolution_path = output_dir / "ecos_resolution.json"
    write_json(path, payloads)
    write_json(resolution_path, resolution_log)
    total_rows = sum(len(v) for v in payloads.values())
    if not payloads:
        return SourceResult("ecos", "error", "ECOS 데이터 수집 실패", payload_path=str(path), warnings=warnings)
    return SourceResult(
        "ecos", "ok" if not warnings else "degraded", "ECOS 국내 금리·환율 수집 완료",
        rows=total_rows, latest_observation=latest_observation, payload_path=str(path), warnings=warnings,
        metadata={"resolution_path": str(resolution_path), "cache_version": 1},
    )
