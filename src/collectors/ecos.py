from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

from src.core.credentials import credential_issue, credential_metadata
from src.core.ecos_resolver import EcosResolution, EcosResolver
from src.core.http import get_json
from src.core.io import read_json, write_json
from src.core.result import SourceResult

BASE = "https://ecos.bok.or.kr/api/StatisticSearch"
PAGE_SIZE = 1000
DAILY_OVERLAP_DAYS = 21
MONTHLY_OVERLAP_MONTHS = 4

# ECOS is a required official source, but GitHub-hosted runners can occasionally
# fail to establish a connection to ecos.bok.or.kr.  One unavailable host must
# never create N x 30-second sequential waits.  The first transport failure opens
# a per-run circuit breaker; committed last-good official history is then reused.
ECOS_CONNECT_TIMEOUT_SECONDS = 4
ECOS_READ_TIMEOUT_SECONDS = 10
ECOS_REQUEST_RETRIES = 1
ECOS_TRANSPORT_FAILURES_BEFORE_CIRCUIT = 3


def _is_transport_failure(exc: Exception) -> bool:
    text = str(exc).lower()
    needles = (
        "timed out", "timeout", "connection", "connecttimeout", "readtimeout",
        "name resolution", "dns", "ssl", "proxy", "network is unreachable",
        "temporarily unavailable", "remote end closed",
    )
    return any(token in text for token in needles)


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


def _fetch_page(
    key: str,
    resolution: EcosResolution,
    start_text: str,
    end_text: str,
    start_row: int,
    end_row: int,
    timeout: int | tuple[float, float],
    retries: int,
) -> list[dict[str, Any]]:
    parts = [
        key,
        "json",
        "kr",
        str(start_row),
        str(end_row),
        resolution.stat_code,
        resolution.cycle,
        start_text,
        end_text,
        resolution.item_code1,
        resolution.item_code2 or "?",
        resolution.item_code3 or "?",
    ]
    encoded = [quote(str(x).strip("/"), safe="?") for x in parts]
    url = BASE.rstrip("/") + "/" + "/".join(encoded)
    return _rows(get_json(url, timeout=timeout, retries=retries))


def _fetch_all_items_page(
    key: str,
    stat_code: str,
    cycle: str,
    start_text: str,
    end_text: str,
    start_row: int,
    end_row: int,
    timeout: int | tuple[float, float],
    retries: int,
) -> list[dict[str, Any]]:
    """Fetch a whole ECOS table slice without an item selector.

    Some compact tables, notably 732Y001 (foreign reserves), expose generic item names
    such as '합계' rather than repeating the table title as an item.  Fetching the small
    table once and filtering locally is both more reliable and cheaper than repeatedly
    searching metadata for an item code.
    """
    parts = [
        key,
        "json",
        "kr",
        str(start_row),
        str(end_row),
        stat_code,
        cycle,
        start_text,
        end_text,
    ]
    encoded = [quote(str(x).strip("/"), safe="?") for x in parts]
    url = BASE.rstrip("/") + "/" + "/".join(encoded)
    return _rows(get_json(url, timeout=timeout, retries=retries))


def _dedup_identity(row: dict[str, Any]) -> str:
    return "|".join(
        [
            str(row.get("TIME", "")),
            str(row.get("ITEM_CODE1", "")),
            str(row.get("ITEM_CODE2", "")),
            str(row.get("ITEM_CODE3", "")),
            str(row.get("ITEM_NAME1", "")),
        ]
    )


def _fetch(
    key: str,
    resolution: EcosResolution,
    start_text: str,
    end_text: str,
    timeout: int | tuple[float, float],
    retries: int,
) -> list[dict[str, Any]]:
    start_row = 1
    all_rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for _ in range(12):
        rows = _fetch_page(
            key,
            resolution,
            start_text,
            end_text,
            start_row,
            start_row + PAGE_SIZE - 1,
            timeout,
            retries,
        )
        if not rows:
            break
        for row in rows:
            identity = _dedup_identity(row)
            if identity not in seen:
                seen.add(identity)
                all_rows.append(row)
        if len(rows) < PAGE_SIZE:
            break
        start_row += PAGE_SIZE
    return all_rows


def _fetch_all_items(
    key: str,
    stat_code: str,
    cycle: str,
    start_text: str,
    end_text: str,
    timeout: int | tuple[float, float],
    retries: int,
) -> list[dict[str, Any]]:
    start_row = 1
    all_rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for _ in range(12):
        rows = _fetch_all_items_page(
            key,
            stat_code,
            cycle,
            start_text,
            end_text,
            start_row,
            start_row + PAGE_SIZE - 1,
            timeout,
            retries,
        )
        if not rows:
            break
        for row in rows:
            identity = _dedup_identity(row)
            if identity not in seen:
                seen.add(identity)
                all_rows.append(row)
        if len(rows) < PAGE_SIZE:
            break
        start_row += PAGE_SIZE
    return all_rows


def _norm(value: Any) -> str:
    return "".join(ch for ch in str(value or "").strip().lower() if ch.isalnum() or "가" <= ch <= "힣")


def _filter_item_rows(rows: list[dict[str, Any]], wanted_name: str) -> list[dict[str, Any]]:
    wanted = _norm(wanted_name)
    if not wanted:
        return rows
    exact = [row for row in rows if _norm(row.get("ITEM_NAME1")) == wanted]
    if exact:
        return exact
    return [row for row in rows if wanted in _norm(row.get("ITEM_NAME1"))]


def _latest_period(rows: list[dict[str, Any]] | None) -> str | None:
    periods = [str(row.get("TIME", "")) for row in rows or [] if isinstance(row, dict) and row.get("TIME")]
    return max(periods) if periods else None


def _month_shift(year: int, month: int, delta: int) -> tuple[int, int]:
    total = year * 12 + (month - 1) + delta
    return total // 12, total % 12 + 1


def _incremental_start(previous_rows: list[dict[str, Any]] | None, cycle: str, fallback_start: date) -> str:
    latest = _latest_period(previous_rows)
    if not latest:
        if cycle == "D":
            return fallback_start.strftime("%Y%m%d")
        if cycle == "M":
            return fallback_start.strftime("%Y%m")
        return fallback_start.strftime("%Y")

    try:
        if cycle == "D" and len(latest) >= 8:
            dt = datetime.strptime(latest[:8], "%Y%m%d").date() - timedelta(days=DAILY_OVERLAP_DAYS)
            return dt.strftime("%Y%m%d")
        if cycle == "M" and len(latest) >= 6:
            y, m = int(latest[:4]), int(latest[4:6])
            y, m = _month_shift(y, m, -MONTHLY_OVERLAP_MONTHS)
            return f"{y:04d}{m:02d}"
        if len(latest) >= 4:
            return f"{max(1900, int(latest[:4]) - 2):04d}"
    except (TypeError, ValueError):
        pass

    if cycle == "D":
        return fallback_start.strftime("%Y%m%d")
    if cycle == "M":
        return fallback_start.strftime("%Y%m")
    return fallback_start.strftime("%Y")


def _merge_rows(previous_rows: list[dict[str, Any]] | None, fresh_rows: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Fresh rows replace revised periods while preserving the committed history."""
    merged: dict[str, dict[str, Any]] = {}
    for row in previous_rows or []:
        if isinstance(row, dict):
            merged[_dedup_identity(row)] = row
    for row in fresh_rows or []:
        if isinstance(row, dict):
            merged[_dedup_identity(row)] = row
    return sorted(merged.values(), key=lambda r: (str(r.get("TIME", "")), str(r.get("ITEM_CODE1", ""))))


def collect(output_dir: Path, timeout: int, retries: int) -> SourceResult:
    print("[ECOS] collector started")
    key = os.getenv("ECOS_API_KEY", "").strip()
    config = read_json("config/ecos_series.json")
    series = {n: v for n, v in config.get("series", {}).items() if v.get("enabled")}

    if not key:
        return SourceResult(
            "ecos",
            "missing_secret",
            "ECOS_API_KEY가 없습니다.",
            metadata=credential_metadata("ECOS_API_KEY", "missing", "API 키를 등록해야 합니다."),
        )
    if not series:
        return SourceResult("ecos", "not_configured", "config/ecos_series.json에 활성 지표가 없습니다.")

    path = output_dir / "raw_ecos.json"
    try:
        previous_payloads = read_json(path) if path.exists() else {}
        if not isinstance(previous_payloads, dict):
            previous_payloads = {}
    except Exception:
        previous_payloads = {}

    resolver = EcosResolver(key, ECOS_READ_TIMEOUT_SECONDS, ECOS_REQUEST_RETRIES)
    payloads: dict[str, list[dict[str, Any]]] = {}
    resolution_log: dict[str, Any] = {}
    warnings: list[str] = []
    auth_failures: list[str] = []
    latest_observation: str | None = None
    incremental_reused: list[str] = []
    circuit_reused: list[str] = []
    ecos_circuit_open = False
    ecos_circuit_reason = ""
    ecos_transport_failures = 0
    # Do not inherit the global 30 s x 3 retry policy for one host.  Accuracy is
    # preserved by committed last-good history; a later scheduled run retries fresh.
    ecos_timeout: tuple[float, float] = (ECOS_CONNECT_TIMEOUT_SECONDS, ECOS_READ_TIMEOUT_SECONDS)
    ecos_retries = ECOS_REQUEST_RETRIES

    for name, item in series.items():
        old_rows = previous_payloads.get(name) if isinstance(previous_payloads.get(name), list) else []
        if ecos_circuit_open:
            if old_rows:
                payloads[name] = list(old_rows)
                incremental_reused.append(name)
                circuit_reused.append(name)
                latest = _latest_period(old_rows)
                if latest and (latest_observation is None or latest > latest_observation):
                    latest_observation = latest
                print(
                    f"[ECOS] {name}: skipped external call | circuit open; "
                    f"last-good rows={len(old_rows)} latest={latest}",
                    flush=True,
                )
            else:
                detail = f"{name}: ECOS circuit open and no committed history"
                warnings.append(detail)
                print(f"[ECOS] {name}: skipped external call | no last-good history", flush=True)
            continue

        print(f"[ECOS] {name}: fetching", flush=True)
        try:
            end = date.today()
            fallback_start = end - timedelta(days=int(item.get("lookback_days", 400)))
            cycle = str(item.get("frequency", "D")).strip() or "D"
            start_text = _incremental_start(old_rows, cycle, fallback_start)
            if cycle == "D":
                end_text = end.strftime("%Y%m%d")
            elif cycle == "M":
                end_text = end.strftime("%Y%m")
            else:
                end_text = end.strftime("%Y")

            if item.get("fetch_all_items"):
                stat_code = str(item.get("stat_code", "")).strip()
                if not stat_code:
                    raise RuntimeError("fetch_all_items에는 stat_code가 필요합니다.")
                fresh_rows = _fetch_all_items(key, stat_code, cycle, start_text, end_text, ecos_timeout, ecos_retries)
                fresh_rows = _filter_item_rows(fresh_rows, str(item.get("item_name_filter", "")).strip())
                resolved = EcosResolution(
                    stat_code=stat_code,
                    stat_name=str(item.get("stat_name", stat_code)).strip() or stat_code,
                    cycle=cycle,
                    item_code1="*",
                    item_name1=str(item.get("item_name_filter", "전체항목")).strip() or "전체항목",
                )
            else:
                resolved = resolver.resolve(name, item)
                fresh_rows = _fetch(key, resolved, start_text, end_text, ecos_timeout, ecos_retries)
                if not fresh_rows and not old_rows:
                    # Only a first-time bootstrap may spend one metadata refresh.  If a
                    # committed history exists, keep it and avoid repeated discovery.
                    resolved = resolver.resolve(name, item, force=True)
                    fresh_rows = _fetch(key, resolved, start_text, end_text, ecos_timeout, ecos_retries)

            merged_rows = _merge_rows(old_rows, fresh_rows)
            if not merged_rows:
                raise RuntimeError("데이터가 없습니다.")
            if old_rows and not fresh_rows:
                incremental_reused.append(name)
                warnings.append(f"{name}: 최신 증분호출 실패/무응답으로 직전 정상 이력 유지")

            payloads[name] = merged_rows
            resolution_log[name] = resolved.to_dict()
            latest = _latest_period(merged_rows)
            if latest and (latest_observation is None or latest > latest_observation):
                latest_observation = latest
            mode = "incremental" if old_rows else "bootstrap"
            print(
                f"[ECOS] {name}: table={resolved.stat_code} item={resolved.item_name1} "
                f"rows={len(merged_rows)} fresh={len(fresh_rows)} latest={latest} mode={mode}",
                flush=True,
            )
        except Exception as exc:
            # A temporary API failure must not erase committed official history.
            if old_rows:
                payloads[name] = list(old_rows)
                incremental_reused.append(name)
                detail = f"{name}: {exc}; 직전 정상 이력 유지"
            else:
                detail = f"{name}: {exc}"
            warnings.append(detail)
            if credential_issue(exc):
                auth_failures.append(detail)
            if _is_transport_failure(exc):
                # V222: one transient ECOS connect failure must not suppress every
                # remaining series.  Permit a few independent probes first; only
                # repeated host-level transport failures open the per-run breaker.
                ecos_transport_failures += 1
                if ecos_transport_failures >= ECOS_TRANSPORT_FAILURES_BEFORE_CIRCUIT:
                    ecos_circuit_open = True
                    ecos_circuit_reason = f"{type(exc).__name__}: {exc}"
                    print(
                        f"[ECOS] circuit breaker opened after {ecos_transport_failures} transport failures; "
                        f"remaining series will reuse last-good history",
                        flush=True,
                    )
                else:
                    print(
                        f"[ECOS] transient transport failure {ecos_transport_failures}/"
                        f"{ECOS_TRANSPORT_FAILURES_BEFORE_CIRCUIT}; next series may still probe live",
                        flush=True,
                    )
            print(f"[ECOS] {name}: failed: {detail}", flush=True)

    resolver.save()
    resolution_path = output_dir / "ecos_resolution.json"
    write_json(path, payloads)
    write_json(resolution_path, resolution_log)
    total_rows = sum(len(v) for v in payloads.values())

    if not payloads:
        if auth_failures:
            return SourceResult(
                "ecos",
                "credential_error",
                "ECOS API 키가 만료되었거나 유효하지 않습니다. 새 키로 갱신해야 합니다.",
                payload_path=str(path),
                warnings=warnings,
                metadata=credential_metadata("ECOS_API_KEY", "renewal_required", auth_failures[0]),
            )
        return SourceResult(
            "ecos",
            "error",
            "ECOS 데이터 수집 실패",
            payload_path=str(path),
            warnings=warnings,
            metadata=credential_metadata("ECOS_API_KEY", "unknown_error"),
        )

    return SourceResult(
        "ecos",
        "ok" if not warnings else "degraded",
        "ECOS 국내 금리·환율·유동성 증분수집 완료",
        rows=total_rows,
        latest_observation=latest_observation,
        payload_path=str(path),
        warnings=warnings,
        metadata={
            "resolution_path": str(resolution_path),
            "cache_version": 2,
            "incremental_fetch": True,
            "daily_overlap_days": DAILY_OVERLAP_DAYS,
            "monthly_overlap_months": MONTHLY_OVERLAP_MONTHS,
            "last_good_reused": sorted(set(incremental_reused)),
            "circuit_breaker_open": ecos_circuit_open,
            "circuit_breaker_reason": ecos_circuit_reason or None,
            "circuit_last_good_reused": sorted(set(circuit_reused)),
            "ecos_connect_timeout_seconds": ECOS_CONNECT_TIMEOUT_SECONDS,
            "ecos_read_timeout_seconds": ECOS_READ_TIMEOUT_SECONDS,
            "ecos_request_retries": ECOS_REQUEST_RETRIES,
            **credential_metadata("ECOS_API_KEY", "valid"),
        },
    )
