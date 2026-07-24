from __future__ import annotations

from dataclasses import dataclass
from math import tanh
from statistics import mean
from typing import Any


@dataclass
class KrwStrengthResult:
    score: float
    percentile: float | None
    grade: str
    direction_grade: str
    confidence: float


def grade_from_score(score: float) -> str:
    if score >= 0.67: return "강강"
    if score >= 0.34: return "강약"
    if score >= 0.08: return "강중립"
    if score > -0.08: return "약중립"
    if score > -0.34: return "약강"
    return "약약"


def _values(rows: list[dict[str, Any]]) -> list[float]:
    out = []
    for row in rows:
        try: out.append(float(row.get("DATA_VALUE")))
        except (TypeError, ValueError): pass
    return out


def _pct(values: list[float], periods: int) -> float | None:
    if len(values) <= periods or values[-periods-1] == 0: return None
    return values[-1] / values[-periods-1] - 1.0


def build_snapshot(ecos: dict[str, list[dict[str, Any]]], kosis: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    fx = _values(ecos.get("usdkrw", []))
    base = _values(ecos.get("kr_base_rate", []))
    y2 = _values(ecos.get("kr_gov_2y", []))
    y3 = _values(ecos.get("kr_gov_3y", []))
    y10 = _values(ecos.get("kr_gov_10y", []))

    # USD/KRW decline means KRW strength. Combine 1m and 3m momentum and yield-curve policy signal.
    fx1 = _pct(fx, min(20, max(1, len(fx)-1))) if len(fx) > 1 else None
    fx3 = _pct(fx, min(60, max(1, len(fx)-1))) if len(fx) > 1 else None
    momentum_parts = [x for x in [fx1, fx3] if x is not None]
    fx_momentum = mean(momentum_parts) if momentum_parts else 0.0
    curve = ((y2[-1] if y2 else (y3[-1] if y3 else 0.0)) - (base[-1] if base else 0.0))
    score = max(-1.0, min(1.0, -tanh(fx_momentum * 12.0) * 0.8 - tanh(curve / 1.5) * 0.2))

    # Forecast is a transparent signal range, not a point promise.
    latest_fx = fx[-1] if fx else None
    fx_bias_pct = max(-0.06, min(0.06, -score * 0.035))
    fx_mid = latest_fx * (1 + fx_bias_pct) if latest_fx else None
    fx_range = [round(fx_mid * 0.97, 1), round(fx_mid * 1.03, 1)] if fx_mid else None

    latest_rate = base[-1] if base else None
    market_yield = y2[-1] if y2 else (y3[-1] if y3 else None)
    gap = (market_yield - latest_rate) if market_yield is not None and latest_rate is not None else None
    if gap is None:
        rate_direction, rate_expected = "판단보류", latest_rate
    elif gap <= -0.20:
        rate_direction, rate_expected = "인하 우세", max(0.0, latest_rate - 0.25)
    elif gap >= 0.30:
        rate_direction, rate_expected = "인상 위험", latest_rate + 0.25
    else:
        rate_direction, rate_expected = "동결 우세", latest_rate

    confidence_inputs = sum(bool(x) for x in [fx, base, y2 or y3, kosis.get("cpi_core"), kosis.get("industrial_production")])
    confidence = round(confidence_inputs / 5, 2)
    future_score = max(-1.0, min(1.0, score * 0.65 + (-fx_bias_pct / 0.06) * 0.35))

    return {
        "status": "ok" if fx and base else "partial",
        "current": {
            "kr_base_rate_pct": latest_rate,
            "usdkrw": latest_fx,
            "kr_gov_2y_pct": y2[-1] if y2 else None,
            "kr_gov_3y_pct": y3[-1] if y3 else None,
            "kr_gov_10y_pct": y10[-1] if y10 else None,
            "krw_strength_score": round(score, 4),
            "krw_strength_grade": grade_from_score(score),
        },
        "forecast": {
            "horizon": "향후 1~3개월",
            "kr_base_rate_direction": rate_direction,
            "kr_base_rate_expected_pct": round(rate_expected, 2) if rate_expected is not None else None,
            "usdkrw_mid": round(fx_mid, 1) if fx_mid is not None else None,
            "usdkrw_range": fx_range,
            "krw_strength_score": round(future_score, 4),
            "krw_strength_grade": grade_from_score(future_score),
            "confidence": confidence,
        },
        "methodology": {
            "note": "시장금리·환율 모멘텀·근원물가·산업생산 기반 규칙형 1차 전망입니다. 미국 정책금리 엔진 연결 전에는 신뢰도를 제한합니다.",
            "fx_1m_change": round(fx1, 5) if fx1 is not None else None,
            "fx_3m_change": round(fx3, 5) if fx3 is not None else None,
            "policy_market_gap_pctp": round(gap, 3) if gap is not None else None,
        },
    }


def calculate_placeholder() -> KrwStrengthResult:
    return KrwStrengthResult(0.0, None, "약중립", "약중립", 0.0)
