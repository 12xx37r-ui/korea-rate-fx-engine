"""Point-in-Time (PiT) 데이터베이스 — 공표 시차(Reporting Lag) 관리.

한국 거시 지표는 기준일(as-of date) 대비 1~8주 늦게 공표됩니다.
백테스트·빈티지 생성 시 아직 발표되지 않은 데이터를 사용하는
Look-ahead bias를 방지합니다.

사용 예:
    from src.core.pit_database import filter_pit_rows, pit_coverage_check

    # 2024-06-01 기준 실제 사용 가능한 행만 반환
    rows = filter_pit_rows(rows, "kr_m2", as_of_date="2024-06-01")

    # 여러 시리즈 일괄 PiT 검사
    report = pit_coverage_check(ecos_dict, as_of_date="2024-06-01")
"""
from __future__ import annotations

import calendar
import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

# ── 공표 시차 레지스트리 (config/reporting_lags.json 로드) ─────────────────────
def _load_registry() -> dict[str, dict[str, Any]]:
    cfg_path = Path(__file__).parent.parent.parent / "config" / "reporting_lags.json"
    try:
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
        merged: dict[str, dict[str, Any]] = {}
        for section in ("ecos", "kosis", "global"):
            for k, v in (raw.get(section) or {}).items():
                merged[k] = v
        return merged
    except Exception:
        return {}


_REGISTRY: dict[str, dict[str, Any]] = _load_registry()

# fallback defaults by frequency pattern
_DEFAULT_DAILY = {"lag_days": 1, "frequency": "daily", "source": "미등록(기본1일)"}
_DEFAULT_MONTHLY = {"lag_days": 30, "frequency": "monthly", "source": "미등록(기본30일)"}


def get_lag_info(series_key: str) -> dict[str, Any]:
    """시리즈의 공표 시차 메타데이터 반환."""
    return _REGISTRY.get(series_key, _DEFAULT_DAILY)


def _parse_date(raw: str) -> date | None:
    """ECOS(YYYYMM or YYYYMMDD), global(YYYY-MM-DD) 모두 파싱."""
    s = str(raw).replace("-", "").replace("/", "").strip()
    if len(s) == 6:
        s += "01"
    if len(s) != 8:
        return None
    try:
        return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    except ValueError:
        return None


def _month_end(d: date) -> date:
    last = calendar.monthrange(d.year, d.month)[1]
    return date(d.year, d.month, last)


def _quarter_end(d: date) -> date:
    q_end_month = ((d.month - 1) // 3 + 1) * 3
    year = d.year + (1 if q_end_month > 12 else 0)
    q_end_month = q_end_month if q_end_month <= 12 else q_end_month - 12
    return _month_end(date(year, q_end_month, 1))


def effective_release_date(row: dict[str, Any], series_key: str) -> date | None:
    """행의 기준일로부터 실제 공표 가능일을 계산한다."""
    lag_info = get_lag_info(series_key)
    lag_days = int(lag_info.get("lag_days", 0))
    freq = lag_info.get("frequency", "daily")

    raw_date = row.get("TIME") or row.get("date") or row.get("DATE") or ""
    as_of = _parse_date(str(raw_date))
    if as_of is None:
        return None

    if freq == "monthly":
        # 해당 월 말일 + lag_days
        return _month_end(as_of) + timedelta(days=lag_days)
    elif freq == "quarterly":
        return _quarter_end(as_of) + timedelta(days=lag_days)
    else:
        # daily / event
        return as_of + timedelta(days=lag_days)


def filter_pit_rows(
    rows: list[dict[str, Any]],
    series_key: str,
    as_of_date: str | date | None = None,
) -> list[dict[str, Any]]:
    """as_of_date 시점에 이미 공표되었을 행만 반환.

    as_of_date가 None이면 live 모드 — 전체 행을 반환한다.
    백테스트·빈티지 시뮬레이션에서만 as_of_date를 지정한다.
    """
    if not rows or as_of_date is None:
        return rows

    if isinstance(as_of_date, str):
        try:
            cutoff = date.fromisoformat(as_of_date)
        except ValueError:
            return rows
    else:
        cutoff = as_of_date

    out = []
    for row in rows:
        rel = effective_release_date(row, series_key)
        if rel is None or rel <= cutoff:
            out.append(row)
    return out


def pit_coverage_check(
    series_dict: dict[str, list[dict[str, Any]]],
    as_of_date: str | date | None = None,
) -> dict[str, Any]:
    """여러 시리즈에 대해 PiT 커버리지 일괄 검사.

    반환값 구조:
    {
        "as_of_date": "live" or "YYYY-MM-DD",
        "series": {
            "kr_m2": {"total": 120, "available": 118, "removed": 2, "lag_days": 45, ...},
            ...
        },
        "total_lookahead_removed": 3,
        "has_lookahead_risk": False   # live 모드에서는 항상 False
    }
    """
    if as_of_date is None:
        return {
            "as_of_date": "live",
            "mode": "live_no_pit_filter",
            "series": {},
            "total_lookahead_removed": 0,
            "has_lookahead_risk": False,
        }

    results: dict[str, Any] = {}
    total_removed = 0

    for key, rows in series_dict.items():
        if not isinstance(rows, list):
            continue
        lag_info = get_lag_info(key)
        filtered = filter_pit_rows(rows, key, as_of_date)
        removed = len(rows) - len(filtered)
        total_removed += removed
        results[key] = {
            "total_rows": len(rows),
            "pit_available_rows": len(filtered),
            "lookahead_rows_removed": removed,
            "lag_days": lag_info.get("lag_days", 0),
            "frequency": lag_info.get("frequency", "unknown"),
            "source": lag_info.get("source", ""),
        }

    return {
        "as_of_date": str(as_of_date),
        "series": results,
        "total_lookahead_removed": total_removed,
        "has_lookahead_risk": total_removed > 0,
    }
