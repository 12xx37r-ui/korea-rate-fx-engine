from __future__ import annotations

from bisect import bisect_right
from math import exp, log, sqrt
from statistics import mean
from typing import Any


def _period(row: dict[str, Any]) -> str:
    for key in ("TIME", "PRD_DE", "TIME_PERIOD", "DATE", "baseYm", "WRTTIME"):
        value = row.get(key)
        if value not in (None, ""):
            text = "".join(ch for ch in str(value) if ch.isdigit())
            if len(text) == 4:
                text += "0101"
            elif len(text) == 6:
                text += "01"
            return text[:8]
    return ""


def _number(row: dict[str, Any]) -> float | None:
    for key in ("DATA_VALUE", "DT"):
        value = row.get(key)
        try:
            if value not in (None, "", "-"):
                return float(str(value).replace(",", ""))
        except (TypeError, ValueError):
            pass
    return None


def numeric_series(rows: list[dict[str, Any]]) -> list[tuple[str, float]]:
    dedup: dict[str, float] = {}
    for row in rows:
        p, v = _period(row), _number(row)
        if p and v is not None:
            dedup[p] = v
    return sorted(dedup.items())


def core_cpi_yoy_series(rows: list[dict[str, Any]]) -> tuple[list[tuple[str, float]], dict[str, Any]]:
    """KOSIS 핵심물가를 일관된 전년동월비(소수)로 정규화한다.

    우선순위:
    1) 공식 전년동월비 항목
    2) 지수 수준에서 12개월 변화율 계산
    3) 공식 전월비 12개를 복리 누적
    """
    core_tokens = ("농산물및석유류제외", "농산물 및 석유류 제외", "식료품및에너지제외", "식료품 및 에너지 제외")
    filtered = []
    for row in rows:
        text = " ".join(str(row.get(k, "")) for k in ("C1_NM", "C2_NM", "ITM_NM", "TBL_NM"))
        if any(token in text for token in core_tokens):
            filtered.append(row)
    if not filtered:
        filtered = list(rows)

    def rows_with(token: str) -> list[dict[str, Any]]:
        return [r for r in filtered if token in str(r.get("ITM_NM", ""))]

    yoy_rows = rows_with("전년동월비") or rows_with("전년비")
    if yoy_rows:
        series = [(p, v / 100.0) for p, v in numeric_series(yoy_rows)]
        valid = [(p, v) for p, v in series if -0.03 <= v <= 0.15]
        if len(valid) >= 12:
            return valid, {"source_mode": "official_yoy", "valid": True, "rows": len(valid)}

    index_rows = [
        r for r in filtered
        if "지수" in str(r.get("UNIT_NM", ""))
        or "=100" in str(r.get("UNIT_NM", ""))
        or "＝100" in str(r.get("UNIT_NM", ""))
    ]
    levels = numeric_series(index_rows)
    if len(levels) >= 13:
        result = []
        for i in range(12, len(levels)):
            prev = levels[i - 12][1]
            value = levels[i][1]
            if prev > 0:
                yoy = value / prev - 1.0
                if -0.03 <= yoy <= 0.15:
                    result.append((levels[i][0], yoy))
        if len(result) >= 12:
            return result, {"source_mode": "index_derived_yoy", "valid": True, "rows": len(result)}

    mom_rows = rows_with("전월비")
    mom = numeric_series(mom_rows)
    if len(mom) >= 12:
        result = []
        for i in range(11, len(mom)):
            factor = 1.0
            for _, rate in mom[i - 11 : i + 1]:
                factor *= 1.0 + rate / 100.0
            yoy = factor - 1.0
            if -0.03 <= yoy <= 0.15:
                result.append((mom[i][0], yoy))
        if result:
            return result, {"source_mode": "monthly_compounded_yoy", "valid": True, "rows": len(result)}

    return [], {"source_mode": "unavailable", "valid": False, "rows": 0}


def _latest_at(series: list[tuple[str, float]], date: str) -> float | None:
    dates = [p for p, _ in series]
    i = bisect_right(dates, date) - 1
    return series[i][1] if i >= 0 else None


def _history_at(series: list[tuple[str, float]], date: str) -> list[float]:
    dates = [p for p, _ in series]
    i = bisect_right(dates, date)
    return [v for _, v in series[:i]]


def _softmax(raw: dict[str, float]) -> dict[str, float]:
    peak = max(raw.values())
    vals = {k: exp(v - peak) for k, v in raw.items()}
    total = sum(vals.values()) or 1.0
    return {k: vals[k] / total for k in vals}


def probability_from_features(gap: float | None, cpi_yoy: float | None, industrial_3m_ann: float | None) -> dict[str, float]:
    raw = {"hold": 1.65, "hike": 0.0, "cut": 0.0}
    if gap is not None:
        raw["hike"] += max(0.0, min(1.10, gap * 0.70))
        raw["cut"] += max(0.0, min(1.10, -gap * 1.00))
    if cpi_yoy is not None:
        inflation_gap = (cpi_yoy - 0.02) / 0.01
        raw["hike"] += max(0.0, min(0.90, inflation_gap * 0.32))
        raw["cut"] += max(0.0, min(0.75, -inflation_gap * 0.28))
    if industrial_3m_ann is not None:
        growth_signal = industrial_3m_ann / 0.06
        raw["hike"] += max(0.0, min(0.55, growth_signal * 0.28))
        raw["cut"] += max(0.0, min(0.65, -growth_signal * 0.34))
    return _softmax(raw)


def _next_actual_class(base: list[tuple[str, float]], index: int, horizon_days_approx: int = 100) -> str:
    current_date, current_rate = base[index]
    current_num = int(current_date)
    # 날짜 정수 차이는 정확한 일수는 아니지만 100일 창을 넉넉히 잡고 최대 4개월까지만 본다.
    current_month = int(current_date[:6])
    for date, rate in base[index + 1 :]:
        month_gap = (int(date[:4]) - int(current_date[:4])) * 12 + int(date[4:6]) - int(current_date[4:6])
        if month_gap > 4:
            break
        if abs(rate - current_rate) >= 0.10:
            return "hike" if rate > current_rate else "cut"
    return "hold"



def _shift_month(date: str, months: int) -> str:
    year = int(date[:4])
    month = int(date[4:6])
    total = year * 12 + (month - 1) + months
    y, m0 = divmod(total, 12)
    return f"{y:04d}{m0 + 1:02d}01"


def _next_actual_class_from_date(
    base: list[tuple[str, float]],
    date: str,
    horizon_months: int = 4,
) -> str:
    current_rate = _latest_at(base, date)
    if current_rate is None:
        return "hold"
    end_date = _shift_month(date, horizon_months)
    for future_date, rate in base:
        if future_date <= date:
            continue
        if future_date > end_date:
            break
        if abs(rate - current_rate) >= 0.10:
            return "hike" if rate > current_rate else "cut"
    return "hold"


def _fx_selective_forecast(hist: list[float], horizon_obs: int) -> tuple[float, bool, dict[str, float]]:
    """Pre-declared selective USD/KRW forecast.

    FX levels are close to a random walk.  The production model therefore
    predicts only when a sufficiently large 60-observation move exists and
    applies a deliberately shrunken contrarian adjustment.  Otherwise it
    abstains and returns the no-change benchmark.  This avoids forcing weak
    directional forecasts into every period.
    """
    current = hist[-1]
    if len(hist) <= 61 or hist[-61] <= 0:
        return current, False, {"signal_60d": 0.0, "drift": 0.0}
    signal = current / hist[-61] - 1.0
    active = abs(signal) >= 0.03
    if not active:
        return current, False, {"signal_60d": signal, "drift": 0.0}
    scale = min(1.0, max(0.20, horizon_obs / 63.0))
    drift = max(-0.06, min(0.06, -0.35 * signal * scale))
    return current * (1.0 + drift), True, {"signal_60d": signal, "drift": drift}


def fx_walk_forward_validation(
    fx_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Strict expanding walk-forward validation with abstention.

    The fixed model specification is declared in code before evaluation:
    60-observation contrarian signal, 3% activation threshold, 35% shrinkage,
    and maximum 6% forecast drift.  No horizon is tuned in-sample.  When the
    signal is weak, the model returns the random-walk benchmark and records an
    abstention.  Directional accuracy is reported both for all origins and for
    active calls only; quality gating uses active-call accuracy plus minimum
    signal coverage.
    """
    series = numeric_series(fx_rows)
    values = [v for _, v in series if v > 0]
    horizons = {"1m": 21, "3m": 63, "6m": 126, "12m": 252}
    result: dict[str, Any] = {}
    for label, h in horizons.items():
        errors: list[float] = []
        bench_errors: list[float] = []
        abs_errors: list[float] = []
        all_direction_hits: list[bool] = []
        active_direction_hits: list[bool] = []
        interval_hits: list[bool] = []
        active_flags: list[bool] = []
        start = max(260, h + 130)
        for t in range(start, len(values) - h, 5):
            hist = values[:t]
            current = hist[-1]
            actual = values[t + h]
            pred, active, _ = _fx_selective_forecast(hist, h)
            if current <= 0 or actual <= 0:
                continue
            err = pred / actual - 1.0
            bench = current / actual - 1.0
            errors.append(err)
            bench_errors.append(bench)
            abs_errors.append(abs(err))
            active_flags.append(active)

            actual_dir = 0 if abs(actual / current - 1.0) < 0.0025 else (1 if actual > current else -1)
            pred_dir = 0 if abs(pred / current - 1.0) < 0.0025 else (1 if pred > current else -1)
            all_direction_hits.append(pred_dir == actual_dir)
            if active:
                active_direction_hits.append(pred_dir == actual_dir)

            prior = errors[:-1]
            sigma = sqrt(mean(e * e for e in prior)) if len(prior) >= 24 else 0.055 * sqrt(max(1.0, h / 63.0))
            half = 1.2816 * sigma
            interval_hits.append(pred * (1 - half) <= actual <= pred * (1 + half))

        n = len(errors)
        rmse = sqrt(mean(e * e for e in errors)) if errors else None
        bench_rmse = sqrt(mean(e * e for e in bench_errors)) if bench_errors else None
        skill = (1.0 - rmse / bench_rmse) * 100.0 if rmse is not None and bench_rmse and bench_rmse > 0 else None
        active_n = len(active_direction_hits)
        coverage = active_n / n if n else None
        active_successes = sum(active_direction_hits)
        active_wilson = _wilson_lower(active_successes, active_n) if active_n else None
        result[label] = {
            "samples": n,
            "rmse_pct": round(rmse * 100, 3) if rmse is not None else None,
            "mae_pct": round(mean(abs_errors) * 100, 3) if abs_errors else None,
            "random_walk_rmse_pct": round(bench_rmse * 100, 3) if bench_rmse is not None else None,
            "persistence_skill_pct": round(skill, 3) if skill is not None else None,
            "direction_accuracy": round(mean(all_direction_hits), 4) if all_direction_hits else None,
            "active_direction_accuracy": round(mean(active_direction_hits), 4) if active_direction_hits else None,
            "active_direction_wilson_lower_95": round(active_wilson, 4) if active_wilson is not None else None,
            "active_signal_samples": active_n,
            "active_signal_coverage": round(coverage, 4) if coverage is not None else None,
            "interval_80_coverage": round(mean(interval_hits), 4) if interval_hits else None,
            "model": "selective_60d_contrarian_shrunk",
        }
    primary = result.get("3m", {})
    return {
        "method": "expanding_walk_forward_weekly_origins_selective_fixed_spec",
        "model_specification": {
            "signal_lookback_observations": 60,
            "activation_threshold_abs_return": 0.03,
            "contrarian_shrinkage": 0.35,
            "max_abs_drift": 0.06,
            "abstention_model": "random_walk_no_change",
        },
        "horizons": result,
        "samples": primary.get("samples", 0),
        "rmse_pct": primary.get("rmse_pct"),
        "mae_pct": primary.get("mae_pct"),
        "direction_accuracy": primary.get("active_direction_accuracy") if primary.get("active_direction_accuracy") is not None else primary.get("direction_accuracy"),
        "active_direction_wilson_lower_95": primary.get("active_direction_wilson_lower_95"),
        "all_origin_direction_accuracy": primary.get("direction_accuracy"),
        "active_signal_samples": primary.get("active_signal_samples"),
        "active_signal_coverage": primary.get("active_signal_coverage"),
        "persistence_skill_pct": primary.get("persistence_skill_pct"),
        "interval_80_coverage": primary.get("interval_80_coverage"),
        "horizon_specific_oos": all((row.get("samples") or 0) >= 100 for row in result.values()),
    }



def combine_market_rate_rows_for_backtest(
    y2_rows: list[dict[str, Any]],
    y3_rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Build a longer fixed validation history without extra network calls.

    The live policy-gap input keeps preferring the Korean 2Y government yield.
    For historical OOS only, periods before the 2Y series begins use the 3Y
    government yield as a declared proxy; once 2Y exists, 2Y is used.  This
    avoids throwing away nearly a decade of already-collected ECOS history and
    keeps the proxy rule fixed before evaluation.
    """
    y2 = numeric_series(y2_rows)
    y3 = numeric_series(y3_rows)
    if not y2:
        rows = [{"TIME": d, "DATA_VALUE": v} for d, v in y3]
        return rows, {
            "mode": "kr_gov_3y_only_proxy",
            "live_reference": "kr_gov_3y",
            "pre_2y_proxy": None,
            "first_2y_date": None,
            "observations": len(rows),
        }
    first_2y = y2[0][0]
    merged: dict[str, float] = {d: v for d, v in y3 if d < first_2y}
    merged.update({d: v for d, v in y2})
    rows = [{"TIME": d, "DATA_VALUE": v} for d, v in sorted(merged.items())]
    return rows, {
        "mode": "kr_gov_3y_pre_2y_then_2y_fixed_proxy",
        "live_reference": "kr_gov_2y",
        "pre_2y_proxy": "kr_gov_3y",
        "first_2y_date": first_2y,
        "observations": len(rows),
    }


def evaluate_rate_vintage_snapshots(
    vintage_dir: str | Any,
    base_rows: list[dict[str, Any]],
    min_matured_samples: int = 24,
) -> dict[str, Any]:
    """Evaluate truly saved point-in-time rate forecasts using matured outcomes.

    This is intentionally local-only: it reads committed ``output/vintages``
    snapshots and never performs an API call.  To reduce serial dependence, at
    most one (latest) usable forecast per calendar month is evaluated.
    """
    from pathlib import Path
    import json

    root = Path(vintage_dir)
    base = numeric_series(base_rows)
    if not root.exists() or not base:
        return {
            "qualified": False, "samples": 0, "min_samples": min_matured_samples,
            "brier_score": None, "benchmark_brier": None, "brier_skill_score": None,
            "accuracy": None, "accuracy_wilson_lower_95": None,
            "method": "committed_point_in_time_monthly_snapshots",
            "reason": "matured point-in-time snapshots unavailable",
        }

    latest_base_date = base[-1][0]
    monthly: dict[str, tuple[str, dict[str, float]]] = {}
    for path in sorted(root.glob('*.json')):
        try:
            obj = json.loads(path.read_text(encoding='utf-8'))
        except Exception:
            continue
        rf = obj.get('rate_forecast') if isinstance(obj, dict) else None
        if not isinstance(rf, dict) or rf.get('continuity_mode'):
            continue
        meetings = rf.get('meeting_path') or []
        first = meetings[0] if meetings else {}
        probs = first.get('probabilities') if isinstance(first, dict) else None
        if not isinstance(probs, dict):
            continue
        try:
            p = {k: float(probs[k]) for k in ('hold','hike','cut')}
        except Exception:
            continue
        total = sum(p.values())
        if total <= 0:
            continue
        p = {k: p[k] / total for k in p}
        captured = str(obj.get('captured_at') or rf.get('generated_at') or path.stem)
        digits = ''.join(ch for ch in captured[:10] if ch.isdigit())
        if len(digits) < 8:
            digits = ''.join(ch for ch in path.stem if ch.isdigit())[:8]
        if len(digits) != 8:
            continue
        origin = digits
        # Only evaluate after the full four-month outcome window has matured.
        if _shift_month(origin, 4) > latest_base_date:
            continue
        key = origin[:6]
        prev = monthly.get(key)
        if prev is None or origin > prev[0]:
            monthly[key] = (origin, p)

    origins = [monthly[k] for k in sorted(monthly)]
    classes = ('hold','hike','cut')
    counts = {c: 1 for c in classes}
    briers: list[float] = []
    bench: list[float] = []
    hits: list[bool] = []
    rows: list[dict[str, Any]] = []
    for origin, probs in origins:
        actual = _next_actual_class_from_date(base, origin)
        total_prior = sum(counts.values())
        benchmark = {c: counts[c] / total_prior for c in classes}
        model_bs = sum((probs[c] - (1.0 if actual == c else 0.0)) ** 2 for c in classes)
        bench_bs = sum((benchmark[c] - (1.0 if actual == c else 0.0)) ** 2 for c in classes)
        hit = max(probs, key=probs.get) == actual
        briers.append(model_bs); bench.append(bench_bs); hits.append(hit)
        rows.append({
            'origin': origin, 'probabilities': {k: round(v, 6) for k, v in probs.items()},
            'actual': actual, 'hit': hit, 'brier': round(model_bs, 6),
            'benchmark_brier': round(bench_bs, 6),
        })
        counts[actual] += 1

    n = len(rows)
    bs = mean(briers) if briers else None
    bb = mean(bench) if bench else None
    skill = (1.0 - bs / bb) if bs is not None and bb and bb > 0 else None
    acc = mean(hits) if hits else None
    lb = _wilson_lower(sum(hits), n) if hits else None
    qualified = bool(
        n >= min_matured_samples
        and skill is not None and skill > 0.0
        and acc is not None and acc >= 0.50
    )
    return {
        'qualified': qualified,
        'samples': n,
        'min_samples': min_matured_samples,
        'brier_score': round(bs, 4) if bs is not None else None,
        'benchmark_brier': round(bb, 4) if bb is not None else None,
        'brier_skill_score': round(skill, 4) if skill is not None else None,
        'accuracy': round(acc, 4) if acc is not None else None,
        'accuracy_wilson_lower_95': round(lb, 4) if lb is not None else None,
        'method': 'committed_point_in_time_monthly_snapshots',
        'selection': 'latest usable saved snapshot per calendar month; four-month label maturation',
        'network_calls_added': 0,
        'rows': rows,
        'reason': None if qualified else f'need >= {min_matured_samples} matured monthly snapshots with positive Brier skill and >=50% accuracy',
    }

def _wilson_lower(successes: int, n: int, z: float = 1.959963984540054) -> float | None:
    if n <= 0:
        return None
    phat = successes / n
    denom = 1.0 + z * z / n
    centre = phat + z * z / (2.0 * n)
    margin = z * sqrt((phat * (1.0 - phat) + z * z / (4.0 * n)) / n)
    return max(0.0, (centre - margin) / denom)


def rate_probability_backtest(
    base_rows: list[dict[str, Any]],
    market_rows: list[dict[str, Any]],
    cpi_yoy: list[tuple[str, float]],
    industrial_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Release-lagged expanding walk-forward probability validation.

    The benchmark is generated sequentially from outcomes available before each
    origin with Laplace smoothing.  This removes the full-sample class-frequency
    look-ahead present in the earlier benchmark and makes Brier skill auditable.
    """
    base = numeric_series(base_rows)
    market = numeric_series(market_rows)
    industrial = numeric_series(industrial_rows)
    if len(base) < 120 or len(market) < 120 or len(cpi_yoy) < 12 or len(industrial) < 16:
        return {"samples": 0, "brier_score": None, "benchmark_brier": None, "brier_skill_score": None, "accuracy": None, "accuracy_wilson_lower_95": None, "log_loss": None, "class_frequency": {"hold": 1.0, "hike": 0.0, "cut": 0.0}, "walk_forward_backtest": False}

    seen: dict[str, str] = {}
    for date, _ in market:
        seen[date[:6]] = date
    monthly_dates = sorted(seen.values())

    samples: list[tuple[str, dict[str, float], str]] = []
    for date in monthly_dates:
        rate = _latest_at(base, date)
        yld = _latest_at(market, date)
        cpi = _latest_at(cpi_yoy, _shift_month(date, -1))
        hist_ind = _history_at(industrial, _shift_month(date, -2))
        ind = None
        if len(hist_ind) >= 4 and hist_ind[-4] > 0 and hist_ind[-1] > 0:
            ind = (hist_ind[-1] / hist_ind[-4]) ** 4 - 1.0
            if not (-0.40 <= ind <= 0.40):
                ind = None
        if rate is None or yld is None or cpi is None:
            continue
        probs = probability_from_features(yld - rate, cpi, ind)
        actual = _next_actual_class_from_date(base, date)
        samples.append((date, probs, actual))

    if len(samples) < 18:
        return {"samples": len(samples), "brier_score": None, "benchmark_brier": None, "brier_skill_score": None, "accuracy": None, "accuracy_wilson_lower_95": None, "log_loss": None, "class_frequency": {"hold": 1.0, "hike": 0.0, "cut": 0.0}, "walk_forward_backtest": False}

    classes = ("hold", "hike", "cut")
    counts = {c: 1 for c in classes}  # Laplace prior, fixed before evaluation.
    briers: list[float] = []
    bench: list[float] = []
    logs: list[float] = []
    hits: list[bool] = []
    rows: list[dict[str, Any]] = []
    for date, probs, actual in samples:
        total_prior = sum(counts.values())
        benchmark = {c: counts[c] / total_prior for c in classes}
        model_bs = sum((probs[c] - (1.0 if actual == c else 0.0)) ** 2 for c in classes)
        bench_bs = sum((benchmark[c] - (1.0 if actual == c else 0.0)) ** 2 for c in classes)
        hit = max(probs, key=probs.get) == actual
        briers.append(model_bs)
        bench.append(bench_bs)
        logs.append(-log(max(1e-9, probs[actual])))
        hits.append(hit)
        rows.append({"origin": date, "probabilities": {k: round(v, 6) for k, v in probs.items()}, "benchmark_probabilities": {k: round(v, 6) for k, v in benchmark.items()}, "actual": actual, "hit": hit, "brier": round(model_bs, 6), "benchmark_brier": round(bench_bs, 6)})
        counts[actual] += 1

    n = len(samples)
    freq = {c: sum(a == c for _, _, a in samples) / n for c in classes}
    bs, bb = mean(briers), mean(bench)
    skill = 1.0 - bs / bb if bb > 0 else None
    successes = sum(hits)
    return {
        "samples": n,
        "brier_score": round(bs, 4),
        "benchmark_brier": round(bb, 4),
        "brier_skill_score": round(skill, 4) if skill is not None else None,
        "accuracy": round(mean(hits), 4),
        "accuracy_wilson_lower_95": round(_wilson_lower(successes, n), 4),
        "log_loss": round(mean(logs), 4),
        "class_frequency": {k: round(v, 4) for k, v in freq.items()},
        "evaluation_horizon": "시장금리 월말 기준 향후 최대 4개월 내 첫 기준금리 변경",
        "validation_method": "release_lagged_expanding_walk_forward_fixed_spec",
        "benchmark_method": "expanding_prior_class_frequency_laplace",
        "release_lag_backtest": True,
        "walk_forward_backtest": True,
        "release_lags": {"core_cpi_months": 1, "industrial_production_months": 2},
        "real_time_vintage": False,
        "rows": rows,
    }


def calibrate_probabilities(probs: dict[str, float], backtest: dict[str, Any]) -> tuple[dict[str, float], float]:
    freq = backtest.get("class_frequency") or {"hold": 1.0, "hike": 0.0, "cut": 0.0}
    samples = int(backtest.get("samples") or 0)
    skill = backtest.get("brier_skill_score")
    if samples < 18 or skill is None:
        alpha = 0.55
    elif skill <= 0:
        alpha = 0.35
    else:
        alpha = min(0.90, 0.55 + skill * 0.60)
    mixed = {k: alpha * probs[k] + (1.0 - alpha) * float(freq.get(k, 0.0)) for k in ("hold", "hike", "cut")}
    total = sum(mixed.values()) or 1.0
    return ({k: round(v / total, 3) for k, v in mixed.items()}, round(alpha, 3))
