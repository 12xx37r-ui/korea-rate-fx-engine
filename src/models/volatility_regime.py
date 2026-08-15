from __future__ import annotations

"""변동성 레짐 감지 및 위험 관리 모듈 (개선 5~8번).

구현 항목:
  5. 변동성 임계값 기반 가중치 동적 전환 (Volatility Trigger)
  6. 통화 압력 지수 (Exchange Market Pressure, EMP)
  7. 비선형 스트레스 전이 함수 (Non-linear Shock Multiplier)
  8. 긴급 서킷브레이커 (Circuit Breaker)
"""

import math
from datetime import datetime, timezone
from typing import Any


MODEL_VERSION = "volatility-regime-v1.0"

# 레짐 분류 기준
REGIME_THRESHOLDS = {
    "fx_3d_sigma_crisis": 2.0,      # USD/KRW 3일 변화가 이 σ를 넘으면 위기 레짐
    "fx_3d_sigma_blackswan": 3.0,   # 서킷브레이커 발동 임계값
    "vkospi_elevated": 30.0,        # VKOSPI 이 이상이면 위험 신호
    "vkospi_crisis": 45.0,          # 서킷브레이커 보조 조건
    "cds_proxy_elevated": 2.0,      # CDS 프록시 이 이상이면 위험
}

# 레짐별 가중치 (Layer 2에서 실시간 vs 매크로 비중)
REGIME_WEIGHTS = {
    "normal": {
        "macro_env": 0.70,
        "realtime_risk": 0.30,
    },
    "elevated": {
        "macro_env": 0.40,
        "realtime_risk": 0.60,
    },
    "crisis": {
        "macro_env": 0.20,
        "realtime_risk": 0.80,
    },
}


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


def compute_emp(
    usdkrw_current: float | None,
    usdkrw_prev: float | None,
    fx_reserves_current: float | None,
    fx_reserves_prev: float | None,
    kr_rate_current: float | None,
    us_rate_current: float | None,
) -> dict[str, Any]:
    """통화 압력 지수(Exchange Market Pressure) 산출 (개선 6번).

    EMP = w1 * ΔFXR + w2 * ΔReserves + w3 * Δ(내외금리차)
    값이 높을수록 원화 절하 압력이 크다.
    """
    components: list[float] = []
    evidence: dict[str, Any] = {}

    # 1. 환율 변화율 (상승=원화절하 압력)
    if usdkrw_current is not None and usdkrw_prev is not None and usdkrw_prev > 0:
        fx_change_pct = (usdkrw_current / usdkrw_prev - 1.0) * 100.0
        components.append(fx_change_pct * 0.5)
        evidence["fx_change_pct"] = _round(fx_change_pct)
    else:
        evidence["fx_change_pct"] = None

    # 2. 외환보유액 감소율 (감소=당국 개입 시그널, 절하 압력)
    if fx_reserves_current is not None and fx_reserves_prev is not None and fx_reserves_prev > 0:
        reserve_change_pct = (fx_reserves_current / fx_reserves_prev - 1.0) * 100.0
        components.append(-reserve_change_pct * 0.3)  # 감소는 압력 증가
        evidence["reserve_change_pct"] = _round(reserve_change_pct)
    else:
        evidence["reserve_change_pct"] = None

    # 3. 내외 금리차 변화 (한국-미국, 격차 축소=원화 약세 압력)
    if kr_rate_current is not None and us_rate_current is not None:
        rate_differential = kr_rate_current - us_rate_current
        # 금리차가 낮을수록 자본유출 압력
        components.append(-rate_differential * 0.2)
        evidence["rate_differential_pct"] = _round(rate_differential)
    else:
        evidence["rate_differential_pct"] = None

    if not components:
        return {
            "available": False,
            "emp_score": None,
            "reason": "EMP 계산에 필요한 데이터 부족",
        }

    emp_score = sum(components)
    return {
        "available": True,
        "emp_score": _round(emp_score),
        "components_count": len(components),
        "evidence": evidence,
        "interpretation": "양수=원화절하 압력 증가, 음수=원화절상 압력",
    }


def nonlinear_shock_multiplier(delta_fx_pct: float | None, sigma: float | None) -> dict[str, Any]:
    """비선형 스트레스 전이 함수 (개선 7번).

    환율 변동률이 정상 범위를 벗어나면 기하급수적으로 페널티 증가.
    페널티 = (ΔFXR / σ)^2 * base_penalty (단, |z| > 1 구간에서만 적용)
    """
    if delta_fx_pct is None or sigma is None or sigma <= 0:
        return {"available": False, "penalty": 0.0, "z_score": None}

    z = delta_fx_pct / sigma
    abs_z = abs(z)

    if abs_z <= 1.0:
        penalty = 0.0
        label = "정상"
    elif abs_z <= 2.0:
        # 선형 전이 구간
        penalty = (abs_z - 1.0) * 5.0
        label = "주의"
    else:
        # 비선형 가속 구간: z^2 페널티
        penalty = z ** 2 * 3.0
        label = "충격"

    penalty = min(penalty, 50.0)  # 최대 페널티 상한

    return {
        "available": True,
        "penalty": _round(penalty),
        "z_score": _round(z),
        "abs_z": _round(abs_z),
        "regime_label": label,
        "formula": "penalty = z^2 * 3.0 (|z|>2 구간), (|z|-1)*5.0 (1<|z|<=2), 0 (정상)",
    }


def detect_regime(
    realtime: dict[str, Any],
    global_data: dict[str, Any],
) -> dict[str, Any]:
    """변동성 임계값 기반 레짐 감지 (개선 5번).

    Returns:
        regime: 'normal' | 'elevated' | 'crisis'
        circuit_breaker: bool (개선 8번)
        weights: 레짐별 가중치
    """
    usdkrw_vol = realtime.get("usdkrw_volatility") or {}
    chg_3d = _num(usdkrw_vol.get("change_3d_pct"))
    std_5d = _num(usdkrw_vol.get("std_5d"))
    std_10d = _num(usdkrw_vol.get("std_10d"))

    vkospi_info = realtime.get("vkospi") or {}
    vkospi = _num(vkospi_info.get("latest"))

    cds_info = realtime.get("cds_proxy") or {}
    cds_proxy = _num(cds_info.get("cds_proxy_score"))

    # 현재 시그마 대비 3일 변화 계산
    reference_sigma = std_10d or std_5d
    fx_z_score = abs(chg_3d / reference_sigma) if (chg_3d is not None and reference_sigma and reference_sigma > 0) else None

    evidence: dict[str, Any] = {
        "fx_3d_change_pct": _round(chg_3d),
        "fx_std_5d": _round(std_5d),
        "fx_std_10d": _round(std_10d),
        "fx_z_score": _round(fx_z_score),
        "vkospi_latest": _round(vkospi),
        "cds_proxy": _round(cds_proxy),
    }

    # 서킷브레이커 조건 (개선 8번)
    circuit_breaker = False
    circuit_breaker_reasons: list[str] = []

    if fx_z_score is not None and fx_z_score >= REGIME_THRESHOLDS["fx_3d_sigma_blackswan"]:
        circuit_breaker = True
        circuit_breaker_reasons.append(f"USD/KRW 3일 변화 {fx_z_score:.1f}σ ≥ {REGIME_THRESHOLDS['fx_3d_sigma_blackswan']}σ")

    if vkospi is not None and vkospi >= REGIME_THRESHOLDS["vkospi_crisis"]:
        circuit_breaker = True
        circuit_breaker_reasons.append(f"VKOSPI {vkospi:.1f} ≥ {REGIME_THRESHOLDS['vkospi_crisis']}")

    # 레짐 분류 (개선 5번)
    if circuit_breaker:
        regime = "crisis"
    elif (
        (fx_z_score is not None and fx_z_score >= REGIME_THRESHOLDS["fx_3d_sigma_crisis"])
        or (vkospi is not None and vkospi >= REGIME_THRESHOLDS["vkospi_elevated"])
        or (cds_proxy is not None and cds_proxy >= REGIME_THRESHOLDS["cds_proxy_elevated"])
    ):
        regime = "elevated"
    else:
        regime = "normal"

    shock_mult = nonlinear_shock_multiplier(chg_3d, reference_sigma)

    return {
        "regime": regime,
        "circuit_breaker": circuit_breaker,
        "circuit_breaker_reasons": circuit_breaker_reasons,
        "circuit_breaker_action": "현금비중최대화/헤지필요 - 방향성 예측 일시 정지" if circuit_breaker else None,
        "weights": REGIME_WEIGHTS[regime],
        "shock_multiplier": shock_mult,
        "evidence": evidence,
        "thresholds": REGIME_THRESHOLDS,
        "interpretation": {
            "normal": "평상시: 거시 지표 70% + 실시간 위험 30%",
            "elevated": "주의: 거시 지표 40% + 실시간 위험 60%",
            "crisis": "위기: 거시 지표 20% + 실시간 위험 80%, 서킷브레이커 발동 시 예측 정지",
        },
    }


def apply_emp_penalty(
    score: float | None,
    emp: dict[str, Any],
) -> dict[str, Any]:
    """EMP 지수 기반 주식시장 환경점수 하향 패널티 적용 (개선 6번)."""
    if score is None:
        return {"adjusted_score": None, "emp_applied": False}
    if not emp.get("available"):
        return {"adjusted_score": score, "emp_applied": False}

    emp_score = _num(emp.get("emp_score"))
    if emp_score is None:
        return {"adjusted_score": score, "emp_applied": False}

    # EMP가 양수(절하 압력)일 때 주식 환경점수에 페널티
    if emp_score > 0:
        penalty = min(emp_score * 3.0, 20.0)  # 최대 20점 하향
        adjusted = max(0.0, score - penalty)
    else:
        # 절상 압력은 소폭 상향 가능
        bonus = min(abs(emp_score) * 1.5, 8.0)
        adjusted = min(100.0, score + bonus)

    return {
        "adjusted_score": _round(adjusted),
        "original_score": score,
        "emp_score": _round(emp_score),
        "adjustment": _round(adjusted - score),
        "emp_applied": True,
    }


def build_regime_output(
    macro_env_score: float | None,
    realtime: dict[str, Any],
    global_data: dict[str, Any],
    ecos_data: dict[str, Any],
) -> dict[str, Any]:
    """레짐 감지 + EMP + 비선형 충격 + 서킷브레이커 통합 출력."""
    regime_info = detect_regime(realtime, global_data)

    # EMP 계산용 데이터 추출
    usdkrw_rows = list(global_data.get("usd_krw_yahoo") or global_data.get("usd_krw_fred") or [])
    usdkrw_now = _num(usdkrw_rows[-1].get("value")) if len(usdkrw_rows) >= 2 else None
    usdkrw_prev = _num(usdkrw_rows[-20].get("value")) if len(usdkrw_rows) >= 20 else None

    kr_rate_rows = list(ecos_data.get("kr_base_rate") or [])
    us_rate_rows = list(global_data.get("us_2y") or [])

    def _last(rows: list[dict[str, Any]], key: str = "value") -> float | None:
        for row in reversed(rows):
            v = _num(row.get(key) or row.get("DATA_VALUE"))
            if v is not None:
                return v
        return None

    emp = compute_emp(
        usdkrw_current=usdkrw_now,
        usdkrw_prev=usdkrw_prev,
        fx_reserves_current=None,
        fx_reserves_prev=None,
        kr_rate_current=_last(kr_rate_rows),
        us_rate_current=_last(us_rate_rows),
    )

    # EMP 기반 점수 조정
    emp_adjusted = apply_emp_penalty(macro_env_score, emp)

    # 서킷브레이커 발동 시 점수 무효화
    circuit_breaker = regime_info.get("circuit_breaker", False)
    final_score = None if circuit_breaker else emp_adjusted.get("adjusted_score")

    return {
        "model_version": MODEL_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "regime": regime_info.get("regime"),
        "circuit_breaker": circuit_breaker,
        "circuit_breaker_reasons": regime_info.get("circuit_breaker_reasons") or [],
        "circuit_breaker_action": regime_info.get("circuit_breaker_action"),
        "dynamic_weights": regime_info.get("weights"),
        "shock_multiplier": regime_info.get("shock_multiplier"),
        "emp": emp,
        "emp_adjustment": emp_adjusted,
        "macro_env_score_original": macro_env_score,
        "macro_env_score_after_emp": emp_adjusted.get("adjusted_score"),
        "final_score": final_score,
        "final_score_valid": final_score is not None,
        "regime_evidence": regime_info.get("evidence"),
    }
