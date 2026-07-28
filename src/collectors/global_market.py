from __future__ import annotations

import csv
import io
import time
from pathlib import Path
from typing import Any

import requests

from src.core.io import write_json
from src.core.result import SourceResult

FRED = {
    "broad_dollar": "DTWEXBGS",
    "us_2y": "DGS2",
    "us_10y": "DGS10",
    "us_breakeven_10y": "T10YIE",
    "vix": "VIXCLS",
    "hy_oas": "BAMLH0A0HYM2",
    "wti": "DCOILWTICO",
    "commodity_index": "PPIACO",
    "usd_cny": "DEXCHUS",
    "usd_jpy": "DEXJPUS",
    "usd_krw_fred": "DEXKOUS",
}

# 글로벌 수집기는 보조 입력이다. 한 항목의 지연 때문에 전체 엔진이
# 멈추지 않도록 요청·전체 실행 시간을 강제로 제한한다.
CONNECT_TIMEOUT_SECONDS = 5
READ_TIMEOUT_SECONDS = 12
MAX_SERIES_RETRIES = 1
TOTAL_DEADLINE_SECONDS = 150


def _fred_csv(session: requests.Session, series_id: str) -> list[dict[str, Any]]:
    url = "https://fred.stlouisfed.org/graph/fredgraph.csv"
    response = session.get(
        url,
        params={"id": series_id, "cosd": "2006-01-01"},
        timeout=(CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS),
    )
    response.raise_for_status()

    rows: list[dict[str, Any]] = []
    for row in csv.DictReader(io.StringIO(response.text)):
        value = row.get(series_id)
        if not value or value == ".":
            continue
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            continue
        rows.append(
            {
                "date": str(row.get("DATE", "")).replace("-", ""),
                "value": numeric,
                "source": "FRED",
                "series_id": series_id,
            }
        )
    return rows[-5000:]


def collect(output_dir: Path, timeout: int, retries: int) -> SourceResult:
    del timeout, retries  # 전역 설정과 무관하게 이 수집기는 자체 안전 제한을 사용한다.

    payload: dict[str, list[dict[str, Any]]] = {}
    errors: dict[str, str] = {}
    started = time.monotonic()

    session = requests.Session()
    session.headers.update({"User-Agent": "korea-rate-fx-engine/3.0"})

    try:
        for index, (key, series_id) in enumerate(FRED.items(), start=1):
            if time.monotonic() - started >= TOTAL_DEADLINE_SECONDS:
                errors[key] = "전체 수집 제한시간 초과로 건너뜀"
                payload[key] = []
                print(
                    f"[GLOBAL_MARKET] {key}: skipped | total deadline reached",
                    flush=True,
                )
                continue

            print(
                f"[GLOBAL_MARKET] {index}/{len(FRED)} {key}: fetching",
                flush=True,
            )

            last_error: Exception | None = None
            series_started = time.monotonic()
            for attempt in range(MAX_SERIES_RETRIES + 1):
                try:
                    rows = _fred_csv(session, series_id)
                    payload[key] = rows
                    elapsed = time.monotonic() - series_started
                    print(
                        f"[GLOBAL_MARKET] {key}: rows={len(rows)} | elapsed={elapsed:.1f}s",
                        flush=True,
                    )
                    last_error = None
                    break
                except (requests.RequestException, ValueError, csv.Error) as exc:
                    last_error = exc
                    if attempt < MAX_SERIES_RETRIES:
                        time.sleep(1.0)

            if last_error is not None:
                payload[key] = []
                errors[key] = f"{type(last_error).__name__}: {last_error}"
                elapsed = time.monotonic() - series_started
                print(
                    f"[GLOBAL_MARKET] {key}: failed and skipped | elapsed={elapsed:.1f}s",
                    flush=True,
                )
    finally:
        session.close()

    path = output_dir / "raw_global_market.json"
    write_json(path, payload)

    ok = sum(bool(values) for values in payload.values())
    status = "ok" if ok >= 8 else ("degraded" if ok >= 4 else "error")
    return SourceResult(
        source="GLOBAL_MARKET",
        status=status,
        message=f"{ok}/{len(FRED)} public series collected",
        payload_path=str(path),
        metadata={
            "series_ok": ok,
            "series_total": len(FRED),
            "errors": errors,
            "credential_status": "not_required",
            "action_required": False,
            "connect_timeout_seconds": CONNECT_TIMEOUT_SECONDS,
            "read_timeout_seconds": READ_TIMEOUT_SECONDS,
            "total_deadline_seconds": TOTAL_DEADLINE_SECONDS,
        },
    )
