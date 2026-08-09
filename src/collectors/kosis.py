from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from src.core.http import get_json
from src.core.credentials import credential_issue, credential_metadata
from src.core.io import read_json, write_json
from src.core.kosis_resolver import DATA_URL, ResolvedSeries, resolve_series
from src.core.result import SourceResult

FETCH_TIMEOUT_SECONDS = 4
FETCH_RETRIES = 1
INCREMENTAL_MONTHS = 18


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


def _merge_rows(previous: list[dict[str, Any]] | None, fresh: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for row in previous or []:
        if isinstance(row, dict):
            key = str(row.get("PRD_DE") or row.get("TIME") or "").strip()
            if key:
                merged[key] = row
    for row in fresh or []:
        if isinstance(row, dict):
            key = str(row.get("PRD_DE") or row.get("TIME") or "").strip()
            if key:
                merged[key] = row
    return [merged[key] for key in sorted(merged)]


def _is_transport_failure(exc: Exception | str) -> bool:
    text = str(exc or "").lower()
    markers = (
        "connecttimeouterror", "readtimeout", "read timed out", "connect timeout",
        "connection to", "connectionerror", "max retries exceeded", "name resolution",
        "temporary failure", "network is unreachable",
    )
    return any(marker in text for marker in markers)


def _cached_resolution(name: str, cache_version: int) -> ResolvedSeries | None:
    try:
        cache = read_json("cache/kosis_resolved.json")
        row = (cache.get("series") or {}).get(name) if isinstance(cache, dict) else None
        if not isinstance(row, dict) or int(row.get("cache_version", 0)) != cache_version:
            return None
        required = ("orgId", "tblId", "itmId", "objL1", "prdSe")
        if not all(row.get(k) for k in required):
            return None
        values = {
            key: row.get(key, field.default if field.default is not None else "")
            for key, field in ResolvedSeries.__dataclass_fields__.items()
        }
        return ResolvedSeries(**values)
    except Exception:
        return None


def collect(output_dir: Path, timeout: int, retries: int) -> SourceResult:
    # KOSIS is monthly and must not hold the daily forecast pipeline for minutes.
    # The caller's long timeout/retry policy is intentionally replaced by a short,
    # one-attempt policy plus last-good continuity.
    del timeout, retries

    print("[KOSIS] collector started", flush=True)
    key = os.getenv("KOSIS_API_KEY", "").strip()
    config = read_json("config/kosis_series.json")
    cache_version = int(config.get("cache_version", 2))
    series = {name: spec for name, spec in config.get("series", {}).items() if spec.get("enabled")}

    if not key:
        return SourceResult(
            "kosis", "missing_secret", "GitHub Secret KOSIS_API_KEY가 없습니다.",
            metadata=credential_metadata("KOSIS_API_KEY", "missing", "API 키를 등록해야 합니다."),
        )
    if not series:
        return SourceResult("kosis", "not_configured", "config/kosis_series.json에서 지표를 enabled=true로 설정해야 합니다.")

    data_path = output_dir / "raw_kosis.json"
    resolution_path = output_dir / "kosis_resolution.json"
    try:
        previous = read_json(data_path) if data_path.exists() else {}
        previous = previous if isinstance(previous, dict) else {}
    except Exception:
        previous = {}

    payloads: dict[str, Any] = {}
    resolutions: dict[str, Any] = {}
    warnings: list[str] = []
    auth_failures: list[str] = []
    last_good_reused: list[str] = []
    external_requests = 0
    circuit_open = False
    circuit_reason = ""

    for name, spec in series.items():
        old_rows = previous.get(name) if isinstance(previous.get(name), list) else []
        if circuit_open:
            payloads[name] = list(old_rows)
            if old_rows:
                last_good_reused.append(name)
            warnings.append(f"{name}: 외부호출 생략 · circuit open · 직전 정상 이력 유지={bool(old_rows)}")
            print(f"[KOSIS] {name}: skipped external call | circuit open; last-good={bool(old_rows)}", flush=True)
            continue

        print(f"[KOSIS] {name}: fetching", flush=True)
        try:
            resolved = _cached_resolution(name, cache_version)
            if resolved is None:
                # Resolver is only used when the committed local cache is missing.
                # It receives the same fast-fail policy so discovery can never block
                # the whole workflow for minutes.
                resolved = resolve_series(
                    name, spec, key, FETCH_TIMEOUT_SECONDS, FETCH_RETRIES,
                    cache_version=cache_version,
                )
            params = {
                "method": "getList", "apiKey": key, "orgId": resolved.orgId,
                "tblId": resolved.tblId, "itmId": resolved.itmId,
                "objL1": resolved.objL1, "prdSe": resolved.prdSe,
                # With committed history, fetch only an overlap window and merge it
                # locally.  First bootstrap keeps the configured long history.
                "newEstPrdCnt": str(INCREMENTAL_MONTHS if old_rows else spec.get("newEstPrdCnt", 24)),
                "format": "json", "jsonVD": "Y",
            }
            for level in range(2, 5):
                value = getattr(resolved, f"objL{level}")
                if value:
                    params[f"objL{level}"] = value

            external_requests += 1
            rows = _validate_payload(
                get_json(DATA_URL, params=params, timeout=FETCH_TIMEOUT_SECONDS, retries=FETCH_RETRIES)
            )
            if not rows:
                raise RuntimeError("조회 결과가 0건입니다.")
            merged = _merge_rows(old_rows, rows)
            payloads[name] = merged
            resolutions[name] = resolved.to_dict()
            print(
                f"[KOSIS] {name}: table={resolved.tblId} item={resolved.item_name} "
                f"rows={len(merged)} fresh={len(rows)} latest={_latest(merged)} "
                f"mode={'incremental' if old_rows else 'bootstrap'}",
                flush=True,
            )
        except Exception as exc:
            detail = f"{name}: {exc}"
            warnings.append(detail)
            is_auth = credential_issue(exc)
            is_network = _is_transport_failure(exc)
            if is_auth:
                auth_failures.append(detail)
            if old_rows:
                payloads[name] = list(old_rows)
                last_good_reused.append(name)
            print(f"[KOSIS] {name}: failed: {exc}; 직전 정상 이력 유지={bool(old_rows)}", flush=True)

            # One KOSIS transport/auth failure means the next call is overwhelmingly
            # likely to fail too.  Open the circuit immediately; do not spend another
            # 30~90 seconds on the second monthly series.
            if is_network or is_auth:
                circuit_open = True
                circuit_reason = "network_timeout" if is_network else "credential"
                print(f"[KOSIS] circuit breaker opened | reason={circuit_reason}", flush=True)

    write_json(data_path, payloads)
    write_json(resolution_path, resolutions)

    all_rows = [row for rows in payloads.values() if isinstance(rows, list) for row in rows]
    latest = _latest(all_rows)
    usable = sum(bool(payloads.get(name)) for name in series)
    fresh_success = usable - len(set(last_good_reused))

    if not payloads or not usable:
        if auth_failures:
            return SourceResult(
                "kosis", "credential_error",
                "KOSIS API 키가 만료되었거나 유효하지 않습니다. 새 키로 갱신해야 합니다.",
                payload_path=str(data_path), warnings=warnings,
                metadata={
                    **credential_metadata("KOSIS_API_KEY", "renewal_required", auth_failures[0]),
                    "external_request_count": external_requests,
                    "circuit_open": circuit_open,
                    "circuit_reason": circuit_reason,
                },
            )
        return SourceResult(
            "kosis", "error", "KOSIS 데이터 수집 실패", payload_path=str(data_path), warnings=warnings,
            metadata={
                **credential_metadata("KOSIS_API_KEY", "unknown_error"),
                "external_request_count": external_requests,
                "circuit_open": circuit_open,
                "circuit_reason": circuit_reason,
            },
        )

    status = "ok" if not warnings else "degraded"
    return SourceResult(
        "kosis", status,
        f"KOSIS 국내 공식 지표 사용 가능 {usable}/{len(series)} · 신규성공 {fresh_success}/{len(series)}",
        rows=len(all_rows), latest_observation=latest,
        payload_path=str(data_path), warnings=warnings,
        metadata={
            "resolution_path": str(resolution_path),
            "cache_version": cache_version,
            "external_request_count": external_requests,
            "max_external_requests": len(series),
            "circuit_open": circuit_open,
            "circuit_reason": circuit_reason,
            "last_good_reused": sorted(set(last_good_reused)),
            **credential_metadata("KOSIS_API_KEY", "valid"),
        },
    )
