"""Data Quality Gate — raw collector output validation and API failure tracking.

Zero new API calls. Validates in-memory data only.
"""
from __future__ import annotations

from typing import Any

_ZERO_FILL_RATIO = 0.80   # >80% zeros → suspicious
_NULL_FILL_RATIO = 0.90   # >90% nulls → suspicious
_SUDDEN_CHANGE   = 0.50   # >50% change vs prior latest → warning
_MIN_RECORDS     = 2


def _grade(issues: list[str], warnings: list[str]) -> str:
    if issues:
        return "F"
    if len(warnings) >= 3:
        return "C"
    if warnings:
        return "B"
    return "A"


def validate_ecos_series(
    name: str,
    rows: list[dict],
    prev_rows: list[dict] | None = None,
) -> dict[str, Any]:
    """Validate ECOS-format rows: {STAT_CODE, TIME, DATA_VALUE}."""
    issues: list[str] = []
    warnings: list[str] = []

    if not rows:
        return {
            "passed": False, "grade": "F",
            "issues": [f"{name}: 빈 배열 응답"],
            "warnings": [], "record_count": 0,
        }

    if len(rows) < _MIN_RECORDS:
        warnings.append(f"{name}: 레코드 부족 ({len(rows)}건, 최소 {_MIN_RECORDS}건 필요)")

    values: list[float] = []
    null_count = 0
    for r in rows:
        raw = r.get("DATA_VALUE")
        if raw in (None, "", "-", "N/A"):
            null_count += 1
            continue
        try:
            values.append(float(raw))
        except (TypeError, ValueError):
            null_count += 1

    total = len(rows)
    if total > 0 and null_count / total >= _NULL_FILL_RATIO:
        issues.append(f"{name}: DATA_VALUE의 {null_count/total*100:.0f}%가 null/빈값")

    if values:
        zero_ratio = sum(1 for v in values if v == 0.0) / len(values)
        if zero_ratio >= _ZERO_FILL_RATIO:
            issues.append(f"{name}: DATA_VALUE의 {zero_ratio*100:.0f}%가 0 (zero-fill 의심)")

        if prev_rows:
            prev_vals = [
                float(r["DATA_VALUE"]) for r in prev_rows[-3:]
                if r.get("DATA_VALUE") not in (None, "", "-", "N/A")
            ]
            if prev_vals:
                prev_v, curr_v = prev_vals[-1], values[-1]
                if prev_v != 0 and abs(curr_v - prev_v) / abs(prev_v) > _SUDDEN_CHANGE:
                    pct = (curr_v - prev_v) / abs(prev_v) * 100
                    warnings.append(f"{name}: 최신값 급변 {prev_v:.4f}→{curr_v:.4f} ({pct:+.1f}%)")

    return {
        "passed": not issues,
        "grade": _grade(issues, warnings),
        "record_count": len(rows),
        "issues": issues,
        "warnings": warnings,
    }


def validate_time_series(
    name: str,
    rows: list[dict],
    value_key: str = "value",
    prev_rows: list[dict] | None = None,
    min_value: float | None = None,
    max_value: float | None = None,
) -> dict[str, Any]:
    """Validate generic time-series rows: {date, value, ...}."""
    issues: list[str] = []
    warnings: list[str] = []

    if not rows:
        return {
            "passed": False, "grade": "F",
            "issues": [f"{name}: 빈 배열 응답 (DATA_PENDING)"],
            "warnings": [], "record_count": 0,
        }

    if len(rows) < _MIN_RECORDS:
        warnings.append(f"{name}: 레코드 부족 ({len(rows)}건)")

    values: list[float] = []
    null_count = 0
    for r in rows:
        v = r.get(value_key)
        if v is None:
            null_count += 1
            continue
        try:
            values.append(float(v))
        except (TypeError, ValueError):
            null_count += 1

    total = len(rows)
    if total > 0 and null_count / total >= _NULL_FILL_RATIO:
        issues.append(f"{name}: {value_key}의 {null_count/total*100:.0f}%가 null")

    if values:
        zero_ratio = sum(1 for v in values if v == 0.0) / len(values)
        if zero_ratio >= _ZERO_FILL_RATIO:
            issues.append(f"{name}: {value_key}의 {zero_ratio*100:.0f}%가 0 (zero-fill 의심)")

        curr_v = values[-1]
        if min_value is not None and curr_v < min_value:
            issues.append(f"{name}: 최신값 {curr_v} < 허용하한 {min_value}")
        if max_value is not None and curr_v > max_value:
            issues.append(f"{name}: 최신값 {curr_v} > 허용상한 {max_value}")

        if prev_rows:
            prev_vals = [float(r[value_key]) for r in prev_rows[-3:] if r.get(value_key) is not None]
            if prev_vals:
                prev_v = prev_vals[-1]
                if prev_v != 0 and abs(curr_v - prev_v) / abs(prev_v) > _SUDDEN_CHANGE:
                    pct = (curr_v - prev_v) / abs(prev_v) * 100
                    warnings.append(f"{name}: 급변 {prev_v:.4f}→{curr_v:.4f} ({pct:+.1f}%)")

    return {
        "passed": not issues,
        "grade": _grade(issues, warnings),
        "record_count": len(rows),
        "issues": issues,
        "warnings": warnings,
    }


# Range bounds for key global series
_GLOBAL_BOUNDS: dict[str, dict[str, float]] = {
    "usd_krw_yahoo":   {"min_value": 500.0,  "max_value": 3000.0},
    "usd_krw_fred":    {"min_value": 500.0,  "max_value": 3000.0},
    "sp500":           {"min_value": 500.0,  "max_value": 20000.0},
    "us_10y":          {"min_value": -5.0,   "max_value": 25.0},
    "us_2y":           {"min_value": -5.0,   "max_value": 25.0},
    "usd_cny":         {"min_value": 3.0,    "max_value": 15.0},
    "vix":             {"min_value": 5.0,    "max_value": 200.0},
    "hy_oas":          {"min_value": 0.0,    "max_value": 50.0},
    "krw_neer":        {"min_value": 30.0,   "max_value": 200.0},
    "krw_reer":        {"min_value": 30.0,   "max_value": 200.0},
    "broad_dollar":    {"min_value": 50.0,   "max_value": 200.0},
    "wti":             {"min_value": 5.0,    "max_value": 500.0},
}


def validate_raw_global(
    global_data: dict[str, Any],
    prev_global: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate all key series in raw_global_market.json."""
    results: dict[str, Any] = {}
    for key, bounds in _GLOBAL_BOUNDS.items():
        rows = global_data.get(key) or []
        prev_rows = (prev_global or {}).get(key)
        results[key] = validate_time_series(
            name=key, rows=rows, value_key="value",
            prev_rows=prev_rows,
            **bounds,
        )

    all_issues  = [i for v in results.values() for i in v.get("issues", [])]
    all_warnings = [w for v in results.values() for w in v.get("warnings", [])]
    passed_count = sum(1 for v in results.values() if v["passed"])

    return {
        "passed": not all_issues,
        "passed_series": passed_count,
        "total_series": len(results),
        "series": results,
        "issues": all_issues,
        "warnings": all_warnings,
    }


def validate_raw_ecos(
    ecos_data: dict[str, Any],
    prev_ecos: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate all list-type series in raw_ecos.json."""
    results: dict[str, Any] = {}
    for key, rows in ecos_data.items():
        if not isinstance(rows, list):
            continue
        prev_rows = (prev_ecos or {}).get(key)
        results[key] = validate_ecos_series(name=key, rows=rows, prev_rows=prev_rows)

    all_issues   = [i for v in results.values() for i in v.get("issues", [])]
    all_warnings = [w for v in results.values() for w in v.get("warnings", [])]

    return {
        "passed": not all_issues,
        "series": results,
        "issues": all_issues,
        "warnings": all_warnings,
    }


def compute_source_failure_tracking(
    source_key: str,
    current_status: str,
    prev_health: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Track consecutive failures and cache-fallback events per source."""
    prev_src = (prev_health or {}).get("sources", {}).get(source_key, {})
    prev_tracking = prev_src.get("failure_tracking", {})

    prev_consecutive   = int(prev_tracking.get("consecutive_failure_count", 0))
    prev_fallback_total = int(prev_tracking.get("cache_fallback_total", 0))

    is_failure = current_status not in {"ok", "degraded", "not_configured"}
    is_fallback = current_status == "fallback"

    consecutive_failure_count = (prev_consecutive + 1) if is_failure else 0
    cache_fallback_total = prev_fallback_total + (1 if is_fallback else 0)

    if consecutive_failure_count >= 5:
        alert_level = "ALERT"
    elif consecutive_failure_count >= 3:
        alert_level = "WARNING"
    elif consecutive_failure_count >= 1:
        alert_level = "NOTICE"
    else:
        alert_level = "OK"

    return {
        "consecutive_failure_count": consecutive_failure_count,
        "cache_fallback_triggered": is_fallback,
        "cache_fallback_total": cache_fallback_total,
        "alert_level": alert_level,
    }
