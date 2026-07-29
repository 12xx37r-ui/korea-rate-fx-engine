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
    fx_walk_forward_validation,
    _fx_selective_forecast,
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


def _us_path(us_policy: dict[str, Any] | None) -> tuple[float | None, list[float], dict[str, Any]]:
    """Return a stabilized U.S. path without modifying the U.S. engine.

    The U.S. JSON can contain meeting-day reconstruction spikes even when the
    underlying monthly ZQ/SOFR curve is smooth.  Korea V2 therefore uses the
    monthly-average curve as the preferred read-only signal and rejects
    implausible meeting jumps (>35 bp) or values far from the contract average.
    """
    meta: dict[str, Any] = {
        "mode": "unavailable",
        "accepted": 0,
        "rejected": 0,
        "warnings": [],
    }
    if not isinstance(us_policy, dict):
        return None, [], meta
    fed = us_policy.get("fed") if isinstance(us_policy.get("fed"), dict) else {}
    current = us_policy.get("current_effective_rate", fed.get("current_effective_rate"))
    try:
        current_f = float(current)
    except (TypeError, ValueError):
        current_f = None

    path = us_policy.get("meeting_path")
    if not isinstance(path, list) or not path:
        path = fed.get("expected_path", [])

    stabilized: list[float] = []
    previous = current_f
    for row in path:
        if not isinstance(row, dict):
            continue
        monthly = row.get("monthly_average_rate")
        expected = row.get("expected_post_meeting_rate", row.get("expected_rate", row.get("rate")))
        try:
            monthly_f = float(monthly) if monthly is not None else None
        except (TypeError, ValueError):
            monthly_f = None
        try:
            expected_f = float(expected) if expected is not None else None
        except (TypeError, ValueError):
            expected_f = None

        chosen = None
        # Prefer the directly implied monthly contract average.  It is smoother
        # and less sensitive to a bad meeting-day weighting reconstruction.
        if monthly_f is not None:
            chosen = monthly_f
            if expected_f is not None and abs(expected_f - monthly_f) > 0.35:
                meta["rejected"] += 1
                meta["warnings"].append(
                    f"rejected meeting reconstruction {expected_f:.3f}; monthly average {monthly_f:.3f} used"
                )
        elif expected_f is not None:
            chosen = expected_f

        if chosen is None:
            continue
        if previous is not None and abs(chosen - previous) > 0.35:
            meta["rejected"] += 1
            meta["warnings"].append(
                f"rejected implausible sequential jump {previous:.3f}->{chosen:.3f}"
            )
            continue
        stabilized.append(chosen)
        previous = chosen
        meta["accepted"] += 1

    if stabilized:
        meta["mode"] = "monthly_curve_stabilized"
    return current_f, stabilized, meta


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
    """Production certification gate for the inputs the rate model actually uses.

    KRX and REB are useful enhancement sources, but they are not model inputs in
    the fixed V2 policy specification.  They therefore must not block or inflate
    certification.  The gate evaluates only the active ECOS, KOSIS, U.S.-policy
    and validation-integrity axes.  A release-lagged walk-forward reconstruction
    is accepted for production certification, while the absence of true
    real-time vintages remains explicitly disclosed.
    """
    samples = int(backtest.get("samples") or 0)
    skill = backtest.get("brier_skill_score")
    accuracy = backtest.get("accuracy")
    accuracy_lb = backtest.get("accuracy_wilson_lower_95")
    release_lag = bool(backtest.get("release_lag_backtest"))
    walk_forward = bool(backtest.get("walk_forward_backtest"))
    real_vintage = bool(backtest.get("real_time_vintage"))
    strict_pass = (
        samples >= 60
        and skill is not None
        and float(skill) >= 0.08
        and accuracy is not None
        and float(accuracy) >= 0.52
        and (accuracy_lb is None or float(accuracy_lb) >= 0.45)
        and data_coverage >= 0.95
        and release_lag
        and walk_forward
    )
    candidate = (
        not strict_pass
        and samples >= 48
        and skill is not None
        and float(skill) > 0.0
        and accuracy is not None
        and float(accuracy) >= 0.48
        and data_coverage >= 0.80
        and release_lag
    )
    level = "준기관급" if strict_pass else ("준기관급 후보" if candidate else ("검증중" if samples >= 24 else "자료부족"))
    return {
        "passed": strict_pass,
        "candidate": candidate,
        "level": level,
        "certification_basis": "release_lagged_expanding_walk_forward_fixed_spec",
        "requirements": {
            "samples_min": 60,
            "brier_skill_score_min": 0.08,
            "accuracy_min": 0.52,
            "accuracy_wilson_lower_95_min": 0.45,
            "active_input_coverage_min": 0.95,
            "release_lag_backtest_required": True,
            "walk_forward_backtest_required": True,
            "real_time_vintage_required": False,
        },
        "observed": {
            "samples": samples,
            "brier_skill_score": skill,
            "accuracy": accuracy,
            "accuracy_wilson_lower_95": accuracy_lb,
            "active_input_coverage": round(data_coverage, 3),
            "release_lag_backtest": release_lag,
            "walk_forward_backtest": walk_forward,
            "real_time_vintage": real_vintage,
        },
        "reasons": [
            reason for condition, reason in (
                (samples < 60, "검증 표본이 60개 미만입니다."),
                (skill is None or float(skill) < 0.08, "Brier skill이 8% 기준에 미달합니다."),
                (accuracy is None or float(accuracy) < 0.52, "방향 정확도가 52% 기준에 미달합니다."),
                (accuracy_lb is not None and float(accuracy_lb) < 0.45, "방향 정확도 95% 하한이 45% 기준에 미달합니다."),
                (data_coverage < 0.95, "실제 사용 입력축 완전성이 95% 미만입니다."),
                (not release_lag, "발표시차 반영 백테스트가 없습니다."),
                (not walk_forward, "순차 walk-forward 백테스트가 없습니다."),
            ) if condition
        ],
        "limitations": (["실시간 원본 빈티지 미적용: 발표시차 재구성 OOS 인증"] if not real_vintage else []),
    }


def build_rate_forecast_v2(
    ecos: dict[str, list[dict[str, Any]]],
    kosis: dict[str, list[dict[str, Any]]],
    us_policy: dict[str, Any] | None,
    source_status: dict[str, str] | None = None,
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
    us_current, us_rates, us_path_meta = _us_path(us_policy)
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

    source_status = source_status or {}
    # Certification coverage is based only on inputs used by the fixed model.
    # KRX/REB are reported as optional enhancements and cannot create a false
    # failure (or a false pass) when they are not part of probability_from_features.
    coverage_axes = {
        "ecos_rates_fx": 0.35 if (bool(base) and bool(y2 or y3) and bool(fx)) else 0.0,
        "kosis_macro": 0.25 if (bool(cpi_series) and bool(industrial)) else 0.0,
        "us_policy": 0.25 if bool(us_rates) else 0.0,
        "validation_integrity": 0.15 if bool(backtest.get("release_lag_backtest")) and bool(backtest.get("walk_forward_backtest")) else 0.0,
    }
    coverage = sum(coverage_axes.values())
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
        "engine_version": "2.7.0-objective-validation-final",
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
            "us_path_filter": us_path_meta,
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
            "data_coverage_axes": coverage_axes,
            "optional_enhancement_sources": {
                "krx_market": source_status.get("krx", "not_available"),
                "reb_financial_stability": source_status.get("reb", "not_available"),
                "used_in_fixed_rate_model": False,
            },
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
    ecos: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    current = legacy_snapshot.get("current", {}) if isinstance(legacy_snapshot, dict) else {}
    forecast = legacy_snapshot.get("forecast", {}) if isinstance(legacy_snapshot, dict) else {}
    method = legacy_snapshot.get("methodology", {}) if isinstance(legacy_snapshot, dict) else {}
    spot = current.get("usdkrw")
    base_mid = forecast.get("usdkrw_mid")
    base_range = forecast.get("usdkrw_range")

    fx_rows = (ecos or {}).get("usdkrw", [])
    fx_values = [v for _, v in numeric_series(fx_rows) if v > 0]
    fx_oos = fx_walk_forward_validation(fx_rows)
    horizons = []
    samples = int(fx_oos.get("samples") or method.get("fx_backtest_samples") or 0)
    rmse = fx_oos.get("rmse_pct")
    if rmse is None:
        rmse = method.get("fx_backtest_rmse_pct")
    direction = fx_oos.get("direction_accuracy")
    benchmark_skill = fx_oos.get("persistence_skill_pct")
    interval_coverage = fx_oos.get("interval_80_coverage")
    horizon_specific = bool(fx_oos.get("horizon_specific_oos"))
    active_coverage = fx_oos.get("active_signal_coverage")

    # Production safety: never deploy a model that loses to a no-change
    # random-walk benchmark.  When OOS skill is non-positive or directional
    # accuracy is below 50%, publish the benchmark center (spot) and use the
    # benchmark residual distribution for uncertainty bands.  The weaker model
    # remains visible in validation for auditability, but it cannot drive the
    # live dashboard forecast.
    fallback_to_benchmark = (
        benchmark_skill is None
        or float(benchmark_skill) <= 0.0
        or direction is None
        or float(direction) < 0.52
        or active_coverage is None
        or float(active_coverage) < 0.30
    )
    if isinstance(spot, (int, float)):
        production_horizons = []
        horizon_map = fx_oos.get("horizons", {}) if isinstance(fx_oos, dict) else {}
        for months, label, obs in ((1, "1m", 21), (3, "3m", 63), (6, "6m", 126), (12, "12m", 252)):
            row = horizon_map.get(label, {}) if isinstance(horizon_map, dict) else {}
            bench_rmse = row.get("random_walk_rmse_pct")
            if fallback_to_benchmark or not fx_values:
                mid = float(spot)
                active = False
                model_name = "random_walk_fallback"
                signal_meta = {"signal_60d": None, "drift": 0.0}
            else:
                mid, active, signal_meta = _fx_selective_forecast(fx_values, obs)
                model_name = "selective_60d_contrarian_shrunk" if active else "random_walk_abstention"
            sigma = float(bench_rmse or (3.0 * sqrt(max(1.0, months / 3.0)))) / 100.0
            half = 1.2816 * sigma
            production_horizons.append({
                "months": months,
                "mid": round(float(mid), 1),
                "range_80": [
                    round(float(mid) * (1.0 - half), 1),
                    round(float(mid) * (1.0 + half), 1),
                ],
                "production_model": model_name,
                "signal_active": bool(active),
                "signal_60d": round(float(signal_meta.get("signal_60d")), 5) if signal_meta.get("signal_60d") is not None else None,
                "forecast_drift": round(float(signal_meta.get("drift") or 0.0), 5),
            })
        horizons = production_horizons
    horizon_map = fx_oos.get("horizons", {}) if isinstance(fx_oos, dict) else {}
    horizon_requirements = {
        "1m": {"samples_min": 180, "rmse_pct_max": 3.0, "active_direction_accuracy_min": 0.52, "persistence_skill_pct_min": 0.0},
        "3m": {"samples_min": 180, "rmse_pct_max": 5.5, "active_direction_accuracy_min": 0.52, "persistence_skill_pct_min": 0.0},
        "6m": {"samples_min": 180, "rmse_pct_max": 7.0, "active_direction_accuracy_min": 0.52, "persistence_skill_pct_min": 0.0},
        "12m": {"samples_min": 150, "rmse_pct_max": 8.5, "active_direction_accuracy_min": 0.52, "persistence_skill_pct_min": 2.0},
    }
    horizon_gates: dict[str, Any] = {}
    for label, req in horizon_requirements.items():
        row = horizon_map.get(label, {}) if isinstance(horizon_map, dict) else {}
        row_samples = int(row.get("samples") or 0)
        row_rmse = row.get("rmse_pct")
        row_active_acc = row.get("active_direction_accuracy")
        row_skill = row.get("persistence_skill_pct")
        row_signal_cov = row.get("active_signal_coverage")
        row_interval_cov = row.get("interval_80_coverage")
        passed = (
            row_samples >= req["samples_min"]
            and row_rmse is not None and float(row_rmse) <= req["rmse_pct_max"]
            and row_active_acc is not None and float(row_active_acc) >= req["active_direction_accuracy_min"]
            and row_skill is not None and float(row_skill) > req["persistence_skill_pct_min"]
            and row_signal_cov is not None and float(row_signal_cov) >= 0.30
            and row_interval_cov is not None and 0.72 <= float(row_interval_cov) <= 0.88
        )
        reasons = []
        if row_samples < req["samples_min"]:
            reasons.append(f"표본이 {req['samples_min']}개 미만입니다.")
        if row_rmse is None or float(row_rmse) > req["rmse_pct_max"]:
            reasons.append(f"RMSE가 {req['rmse_pct_max']}% 기준을 초과합니다.")
        if row_active_acc is None or float(row_active_acc) < req["active_direction_accuracy_min"]:
            reasons.append("활성 신호 방향 적중률이 52% 기준에 미달합니다.")
        if row_skill is None or float(row_skill) <= req["persistence_skill_pct_min"]:
            reasons.append(f"랜덤워크 대비 skill이 {req['persistence_skill_pct_min']}% 기준을 넘지 못했습니다.")
        if row_signal_cov is None or float(row_signal_cov) < 0.30:
            reasons.append("활성 신호 표본 비중이 30% 기준에 미달합니다.")
        if row_interval_cov is None or not 0.72 <= float(row_interval_cov) <= 0.88:
            reasons.append("80% 예측구간 포함률이 허용범위를 벗어납니다.")
        horizon_gates[label] = {
            "passed": passed,
            "level": "준기관급" if passed else ("참고용" if row_samples >= req["samples_min"] else "자료부족"),
            "observed": {
                "samples": row_samples,
                "rmse_pct": row_rmse,
                "active_direction_accuracy": row_active_acc,
                "all_origin_direction_accuracy": row.get("direction_accuracy"),
                "persistence_skill_pct": row_skill,
                "active_signal_coverage": row_signal_cov,
                "interval_80_coverage": row_interval_cov,
            },
            "requirements": req | {
                "active_signal_coverage_min": 0.30,
                "interval_80_coverage_range": [0.72, 0.88],
            },
            "reasons": reasons,
        }

    passed_horizons = [label for label, gate in horizon_gates.items() if gate["passed"]]
    primary_gate = horizon_gates.get("3m", {})
    strict_pass = bool(primary_gate.get("passed"))
    candidate = (not strict_pass) and any(
        gate.get("level") == "참고용" for gate in horizon_gates.values()
    )
    fx_gate = {
        "passed": strict_pass,
        "candidate": candidate,
        "level": "준기관급(3개월)" if strict_pass else ("준기관급 후보" if candidate else "검증미달·기준모형 사용"),
        "primary_horizon": "3m",
        "passed_horizons": passed_horizons,
        "observed": {
            "samples": samples,
            "rmse_pct": rmse,
            "active_direction_accuracy": direction,
            "all_origin_direction_accuracy": fx_oos.get("all_origin_direction_accuracy"),
            "persistence_skill_pct": benchmark_skill,
            "active_signal_coverage": active_coverage,
            "interval_80_coverage": interval_coverage,
            "horizon_specific_oos": horizon_specific,
        },
        "requirements": {
            "primary_horizon": "3m",
            "horizon_specific_oos_required": True,
            "see_horizon_quality_gates": True,
        },
        "horizon_quality_gates": horizon_gates,
        "reasons": list(primary_gate.get("reasons") or []),
    }
    return {
        "schema_version": "2.0.0",
        "status": legacy_snapshot.get("status", "partial") if isinstance(legacy_snapshot, dict) else "partial",
        "engine_scope": "korea_fx_v2",
        "current_usdkrw": spot,
        "legacy_3m_center": base_mid,
        "legacy_3m_range": base_range,
        "forecast_path": horizons,
        "production_model": "random_walk_fallback" if fallback_to_benchmark else "selective_60d_contrarian_shrunk",
        "active_model_blocked": bool(fallback_to_benchmark),
        "validation": {
            "samples": samples,
            "rmse_pct": rmse,
            "mae_pct": fx_oos.get("mae_pct"),
            "active_direction_accuracy": direction,
            "all_origin_direction_accuracy": fx_oos.get("all_origin_direction_accuracy"),
            "persistence_skill_pct": benchmark_skill,
            "active_signal_coverage": active_coverage,
            "interval_80_coverage": interval_coverage,
            "horizon_specific_oos": horizon_specific,
            "oos_by_horizon": fx_oos.get("horizons", {}),
            "validation_method": fx_oos.get("method"),
            "model_specification": fx_oos.get("model_specification"),
            "quality_gate": fx_gate,
        },
        "rate_regime_link": rate_v2.get("regime"),
        "limitations": [
            "1·3·6·12개월 OOS를 각각 검증하며, 기간별 품질 게이트 결과를 따로 표시합니다.",
            "12개월 OOS는 수행됐지만 랜덤워크 대비 개선폭이 2% 이하이면 참고용으로 제한합니다.",
            "활성 모형이 랜덤워크보다 못하면 실전 출력은 자동으로 랜덤워크 중심값으로 후퇴합니다.",
            "신호가 약한 시점에는 예측을 강제하지 않고 현재 환율 중심값을 유지합니다."
        ],
    }
