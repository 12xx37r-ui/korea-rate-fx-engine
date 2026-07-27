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


def _fx_model_forecast(hist: list[float], horizon_obs: int) -> float:
    x = hist[-1]
    def ret(period: int) -> float:
        if len(hist) <= period or hist[-period - 1] == 0:
            return 0.0
        return hist[-1] / hist[-period - 1] - 1.0
    r20 = ret(min(20, max(1, len(hist)-2)))
    r60 = ret(min(60, max(1, len(hist)-2)))
    med_window = sorted(hist[-252:])
    med = med_window[len(med_window)//2]
    scale = max(0.35, min(2.2, horizon_obs / 60.0))
    momentum = 0.55 * r20 + 0.25 * r60
    mean_reversion = (med / x - 1.0) * 0.20
    drift = max(-0.10, min(0.10, (momentum + mean_reversion) * scale))
    return x * (1.0 + drift)


def fx_walk_forward_validation(
    fx_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Walk-forward USD/KRW validation against a no-change random-walk baseline.

    Uses only observations available at each forecast origin. Horizons are
    approximated as 21/63/126/252 trading observations and evaluated on weekly
    origins to reduce overlap inflation.
    """
    series = numeric_series(fx_rows)
    values = [v for _, v in series if v > 0]
    horizons = {"1m": 21, "3m": 63, "6m": 126, "12m": 252}
    result: dict[str, Any] = {}
    for label, h in horizons.items():
        errors: list[float] = []
        bench_errors: list[float] = []
        direction_hits: list[bool] = []
        abs_errors: list[float] = []
        interval_hits: list[bool] = []
        start = max(260, h + 130)
        # Weekly forecast origins and fully out-of-sample forward targets.
        for t in range(start, len(values) - h, 5):
            hist = values[:t]
            current = hist[-1]
            actual = values[t + h]
            pred = _fx_model_forecast(hist, h)
            if current <= 0 or actual <= 0:
                continue
            err = pred / actual - 1.0
            bench = current / actual - 1.0
            errors.append(err)
            bench_errors.append(bench)
            abs_errors.append(abs(err))
            actual_dir = 0 if abs(actual/current - 1.0) < 0.0025 else (1 if actual > current else -1)
            pred_dir = 0 if abs(pred/current - 1.0) < 0.0025 else (1 if pred > current else -1)
            direction_hits.append(pred_dir == actual_dir)

            # Expanding-origin residual volatility, strictly using past errors.
            prior = errors[:-1]
            sigma = sqrt(mean(e*e for e in prior)) if len(prior) >= 24 else 0.055 * sqrt(max(1.0, h/63.0))
            half = 1.2816 * sigma
            interval_hits.append(pred*(1-half) <= actual <= pred*(1+half))
        n = len(errors)
        rmse = sqrt(mean(e*e for e in errors)) if errors else None
        bench_rmse = sqrt(mean(e*e for e in bench_errors)) if bench_errors else None
        skill = (1.0 - rmse/bench_rmse) * 100.0 if rmse is not None and bench_rmse and bench_rmse > 0 else None
        result[label] = {
            "samples": n,
            "rmse_pct": round(rmse*100, 3) if rmse is not None else None,
            "mae_pct": round(mean(abs_errors)*100, 3) if abs_errors else None,
            "random_walk_rmse_pct": round(bench_rmse*100, 3) if bench_rmse is not None else None,
            "persistence_skill_pct": round(skill, 3) if skill is not None else None,
            "direction_accuracy": round(mean(direction_hits), 4) if direction_hits else None,
            "interval_80_coverage": round(mean(interval_hits), 4) if interval_hits else None,
        }
    primary = result.get("3m", {})
    return {
        "method": "expanding_walk_forward_weekly_origins",
        "horizons": result,
        "samples": primary.get("samples", 0),
        "rmse_pct": primary.get("rmse_pct"),
        "mae_pct": primary.get("mae_pct"),
        "direction_accuracy": primary.get("direction_accuracy"),
        "persistence_skill_pct": primary.get("persistence_skill_pct"),
        "interval_80_coverage": primary.get("interval_80_coverage"),
        "horizon_specific_oos": all((result.get(k, {}).get("samples") or 0) >= 24 for k in horizons),
    }


def rate_probability_backtest(
    base_rows: list[dict[str, Any]],
    market_rows: list[dict[str, Any]],
    cpi_yoy: list[tuple[str, float]],
    industrial_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    base = numeric_series(base_rows)
    market = numeric_series(market_rows)
    industrial = numeric_series(industrial_rows)
    if len(base) < 120 or len(market) < 120 or len(cpi_yoy) < 12 or len(industrial) < 16:
        return {"samples": 0, "brier_score": None, "benchmark_brier": None, "brier_skill_score": None, "accuracy": None, "log_loss": None, "class_frequency": {"hold": 1.0, "hike": 0.0, "cut": 0.0}}

    # 시장금리 월말 관측을 예측 원점으로 사용한다. 기준금리 변경일만
    # 원점으로 쓰던 이전 방식보다 실제 월별 의사결정 표본을 온전히 보존한다.
    monthly_dates: list[str] = []
    seen: dict[str, str] = {}
    for date, _ in market:
        seen[date[:6]] = date
    monthly_dates = sorted(seen.values())

    samples: list[tuple[dict[str, float], str]] = []
    for date in monthly_dates:
        rate = _latest_at(base, date)
        yld = _latest_at(market, date)
        # 발표시차 보수 반영: CPI 1개월, 산업생산 2개월 지연.
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
        samples.append((probs, actual))

    if len(samples) < 18:
        return {"samples": len(samples), "brier_score": None, "benchmark_brier": None, "brier_skill_score": None, "accuracy": None, "log_loss": None, "class_frequency": {"hold": 1.0, "hike": 0.0, "cut": 0.0}}

    classes = ("hold", "hike", "cut")
    freq = {c: sum(a == c for _, a in samples) / len(samples) for c in classes}
    briers, bench, logs, hits = [], [], [], []
    for probs, actual in samples:
        briers.append(sum((probs[c] - (1.0 if actual == c else 0.0)) ** 2 for c in classes))
        bench.append(sum((freq[c] - (1.0 if actual == c else 0.0)) ** 2 for c in classes))
        logs.append(-log(max(1e-9, probs[actual])))
        hits.append(max(probs, key=probs.get) == actual)
    bs, bb = mean(briers), mean(bench)
    skill = 1.0 - bs / bb if bb > 0 else None
    return {
        "samples": len(samples),
        "brier_score": round(bs, 4),
        "benchmark_brier": round(bb, 4),
        "brier_skill_score": round(skill, 4) if skill is not None else None,
        "accuracy": round(mean(hits), 4),
        "log_loss": round(mean(logs), 4),
        "class_frequency": {k: round(v, 4) for k, v in freq.items()},
        "evaluation_horizon": "시장금리 월말 기준 향후 최대 4개월 내 첫 기준금리 변경",
        "release_lag_backtest": True,
        "release_lags": {"core_cpi_months": 1, "industrial_production_months": 2},
        "real_time_vintage": False,
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
