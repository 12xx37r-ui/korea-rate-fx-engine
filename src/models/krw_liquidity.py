from __future__ import annotations

"""Korean-won liquidity nowcast/forecast.

This module is deliberately operational under partial data.  Monetary aggregates are
preferred, while policy-rate and market-rate signals provide a lower-confidence
fallback.  Missing optional inputs reduce quality; they do not disable forecasting.
"""

from math import tanh
from statistics import mean
from typing import Any


MODEL_VERSION = "1.0.0-continuous-liquidity"


def _float(value: Any) -> float | None:
    try:
        if value in (None, "", "-", "."):
            return None
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _period(row: dict[str, Any]) -> str:
    for key in ("TIME", "date", "DATE", "period", "PRD_DE"):
        value = row.get(key)
        if value not in (None, ""):
            return str(value).replace("-", "").replace(".", "")
    return ""


def _series(rows: list[dict[str, Any]] | None) -> list[tuple[str, float]]:
    dedup: dict[str, float] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        value = None
        for key in ("DATA_VALUE", "value", "DT"):
            value = _float(row.get(key))
            if value is not None:
                break
        date = _period(row)
        if date and value is not None:
            dedup[date] = value
    return sorted(dedup.items())


def _pct(values: list[float], periods: int) -> float | None:
    if len(values) <= periods or values[-periods - 1] == 0:
        return None
    return values[-1] / values[-periods - 1] - 1.0


def _clip(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _grade(score: float) -> str:
    if score >= 0.45:
        return "유동성 확장"
    if score >= 0.12:
        return "유동성 소폭확장"
    if score > -0.12:
        return "유동성 중립"
    if score > -0.45:
        return "유동성 소폭수축"
    return "유동성 수축"


def _expected_rate_change(rate_v2: dict[str, Any] | None, months: int) -> float:
    if not isinstance(rate_v2, dict):
        return 0.0
    current = _float((rate_v2.get("current") or {}).get("kr_base_rate_pct"))
    path = rate_v2.get("meeting_path") or []
    if current is None or not path:
        return 0.0
    idx = 0 if months <= 3 else (1 if months <= 6 else min(3, len(path) - 1))
    idx = min(idx, len(path) - 1)
    future = _float(path[idx].get("expected_rate_pct"))
    return (future - current) if future is not None else 0.0


def build_krw_liquidity_forecast(ecos: dict[str, Any], rate_v2: dict[str, Any] | None = None) -> dict[str, Any]:
    m1 = _series((ecos or {}).get("kr_m1", []))
    m2 = _series((ecos or {}).get("kr_m2", []))
    lf = _series((ecos or {}).get("kr_lf", []))
    base = _series((ecos or {}).get("kr_base_rate", []))
    y2 = _series((ecos or {}).get("kr_gov_2y", []) or (ecos or {}).get("kr_gov_3y", []))

    factors: dict[str, float] = {}
    observations: dict[str, Any] = {}

    for name, series, weight_scale in (("m1_yoy", m1, 0.08), ("m2_yoy", m2, 0.07), ("lf_yoy", lf, 0.07)):
        values = [v for _, v in series]
        yoy = _pct(values, 12)
        if yoy is not None and -0.30 <= yoy <= 0.50:
            factors[name] = _clip((yoy - 0.04) / weight_scale, -2.5, 2.5)
            observations[name] = round(yoy * 100.0, 3)

    current_rate = base[-1][1] if base else None
    market_rate = y2[-1][1] if y2 else None
    if current_rate is not None and market_rate is not None:
        gap = market_rate - current_rate
        # A market rate below the policy rate generally implies easier expected conditions.
        factors["market_policy_gap"] = _clip(-gap / 0.75, -2.5, 2.5)
        observations["market_policy_gap_pctp"] = round(gap, 3)

    rate_3m = _expected_rate_change(rate_v2, 3)
    factors["expected_policy_easing"] = _clip(-rate_3m / 0.50, -2.5, 2.5)
    observations["expected_policy_change_3m_pctp"] = round(rate_3m, 3)

    base_weights = {
        "m2_yoy": 0.34,
        "m1_yoy": 0.18,
        "lf_yoy": 0.18,
        "market_policy_gap": 0.15,
        "expected_policy_easing": 0.15,
    }
    active_weight = sum(base_weights[k] for k in factors if k in base_weights) or 1.0
    raw = sum(factors[k] * base_weights[k] for k in factors if k in base_weights) / active_weight
    current_score = tanh(raw * 0.65)

    m2_values = [v for _, v in m2]
    current_m2_yoy = _pct(m2_values, 12)
    m2_3m = None
    if len(m2_values) > 3 and m2_values[-4] > 0 and m2_values[-1] > 0:
        m2_3m = (m2_values[-1] / m2_values[-4]) ** 4 - 1.0

    forecast_path = []
    for months in (3, 6, 12):
        policy_change = _expected_rate_change(rate_v2, months)
        policy_impulse = _clip(-policy_change / 0.75, -1.5, 1.5)
        monetary_trend = 0.0
        if current_m2_yoy is not None:
            monetary_trend = _clip((current_m2_yoy - 0.04) / 0.07, -2.0, 2.0)
        if m2_3m is not None:
            monetary_trend = 0.65 * monetary_trend + 0.35 * _clip((m2_3m - 0.04) / 0.08, -2.0, 2.0)
        horizon_damping = {3: 0.90, 6: 0.80, 12: 0.65}[months]
        future_score = tanh((current_score * horizon_damping + monetary_trend * 0.18 + policy_impulse * 0.22))
        expected_m2_yoy = None
        if current_m2_yoy is not None:
            trend_anchor = m2_3m if m2_3m is not None else current_m2_yoy
            expected_m2_yoy = current_m2_yoy + 0.25 * (trend_anchor - current_m2_yoy) + 0.012 * policy_impulse
        forecast_path.append(
            {
                "months": months,
                "liquidity_score": round(future_score, 4),
                "grade": _grade(future_score),
                "expected_m2_yoy_pct": round(expected_m2_yoy * 100.0, 3) if expected_m2_yoy is not None else None,
                "policy_impulse": round(policy_impulse, 4),
                "prediction_status": "forecast",
            }
        )

    active_count = len(factors)
    quality_score = int(_clip(42 + active_count * 9 + (12 if current_m2_yoy is not None else 0), 0, 100))
    data_mode = "monetary_plus_policy" if current_m2_yoy is not None else "policy_market_proxy"
    latest_dates = [s[-1][0] for s in (m1, m2, lf, base, y2) if s]

    return {
        "schema_version": "1.0.0",
        "engine_version": MODEL_VERSION,
        "status": "ok",
        "forecast_operational": True,
        "data_mode": data_mode,
        "latest_observation": max(latest_dates) if latest_dates else None,
        "current": {
            "liquidity_score": round(current_score, 4),
            "grade": _grade(current_score),
            "observations": observations,
        },
        "forecast_path": forecast_path,
        "quality": {
            "model_quality_score": quality_score,
            "active_factor_count": active_count,
            "monetary_aggregate_available": current_m2_yoy is not None,
            "note": "통화량이 있으면 M1·M2·Lf를 우선 사용하고, 없을 때도 정책금리·시장금리 경로로 연속 예측합니다.",
        },
    }
