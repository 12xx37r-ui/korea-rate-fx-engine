from __future__ import annotations

"""Independent V2 forecasting layer for Korea policy rate and USD/KRW.

The module deliberately reuses the frozen U.S. engine JSON as an input only.
It does not change U.S. calculations and does not replace the legacy Korea
snapshot.  All V2 outputs are additive and gated by validation quality.
"""

from dataclasses import dataclass
from math import exp, sqrt
from statistics import mean
from typing import Any

from src.models.rate_validation import (
    calibrate_probabilities,
    core_cpi_yoy_series,
    numeric_series,
    probability_from_features,
    rate_probability_backtest,
)


CLASSES = ("hold", "hike", "cut")


def _softmax(raw: dict[str, float]) -> dict[str, float]:
    peak = max(raw.values())
    weights = {k: exp(v - peak) for k, v in raw.items()}
    total = sum(weights.values()) or 1.0
    return {k: weights[k] / total for k in weights}


def _latest(series: list[tuple[str, float]]) -> float | None:
    return series[-1][1] if series else None


def _annualized(series: list[tuple[str, float]], periods: int) -> float | None:
    if len(series) <= periods:
        return None
    old, new = series[-periods - 1][1], series[-1][1]
    if old <= 0 or new <= 0:
        return None
    return (new / old) ** (12.0 / periods) - 1.0


def _pct_change(series: list[tuple[str, float]], periods: int) -> float | None:
    if len(series) <= periods:
        return None
    old, new = series[-periods - 1][1], series[-1][1]
    if old == 0:
        return None
    return new / old - 1.0


def _us_path(us_policy: dict[str, Any] | None) -> tuple[float | None, list[float]]:
    if not isinstance(us_policy, dict):
        return None, []
    fed = us_policy.get("fed") if isinstance(us_policy.get("fed"), dict) else {}
    current = us_policy.get("current_effective_rate", fed.get("current_effective_rate"))
    try:
        current_f = float(current)
    except (TypeError, ValueError):
        current_f = None
    path = us_policy.get("meeting_path")
    if not isinstance(path, list) or not path:
        path = fed.get("expected_path", [])
    result: list[float] = []
    for row in path:
        if not isinstance(row, dict):
            continue
        for key in ("expected_post_meeting_rate", "expected_rate", "rate"):
            try:
                if row.get(key) is not None:
                    result.append(float(row[key]))
                    break
            except (TypeError, ValueError):
                continue
    return current_f, result


def _regime(
    inflation: float | None,
    growth: float | None,
    policy_gap: float | None,
    us_change: float | None,
    fx_3m: float | None,
) -> tuple[str, dict[str, float]]:
    scores = {
        "inflation_defense": 0.0,
        "growth_support": 0.0,
        "fx_defense": 0.0,
        "financial_stability": 0.35,
    }
    if inflation is not None:
        z = (inflation - 0.02) / 0.01
        scores["inflation_defense"] += max(0.0, z)
        scores["growth_support"] += max(0.0, -z) * 0.45
    if growth is not None:
        scores["growth_support"] += max(0.0, -growth / 0.05)
        scores["inflation_defense"] += max(0.0, growth / 0.08) * 0.25
    if policy_gap is not None:
        scores["inflation_defense"] += max(0.0, policy_gap / 0.50) * 0.35
        scores["growth_support"] += max(0.0, -policy_gap / 0.50) * 0.45
    if us_change is not None:
        scores["fx_defense"] += max(0.0, us_change / 0.50)
        scores["growth_support"] += max(0.0, -us_change / 0.50) * 0.25
    if fx_3m is not None:
        scores["fx_defense"] += max(0.0, fx_3m / 0.05)
    regime = max(scores, key=scores.get)
    return regime, {k: round(v, 4) for k, v in scores.items()}


def _regime_adjust(
    probs: dict[str, float],
    regime: str,
    us_change: float | None,
    fx_3m: float | None,
) -> dict[str, float]:
    raw = {k: max(1e-9, probs[k]) for k in CLASSES}
    if regime == "inflation_defense":
        raw["hike"] *= 1.22
        raw["cut"] *= 0.82
    elif regime == "growth_support":
        raw["cut"] *= 1.25
        raw["hike"] *= 0.78
    elif regime == "fx_defense":
        raw["hold"] *= 1.13
        raw["hike"] *= 1.12
        raw["cut"] *= 0.72
    else:
        raw["hold"] *= 1.08
    if us_change is not None:
        if us_change > 0:
            raw["cut"] *= max(0.72, 1.0 - min(0.28, us_change * 0.22))
        elif us_change < 0:
            raw["cut"] *= 1.0 + min(0.20, -us_change * 0.16)
    if fx_3m is not None and fx_3m > 0.04:
        raw["cut"] *= 0.78
        raw["hold"] *= 1.10
    total = sum(raw.values()) or 1.0
    return {k: raw[k] / total for k in CLASSES}


def _next_probs(prev: dict[str, float], regime: str) -> dict[str, float]:
    # Conservative transition: policy-rate changes are sticky but not repeated mechanically.
    raw = {
        "hold": 0.72 + prev["hold"] * 0.32,
        "hike": 0.08 + prev["hike"] * 0.42,
        "cut": 0.08 + prev["cut"] * 0.42,
    }
    if regime == "growth_support":
        raw["cut"] *= 1.16
    elif regime in ("inflation_defense", "fx_defense"):
        raw["hike"] *= 1.10
        raw["cut"] *= 0.88
    return _softmax(raw)


def _expected_rate(rate: float, probs: dict[str, float]) -> float:
    return rate + 0.25 * (probs["hike"] - probs["cut"])


def _quality_gate(backtest: dict[str, Any], data_coverage: float) -> dict[str, Any]:
    samples = int(backtest.get("samples") or 0)
    skill = backtest.get("brier_skill_score")
    accuracy = backtest.get("accuracy")
    passed = (
        samples >= 48
        and skill is not None
        and float(skill) > 0.0
        and accuracy is not None
        and float(accuracy) >= 0.48
        and data_coverage >= 0.80
    )
    level = "준기관급" if passed else ("검증중" if samples >= 24 else "자료부족")
    return {
        "passed": passed,
        "level": level,
        "requirements": {
            "samples_min": 48,
            "positive_brier_skill": True,
            "accuracy_min": 0.48,
            "data_coverage_min": 0.80,
        },
        "observed": {
            "samples": samples,
            "brier_skill_score": skill,
            "accuracy": accuracy,
            "data_coverage": round(data_coverage, 3),
        },
    }


def build_rate_forecast_v2(
    ecos: dict[str, list[dict[str, Any]]],
    kosis: dict[str, list[dict[str, Any]]],
    us_policy: dict[str, Any] | None,
) -> dict[str, Any]:
    base = numeric_series(ecos.get("kr_base_rate", []))
    y2 = numeric_series(ecos.get("kr_gov_2y", []))
    y3 = numeric_series(ecos.get("kr_gov_3y", []))
    fx = numeric_series(ecos.get("usdkrw", []))
    industrial = numeric_series(kosis.get("industrial_production", []))
    cpi_series, cpi_meta = core_cpi_yoy_series(kosis.get("cpi_core", []))

    current_rate = _latest(base)
    market_rate = _latest(y2) if y2 else _latest(y3)
    policy_gap = market_rate - current_rate if market_rate is not None and current_rate is not None else None
    inflation = _latest(cpi_series)
    growth = _annualized(industrial, 3)
    fx_3m = _pct_change(fx, min(60, max(1, len(fx) - 1))) if len(fx) > 1 else None
    us_current, us_rates = _us_path(us_policy)
    us_change = (mean(us_rates[:3]) - us_current) if us_current is not None and us_rates else None

    raw = probability_from_features(policy_gap, inflation, growth)
    backtest = rate_probability_backtest(
        ecos.get("kr_base_rate", []),
        ecos.get("kr_gov_2y", []) or ecos.get("kr_gov_3y", []),
        cpi_series,
        kosis.get("industrial_production", []),
    )
    calibrated, calibration_weight = calibrate_probabilities(raw, backtest)
    regime, regime_scores = _regime(inflation, growth, policy_gap, us_change, fx_3m)
    first = _regime_adjust(calibrated, regime, us_change, fx_3m)

    coverage_items = [bool(base), bool(y2 or y3), bool(fx), bool(cpi_series), bool(industrial), bool(us_rates)]
    coverage = sum(coverage_items) / len(coverage_items)
    gate = _quality_gate(backtest, coverage)

    meetings = []
    rate = current_rate
    probs = first
    for idx in range(1, 4):
        if rate is None:
            expected = None
        else:
            expected = _expected_rate(rate, probs)
        meetings.append({
            "meeting_ahead": idx,
            "probabilities": {k: round(probs[k], 3) for k in CLASSES},
            "expected_rate_pct": round(expected, 3) if expected is not None else None,
            "most_likely_action": max(probs, key=probs.get),
            "probability_type": "model_estimate",
        })
        if expected is not None:
            rate = expected
        probs = _next_probs(probs, regime)

    status = "ok" if current_rate is not None and market_rate is not None else "partial"
    return {
        "schema_version": "2.0.0",
        "status": status,
        "engine_scope": "korea_only_us_engine_read_only",
        "current": {
            "kr_base_rate_pct": current_rate,
            "market_reference_rate_pct": market_rate,
            "market_policy_gap_pctp": round(policy_gap, 3) if policy_gap is not None else None,
            "core_cpi_yoy": round(inflation, 5) if inflation is not None else None,
            "industrial_3m_annualized": round(growth, 5) if growth is not None else None,
            "usdkrw_3m_change": round(fx_3m, 5) if fx_3m is not None else None,
            "us_3meeting_rate_change_pctp": round(us_change, 3) if us_change is not None else None,
        },
        "regime": {
            "name": regime,
            "scores": regime_scores,
        },
        "meeting_path": meetings,
        "validation": {
            **backtest,
            "calibration_weight": calibration_weight,
            "quality_gate": gate,
            "core_cpi": cpi_meta,
        },
        "limitations": [
            "금통위별 직접 선물가격이 없어 확률은 모형 추정치입니다.",
            "미국 엔진 출력은 읽기 전용 입력이며 미국 계산은 변경하지 않습니다.",
            "실제 빈티지 데이터가 아닌 최신 이력으로 재구성된 구간은 개정 편향이 남을 수 있습니다.",
        ],
    }


def build_fx_forecast_v2(
    legacy_snapshot: dict[str, Any],
    rate_v2: dict[str, Any],
) -> dict[str, Any]:
    current = legacy_snapshot.get("current", {}) if isinstance(legacy_snapshot, dict) else {}
    forecast = legacy_snapshot.get("forecast", {}) if isinstance(legacy_snapshot, dict) else {}
    method = legacy_snapshot.get("methodology", {}) if isinstance(legacy_snapshot, dict) else {}
    spot = current.get("usdkrw")
    base_mid = forecast.get("usdkrw_mid")
    base_range = forecast.get("usdkrw_range")

    # Multi-horizon fan around the already walk-forward weighted legacy center.
    horizons = []
    if isinstance(spot, (int, float)) and isinstance(base_mid, (int, float)):
        drift = base_mid / spot - 1.0
        rmse = float(method.get("fx_backtest_rmse_pct") or 6.0) / 100.0
        for months, scale in ((1, 0.35), (3, 1.0), (6, 1.35), (12, 1.70)):
            mid = spot * (1.0 + drift * scale)
            band = rmse * sqrt(max(1.0, months / 3.0))
            horizons.append({
                "months": months,
                "mid": round(mid, 1),
                "range_80": [round(mid * (1.0 - band), 1), round(mid * (1.0 + band), 1)],
            })

    samples = int(method.get("fx_backtest_samples") or 0)
    rmse = method.get("fx_backtest_rmse_pct")
    fx_gate = {
        "passed": samples >= 120 and rmse is not None and float(rmse) <= 6.0,
        "level": "준기관급" if samples >= 120 and rmse is not None and float(rmse) <= 6.0 else "검증중",
        "observed": {"samples": samples, "rmse_pct": rmse},
        "requirements": {"samples_min": 120, "rmse_pct_max": 6.0},
    }
    return {
        "schema_version": "2.0.0",
        "status": legacy_snapshot.get("status", "partial") if isinstance(legacy_snapshot, dict) else "partial",
        "engine_scope": "korea_fx_v2",
        "current_usdkrw": spot,
        "legacy_3m_center": base_mid,
        "legacy_3m_range": base_range,
        "forecast_path": horizons,
        "validation": {
            "samples": samples,
            "rmse_pct": rmse,
            "quality_gate": fx_gate,
        },
        "rate_regime_link": rate_v2.get("regime"),
        "limitations": [
            "장기 구간은 단기 워크포워드 중심값의 확장 경로이며 별도 12개월 OOS 검증 전에는 참고용입니다."
        ],
    }
