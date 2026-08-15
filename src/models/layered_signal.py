from __future__ import annotations

"""레이어드 듀얼 모델 구조 (개선 2번).

Layer 1: 기존 거시경제 환경점수 (korea_equity_environment)
Layer 2: 기술적 지표 + 시장 수급 단기 모멘텀 필터

최종 신호: Layer 1 우호 AND Layer 2 과매도 탈출 신호 동시 충족 시에만 발생
"""

import math
from datetime import datetime, timezone
from typing import Any


MODEL_VERSION = "layered-signal-v1.0"

# Layer 1 판단 기준
LAYER1_FAVORABLE_THRESHOLD = 58  # '약우호' 이상이면 Layer 1 우호
LAYER1_UNFAVORABLE_THRESHOLD = 43  # '약불리' 이하이면 Layer 1 불리

# Layer 2 기술적 지표 기준
RSI_OVERSOLD = 35.0        # 이하이면 과매도
RSI_OVERBOUGHT = 70.0      # 이상이면 과매수
MA_BELOW_THRESHOLD = -3.0  # 이동평균 대비 -3% 이하면 과매도 구간
BOLLINGER_LOWER_THRESHOLD = -1.5  # 볼린저 하단 대비 이 이하이면 과매도


def _num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _round(value: float | None, digits: int = 4) -> float | None:
    return round(float(value), digits) if value is not None and math.isfinite(float(value)) else None


def _compute_rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    recent = changes[-period:]
    gains = [c for c in recent if c > 0]
    losses = [abs(c) for c in recent if c < 0]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _compute_ma_deviation(closes: list[float], window: int = 20) -> float | None:
    if len(closes) < window:
        return None
    ma = sum(closes[-window:]) / window
    current = closes[-1]
    if ma == 0:
        return None
    return (current / ma - 1.0) * 100.0


def _compute_bollinger(closes: list[float], window: int = 20, num_std: float = 2.0) -> dict[str, Any]:
    if len(closes) < window:
        return {"available": False}
    subset = closes[-window:]
    ma = sum(subset) / window
    variance = sum((v - ma) ** 2 for v in subset) / window
    std = math.sqrt(variance)
    current = closes[-1]
    upper = ma + num_std * std
    lower = ma - num_std * std
    width = upper - lower
    b_pct = (current - lower) / width if width > 0 else None
    z_score = (current - ma) / std if std > 0 else None
    return {
        "available": True,
        "ma": _round(ma),
        "upper": _round(upper),
        "lower": _round(lower),
        "current": _round(current),
        "b_pct": _round(b_pct),        # 0=하단, 0.5=중간, 1=상단
        "z_score": _round(z_score),
        "bandwidth": _round(width),
    }


def _extract_kospi_closes(equity_raw: dict[str, Any]) -> list[float]:
    hist = (equity_raw.get("valuation_history") or {}).get("kospi200") or {}
    rows = list(hist.get("rows") or [])
    closes: list[float] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        v = _num(row.get("close"))
        if v is not None and v > 0:
            closes.append(v)
    return closes


def compute_layer2(
    equity_raw: dict[str, Any],
    realtime: dict[str, Any],
) -> dict[str, Any]:
    """Layer 2: 기술적 지표 + 단기 수급 필터."""
    closes = _extract_kospi_closes(equity_raw)
    signals: list[str] = []
    evidence: dict[str, Any] = {}
    scores: list[float] = []

    # 1. RSI
    rsi = _compute_rsi(closes, period=14)
    evidence["rsi_14"] = _round(rsi)
    if rsi is not None:
        if rsi <= RSI_OVERSOLD:
            signals.append("RSI_과매도_탈출_후보")
            scores.append(1.0)
        elif rsi >= RSI_OVERBOUGHT:
            signals.append("RSI_과매수_과열")
            scores.append(-1.0)
        else:
            normalized = (rsi - 50.0) / 50.0
            scores.append(-normalized * 0.3)

    # 2. 이동평균 이격도 (MA deviation)
    ma_dev = _compute_ma_deviation(closes, window=20)
    evidence["ma20_deviation_pct"] = _round(ma_dev)
    if ma_dev is not None:
        if ma_dev <= MA_BELOW_THRESHOLD:
            signals.append("MA20_하방_이격_과매도")
            scores.append(0.8)
        elif ma_dev >= 5.0:
            signals.append("MA20_상방_과열")
            scores.append(-0.5)
        else:
            scores.append(-ma_dev / 10.0)

    # 3. 볼린저 밴드
    bb = _compute_bollinger(closes, window=20)
    evidence["bollinger"] = bb
    if bb.get("available") and bb.get("b_pct") is not None:
        b_pct = float(bb["b_pct"])
        if b_pct <= 0.15:
            signals.append("볼린저_하단_과매도")
            scores.append(0.9)
        elif b_pct >= 0.85:
            signals.append("볼린저_상단_과열")
            scores.append(-0.6)
        else:
            scores.append((0.5 - b_pct) * 0.4)

    # 4. 글로벌 반도체 모멘텀 (선행 지표)
    semi = realtime.get("semiconductor") or {}
    sox_5d = _num(semi.get("sox_5d_pct"))
    evidence["sox_5d_pct"] = _round(sox_5d)
    if sox_5d is not None:
        if sox_5d >= 3.0:
            signals.append("SOX_5일_강세_상승모멘텀")
            scores.append(0.6)
        elif sox_5d <= -3.0:
            signals.append("SOX_5일_약세_하락모멘텀")
            scores.append(-0.6)
        else:
            scores.append(sox_5d / 10.0)

    # 5. VKOSPI 신호
    vkospi_info = realtime.get("vkospi") or {}
    vkospi = _num(vkospi_info.get("latest"))
    evidence["vkospi"] = _round(vkospi)
    if vkospi is not None:
        if vkospi >= 30.0:
            signals.append("VKOSPI_공포_구간")
            scores.append(-0.7)
        elif vkospi <= 15.0:
            signals.append("VKOSPI_안정_구간")
            scores.append(0.3)
        else:
            scores.append(-(vkospi - 22.5) / 22.5 * 0.4)

    if not scores:
        return {
            "available": False,
            "score_normalized": None,
            "signals": signals,
            "evidence": evidence,
            "reason": "Layer 2 지표 데이터 부족",
        }

    avg_score = sum(scores) / len(scores)
    # -1 ~ 1 → 0 ~ 100 변환
    layer2_score = round(max(0.0, min(100.0, 50.0 + avg_score * 50.0)))

    # 과매도 탈출 신호 여부 (RSI/볼린저/MA 중 2개 이상 과매도 신호)
    oversold_count = sum(1 for s in signals if "과매도" in s)
    oversold_exit_signal = oversold_count >= 2

    return {
        "available": True,
        "score": layer2_score,
        "score_normalized": _round(avg_score),
        "signals": signals,
        "oversold_exit_signal": oversold_exit_signal,
        "oversold_indicators_count": oversold_count,
        "evidence": evidence,
    }


def compute_combined_signal(
    layer1_score: float | None,
    layer2: dict[str, Any],
    regime: dict[str, Any],
) -> dict[str, Any]:
    """Layer 1 + Layer 2 결합 최종 신호 (개선 2번).

    최종 매수 신호 조건:
    - Layer 1: 거시 환경 우호 (약우호 이상, 58점 이상)
    - Layer 2: 기술적/수급 과매도 탈출 신호 발생
    - 레짐: 서킷브레이커 미발동
    """
    circuit_breaker = regime.get("circuit_breaker", False)

    if circuit_breaker:
        return {
            "signal": "RISK_OFF_MAX",
            "signal_korean": "위험회피 최고 단계 - 현금 비중 최대화/헤지 필요",
            "layer1_favorable": None,
            "layer2_oversold_exit": None,
            "combined_favorable": False,
            "circuit_breaker": True,
            "reason": "서킷브레이커 발동: 정상 방향성 예측 정지",
            "regime": regime.get("regime"),
        }

    layer1_favorable = (
        layer1_score is not None and layer1_score >= LAYER1_FAVORABLE_THRESHOLD
    )
    layer1_unfavorable = (
        layer1_score is not None and layer1_score < LAYER1_UNFAVORABLE_THRESHOLD
    )
    layer2_available = layer2.get("available", False)
    layer2_oversold_exit = layer2.get("oversold_exit_signal", False)
    layer2_score = _num(layer2.get("score"))

    # 동적 가중치 적용 (레짐에 따라 변경)
    dyn_weights = regime.get("weights") or {"macro_env": 0.70, "realtime_risk": 0.30}
    macro_weight = dyn_weights.get("macro_env", 0.70)
    rt_weight = dyn_weights.get("realtime_risk", 0.30)

    combined_score = None
    if layer1_score is not None and layer2_score is not None:
        combined_score = round(layer1_score * macro_weight + layer2_score * rt_weight)

    # 페널티 적용 (비선형 충격)
    shock = regime.get("shock_multiplier") or {}
    penalty = _num(shock.get("penalty")) or 0.0
    if combined_score is not None and penalty > 0:
        combined_score = max(0, round(combined_score - penalty))

    # 신호 결정
    if layer1_favorable and layer2_oversold_exit and not circuit_breaker:
        signal = "BUY_SIGNAL"
        signal_korean = "매수 신호 - Layer1(거시 우호) + Layer2(과매도 탈출) 동시 충족"
    elif layer1_unfavorable:
        signal = "RISK_OFF"
        signal_korean = "위험 회피 - 거시 환경 불리"
    elif layer2_score is not None and layer2_score >= 65 and layer1_favorable:
        signal = "WATCH_BUY"
        signal_korean = "매수 관망 - Layer1 우호이나 Layer2 과매도 탈출 미확인"
    elif layer2_score is not None and layer2_score <= 35:
        signal = "CAUTION"
        signal_korean = "주의 - 기술적 약세 신호"
    else:
        signal = "NEUTRAL"
        signal_korean = "중립 - 명확한 방향성 없음"

    return {
        "signal": signal,
        "signal_korean": signal_korean,
        "combined_score": combined_score,
        "layer1_score": layer1_score,
        "layer1_favorable": layer1_favorable,
        "layer2_score": layer2_score,
        "layer2_oversold_exit": layer2_oversold_exit if layer2_available else None,
        "layer2_available": layer2_available,
        "circuit_breaker": circuit_breaker,
        "regime": regime.get("regime"),
        "dynamic_weights_applied": dyn_weights,
        "shock_penalty_applied": _round(penalty),
    }


def build_layered_output(
    layer1_score: float | None,
    equity_raw: dict[str, Any],
    realtime: dict[str, Any],
    regime: dict[str, Any],
) -> dict[str, Any]:
    layer2 = compute_layer2(equity_raw, realtime)
    signal = compute_combined_signal(layer1_score, layer2, regime)
    return {
        "model_version": MODEL_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "layer1": {
            "score": layer1_score,
            "favorable": signal.get("layer1_favorable"),
            "threshold": LAYER1_FAVORABLE_THRESHOLD,
        },
        "layer2": layer2,
        "combined_signal": signal,
        "interpretation": {
            "BUY_SIGNAL": "Layer1(거시 우호) + Layer2(과매도 탈출) 동시 충족 시에만 발생하는 고확신 매수 신호",
            "RISK_OFF": "거시 환경 불리: 비중 축소 권고",
            "RISK_OFF_MAX": "서킷브레이커: 현금 비중 최대화/헤지 필요",
            "WATCH_BUY": "Layer1 우호이나 기술적 확인 필요",
            "CAUTION": "기술적 약세: 신규 진입 자제",
            "NEUTRAL": "방향성 중립: 관망",
        },
    }
