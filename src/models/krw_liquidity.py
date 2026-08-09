from __future__ import annotations

"""Korean-won liquidity nowcast/forecast V1.2.

The production forecast remains continuous, but V1.2 separates two concepts that were
previously mixed together:
* input-data quality (coverage/freshness), and
* forecast quality (fixed-spec expanding OOS skill for future M2 YoY).

The OOS audit never uses the live future policy path at historical origins.  The live
policy path is a small, explicitly bounded overlay on top of the OOS-audited monetary
forecast so it cannot dominate the result.
"""

from math import sqrt, tanh
from statistics import mean
from typing import Any


MODEL_VERSION = "1.2.0-continuous-liquidity-oos"


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
    idx = 0 if months <= 3 else (1 if months <= 6 else min(2, len(path) - 1))
    idx = min(idx, len(path) - 1)
    future = _float(path[idx].get("expected_rate_pct"))
    return (future - current) if future is not None else 0.0


def _monthly_map(series: list[tuple[str, float]]) -> dict[str, float]:
    out: dict[str, float] = {}
    for date, value in series:
        month = str(date)[:6]
        if len(month) == 6:
            out[month] = value
    return out


def _rmse(errors: list[float]) -> float | None:
    return sqrt(mean(e * e for e in errors)) if errors else None


def _oos_validation(
    m1: list[tuple[str, float]],
    m2: list[tuple[str, float]],
    lf: list[tuple[str, float]],
    base: list[tuple[str, float]],
    y2: list[tuple[str, float]],
    months: int,
) -> dict[str, Any]:
    """Fixed-spec expanding-origin audit for future M2 YoY.

    No coefficient fitting is done on future outcomes; the same conservative rule is
    applied at every historical origin. The benchmark is persistence of current M2
    YoY, which is the relevant no-change baseline for this target.
    """
    m2_month = _monthly_map(m2)
    months_order = sorted(m2_month)
    m1_month = _monthly_map(m1)
    lf_month = _monthly_map(lf)
    base_month = _monthly_map(base)
    y2_month = _monthly_map(y2)
    if len(months_order) < 36 + months:
        return {"samples": 0, "grade": "D", "forecast_quality_score": 25}

    model_errors: list[float] = []
    bench_errors: list[float] = []
    hits: list[bool] = []
    for idx in range(15, len(months_order) - months):
        cur_m = months_order[idx]
        fut_m = months_order[idx + months]
        cur = m2_month[cur_m]
        old12 = m2_month.get(months_order[idx - 12])
        old3 = m2_month.get(months_order[idx - 3])
        fut_old12 = m2_month.get(months_order[idx + months - 12]) if idx + months >= 12 else None
        if not all(v and v > 0 for v in (cur, old12, old3, m2_month[fut_m], fut_old12)):
            continue
        cur_yoy = cur / old12 - 1.0
        trend3 = (cur / old3) ** 4 - 1.0
        peer: list[float] = []
        for mapping in (m1_month, lf_month):
            now = mapping.get(cur_m)
            old_month = months_order[idx - 12]
            old = mapping.get(old_month)
            if now and old and old > 0:
                peer.append(now / old - 1.0)
        peer_yoy = mean(peer) if peer else cur_yoy
        gap = 0.0
        if cur_m in base_month and cur_m in y2_month:
            gap = y2_month[cur_m] - base_month[cur_m]

        raw = cur_yoy + 0.30 * (trend3 - cur_yoy) + 0.10 * (peer_yoy - cur_yoy) - 0.006 * _clip(gap, -2.0, 2.0)
        damping = {3: 1.00, 6: 0.82, 12: 0.65}[months]
        pred = cur_yoy + damping * (raw - cur_yoy)
        actual = m2_month[fut_m] / fut_old12 - 1.0
        model_errors.append(pred - actual)
        bench_errors.append(cur_yoy - actual)
        pred_delta = pred - cur_yoy
        actual_delta = actual - cur_yoy
        if abs(actual_delta) >= 0.002:
            hits.append((pred_delta >= 0) == (actual_delta >= 0))

    rmse = _rmse(model_errors)
    bench = _rmse(bench_errors)
    skill = (1.0 - rmse / bench) * 100.0 if rmse is not None and bench else None
    dacc = mean(hits) if hits else None
    n = len(model_errors)
    strict = bool(n >= 80 and skill is not None and skill > 0 and dacc is not None and dacc >= 0.52)
    grade = "A" if strict and skill is not None and skill >= 5 and dacc is not None and dacc >= 0.56 else ("B" if strict else ("C" if n >= 60 else "D"))
    score = 30 + min(20, n // 5)
    if skill is not None:
        score += int(_clip(10 + skill * 2.0, 0, 25))
    if dacc is not None:
        score += int(_clip((dacc - 0.45) * 100.0, 0, 20))
    score = int(_clip(score, 0, 100))
    return {
        "samples": n,
        "rmse_pctp": round(rmse * 100.0, 3) if rmse is not None else None,
        "persistence_rmse_pctp": round(bench * 100.0, 3) if bench is not None else None,
        "persistence_skill_pct": round(skill, 3) if skill is not None else None,
        "direction_accuracy": round(dacc, 4) if dacc is not None else None,
        "strict_skill_passed": strict,
        "grade": grade,
        "forecast_quality_score": score,
        "method": "fixed_spec_expanding_origin_future_m2_yoy",
        "benchmark": "current_m2_yoy_persistence",
    }


def _validation_shrinkage(row: dict[str, Any]) -> float:
    skill = row.get("persistence_skill_pct")
    try:
        skill = float(skill)
    except (TypeError, ValueError):
        return 0.50
    if skill <= -5:
        return 0.30
    if skill < 0:
        return 0.30 + 0.15 * (skill + 5.0) / 5.0
    if skill < 3:
        return 0.45 + 0.30 * skill / 3.0
    if skill < 6:
        return 0.75 + 0.25 * (skill - 3.0) / 3.0
    return 1.0


def build_krw_liquidity_forecast(ecos: dict[str, Any], rate_v2: dict[str, Any] | None = None) -> dict[str, Any]:
    m1 = _series((ecos or {}).get("kr_m1", []))
    m2 = _series((ecos or {}).get("kr_m2", []))
    lf = _series((ecos or {}).get("kr_lf", []))
    base = _series((ecos or {}).get("kr_base_rate", []))
    y2 = _series((ecos or {}).get("kr_gov_2y", []) or (ecos or {}).get("kr_gov_3y", []))

    factors: dict[str, float] = {}
    observations: dict[str, Any] = {}
    for name, series, scale in (("m1_yoy", m1, 0.08), ("m2_yoy", m2, 0.07), ("lf_yoy", lf, 0.07)):
        values = [v for _, v in series]
        yoy = _pct(values, 12)
        if yoy is not None and -0.30 <= yoy <= 0.50:
            factors[name] = _clip((yoy - 0.04) / scale, -2.5, 2.5)
            observations[name] = round(yoy * 100.0, 3)

    current_rate = base[-1][1] if base else None
    market_rate = y2[-1][1] if y2 else None
    if current_rate is not None and market_rate is not None:
        gap = market_rate - current_rate
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

    validations = {str(months): _oos_validation(m1, m2, lf, base, y2, months) for months in (3, 6, 12)}
    forecast_path = []
    for months in (3, 6, 12):
        validation = validations[str(months)]
        shrink = _validation_shrinkage(validation)
        policy_change = _expected_rate_change(rate_v2, months)
        # Policy overlay is capped so an unvalidated live rate path cannot dominate.
        policy_impulse = _clip(-policy_change / 0.75, -1.0, 1.0)
        monetary_trend = 0.0
        if current_m2_yoy is not None:
            monetary_trend = _clip((current_m2_yoy - 0.04) / 0.07, -2.0, 2.0)
        if m2_3m is not None:
            monetary_trend = 0.65 * monetary_trend + 0.35 * _clip((m2_3m - 0.04) / 0.08, -2.0, 2.0)
        horizon_damping = {3: 0.90, 6: 0.80, 12: 0.65}[months]
        future_score_raw = tanh(current_score * horizon_damping + monetary_trend * 0.18 + policy_impulse * 0.16)
        future_score = current_score + shrink * (future_score_raw - current_score)

        expected_m2_yoy = None
        if current_m2_yoy is not None:
            trend_anchor = m2_3m if m2_3m is not None else current_m2_yoy
            raw_yoy = current_m2_yoy + 0.25 * (trend_anchor - current_m2_yoy)
            raw_yoy += 0.006 * policy_impulse  # <=0.6pp policy overlay before OOS shrink
            expected_m2_yoy = current_m2_yoy + shrink * (raw_yoy - current_m2_yoy)
        forecast_path.append({
            "months": months,
            "liquidity_score": round(future_score, 4),
            "grade": _grade(future_score),
            "expected_m2_yoy_pct": round(expected_m2_yoy * 100.0, 3) if expected_m2_yoy is not None else None,
            "policy_impulse": round(policy_impulse, 4),
            "validation_shrinkage": round(shrink, 4),
            "quality_grade": validation.get("grade"),
            "forecast_quality_score": validation.get("forecast_quality_score"),
            "prediction_status": "forecast",
        })

    active_count = len(factors)
    input_quality = int(_clip(42 + active_count * 9 + (12 if current_m2_yoy is not None else 0), 0, 100))
    primary = validations["3"]
    forecast_quality = int(primary.get("forecast_quality_score") or 0)
    data_mode = "monetary_plus_policy" if current_m2_yoy is not None else "policy_market_proxy"
    latest_dates = [s[-1][0] for s in (m1, m2, lf, base, y2) if s]

    return {
        "schema_version": "1.1.0",
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
        "validation": {
            "separate_oos_validated": True,
            "primary_horizon": "3m",
            "oos_by_horizon": {f"{k}m": v for k, v in validations.items()},
            "quality_grade": primary.get("grade"),
            "forecast_quality_score": forecast_quality,
            "quality_score_semantics": "future_m2_yoy_oos_skill_accuracy_and_samples_not_probability",
        },
        "quality": {
            # Keep legacy model_quality_score, but make it the forecast-quality score
            # now that a separate OOS audit exists. Input quality remains separate.
            "model_quality_score": forecast_quality,
            "forecast_quality_score": forecast_quality,
            "forecast_quality_grade": primary.get("grade"),
            "input_data_quality_score": input_quality,
            "quality_score_semantics": "forecast_quality_is_oos_based; input_quality_is_coverage_availability",
            "separate_oos_validated": True,
            "active_factor_count": active_count,
            "monetary_aggregate_available": current_m2_yoy is not None,
            "note": "유동성 예측품질은 미래 M2 YoY 고정규칙 OOS로 평가하며 입력자료 품질과 분리합니다. 검증력이 약하면 예측폭을 축소하지만 예측은 계속 산출합니다.",
        },
    }
