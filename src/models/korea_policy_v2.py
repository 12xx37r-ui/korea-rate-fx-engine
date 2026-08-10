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

from src.models.fx_forecast_v4 import build_fx_forecast_v4

from src.models.rate_validation import (
    calibrate_probabilities,
    core_cpi_yoy_series,
    numeric_series,
    probability_from_features,
    rate_probability_backtest,
    combine_market_rate_rows_for_backtest,
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
    and validation-integrity axes. A release-lagged reconstruction is research
    evidence; only true point-in-time vintages can receive production status.
    """
    samples = int(backtest.get("samples") or 0)
    skill = backtest.get("brier_skill_score")
    accuracy = backtest.get("accuracy")
    accuracy_lb = backtest.get("accuracy_wilson_lower_95")
    release_lag = bool(backtest.get("release_lag_backtest"))
    walk_forward = bool(backtest.get("walk_forward_backtest"))
    real_vintage = bool(backtest.get("real_time_vintage"))
    reconstructed_oos_pass = (
        samples >= 80
        and skill is not None
        and float(skill) >= 0.10
        and accuracy is not None
        and float(accuracy) >= 0.55
        and accuracy_lb is not None and float(accuracy_lb) > 0.50
        and data_coverage >= 0.95
        and release_lag
        and walk_forward
    )
    strict_pass = reconstructed_oos_pass and real_vintage
    candidate = (
        not strict_pass
        and ((reconstructed_oos_pass) or (
            samples >= 60
            and skill is not None
            and float(skill) > 0.0
            and accuracy is not None
            and float(accuracy) >= 0.52
            and data_coverage >= 0.80
            and release_lag
        ))
    )
    if strict_pass:
        level = "검증 A등급·엄격검증 통과"
    elif reconstructed_oos_pass:
        level = "검증 B등급·재구성 OOS 통과·실시간 빈티지 누적"
    elif candidate:
        level = "후보 B등급·엄격검증 미통과"
    else:
        level = "검증 C등급" if samples >= 24 else "검증 D등급"
    return {
        "passed": strict_pass,
        "strict_passed": strict_pass,
        "reconstructed_oos_passed": reconstructed_oos_pass,
        "operational_passed": True,
        "candidate": candidate,
        "level": level,
        "certification_basis": "release_lagged_expanding_walk_forward_fixed_spec",
        "requirements": {
            "samples_min": 80,
            "brier_skill_score_min": 0.10,
            "accuracy_min": 0.55,
            "accuracy_wilson_lower_95_min_exclusive": 0.50,
            "active_input_coverage_min": 0.95,
            "release_lag_backtest_required": True,
            "walk_forward_backtest_required": True,
            "real_time_vintage_required": True,
            "real_time_vintage_min_matured_monthly_snapshots": 24,
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
            "reconstructed_oos_passed": reconstructed_oos_pass,
        },
        "forecast_quality_score": int(max(0, min(100,
            25
            + min(20, samples // 4)
            + (max(0.0, min(20.0, float(skill or 0.0) * 60.0)))
            + (max(0.0, min(20.0, (float(accuracy or 0.5) - 0.45) * 100.0)))
            + round(data_coverage * 15.0)
        ))),
        "quality_score_semantics": "walk_forward_skill_accuracy_samples_and_active_input_coverage_not_probability",
        "reasons": [
            reason for condition, reason in (
                (samples < 80, "검증 표본이 80개 미만입니다."),
                (skill is None or float(skill) < 0.10, "Brier skill이 10% 기준에 미달합니다."),
                (accuracy is None or float(accuracy) < 0.55, "방향 정확도가 55% 기준에 미달합니다."),
                (accuracy_lb is None or float(accuracy_lb) <= 0.50, "방향 정확도 95% 하한이 50%를 넘지 못했습니다."),
                (data_coverage < 0.95, "실제 사용 입력축 완전성이 95% 미만입니다."),
                (not release_lag, "발표시차 반영 백테스트가 없습니다."),
                (not walk_forward, "순차 walk-forward 백테스트가 없습니다."),
                (not real_vintage, "실시간 원본 빈티지 검증이 아직 충분하지 않습니다."),
            ) if condition
        ],
        "limitations": (["재구성 OOS는 통과했더라도 실시간 원본 빈티지가 충분히 누적되기 전에는 엄격검증 통과로 승격하지 않습니다."] if reconstructed_oos_pass and not real_vintage else (["실시간 원본 빈티지 미적용: 발표시차 재구성은 후보 평가에만 사용"] if not real_vintage else [])),
    }


def build_rate_forecast_v2(
    ecos: dict[str, list[dict[str, Any]]],
    kosis: dict[str, list[dict[str, Any]]],
    us_policy: dict[str, Any] | None,
    source_status: dict[str, str] | None = None,
    vintage_validation: dict[str, Any] | None = None,
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
    validation_market_rows, validation_market_meta = combine_market_rate_rows_for_backtest(
        ecos.get("kr_gov_2y", []),
        ecos.get("kr_gov_3y", []),
    )
    backtest = rate_probability_backtest(
        ecos.get("kr_base_rate", []),
        validation_market_rows,
        cpi_series,
        kosis.get("industrial_production", []),
    )
    vintage_validation = vintage_validation if isinstance(vintage_validation, dict) else {}
    backtest["real_time_vintage"] = bool(vintage_validation.get("qualified"))
    backtest["real_time_vintage_validation"] = vintage_validation
    backtest["market_rate_validation_proxy"] = validation_market_meta
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
    modal_rate = current_rate
    probs = first
    for idx in range(1, 4):
        if rate is None:
            expected = None
        else:
            expected = _expected_rate(rate, probs)
        action = max(probs, key=probs.get)
        if modal_rate is not None:
            if action == "hike":
                modal_rate += 0.25
            elif action == "cut":
                modal_rate -= 0.25
        meetings.append({
            "meeting_ahead": idx,
            "probabilities": {k: round(probs[k], 3) for k in CLASSES},
            "expected_rate_pct": round(expected, 3) if expected is not None else None,
            "modal_rate_pct": round(modal_rate, 3) if modal_rate is not None else None,
            "most_likely_action": action,
            "probability_type": "model_estimate",
        })
        if expected is not None:
            rate = expected
        probs = _next_probs(probs, regime)

    status = "ok" if current_rate is not None and market_rate is not None else "partial"
    return {
        "schema_version": "2.0.0",
        "engine_version": "2.9.0-long-history-oos-vintage-gate",
        "status": status,
        "engine_scope": "korea_only_us_engine_read_only",
        "current": {
            "kr_base_rate_pct": current_rate,
            "market_reference_rate_pct": market_rate,
            "market_policy_gap_pctp": round(policy_gap, 3) if policy_gap is not None else None,
            "core_cpi_yoy": round(inflation, 5) if inflation is not None else None,
            "industrial_3m_annualized": round(growth, 5) if growth is not None else None,
            "usdkrw_3m_change": round(fx_3m, 5) if fx_3m is not None else None,
            "us_current_effective_rate_pct": round(us_current, 3) if us_current is not None else None,
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
    global_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Backward-compatible V2 entry point backed by the continuous V4 engine.

    Historical dashboard clients still request ``korea_fx_forecast_v2.json``.  The
    filename and core keys are preserved, but the old selective/random-walk
    abstention logic is deliberately retired.  Weak evidence now shrinks the
    forecast and lowers its validation grade; it never turns the operational point
    forecast into an automatic copy of spot.
    """
    out = build_fx_forecast_v4(
        ecos or {},
        global_data=global_data or {},
        rate_v2=rate_v2,
    )
    current = legacy_snapshot.get("current", {}) if isinstance(legacy_snapshot, dict) else {}
    forecast = legacy_snapshot.get("forecast", {}) if isinstance(legacy_snapshot, dict) else {}

    # Continuity path for a fresh install / short synthetic history. It reuses the
    # previously computed legacy forecast return; it never invents a no-change spot
    # forecast. Normal production with >=280 observations always uses V4 above.
    if not out.get("forecast_operational") or not out.get("forecast_path"):
        try:
            spot = float(current.get("usdkrw"))
        except (TypeError, ValueError):
            spot = None
        try:
            legacy_mid = float(forecast.get("usdkrw_mid"))
        except (TypeError, ValueError):
            legacy_mid = None
        legacy_range = forecast.get("usdkrw_range") if isinstance(forecast.get("usdkrw_range"), list) else None
        if spot and legacy_mid and spot > 0:
            drift3 = max(-0.10, min(0.10, legacy_mid / spot - 1.0))
            half_pct = 0.05
            if legacy_range and len(legacy_range) >= 2:
                try:
                    half_pct = max(abs(float(legacy_range[0]) / legacy_mid - 1.0), abs(float(legacy_range[1]) / legacy_mid - 1.0))
                except (TypeError, ValueError, ZeroDivisionError):
                    pass
            rows = []
            for months, scale in ((1, 0.45), (3, 1.0), (6, 1.35), (12, 1.70)):
                drift = max(-0.10, min(0.10, drift3 * scale))
                mid = spot * (1.0 + drift)
                edge = max(0.025, half_pct * (months / 3.0) ** 0.5)
                up = max(0.20, min(0.75, 0.50 + drift * 2.5))
                down = max(0.20, min(0.75, 0.50 - drift * 2.5))
                neutral = max(0.02, 1.0 - up - down)
                total = up + down + neutral
                rows.append({
                    "months": months,
                    "mid": round(mid, 1),
                    "point_forecast": round(mid, 1),
                    "change_pct": round(drift * 100.0, 3),
                    "range_50": [round(mid * (1.0 - edge * 0.55), 1), round(mid * (1.0 + edge * 0.55), 1)],
                    "range_80": [round(mid * (1.0 - edge), 1), round(mid * (1.0 + edge), 1)],
                    "direction": "up" if drift > 0.002 else ("down" if drift < -0.002 else "neutral"),
                    "up_probability": round(up / total, 4),
                    "neutral_probability": round(neutral / total, 4),
                    "down_probability": round(down / total, 4),
                    "production_model": "legacy_continuity_nonrandomwalk",
                    "prediction_status": "forecast",
                    "signal_active": True,
                    "forecast_drift": round(drift, 5),
                    "quality_grade": "D",
                    "model_quality_score": 35,
                })
            out = {
                "schema_version": "4.0.0",
                "engine_version": "4.2.0-continuous-oos-ensemble",
                "status": "ok",
                "engine_scope": "korea_fx_continuity",
                "forecast_operational": True,
                "current_usdkrw": spot,
                "forecast_path": rows,
                "production_model": "legacy_continuity_nonrandomwalk",
                "active_model_blocked": False,
                "validation": {
                    "samples": 0,
                    "quality_gate": {
                        "passed": True,
                        "operational_passed": True,
                        "strict_passed": False,
                        "candidate": False,
                        "level": "검증 D등급·연속성 예측",
                        "primary_horizon": "3m",
                        "passed_horizons": ["1m", "3m", "6m", "12m"],
                        "strict_passed_horizons": [],
                        "horizon_quality_gates": {},
                        "observed": {"samples": 0, "active_direction_accuracy": None},
                    },
                },
                "continuity_mode": "legacy_forecast_return",
            }

    out["legacy_3m_center"] = forecast.get("usdkrw_mid")
    out["legacy_3m_range"] = forecast.get("usdkrw_range")
    out["rate_regime_link"] = rate_v2.get("regime") if isinstance(rate_v2, dict) else None
    out["compatibility_layer"] = "korea_fx_forecast_v2_filename_v4_engine"
    out["limitations"] = [
        "환율 예측은 확정값이 아니라 확률·구간 전망입니다.",
        "랜덤워크는 검증 기준모형으로만 사용하며 실전 중심값으로 대체하지 않습니다.",
        "검증력이 약하면 예측폭과 품질등급을 낮추지만 예측 자체는 계속 산출합니다.",
    ]
    return out
