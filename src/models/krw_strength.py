from __future__ import annotations

from dataclasses import dataclass
from math import exp, tanh
from statistics import mean, median
from typing import Any


@dataclass
class KrwStrengthResult:
    score: float
    percentile: float | None
    grade: str
    direction_grade: str
    confidence: float


def grade_from_score(score: float) -> str:
    if score >= 0.67:
        return "강강"
    if score >= 0.34:
        return "강약"
    if score >= 0.08:
        return "강중립"
    if score > -0.08:
        return "약중립"
    if score > -0.34:
        return "약강"
    return "약약"


def _values(rows: list[dict[str, Any]], keys: tuple[str, ...] = ("DATA_VALUE", "DT")) -> list[float]:
    out: list[float] = []
    for row in rows:
        for key in keys:
            try:
                value = row.get(key)
                if value not in (None, "", "-"):
                    out.append(float(value))
                    break
            except (TypeError, ValueError):
                continue
    return out


def _pct(values: list[float], periods: int) -> float | None:
    if len(values) <= periods or values[-periods - 1] == 0:
        return None
    return values[-1] / values[-periods - 1] - 1.0


def _percentile(values: list[float], value: float) -> float | None:
    if not values:
        return None
    return sum(v <= value for v in values) / len(values)


def _softmax(raw: dict[str, float]) -> dict[str, float]:
    peak = max(raw.values())
    weights = {key: exp(value - peak) for key, value in raw.items()}
    total = sum(weights.values()) or 1.0
    return {key: round(value / total, 3) for key, value in weights.items()}


def _rate_probabilities(
    gap: float | None,
    core_cpi: list[float],
    industrial: list[float],
) -> dict[str, float]:
    # 국채금리-기준금리 격차는 정책 기대뿐 아니라 기간 프리미엄과 수급을 포함하므로
    # 동결을 기본 시나리오로 두고 격차의 영향은 제한한다.
    raw = {"hold": 1.35, "hike": 0.0, "cut": 0.0}
    if gap is not None:
        raw["hike"] += max(0.0, min(1.2, gap * 0.75))
        raw["cut"] += max(0.0, min(1.2, -gap * 1.2))

    # 근원물가 표가 전월비 계열이면 최근 3개월 평균의 양/음 방향만 완만하게 반영한다.
    if core_cpi:
        cpi_pressure = mean(core_cpi[-3:])
        raw["hike"] += max(0.0, min(0.35, cpi_pressure * 0.25))
        raw["cut"] += max(0.0, min(0.25, -cpi_pressure * 0.20))

    # 전산업생산 최근 3개월 변화는 경기 방향 신호로만 제한적으로 사용한다.
    production_3m = _pct(industrial, min(3, max(1, len(industrial) - 1))) if len(industrial) > 1 else None
    if production_3m is not None:
        raw["hike"] += max(0.0, min(0.25, production_3m * 4.0))
        raw["cut"] += max(0.0, min(0.25, -production_3m * 4.0))

    return _softmax(raw)


def _rate_label(prob: dict[str, float]) -> str:
    order = sorted(prob.items(), key=lambda item: item[1], reverse=True)
    names = {"hold": "동결", "hike": "인상", "cut": "인하"}
    first, second = order[0], order[1]
    if first[0] == "hold" and second[1] >= 0.20:
        return f"동결 우세·{names[second[0]]} 위험"
    return f"{names[first[0]]} 우세"


def build_snapshot(
    ecos: dict[str, list[dict[str, Any]]],
    kosis: dict[str, list[dict[str, Any]]],
    us_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    fx = _values(ecos.get("usdkrw", []))
    base = _values(ecos.get("kr_base_rate", []))
    y2 = _values(ecos.get("kr_gov_2y", []))
    y3 = _values(ecos.get("kr_gov_3y", []))
    y10 = _values(ecos.get("kr_gov_10y", []))
    core_cpi = _values(kosis.get("cpi_core", []))
    industrial = _values(kosis.get("industrial_production", []))

    latest_fx = fx[-1] if fx else None
    latest_rate = base[-1] if base else None
    market_yield = y2[-1] if y2 else (y3[-1] if y3 else None)
    gap = market_yield - latest_rate if market_yield is not None and latest_rate is not None else None

    # 절대 수준: 최근 최대 1년(252영업일) 분포에서 USD/KRW가 높을수록 원화 약세.
    level_window = fx[-252:] if fx else []
    fx_percentile = _percentile(level_window, latest_fx) if latest_fx is not None else None
    level_score = (1.0 - 2.0 * fx_percentile) if fx_percentile is not None else 0.0

    # 방향: USD/KRW 하락은 원화 강세. 1개월과 3개월 모멘텀을 결합한다.
    fx1 = _pct(fx, min(20, max(1, len(fx) - 1))) if len(fx) > 1 else None
    fx3 = _pct(fx, min(60, max(1, len(fx) - 1))) if len(fx) > 1 else None
    momentum_parts = [value for value in (fx1, fx3) if value is not None]
    fx_momentum = mean(momentum_parts) if momentum_parts else 0.0
    momentum_score = -tanh(fx_momentum * 12.0)

    # 현재 강도는 절대 수준을 더 크게 반영하여, 고환율인데 단기 하락했다는 이유만으로
    # 원화가 강한 상태로 표시되는 오류를 방지한다.
    score = max(-1.0, min(1.0, level_score * 0.65 + momentum_score * 0.35))

    rate_prob = _rate_probabilities(gap, core_cpi, industrial)
    rate_direction = _rate_label(rate_prob)
    rate_expected = None
    if latest_rate is not None:
        rate_expected = (
            latest_rate * rate_prob["hold"]
            + (latest_rate + 0.25) * rate_prob["hike"]
            + max(0.0, latest_rate - 0.25) * rate_prob["cut"]
        )

    # 환율 전망은 최근 추세를 일부 연장하되 절대수준의 평균회귀를 함께 적용한다.
    # 미국 정책금리 엔진 미연결 상태에서는 이동 폭과 신뢰도를 제한한다.
    recent_median = median(level_window) if level_window else latest_fx
    trend_component = max(-0.035, min(0.035, fx_momentum * 0.40))
    mean_reversion_component = 0.0
    if latest_fx and recent_median:
        mean_reversion_component = max(-0.025, min(0.025, (recent_median / latest_fx - 1.0) * 0.30))
    fx_bias_pct = trend_component + mean_reversion_component
    fx_mid = latest_fx * (1.0 + fx_bias_pct) if latest_fx else None

    us_connected = bool(us_policy) and us_policy.get("status") not in {None, "not_configured", "error"}
    range_pct = 0.025 if us_connected else 0.04
    fx_range = [round(fx_mid * (1 - range_pct), 1), round(fx_mid * (1 + range_pct), 1)] if fx_mid else None

    # 예상 강도도 절대수준과 예상 방향을 분리해 같은 방식으로 계산한다.
    future_level_pct = _percentile(level_window, fx_mid) if fx_mid is not None else None
    future_level_score = 1.0 - 2.0 * future_level_pct if future_level_pct is not None else level_score
    future_direction_score = -tanh(fx_bias_pct * 12.0)
    future_score = max(-1.0, min(1.0, future_level_score * 0.65 + future_direction_score * 0.35))

    coverage = sum(bool(value) for value in (fx, base, y2 or y3, core_cpi, industrial)) / 5.0
    confidence_cap = 0.75 if us_connected else 0.55
    confidence = round(min(confidence_cap, 0.25 + coverage * 0.30), 2)

    current_grade = grade_from_score(score)
    direction_grade = grade_from_score(momentum_score)
    future_grade = grade_from_score(future_score)

    return {
        "status": "ok" if fx and base else "partial",
        "current": {
            "kr_base_rate_pct": latest_rate,
            "usdkrw": latest_fx,
            "kr_gov_2y_pct": y2[-1] if y2 else None,
            "kr_gov_3y_pct": y3[-1] if y3 else None,
            "kr_gov_10y_pct": y10[-1] if y10 else None,
            "krw_strength_score": round(score, 4),
            "krw_strength_grade": current_grade,
            "krw_absolute_level_score": round(level_score, 4),
            "krw_absolute_level_grade": grade_from_score(level_score),
            "krw_short_term_momentum_score": round(momentum_score, 4),
            "krw_short_term_momentum_grade": direction_grade,
            "usdkrw_1y_percentile": round(fx_percentile, 4) if fx_percentile is not None else None,
        },
        "forecast": {
            "horizon": "향후 1~3개월",
            "kr_base_rate_direction": rate_direction,
            "kr_base_rate_expected_pct": round(rate_expected, 2) if rate_expected is not None else None,
            "kr_base_rate_scenarios": {
                "hold": {"rate_pct": latest_rate, "probability": rate_prob["hold"]},
                "hike_25bp": {"rate_pct": round(latest_rate + 0.25, 2) if latest_rate is not None else None, "probability": rate_prob["hike"]},
                "cut_25bp": {"rate_pct": round(max(0.0, latest_rate - 0.25), 2) if latest_rate is not None else None, "probability": rate_prob["cut"]},
            },
            "usdkrw_mid": round(fx_mid, 1) if fx_mid is not None else None,
            "usdkrw_range": fx_range,
            "krw_strength_score": round(future_score, 4),
            "krw_strength_grade": future_grade,
            "confidence": confidence,
            "confidence_limit": "미국 정책금리 엔진 미연결로 0.55 상한 적용" if not us_connected else "미국 정책금리 엔진 연결 반영",
        },
        "methodology": {
            "note": "현재 원화 강도는 환율의 절대 수준과 단기 모멘텀을 분리해 계산합니다. 금리 전망은 동결을 기준 시나리오로 두고 시장금리 격차·근원물가·산업생산을 제한적으로 반영한 규칙형 1차 전망입니다.",
            "fx_1m_change": round(fx1, 5) if fx1 is not None else None,
            "fx_3m_change": round(fx3, 5) if fx3 is not None else None,
            "fx_forecast_bias_pct": round(fx_bias_pct, 5),
            "policy_market_gap_pctp": round(gap, 3) if gap is not None else None,
            "us_policy_engine_connected": us_connected,
        },
    }


def calculate_placeholder() -> KrwStrengthResult:
    return KrwStrengthResult(0.0, None, "약중립", "약중립", 0.0)
