from __future__ import annotations

import os
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from src.core.http import get_json
from src.core.io import read_json, write_json
from src.core.result import SourceResult


def _extract_rows(payload: Any) -> list:
    """
    KRX 응답에서 실제 데이터 행을 꺼냅니다.
    일반적으로 OutBlock_1 배열에 데이터가 들어옵니다.
    """
    if not isinstance(payload, dict):
        return []

    rows = payload.get("OutBlock_1", [])
    return rows if isinstance(rows, list) else []


def _collect_latest_available(
    endpoint: str,
    api_key: str,
    timeout: int,
    retries: int,
    lookback_days: int = 10,
) -> tuple[dict, str | None]:
    """
    오늘부터 최대 lookback_days일 전까지 역순으로 조회하여
    실제 데이터가 존재하는 가장 최근 영업일 응답을 반환합니다.
    """
    today = date.today()

    for offset in range(lookback_days + 1):
        target_date = today - timedelta(days=offset)
        bas_dd = target_date.strftime("%Y%m%d")

        payload = get_json(
            endpoint,
            headers={"AUTH_KEY": api_key},
            params={"basDd": bas_dd},
            timeout=timeout,
            retries=retries,
        )

        if _extract_rows(payload):
            return payload, bas_dd

    return {"OutBlock_1": []}, None


def collect(output_dir: Path, timeout: int, retries: int) -> SourceResult:
    api_key = os.getenv("KRX_API_KEY", "").strip()
    config = read_json("config/krx_apis.json")

    configured = [
        (name, value)
        for name, value in config.items()
        if not name.startswith("_")
        and isinstance(value, dict)
        and value.get("enabled")
    ]

    if not api_key:
        return SourceResult(
            "krx",
            "missing_secret",
            "KRX_API_KEY가 없습니다.",
        )

    if not configured:
        return SourceResult(
            "krx",
            "not_configured",
            "config/krx_apis.json에 승인된 실제 endpoint와 API ID를 입력해야 합니다.",
        )

    payloads: dict[str, Any] = {}
    warnings: list[str] = []
    total_rows = 0
    latest_dates: list[str] = []

    for name, item in configured:
        endpoint = str(item.get("endpoint", "")).strip()
        api_id = str(item.get("api_id", "")).strip()

        if not endpoint or not api_id:
            warnings.append(f"{name}: endpoint 또는 api_id 누락")
            continue

        try:
            payload, used_date = _collect_latest_available(
                endpoint=endpoint,
                api_key=api_key,
                timeout=timeout,
                retries=retries,
                lookback_days=10,
            )

            rows = _extract_rows(payload)

            payloads[name] = {
                "api_id": api_id,
                "requested_latest_available_date": used_date,
                "row_count": len(rows),
                "response": payload,
            }

            total_rows += len(rows)

            if used_date:
                latest_dates.append(used_date)
            else:
                warnings.append(
                    f"{name}: 최근 10일 동안 데이터가 있는 영업일을 찾지 못했습니다."
                )

        except Exception as exc:
            warnings.append(f"{name}: {exc}")

    path = output_dir / "raw_krx.json"
    write_json(path, payloads)

    if not payloads:
        return SourceResult(
            "krx",
            "error",
            "KRX 데이터 수집 실패",
            payload_path=str(path),
            warnings=warnings,
        )

    if total_rows == 0:
        return SourceResult(
            "krx",
            "degraded",
            "KRX API 호출은 성공했지만 최근 10일 내 실제 데이터가 없습니다.",
            rows=0,
            latest_observation=max(latest_dates) if latest_dates else None,
            payload_path=str(path),
            warnings=warnings,
        )

    return SourceResult(
        "krx",
        "ok" if not warnings else "degraded",
        "KRX 최근 영업일 데이터를 수집했습니다.",
        rows=total_rows,
        latest_observation=max(latest_dates) if latest_dates else None,
        payload_path=str(path),
        warnings=warnings,
    )
