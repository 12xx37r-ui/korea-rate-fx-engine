from __future__ import annotations

from bisect import bisect_right
from math import exp, log
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

    # 월말에 가까운 마지막 관측만 남겨 중복·과대 표본 문제를 줄인다.
    monthly_indices: list[int] = []
    seen: dict[str, int] = {}
    for i, (date, _) in enumerate(base[:-1]):
        seen[date[:6]] = i
    monthly_indices = sorted(seen.values())

    samples: list[tuple[dict[str, float], str]] = []
    for i in monthly_indices:
        date, rate = base[i]
        yld = _latest_at(market, date)
        cpi = _latest_at(cpi_yoy, date)
        hist_ind = _history_at(industrial, date)
        ind = None
        if len(hist_ind) >= 4 and hist_ind[-4] > 0 and hist_ind[-1] > 0:
            ind = (hist_ind[-1] / hist_ind[-4]) ** 4 - 1.0
            if not (-0.40 <= ind <= 0.40):
                ind = None
        if yld is None or cpi is None:
            continue
        probs = probability_from_features(yld - rate, cpi, ind)
        actual = _next_actual_class(base, i)
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
        "evaluation_horizon": "월말 기준 향후 최대 4개월 내 첫 기준금리 변경",
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
