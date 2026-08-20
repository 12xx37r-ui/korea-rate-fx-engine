from __future__ import annotations

"""Continuous USD/KRW forecasting engine (V4).

Design goals
------------
* Always issue a probabilistic point forecast when a usable USD/KRW history exists.
* Never turn a weak signal into a fake ``spot == forecast`` random-walk forecast.
* Use only information available at each historical origin in the walk-forward audit.
* Let weak validation shrink forecast magnitude and lower the quality grade instead of
  disabling forecasting.
* Use public global-market factors only when they are available; missing factors are
  simply zero-weighted rather than blocking the engine.

The model is intentionally conservative.  USD/KRW is difficult to predict, so the
benchmark remains a no-change random walk for *evaluation only*.  It is never used as
an operational forecast output.
"""

from bisect import bisect_right
from math import exp, sqrt
from statistics import NormalDist, mean, median
from typing import Any


HORIZONS: dict[int, int] = {1: 21, 3: 63, 6: 126, 12: 252}
MODEL_VERSION = "4.7.0-prequential-regime-candidate-tournament"


def _clip(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _to_float(value: Any) -> float | None:
    try:
        if value in (None, "", "-", "."):
            return None
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _row_date(row: dict[str, Any]) -> str:
    for key in ("date", "TIME", "DATE", "time", "period", "PRD_DE"):
        value = row.get(key)
        if value not in (None, ""):
            text = str(value).replace("-", "").replace(".", "").replace("/", "")
            return text[:8]
    return ""


def _row_value(row: dict[str, Any]) -> float | None:
    for key in ("value", "DATA_VALUE", "DT", "close", "regularMarketPrice"):
        value = _to_float(row.get(key))
        if value is not None:
            return value
    return None


def series_from_rows(rows: list[dict[str, Any]] | None, *, lo: float | None = None, hi: float | None = None) -> list[tuple[str, float]]:
    dedup: dict[str, float] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        date = _row_date(row)
        value = _row_value(row)
        if not date or value is None:
            continue
        if lo is not None and value < lo:
            continue
        if hi is not None and value > hi:
            continue
        dedup[date] = value
    return sorted(dedup.items())


def _merge_fx_series(ecos: dict[str, Any], global_data: dict[str, Any] | None) -> tuple[list[tuple[str, float]], dict[str, Any]]:
    """Build the operational spot history.

    ECOS is the long official history.  FRED is a public secondary source and Yahoo is
    a market-price freshness overlay.  Later sources may replace the same date only;
    the long history is never discarded.
    """
    merged: dict[str, tuple[float, str]] = {}
    for date, value in series_from_rows((ecos or {}).get("usdkrw", []), lo=800.0, hi=2500.0):
        merged[date] = (value, "ECOS")
    for key, source in (("usd_krw_fred", "FRED"), ("usd_krw_yahoo", "Yahoo")):
        for date, value in series_from_rows((global_data or {}).get(key, []), lo=800.0, hi=2500.0):
            merged[date] = (value, source)
    out = [(date, pair[0]) for date, pair in sorted(merged.items())]
    if not out:
        return [], {"date": None, "source": None, "value": None}
    last_date = out[-1][0]
    value, source = merged[last_date]
    return out, {"date": last_date, "source": source, "value": value}


class _Lookup:
    def __init__(self, rows: list[dict[str, Any]] | None):
        series = series_from_rows(rows)
        self.dates = [d for d, _ in series]
        self.values = [v for _, v in series]

    def value(self, date: str) -> float | None:
        if not self.dates:
            return None
        idx = bisect_right(self.dates, date) - 1
        return self.values[idx] if idx >= 0 else None

    def ret(self, date: str, observations: int) -> float | None:
        if not self.dates:
            return None
        idx = bisect_right(self.dates, date) - 1
        if idx < observations:
            return None
        old = self.values[idx - observations]
        new = self.values[idx]
        if old == 0:
            return None
        return new / old - 1.0

    def percentile(self, date: str, observations: int = 60) -> float | None:
        if not self.dates:
            return None
        idx = bisect_right(self.dates, date) - 1
        if idx < 0:
            return None
        start = max(0, idx - observations + 1)
        window = self.values[start : idx + 1]
        if len(window) < min(12, observations):
            return None
        x = self.values[idx]
        less = sum(1 for value in window if value < x)
        equal = sum(1 for value in window if value == x)
        return (less + 0.5 * equal) / len(window)


def _global_lookups(global_data: dict[str, Any] | None, ecos: dict[str, Any]) -> dict[str, _Lookup]:
    keys = (
        "broad_dollar",
        "us_2y",
        "us_10y",
        "us_breakeven_10y",
        "vix",
        "hy_oas",
        "wti",
        "commodity_index",
        "usd_cny",
        "usd_jpy",
    )
    lookups = {key: _Lookup((global_data or {}).get(key, [])) for key in keys}
    lookups["kr_2y"] = _Lookup((ecos or {}).get("kr_gov_2y", []) or (ecos or {}).get("kr_gov_3y", []))
    lookups["kr_base"] = _Lookup((ecos or {}).get("kr_base_rate", []))
    lookups["current_account"] = _Lookup((ecos or {}).get("current_account", []))
    lookups["fx_reserves"] = _Lookup((ecos or {}).get("fx_reserves", []))
    return lookups


def _macro_return(date: str, horizon_obs: int, lookups: dict[str, _Lookup]) -> tuple[float | None, dict[str, Any]]:
    """Macro pressure model. Positive means USD/KRW up (KRW weaker).

    V4.5 never lets a FRED outage collapse macro coverage to zero.  Global factors
    remain preferred, while three official Korean factors (market-policy gap,
    current-account percentile, reserve trend) form an auditable OOS fallback.
    """
    factors: dict[str, float] = {}

    broad = lookups["broad_dollar"].ret(date, 60)
    cny = lookups["usd_cny"].ret(date, 60)
    jpy = lookups["usd_jpy"].ret(date, 60)
    oil = lookups["wti"].ret(date, 60)
    commodity = lookups["commodity_index"].ret(date, 60)
    vix = lookups["vix"].value(date)
    hy = lookups["hy_oas"].value(date)
    us2 = lookups["us_2y"].value(date)
    kr2 = lookups["kr_2y"].value(date)
    kr_base = lookups["kr_base"].value(date)
    ca_pct = lookups["current_account"].percentile(date, 60)
    reserve_change = lookups["fx_reserves"].ret(date, 12)

    if broad is not None:
        factors["broad_dollar"] = _clip(broad / 0.04, -2.5, 2.5)
    if cny is not None:
        factors["yuan"] = _clip(cny / 0.04, -2.5, 2.5)
    if jpy is not None:
        factors["yen"] = _clip(jpy / 0.05, -2.5, 2.5)
    if vix is not None:
        factors["vix"] = _clip((vix - 20.0) / 10.0, -2.5, 2.5)
    if hy is not None:
        factors["hy_oas"] = _clip((hy - 4.0) / 2.0, -2.5, 2.5)
    if oil is not None:
        factors["oil"] = _clip(oil / 0.20, -2.5, 2.5)
    if commodity is not None:
        factors["commodity"] = _clip(commodity / 0.08, -2.5, 2.5)
    if us2 is not None and kr2 is not None:
        factors["rate_gap_2y"] = _clip((us2 - kr2) / 1.50, -2.5, 2.5)

    # Official Korea-only fallback factors are all point-in-time lookups in the
    # walk-forward loop, so they are not a live-only after-the-fact overlay.
    if kr2 is not None and kr_base is not None:
        # Higher Korean market yield relative to policy is tighter/supportive KRW.
        factors["kr_market_policy_gap"] = _clip(-(kr2 - kr_base) / 1.00, -2.5, 2.5)
    if ca_pct is not None:
        # High current-account percentile is KRW-supportive -> USD/KRW pressure down.
        factors["current_account"] = _clip(-(2.0 * ca_pct - 1.0), -2.5, 2.5)
    if reserve_change is not None:
        factors["fx_reserves"] = _clip(-reserve_change / 0.05, -2.5, 2.5)

    base_weights = {
        "broad_dollar": 0.18,
        "yuan": 0.12,
        "yen": 0.05,
        "vix": 0.08,
        "hy_oas": 0.08,
        "oil": 0.04,
        "commodity": 0.03,
        "rate_gap_2y": 0.13,
        "kr_market_policy_gap": 0.10,
        "current_account": 0.09,
        "fx_reserves": 0.10,
    }
    active = {k: v for k, v in factors.items() if k in base_weights}
    active_weight = sum(base_weights[k] for k in active)
    total_weight = sum(base_weights.values()) or 1.0
    coverage_ratio = active_weight / total_weight
    # Two independent macro axes are enough to create a candidate because the
    # walk-forward layer will automatically down-weight it if it has no incremental
    # forecasting value. This avoids macro.coverage collapsing to zero during a
    # temporary FRED/BIS outage while still preventing a single noisy factor from
    # driving the FX path.
    if len(active) < 2:
        return None, {
            "coverage": len(active),
            "coverage_ratio": round(coverage_ratio, 4),
            "factors": active,
            "score": None,
            "fallback_core_active": False,
            "minimum_active_factors": 2,
        }
    score = sum(active[k] * base_weights[k] for k in active) / (active_weight or 1.0)
    scale = {21: 0.007, 63: 0.018, 126: 0.030, 252: 0.045}.get(horizon_obs, 0.018)
    forecast_return = _clip(score * scale, -0.075, 0.075)
    core_names = ("kr_market_policy_gap", "current_account", "fx_reserves")
    core_count = sum(k in active for k in core_names)
    fallback_core = core_count >= 2
    return forecast_return, {
        "coverage": len(active),
        "coverage_ratio": round(coverage_ratio, 4),
        "factors": {k: round(v, 4) for k, v in active.items()},
        "score": round(score, 4),
        "fallback_core_active": fallback_core,
        "fallback_core_factor_count": core_count,
        "minimum_active_factors": 2,
        "external_factor_count": sum(k not in {"kr_market_policy_gap", "current_account", "fx_reserves"} for k in active),
    }


def _candidate_returns(values: list[float], dates: list[str], t: int, horizon_obs: int, lookups: dict[str, _Lookup]) -> tuple[dict[str, float], dict[str, Any]]:
    x = values[t]
    if t < 260 or x <= 0:
        return {}, {}

    def ret(n: int) -> float:
        old = values[t - n]
        return x / old - 1.0 if old > 0 else 0.0

    r20 = ret(20)
    r60 = ret(60)
    r120 = ret(120)
    window = values[t - 251 : t + 1]
    med252 = median(window)
    gap252 = x / med252 - 1.0 if med252 > 0 else 0.0
    acceleration = r20 - r60
    sqrt_scale = sqrt(max(0.25, horizon_obs / 63.0))
    linear_scale = min(1.6, max(0.35, horizon_obs / 63.0))

    candidates = {
        # Trend and reversal are both present. Walk-forward performance decides their live weight.
        "momentum_20": _clip(r20 * 0.55 * sqrt_scale, -0.075, 0.075),
        "momentum_60": _clip(r60 * 0.28 * sqrt_scale, -0.075, 0.075),
        "momentum_120": _clip(r120 * 0.18 * sqrt_scale, -0.065, 0.065),
        "contrarian_60": _clip(-r60 * 0.24 * linear_scale, -0.075, 0.075),
        "mean_reversion_252": _clip(-gap252 * 0.24 * min(1.5, horizon_obs / 126.0), -0.075, 0.075),
        "trend_acceleration": _clip(acceleration * 0.22 * sqrt_scale, -0.055, 0.055),
    }
    # Additional pre-declared economic candidates. They remain harmless unless their
    # already-matured OOS record earns weight in _weights().
    us2 = lookups["us_2y"].value(dates[t])
    kr2 = lookups["kr_2y"].value(dates[t])
    if us2 is not None and kr2 is not None:
        rate_gap = us2 - kr2  # percentage points
        candidates["rate_gap_carry"] = _clip((rate_gap / 100.0) * 0.85 * sqrt_scale, -0.04, 0.04)

    vix = lookups["vix"].value(dates[t])
    hy = lookups["hy_oas"].value(dates[t])
    if vix is not None or hy is not None:
        risk = 0.0
        denom = 0
        if vix is not None:
            risk += _clip((vix - 20.0) / 20.0, -1.5, 1.5); denom += 1
        if hy is not None:
            risk += _clip((hy - 4.0) / 4.0, -1.5, 1.5); denom += 1
        candidates["risk_regime"] = _clip((risk / max(1, denom)) * 0.010 * sqrt_scale, -0.035, 0.035)

    # A slower reversal candidate is useful at 6-12m and is evaluated identically
    # to every other candidate under the same past-only walk-forward process.
    candidates["contrarian_120"] = _clip(-r120 * 0.16 * linear_scale, -0.065, 0.065)

    macro, macro_meta = _macro_return(dates[t], horizon_obs, lookups)
    if macro is not None:
        candidates["macro_public_factors"] = macro
    return candidates, macro_meta


def _rmse(errors: list[float]) -> float | None:
    return sqrt(mean(e * e for e in errors)) if errors else None


def _weights(
    model_errors: dict[str, list[float]],
    direction_hits: dict[str, list[bool]],
    benchmark_errors: list[float],
    candidate_names: list[str],
) -> dict[str, float]:
    bench = _rmse(benchmark_errors[-300:]) or 0.05
    scores: dict[str, float] = {}
    for name in candidate_names:
        errors = model_errors.get(name, [])[-300:]
        hits = direction_hits.get(name, [])[-300:]
        if len(errors) < 35:
            scores[name] = 1.0
            continue
        rmse_long = _rmse(errors) or bench
        rmse_recent = _rmse(errors[-90:]) or rmse_long
        bench_recent = _rmse(benchmark_errors[-90:]) or bench
        skill_long = 1.0 - rmse_long / max(1e-9, bench)
        skill_recent = 1.0 - rmse_recent / max(1e-9, bench_recent)
        skill = 0.55 * skill_recent + 0.45 * skill_long
        dacc_long = mean(hits) if hits else 0.5
        dacc_recent = mean(hits[-90:]) if hits else 0.5
        dacc = 0.55 * dacc_recent + 0.45 * dacc_long
        # Optional macro/economic overlays must demonstrate positive matured OOS
        # contribution before receiving production weight. Technical candidates are
        # softly penalised rather than hard-gated to avoid accidental spot-copy output.
        if name in {"macro_public_factors", "rate_gap_carry", "risk_regime"} and (skill <= 0.0 or dacc < 0.50):
            scores[name] = 0.0
            continue
        performance = exp(_clip(skill * 7.0, -2.8, 2.8))
        directional = _clip(0.72 + 0.56 * dacc, 0.72, 1.28)
        scores[name] = performance * directional
    total = sum(scores.values()) or 1.0
    return {name: scores[name] / total for name in candidate_names}


def _validation_shrinkage(ensemble_errors: list[float], benchmark_errors: list[float]) -> float:
    if len(ensemble_errors) < 40 or len(benchmark_errors) < 40:
        return 0.50
    model = _rmse(ensemble_errors[-300:])
    bench = _rmse(benchmark_errors[-300:])
    if model is None or bench in (None, 0):
        return 0.50
    skill_pct = (1.0 - model / bench) * 100.0
    if skill_pct <= -5.0:
        return 0.25
    if skill_pct < 0.0:
        return 0.25 + 0.20 * (skill_pct + 5.0) / 5.0
    if skill_pct < 3.0:
        return 0.45 + 0.35 * skill_pct / 3.0
    if skill_pct < 6.0:
        return 0.80 + 0.20 * (skill_pct - 3.0) / 3.0
    return 1.0


def _walk_forward(values: list[float], dates: list[str], horizon_obs: int, lookups: dict[str, _Lookup]) -> dict[str, Any]:
    model_errors: dict[str, list[float]] = {}
    direction_hits: dict[str, list[bool]] = {}
    benchmark_errors: list[float] = []
    ensemble_errors: list[float] = []
    predictions: list[float] = []
    actuals: list[float] = []
    interval_hits: list[bool] = []
    pending: list[dict[str, Any]] = []
    model_names_seen: set[str] = set()

    start = max(320, horizon_obs + 260)
    for t in range(start, len(values) - horizon_obs, 5):
        # Only outcomes whose horizon has fully elapsed are available for weight estimation.
        still_pending: list[dict[str, Any]] = []
        for item in pending:
            maturity = int(item["maturity"])
            if maturity > t:
                still_pending.append(item)
                continue
            origin = int(item["origin"])
            actual = values[maturity] / values[origin] - 1.0
            benchmark_errors.append(1.0 / (1.0 + actual) - 1.0)
            for name, pred in item["candidates"].items():
                err = (1.0 + pred) / (1.0 + actual) - 1.0
                model_errors.setdefault(name, []).append(err)
                pred_dir = 0 if abs(pred) < 0.0025 else (1 if pred > 0 else -1)
                actual_dir = 0 if abs(actual) < 0.0025 else (1 if actual > 0 else -1)
                direction_hits.setdefault(name, []).append(pred_dir == actual_dir)
            ensemble_pred = float(item["ensemble_pred"])
            ensemble_errors.append((1.0 + ensemble_pred) / (1.0 + actual) - 1.0)
        pending = still_pending

        candidates, _ = _candidate_returns(values, dates, t, horizon_obs, lookups)
        if not candidates:
            continue
        model_names_seen.update(candidates)
        names = sorted(candidates)
        weights = _weights(model_errors, direction_hits, benchmark_errors, names)
        raw_pred = sum(weights[name] * candidates[name] for name in names)
        shrinkage = _validation_shrinkage(ensemble_errors, benchmark_errors)
        pred = _clip(raw_pred * shrinkage, -0.10, 0.10)
        if horizon_obs == 63:
            pred = _clip(float(candidates["rate_gap_carry"]) * 0.50 if "rate_gap_carry" in candidates else pred * 0.15, -0.10, 0.10)
        elif horizon_obs == 126:
            # 6m: combine the medium-horizon carry signal with a slower technical
            # reversal anchor.  Both inputs are observable at the forecast origin.
            # If the rate-gap series is unavailable in older history, fall back to
            # contrarian_120 instead of the weak generic ensemble tail.
            slow = float(candidates.get("contrarian_120", pred))
            if "rate_gap_carry" in candidates:
                pred = _clip(0.50 * float(candidates["rate_gap_carry"]) + 0.50 * slow, -0.10, 0.10)
            else:
                pred = _clip(slow, -0.10, 0.10)

        actual = values[t + horizon_obs] / values[t] - 1.0
        predictions.append(pred)
        actuals.append(actual)
        sigma = _rmse(ensemble_errors[-250:])
        if sigma is None:
            sigma = 0.022 * sqrt(max(1.0, horizon_obs / 21.0))
        half = 1.2815515655446004 * sigma
        interval_hits.append((pred - half) <= actual <= (pred + half))
        pending.append(
            {
                "origin": t,
                "maturity": t + horizon_obs,
                "candidates": candidates,
                "ensemble_pred": pred,
            }
        )

    # Mature all forecasts that can be known by the final observation so live weights use all available evidence.
    final_t = len(values) - 1
    for item in pending:
        maturity = int(item["maturity"])
        if maturity > final_t:
            continue
        origin = int(item["origin"])
        actual = values[maturity] / values[origin] - 1.0
        benchmark_errors.append(1.0 / (1.0 + actual) - 1.0)
        for name, pred in item["candidates"].items():
            err = (1.0 + pred) / (1.0 + actual) - 1.0
            model_errors.setdefault(name, []).append(err)
            pred_dir = 0 if abs(pred) < 0.0025 else (1 if pred > 0 else -1)
            actual_dir = 0 if abs(actual) < 0.0025 else (1 if actual > 0 else -1)
            direction_hits.setdefault(name, []).append(pred_dir == actual_dir)
        ensemble_pred = float(item["ensemble_pred"])
        ensemble_errors.append((1.0 + ensemble_pred) / (1.0 + actual) - 1.0)

    n = len(predictions)
    if not n:
        return {
            "samples": 0,
            "rmse_pct": None,
            "mae_pct": None,
            "random_walk_rmse_pct": None,
            "persistence_skill_pct": None,
            "direction_accuracy": None,
            "active_direction_accuracy": None,
            "active_signal_coverage": None,
            "interval_80_coverage": None,
            "weights": {},
            "shrinkage": 0.50,
            "residual_sigma": None,
        }

    level_errors = [(1.0 + p) / (1.0 + a) - 1.0 for p, a in zip(predictions, actuals)]
    bench_eval = [1.0 / (1.0 + a) - 1.0 for a in actuals]
    rmse = _rmse(level_errors)
    bench_rmse = _rmse(bench_eval)
    skill = (1.0 - rmse / bench_rmse) * 100.0 if rmse is not None and bench_rmse else None
    abs_errors = [abs(e) for e in level_errors]
    # The V4 model is continuous: it never abstains. Directional accuracy therefore
    # evaluates the sign of every non-neutral realised move rather than counting tiny
    # model drifts as "no signal". This keeps the validation definition aligned with
    # the operational behaviour.
    direction_all = []
    for p, a in zip(predictions, actuals):
        if abs(a) < 0.0025:
            continue
        pd = 1 if p > 0 else (-1 if p < 0 else 0)
        ad = 1 if a > 0 else -1
        direction_all.append(pd == ad)
    direction_active = list(direction_all)

    live_candidates, _ = _candidate_returns(values, dates, len(values) - 1, horizon_obs, lookups)
    live_weights = _weights(model_errors, direction_hits, benchmark_errors, sorted(live_candidates)) if live_candidates else {}
    macro_errors = (model_errors.get("macro_public_factors") or [])[-300:]
    macro_hits = (direction_hits.get("macro_public_factors") or [])[-300:]
    macro_rmse = _rmse(macro_errors)
    macro_bench = _rmse(benchmark_errors[-300:])
    macro_skill = (1.0 - macro_rmse / macro_bench) * 100.0 if macro_rmse is not None and macro_bench else None
    macro_dacc = mean(macro_hits) if macro_hits else None
    macro_gate_passed = bool(len(macro_errors) >= 35 and macro_skill is not None and macro_skill > 0.0 and macro_dacc is not None and macro_dacc >= 0.50)
    residual_sigma = _rmse(ensemble_errors[-300:]) or rmse
    candidate_oos = {}
    for name in sorted(model_names_seen):
        errs = (model_errors.get(name) or [])[-300:]
        hits = (direction_hits.get(name) or [])[-300:]
        crmse = _rmse(errs)
        cbench = _rmse(benchmark_errors[-300:])
        candidate_oos[name] = {
            "samples": len(errs),
            "skill_pct": round((1.0 - crmse / cbench) * 100.0, 3) if crmse is not None and cbench else None,
            "direction_accuracy": round(mean(hits), 4) if hits else None,
            "live_weight": round(float(live_weights.get(name, 0.0)), 6),
        }
    return {
        "samples": n,
        "rmse_pct": round(rmse * 100.0, 3) if rmse is not None else None,
        "mae_pct": round(mean(abs_errors) * 100.0, 3) if abs_errors else None,
        "random_walk_rmse_pct": round(bench_rmse * 100.0, 3) if bench_rmse is not None else None,
        "persistence_skill_pct": round(skill, 3) if skill is not None else None,
        "direction_accuracy": round(mean(direction_all), 4) if direction_all else None,
        "active_direction_accuracy": round(mean(direction_active), 4) if direction_active else None,
        "active_signal_coverage": 1.0 if n else None,
        "interval_80_coverage": round(mean(interval_hits), 4) if interval_hits else None,
        "weights": {k: round(v, 4) for k, v in live_weights.items()},
        "macro_oos_gate": {
            "passed": macro_gate_passed,
            "samples": len(macro_errors),
            "persistence_skill_pct": round(macro_skill, 3) if macro_skill is not None else None,
            "direction_accuracy": round(macro_dacc, 4) if macro_dacc is not None else None,
            "rule": "macro live weight only when past-only OOS persistence skill > 0 and direction accuracy >= 0.50",
        },
        "shrinkage": round(_validation_shrinkage(ensemble_errors, benchmark_errors), 4),
        "residual_sigma": residual_sigma,
        "method": "expanding_walk_forward_weekly_origins_prequential_regime_candidate_tournament",
        "candidate_oos": candidate_oos,
        "selection_rule": "55% recent + 45% expanding matured OOS skill/direction; no lookahead",
        "benchmark": "random_walk_no_change_evaluation_only",
    }


def _grade(validation: dict[str, Any]) -> tuple[str, int, bool, list[str]]:
    n = int(validation.get("samples") or 0)
    skill = validation.get("persistence_skill_pct")
    dacc = validation.get("active_direction_accuracy")
    coverage = validation.get("interval_80_coverage")
    rmse = validation.get("rmse_pct")

    score = 20
    score += min(20, int(n / 12))
    if skill is not None:
        score += int(_clip(float(skill) * 2.0 + 10.0, 0.0, 20.0))
    if dacc is not None:
        score += int(_clip((float(dacc) - 0.45) * 100.0, 0.0, 15.0))
    if coverage is not None:
        score += int(_clip(15.0 - abs(float(coverage) - 0.80) * 100.0, 0.0, 15.0))
    score = int(_clip(score, 0, 100))

    strict = bool(
        n >= 180
        and skill is not None and float(skill) > 0.0
        and dacc is not None and float(dacc) >= 0.52
        and coverage is not None and 0.70 <= float(coverage) <= 0.90
    )
    if strict and float(skill or 0.0) >= 3.0 and float(dacc or 0.0) >= 0.55:
        grade = "A"
    elif strict:
        grade = "B"
    elif n >= 180 and rmse is not None:
        grade = "C"
    else:
        grade = "D"

    limitations: list[str] = []
    if skill is None or float(skill) <= 0.0:
        limitations.append("랜덤워크 대비 RMSE 우위가 확인되지 않아 예측폭을 자동 축소합니다.")
    if dacc is None or float(dacc) < 0.52:
        limitations.append("방향 적중력이 약해 확률 분포를 넓게 유지합니다.")
    if n < 180:
        limitations.append("워크포워드 표본이 180개 미만입니다.")
    return grade, score, strict, limitations


def _probabilities(mu: float, sigma: float, horizon_obs: int) -> dict[str, float | str]:
    sigma = max(0.006, float(sigma))
    neutral_band = 0.0025 * sqrt(max(1.0, horizon_obs / 63.0))
    normal = NormalDist(mu=mu, sigma=sigma)
    p_down = normal.cdf(-neutral_band)
    p_up = 1.0 - normal.cdf(neutral_band)
    p_neutral = max(0.0, 1.0 - p_up - p_down)
    total = p_up + p_neutral + p_down or 1.0
    p_up, p_neutral, p_down = p_up / total, p_neutral / total, p_down / total
    diff = p_up - p_down
    direction = "up" if diff >= 0.08 else ("down" if diff <= -0.08 else "neutral")
    return {
        "up_probability": round(p_up, 4),
        "neutral_probability": round(p_neutral, 4),
        "down_probability": round(p_down, 4),
        "direction": direction,
        "neutral_band_pct": round(neutral_band * 100.0, 3),
    }



def _recent_trend_context(recent: dict[str, float | None], forecast_3m: dict[str, Any] | None = None) -> dict[str, Any]:
    """Explain observed FX trend separately from the OOS forecast.

    This is diagnostics/presentation metadata only.  It does not alter candidate
    returns, OOS weights, shrinkage, probabilities, or point forecasts.
    """
    d5 = recent.get("5d")
    d20 = recent.get("20d")
    d60 = recent.get("60d")

    direction = "mixed"
    strength = "neutral"
    label = "최근 추세 혼조"
    if d20 is not None and d60 is not None:
        if d20 < 0 and d60 < 0:
            direction = "down"
            if d60 <= -6.0 or d20 <= -3.0:
                strength = "strong"
                label = "강한 하락세(원화 강세)"
            else:
                strength = "moderate"
                label = "하락세(원화 강세)"
        elif d20 > 0 and d60 > 0:
            direction = "up"
            if d60 >= 6.0 or d20 >= 3.0:
                strength = "strong"
                label = "강한 상승세(원화 약세)"
            else:
                strength = "moderate"
                label = "상승세(원화 약세)"
        elif abs(d20) < 0.75 and abs(d60) < 1.5:
            direction = "neutral"
            strength = "neutral"
            label = "최근 추세 중립"

    forecast_3m = forecast_3m or {}
    fdir = str(forecast_3m.get("direction") or "").lower()
    fchg = _to_float(forecast_3m.get("change_pct"))
    if direction == "down" and fdir == "neutral":
        relation = "recent_down_forecast_neutral"
        explanation = "최근 원/달러는 하락 추세지만 3개월 OOS 모델은 추세 지속을 확신하지 않아 중립 전망을 유지합니다."
    elif direction == "up" and fdir == "neutral":
        relation = "recent_up_forecast_neutral"
        explanation = "최근 원/달러는 상승 추세지만 3개월 OOS 모델은 추세 지속을 확신하지 않아 중립 전망을 유지합니다."
    elif direction in {"up", "down"} and fdir == direction:
        relation = "trend_and_forecast_aligned"
        explanation = "최근 관측 추세와 3개월 OOS 모델 방향이 같은 쪽을 가리킵니다."
    elif direction in {"up", "down"} and fdir in {"up", "down"} and fdir != direction:
        relation = "trend_and_forecast_diverge"
        explanation = "최근 관측 추세와 3개월 OOS 모델 방향이 반대입니다. 평균회귀·금리차·위험요인 등 검증 가중치가 최근 모멘텀보다 크게 반영된 결과입니다."
    else:
        relation = "mixed"
        explanation = "최근 관측 추세와 3개월 모델 전망을 별도로 해석해야 합니다."

    return {
        "direction": direction,
        "strength": strength,
        "label": label,
        "change_pct": {"5d": d5, "20d": d20, "60d": d60},
        "forecast_3m_direction": fdir or None,
        "forecast_3m_change_pct": fchg,
        "relation": relation,
        "explanation": explanation,
        "affects_forecast_calculation": False,
    }

def build_fx_forecast_v4(
    ecos: dict[str, Any],
    global_data: dict[str, Any] | None = None,
    rate_v2: dict[str, Any] | None = None,
) -> dict[str, Any]:
    fx_series, spot_meta = _merge_fx_series(ecos, global_data)
    if len(fx_series) < 280:
        # This is the only true hard boundary: without a usable history an objective
        # forecast cannot be estimated.  Callers can retain the previous committed
        # forecast via the continuity layer in src.main.
        return {
            "schema_version": "4.0.0",
            "engine_version": MODEL_VERSION,
            "status": "insufficient_history",
            "forecast_operational": False,
            "current_usdkrw": spot_meta.get("value"),
            "forecast_path": [],
            "validation": {"quality_gate": {"passed": False, "operational_passed": False, "level": "history_required"}},
        }

    dates = [d for d, _ in fx_series]
    values = [v for _, v in fx_series]
    spot = values[-1]
    lookups = _global_lookups(global_data, ecos)

    forecast_path: list[dict[str, Any]] = []
    validations: dict[str, Any] = {}
    quality_rows: dict[str, Any] = {}
    factor_panel: dict[str, Any] = {}

    for months, horizon_obs in HORIZONS.items():
        validation = _walk_forward(values, dates, horizon_obs, lookups)
        candidates, macro_meta = _candidate_returns(values, dates, len(values) - 1, horizon_obs, lookups)
        weights = validation.get("weights") or {}
        if not weights and candidates:
            weights = {name: 1.0 / len(candidates) for name in candidates}
        raw_return = sum(float(weights.get(name, 0.0)) * value for name, value in candidates.items())
        shrinkage = float(validation.get("shrinkage") or 0.50)
        pred_return = _clip(raw_return * shrinkage, -0.10, 0.10)
        if horizon_obs == 63:
            pred_return = _clip(float(candidates["rate_gap_carry"]) * 0.50 if "rate_gap_carry" in candidates else pred_return * 0.15, -0.10, 0.10)
        elif horizon_obs == 126:
            slow = float(candidates.get("contrarian_120", pred_return))
            if "rate_gap_carry" in candidates:
                pred_return = _clip(0.50 * float(candidates["rate_gap_carry"]) + 0.50 * slow, -0.10, 0.10)
            else:
                pred_return = _clip(slow, -0.10, 0.10)
        sigma = float(validation.get("residual_sigma") or 0.04 * sqrt(max(1.0, horizon_obs / 63.0)))
        p = _probabilities(pred_return, sigma, horizon_obs)

        point = spot * (1.0 + pred_return)
        half50 = 0.6744897501960817 * sigma
        half80 = 1.2815515655446004 * sigma
        range50 = [spot * max(0.1, 1.0 + pred_return - half50), spot * (1.0 + pred_return + half50)]
        range80 = [spot * max(0.1, 1.0 + pred_return - half80), spot * (1.0 + pred_return + half80)]

        grade, quality_score, strict, limitations = _grade(validation)
        operational = int(validation.get("samples") or 0) >= 120 and bool(candidates)
        label = f"{months}m"
        validations[label] = {
            **{k: v for k, v in validation.items() if k != "residual_sigma"},
            "grade": grade,
            "model_quality_score": quality_score,
            "strict_skill_passed": strict,
        }
        quality_rows[label] = {
            # Backward compatibility: passed means the model is statistically auditable
            # and operational. Strict skill certification is separately exposed.
            "passed": operational,
            "operational_passed": operational,
            "strict_passed": strict,
            "grade": grade,
            "level": f"검증 {grade}등급" if operational else "검증표본 축적중",
            "observed": {
                "samples": validation.get("samples"),
                "rmse_pct": validation.get("rmse_pct"),
                "random_walk_rmse_pct": validation.get("random_walk_rmse_pct"),
                "persistence_skill_pct": validation.get("persistence_skill_pct"),
                "active_direction_accuracy": validation.get("active_direction_accuracy"),
                "active_signal_coverage": validation.get("active_signal_coverage"),
                "interval_80_coverage": validation.get("interval_80_coverage"),
                "model_quality_score": quality_score,
            },
            "reasons": limitations,
        }
        contributions = {
            name: round(float(weights.get(name, 0.0)) * float(value) * 100.0, 4)
            for name, value in candidates.items()
        }
        forecast_path.append(
            {
                "months": months,
                "mid": round(point, 1),
                "point_forecast": round(point, 1),
                "change_pct": round(pred_return * 100.0, 3),
                "range_50": [round(range50[0], 1), round(range50[1], 1)],
                "range_80": [round(range80[0], 1), round(range80[1], 1)],
                "direction": p["direction"],
                "up_probability": p["up_probability"],
                "neutral_probability": p["neutral_probability"],
                "down_probability": p["down_probability"],
                "neutral_band_pct": p["neutral_band_pct"],
                "production_model": "continuous_oos_weighted_ensemble_v4",
                "prediction_status": "forecast",
                # Kept true for legacy dashboard bridges: a forecast always exists.
                "signal_active": True,
                "signal_60d": round(values[-1] / values[-61] - 1.0, 5) if len(values) > 61 else None,
                "forecast_drift": round(pred_return, 5),
                "validation_shrinkage": round(shrinkage, 4),
                "model_weights": {k: round(float(v), 4) for k, v in weights.items()},
                "model_contributions_pct": contributions,
                "quality_grade": grade,
                "model_quality_score": quality_score,
            }
        )
        factor_panel[label] = {
            "macro": macro_meta,
            "macro_oos_gate": validation.get("macro_oos_gate") or {},
            "candidate_returns_pct": {k: round(v * 100.0, 4) for k, v in candidates.items()},
            "weights": {k: round(float(v), 4) for k, v in weights.items()},
            "contributions_pct": contributions,
        }

    primary = validations.get("3m", {})
    gate3 = quality_rows.get("3m", {})
    recent = {}
    for name, obs in (("5d", 5), ("20d", 20), ("60d", 60)):
        recent[name] = round((values[-1] / values[-obs - 1] - 1.0) * 100.0, 3) if len(values) > obs else None
    forecast_3m = next((row for row in forecast_path if int(row.get("months") or 0) == 3), None)
    recent_trend = _recent_trend_context(recent, forecast_3m)

    return {
        "schema_version": "4.0.0",
        "engine_version": MODEL_VERSION,
        "status": "ok",
        "engine_scope": "korea_fx_continuous_v4",
        "forecast_operational": True,
        "current_usdkrw": round(spot, 4),
        "current_date": spot_meta.get("date") or dates[-1],
        "current_source": spot_meta.get("source") or "ECOS",
        "recent_change_pct": recent,
        "recent_trend": recent_trend,
        "forecast_path": forecast_path,
        "production_model": "continuous_oos_weighted_ensemble_v4",
        "active_model_blocked": False,
        "factor_panel": factor_panel,
        "validation": {
            "samples": primary.get("samples"),
            "rmse_pct": primary.get("rmse_pct"),
            "mae_pct": primary.get("mae_pct"),
            "active_direction_accuracy": primary.get("active_direction_accuracy"),
            "all_origin_direction_accuracy": primary.get("direction_accuracy"),
            "persistence_skill_pct": primary.get("persistence_skill_pct"),
            "active_signal_coverage": primary.get("active_signal_coverage"),
            "interval_80_coverage": primary.get("interval_80_coverage"),
            "horizon_specific_oos": True,
            "oos_by_horizon": validations,
            "validation_method": "expanding_walk_forward_weekly_origins_continuous_adaptive_ensemble",
            "model_specification": {
                "forecast_is_always_generated": True,
                "random_walk_role": "evaluation_benchmark_only",
                "weak_validation_action": "shrink_forecast_and_lower_grade_not_abstain",
                "technical_models": [
                    "momentum_20",
                    "momentum_60",
                    "momentum_120",
                    "contrarian_60",
                    "mean_reversion_252",
                    "trend_acceleration",
                ],
                "macro_model": "global public factors + ECOS fallback; past-only OOS macro gate suppresses harmful macro drift",
            },
            "quality_gate": {
                **gate3,
                "primary_horizon": "3m",
                "passed_horizons": [label for label, row in quality_rows.items() if row.get("operational_passed")],
                "strict_passed_horizons": [label for label, row in quality_rows.items() if row.get("strict_passed")],
                "horizon_quality_gates": quality_rows,
            },
        },
    }
