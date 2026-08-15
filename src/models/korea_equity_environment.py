from __future__ import annotations

import math
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from src.core.io import read_json, write_json
from src.models.volatility_regime import build_regime_output
from src.models.layered_signal import build_layered_output


SCHEMA_VERSION = "3.0.0"
MODEL_VERSION = "korea-equity-environment-v3.0-semiconductor-pbr-band"

# Task 1: 반도체 마이크로 팩터 통합 → semiconductor 최소 30% 비중
# 기존 컴포넌트 합산 비중 = 70%, SemiconductorCycleScore = 30%
COMPONENT_WEIGHTS = {
    "flow": 0.21,           # 기존 0.30 × 0.70
    "breadth": 0.175,       # 기존 0.25 × 0.70
    "valuation": 0.14,      # 기존 0.20 × 0.70
    "earnings_revision": 0.105,  # 기존 0.15 × 0.70
    "credit_spread": 0.07,  # 기존 0.10 × 0.70
    "semiconductor": 0.30,  # Task 1 신설
}

# Task 3: PBR 밸류에이션 밴드 기준
PBR_EXTREME_LOW = 0.8    # 이하: 극단적 저평가 → 기계적 하방 지지
PBR_LOW = 1.0            # 이하: 저평가 구간
PBR_FAIR = 1.3           # 적정 밸류에이션 기준
PBR_HIGH = 1.6           # 이상: 고평가 구간


def _num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(value)))


def _round(value: float | None, digits: int = 6) -> float | None:
    return round(float(value), digits) if value is not None and math.isfinite(float(value)) else None


def _safe_read(path: Path, default: Any) -> Any:
    try:
        return read_json(path) if path.exists() else default
    except Exception:
        return default


def _percentile(values: list[float], current: float) -> float | None:
    clean = sorted(float(v) for v in values if math.isfinite(float(v)))
    if len(clean) < 20:
        return None
    below = sum(1 for value in clean if value < current)
    equal = sum(1 for value in clean if value == current)
    return (below + 0.5 * equal) / len(clean)


def _band(score: float | None) -> str:
    if score is None:
        return "데이터 부족"
    if score >= 85:
        return "매우 우호"
    if score >= 70:
        return "우호"
    if score >= 58:
        return "약우호"
    if score >= 43:
        return "중립"
    if score >= 31:
        return "약불리"
    if score >= 16:
        return "불리"
    return "매우 불리"


def _combine_flow(raw: dict[str, Any]) -> dict[str, Any]:
    rows = []
    total_buy = 0.0
    total_foreign = 0.0
    total_institution = 0.0
    total_combined = 0.0
    for market in ("kospi", "kosdaq"):
        row = ((raw.get("flows") or {}).get(market) or {})
        if not row.get("available"):
            continue
        buy = _num(row.get("total_buy_value"))
        foreign = _num(row.get("foreign_net_value"))
        institution = _num(row.get("institution_net_value"))
        combined = _num(row.get("combined_net_value"))
        if buy is None or buy <= 0 or combined is None:
            continue
        total_buy += buy
        total_foreign += foreign or 0.0
        total_institution += institution or 0.0
        total_combined += combined
        rows.append(market)
    if not rows or total_buy <= 0:
        return {"available": False, "score_normalized": None, "markets": rows}
    combined_bps = total_combined / total_buy * 10000.0
    # Smooth saturation prevents one exceptional flow day from dominating the whole gauge.
    normalized = math.tanh(combined_bps / 50.0)
    return {
        "available": True,
        "score_normalized": _round(normalized),
        "combined_net_bps_of_turnover": _round(combined_bps, 3),
        "foreign_net_bps_of_turnover": _round(total_foreign / total_buy * 10000.0, 3),
        "institution_net_bps_of_turnover": _round(total_institution / total_buy * 10000.0, 3),
        "markets": rows,
        "detail": "외국인+기관 순매수 / 동기간 전체 매수대금, tanh 완만한 포화 적용",
    }


def _combine_breadth(raw: dict[str, Any]) -> dict[str, Any]:
    advances = declines = unchanged = 0
    markets: list[str] = []
    for market in ("kospi", "kosdaq"):
        row = ((raw.get("breadth") or {}).get(market) or {})
        if not row.get("available"):
            continue
        advances += int(row.get("advances") or 0)
        declines += int(row.get("declines") or 0)
        unchanged += int(row.get("unchanged") or 0)
        markets.append(market)
    valid = advances + declines + unchanged
    if valid <= 0:
        return {"available": False, "score_normalized": None, "markets": markets}
    balance = (advances - declines) / valid
    return {
        "available": True,
        "score_normalized": _round(_clamp(balance, -1, 1)),
        "advances": advances,
        "declines": declines,
        "unchanged": unchanged,
        "advance_share": _round(advances / valid),
        "advance_decline_balance": _round(balance),
        "markets": markets,
        "detail": "(상승종목수-하락종목수)/유효종목수",
    }


def _valuation_component(raw: dict[str, Any], fundamentals: dict[str, Any]) -> dict[str, Any]:
    scores: list[float] = []
    evidence: list[dict[str, Any]] = []
    indices = fundamentals.get("indices") or {}
    histories = raw.get("valuation_history") or {}
    for key in ("kospi200", "kosdaq150"):
        current = indices.get(key) or {}
        hist = histories.get(key) or {}
        if not current.get("available") or not hist.get("available"):
            continue
        current_per = _num(current.get("per"))
        current_pbr = _num(current.get("pbr"))
        rows = list(hist.get("rows") or [])
        per_values = [_num(row.get("per")) for row in rows if isinstance(row, dict)]
        pbr_values = [_num(row.get("pbr")) for row in rows if isinstance(row, dict)]
        per_values = [value for value in per_values if value is not None and value > 0]
        pbr_values = [value for value in pbr_values if value is not None and value > 0]
        local_scores: list[float] = []
        per_pct = _percentile(per_values, current_per) if current_per is not None else None
        pbr_pct = _percentile(pbr_values, current_pbr) if current_pbr is not None else None
        if per_pct is not None:
            local_scores.append(1.0 - 2.0 * per_pct)
        if pbr_pct is not None:
            local_scores.append(1.0 - 2.0 * pbr_pct)
        if not local_scores:
            continue
        local = sum(local_scores) / len(local_scores)
        scores.append(local)
        evidence.append(
            {
                "index": key,
                "per": current_per,
                "pbr": current_pbr,
                "per_percentile": _round(per_pct),
                "pbr_percentile": _round(pbr_pct),
                "history_samples": len(rows),
            }
        )
    if not scores:
        return {
            "available": False,
            "score_normalized": None,
            "evidence": evidence,
            "reason": "KRX PER/PBR 과거분포 표본 부족 또는 현재 지표 부재",
        }
    score = sum(scores) / len(scores)
    return {
        "available": True,
        "score_normalized": _round(_clamp(score, -1, 1)),
        "evidence": evidence,
        "detail": "현재 PER·PBR의 최근 약 1.5년 KRX 분포 백분위. 낮을수록 우호.",
    }


class SemiconductorCycleScore:
    """Task 1: 글로벌 반도체 사이클 점수 산출.

    SOX, TWII, NVDA, TSM의 20d/60d 모멘텀을 가중 합산하여
    한국 수출/반도체 의존도를 반영한 선행 지표를 생성한다.
    """

    # 각 지표 가중치 (합 = 1.0)
    WEIGHTS = {
        "sox": 0.35,   # 필라델피아 반도체 지수 (한국 수출 연동도 최고)
        "nvda": 0.25,  # NVDA AI 수요 프록시
        "tsm": 0.25,   # TSM 대만 공급망 (삼성/SK하이닉스와 경쟁·협력)
        "twii": 0.15,  # 대만 가권 (반도체 밸류체인 전체)
    }
    # 20d/60d 모멘텀 내부 가중치
    MOM_W_20D = 0.6
    MOM_W_60D = 0.4

    @classmethod
    def compute(cls, global_data: dict[str, Any]) -> dict[str, Any]:
        momentum = global_data.get("semiconductor_momentum") or {}
        if not momentum:
            return {
                "available": False,
                "score_normalized": None,
                "reason": "global_market에 semiconductor_momentum 데이터 없음",
            }

        scores: list[float] = []
        weights_used: float = 0.0
        evidence: dict[str, Any] = {}

        for key, weight in cls.WEIGHTS.items():
            info = momentum.get(key) or {}
            m20 = _num(info.get("mom_20d_pct"))
            m60 = _num(info.get("mom_60d_pct"))
            latest = _num(info.get("latest"))
            evidence[key] = {
                "latest": _round(latest),
                "mom_20d_pct": _round(m20),
                "mom_60d_pct": _round(m60),
            }
            if m20 is None and m60 is None:
                continue
            # 모멘텀 → tanh 포화 정규화 (-1 ~ 1)
            m20_norm = math.tanh(m20 / 10.0) if m20 is not None else 0.0
            m60_norm = math.tanh(m60 / 15.0) if m60 is not None else 0.0
            # 20d가 없으면 60d 단독 사용
            if m20 is None:
                combined = m60_norm
            elif m60 is None:
                combined = m20_norm
            else:
                combined = cls.MOM_W_20D * m20_norm + cls.MOM_W_60D * m60_norm
            scores.append(combined * weight)
            weights_used += weight

        if weights_used < 0.3:
            return {
                "available": False,
                "score_normalized": None,
                "evidence": evidence,
                "reason": f"유효 가중치 합 {weights_used:.2f} < 0.30: 반도체 데이터 부족",
            }

        score_norm = sum(scores) / weights_used
        return {
            "available": True,
            "score_normalized": _round(_clamp(score_norm, -1.0, 1.0)),
            "weights_used": _round(weights_used),
            "evidence": evidence,
            "detail": "SOX 35% + NVDA 25% + TSM 25% + TWII 15% 가중 20d/60d 모멘텀 tanh 정규화",
        }


def _pbr_valuation_band(
    raw: dict[str, Any],
    fundamentals: dict[str, Any],
    base_score: float | None,
) -> dict[str, Any]:
    """Task 3: KOSPI PBR 밸류에이션 밴드 및 평균 회귀 로직.

    PBR ≤ 0.8 (극단적 저평가) 구간에서는 거시 지표 부진에도 불구하고
    '기계적 하방 지지 / 밸류에이션 리바운드' 가중치를 부여하여
    숏 신호를 억제하고 분할 매수 시그널로 전환한다.
    """
    indices = fundamentals.get("indices") or {}
    pbr_values: list[float] = []
    for key in ("kospi200", "kosdaq150"):
        pbr = _num((indices.get(key) or {}).get("pbr"))
        if pbr is not None and pbr > 0:
            pbr_values.append(pbr)

    # valuation_history에서 최신 PBR 보완
    if not pbr_values:
        histories = raw.get("valuation_history") or {}
        for key in ("kospi200", "kosdaq150"):
            rows = list(((histories.get(key) or {}).get("rows") or []))
            if rows:
                pbr = _num(rows[-1].get("pbr"))
                if pbr is not None and pbr > 0:
                    pbr_values.append(pbr)

    if not pbr_values:
        return {
            "available": False,
            "pbr": None,
            "band": "데이터 없음",
            "adjusted_score": base_score,
            "adjustment": 0,
            "signal_override": None,
        }

    pbr = sum(pbr_values) / len(pbr_values)

    # 밴드 분류
    if pbr <= PBR_EXTREME_LOW:
        band = "극단적_저평가"
        # 기계적 하방 지지: 점수를 50점 이상으로 강제 하한 설정 + 분할매수 신호
        adjustment = max(0, 50 - (base_score or 0))
        adjusted_score = max(50, base_score or 0)
        signal_override = "분할_매수_시그널 (PBR 극단 저평가 하방 지지)"
    elif pbr <= PBR_LOW:
        band = "저평가"
        # 부드러운 하향 지지: 거시 불리 시에도 최소 10점 보너스
        adjustment = 10.0 * (PBR_LOW - pbr) / (PBR_LOW - PBR_EXTREME_LOW)
        adjusted_score = min(100, (base_score or 0) + adjustment)
        signal_override = "숏_신호_억제 (PBR 저평가 구간)" if (base_score or 0) < 43 else None
    elif pbr <= PBR_FAIR:
        band = "적정"
        adjustment = 0
        adjusted_score = base_score
        signal_override = None
    elif pbr <= PBR_HIGH:
        band = "고평가"
        adjustment = -5.0
        adjusted_score = max(0, (base_score or 0) + adjustment)
        signal_override = None
    else:
        band = "과열"
        adjustment = -10.0
        adjusted_score = max(0, (base_score or 0) + adjustment)
        signal_override = "과열_경고 (PBR 고평가)"

    return {
        "available": True,
        "pbr": _round(pbr, 3),
        "pbr_samples": len(pbr_values),
        "band": band,
        "adjusted_score": round(_clamp(adjusted_score or 0, 0, 100)),
        "adjustment": _round(adjustment),
        "signal_override": signal_override,
        "thresholds": {
            "extreme_low": PBR_EXTREME_LOW,
            "low": PBR_LOW,
            "fair": PBR_FAIR,
            "high": PBR_HIGH,
        },
        "interpretation": f"KOSPI PBR {pbr:.3f}x → {band}: {signal_override or '조정 없음'}",
    }


def _history_revision_component(fundamentals: dict[str, Any], history: list[dict[str, Any]], raw: dict[str, Any] | None = None) -> dict[str, Any]:
    indices = fundamentals.get("indices") or {}
    current_values: list[float] = []
    for key in ("kospi200", "kosdaq150"):
        value = _num((indices.get(key) or {}).get("eps_growth_pct"))
        if value is not None:
            current_values.append(value)
    if not current_values:
        # No extra API call fallback: pykrx get_index_fundamental already returns
        # index close and PER in the same historical frame used by valuation.
        # close / PER is a trailing earnings-per-index-unit proxy. Compare recent
        # and earlier windows to measure the current earnings environment without
        # pretending it is analyst forward revision data.
        raw = raw or {}
        histories = raw.get("valuation_history") or {}
        growth_scores: list[float] = []
        evidence: list[dict[str, Any]] = []
        for key in ("kospi200", "kosdaq150"):
            rows = list(((histories.get(key) or {}).get("rows") or []))
            eps_rows: list[tuple[str, float]] = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                d = str(row.get("date") or "")
                close = _num(row.get("close"))
                per = _num(row.get("per"))
                if d and close is not None and close > 0 and per is not None and per > 0:
                    eps_rows.append((d, close / per))
            eps_rows.sort(key=lambda item: item[0])
            values = [value for _, value in eps_rows]
            if len(values) < 60:
                evidence.append({"index": key, "samples": len(values), "available": False})
                continue
            recent_window = values[-20:]
            baseline_window = values[-60:-40]
            if len(baseline_window) < 10:
                evidence.append({"index": key, "samples": len(values), "available": False})
                continue
            recent = sum(recent_window) / len(recent_window)
            baseline = sum(baseline_window) / len(baseline_window)
            if baseline <= 0:
                continue
            growth_pct = (recent / baseline - 1.0) * 100.0
            growth_scores.append(growth_pct)
            evidence.append({
                "index": key, "samples": len(values),
                "recent_eps_proxy": _round(recent, 6),
                "baseline_eps_proxy": _round(baseline, 6),
                "growth_pct": _round(growth_pct, 3),
                "available": True,
            })
        if growth_scores:
            average_growth = sum(growth_scores) / len(growth_scores)
            return {
                "available": True,
                "score_normalized": _round(math.tanh(average_growth / 10.0)),
                "current_growth_proxy_pct": _round(average_growth, 3),
                "baseline_growth_proxy_pct": None,
                "revision_pct_point": None,
                "history_samples": min((int(item.get("samples") or 0) for item in evidence if item.get("available")), default=0),
                "mode": "trailing_eps_proxy_from_existing_krx_close_per",
                "evidence": evidence,
                "detail": "공개 forward 컨센서스 미확보: 기존 KRX 지수 종가/PER로 계산한 후행 EPS 대용치의 최근 20거래일과 이전 구간 추세. 추가 API 호출 없음.",
            }
        return {
            "available": False,
            "score_normalized": None,
            "reason": "공개 컨센서스 forward proxy 미확보 및 기존 KRX 종가/PER 후행 EPS 대용치 표본 부족",
            "mode": "unavailable",
            "evidence": evidence,
        }
    current = sum(current_values) / len(current_values)

    previous_values: list[float] = []
    for row in history[-35:]:
        value = _num((row.get("earnings") or {}).get("growth_proxy_pct")) if isinstance(row, dict) else None
        if value is not None:
            previous_values.append(value)
    if len(previous_values) < 5:
        # First-run bootstrap: do not fabricate a revision history.  Until five
        # committed observations exist, use the currently observed forward earnings
        # growth proxy as an *earnings outlook level*.  This is explicitly labelled
        # and automatically switches to true within-engine revision once history is
        # sufficient.  No extra network call is introduced.
        normalized = math.tanh(current / 20.0)
        return {
            "available": True,
            "score_normalized": _round(normalized),
            "current_growth_proxy_pct": _round(current, 3),
            "baseline_growth_proxy_pct": None,
            "revision_pct_point": None,
            "history_samples": len(previous_values),
            "mode": "forward_growth_level_bootstrap",
            "detail": "초기 이력 5회 전: 공개 forward 성장 대용치의 현재 수준을 사용. 5회 누적 후 최근 이력 대비 revision 변화로 자동 전환.",
        }
    baseline = sum(previous_values[-20:]) / min(20, len(previous_values))
    revision_pp = current - baseline
    normalized = math.tanh(revision_pp / 5.0)
    return {
        "available": True,
        "score_normalized": _round(normalized),
        "current_growth_proxy_pct": _round(current, 3),
        "baseline_growth_proxy_pct": _round(baseline, 3),
        "revision_pct_point": _round(revision_pp, 3),
        "history_samples": len(previous_values),
        "mode": "revision_vs_committed_history",
        "detail": "기존 공개 컨센서스 성장률 대용치의 최근 이력 대비 변화. 정확한 증권사 EPS revision 원자료가 아님.",
    }


def _credit_component(raw: dict[str, Any], history: list[dict[str, Any]]) -> dict[str, Any]:
    credit = raw.get("credit") or {}
    current = _num(credit.get("spread_pct_point"))
    if current is None:
        return {"available": False, "score_normalized": None, "reason": "AA- 회사채-국고채 3년 스프레드 부재"}

    # Prefer same-run historical spreads built from same-date ECOS corporate and
    # government yields.  This makes the percentile available on the first successful
    # run and avoids waiting for 20 daily commits.  Fall back to committed model history
    # for backward compatibility with older raw files.
    values: list[float] = []
    for row in list(credit.get("spread_history") or [])[-320:]:
        value = _num(row.get("spread_pct_point")) if isinstance(row, dict) else None
        if value is not None:
            values.append(value)
    history_source = "same_run_ecos_history"
    if len(values) < 20:
        values = []
        for row in history[-320:]:
            value = _num((row.get("credit") or {}).get("spread_pct_point")) if isinstance(row, dict) else None
            if value is not None:
                values.append(value)
        history_source = "committed_model_history"

    percentile = _percentile(values, current)
    if percentile is None:
        return {
            "available": False,
            "score_normalized": None,
            "spread_pct_point": _round(current, 4),
            "history_samples": len(values),
            "history_source": history_source,
            "reason": "신용스프레드 분포 표본 20개 미만",
        }
    normalized = 1.0 - 2.0 * percentile
    return {
        "available": True,
        "score_normalized": _round(_clamp(normalized, -1, 1)),
        "spread_pct_point": _round(current, 4),
        "percentile": _round(percentile),
        "history_samples": len(values),
        "history_source": history_source,
        "detail": "AA- 회사채3년-국고채3년의 동일날짜 스프레드 과거분포 백분위. 좁을수록 우호.",
    }


def _history_row(raw: dict[str, Any], fundamentals: dict[str, Any]) -> dict[str, Any]:
    indices = fundamentals.get("indices") or {}
    growth_values = []
    for key in ("kospi200", "kosdaq150"):
        value = _num((indices.get(key) or {}).get("eps_growth_pct"))
        if value is not None:
            growth_values.append(value)
    growth_proxy = sum(growth_values) / len(growth_values) if growth_values else None
    return {
        "date": date.today().isoformat(),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "earnings": {"growth_proxy_pct": _round(growth_proxy, 4)},
        "credit": {"spread_pct_point": _round(_num((raw.get("credit") or {}).get("spread_pct_point")), 4)},
    }


def _update_history(path: Path, row: dict[str, Any]) -> list[dict[str, Any]]:
    existing = _safe_read(path, [])
    rows = list(existing) if isinstance(existing, list) else []
    current_date = str(row.get("date") or "")
    rows = [item for item in rows if isinstance(item, dict) and str(item.get("date") or "") != current_date]
    rows.append(row)
    rows = sorted(rows, key=lambda item: str(item.get("date") or ""))[-400:]
    write_json(path, rows)
    return rows


def build_and_write(
    output_dir: Path,
    raw: dict[str, Any],
    realtime_risk: dict[str, Any] | None = None,
) -> dict[str, Any]:
    fundamentals = _safe_read(output_dir / "korea_asset_fundamentals.json", {})
    history_path = output_dir / "korea_equity_environment_history.json"
    prior_history = _safe_read(history_path, [])
    prior_history = list(prior_history) if isinstance(prior_history, list) else []
    global_data = _safe_read(output_dir / "raw_global_market.json", {})

    flow = _combine_flow(raw)
    breadth = _combine_breadth(raw)
    valuation = _valuation_component(raw, fundamentals)
    earnings_revision = _history_revision_component(fundamentals, prior_history, raw)
    credit_spread = _credit_component(raw, prior_history)

    # Task 1: 반도체 마이크로 팩터 (SemiconductorCycleScore)
    semiconductor = SemiconductorCycleScore.compute(global_data)

    component_map = {
        "flow": flow,
        "breadth": breadth,
        "valuation": valuation,
        "earnings_revision": earnings_revision,
        "credit_spread": credit_spread,
        "semiconductor": semiconductor,
    }
    weighted_sum = 0.0
    active_weight = 0.0
    for name, component in component_map.items():
        comp_score = _num(component.get("score_normalized"))
        weight = COMPONENT_WEIGHTS[name]
        if component.get("available") and comp_score is not None:
            weighted_sum += comp_score * weight
            active_weight += weight

    score = None
    if active_weight >= 0.55:
        normalized = weighted_sum / active_weight
        score = round(_clamp(50.0 + normalized * 50.0, 0, 100))

    # Task 3: PBR 밸류에이션 밴드 적용
    pbr_band = _pbr_valuation_band(raw, fundamentals, score)
    # PBR 조정 후 점수를 최종 Layer 1 기준으로 사용
    score_after_pbr = pbr_band.get("adjusted_score") if pbr_band.get("available") else score

    data_coverage_pct = round(active_weight * 100)
    stale_sections = []
    for section_name in ("flows", "breadth"):
        for market, section in ((raw.get(section_name) or {}).items()):
            if isinstance(section, dict) and section.get("stale"):
                stale_sections.append(f"{section_name}.{market}")
    if (raw.get("credit") or {}).get("stale"):
        stale_sections.append("credit")

    # 변동성 레짐 감지 + EMP + 비선형 충격 + 서킷브레이커
    realtime = realtime_risk or {}
    ecos_data = _safe_read(output_dir / "raw_ecos.json", {})

    # Task 3 PBR 조정 점수를 레짐 계산에 전달
    regime_output = {}
    layered_output = {}
    try:
        regime_output = build_regime_output(score_after_pbr, realtime, global_data, ecos_data)
    except Exception as exc:
        regime_output = {"available": False, "error": f"{type(exc).__name__}: {str(exc)[:180]}"}

    # 개선 2 + Task 2: 레이어드 듀얼 모델 (Layer 1 거시 + Layer 2 기술/수급 + Flow Momentum)
    krx_data = _safe_read(output_dir / "raw_krx.json", {})
    try:
        layered_output = build_layered_output(score_after_pbr, raw, realtime, regime_output, krx_data=krx_data)
    except Exception as exc:
        layered_output = {"available": False, "error": f"{type(exc).__name__}: {str(exc)[:180]}"}

    # 최종 점수: 레짐 조정 후 점수 사용 (서킷브레이커 발동 시 None)
    # PBR 극단 저평가 구간에서는 서킷브레이커 발동에도 '하방 지지' 경보는 유지
    circuit_breaker = bool(regime_output.get("circuit_breaker"))
    pbr_override = pbr_band.get("signal_override") if pbr_band.get("available") else None
    final_score = regime_output.get("final_score") if regime_output.get("final_score_valid") else score_after_pbr
    if circuit_breaker:
        final_score = None

    result = {
        "schema_version": SCHEMA_VERSION,
        "engine_version": MODEL_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope": "한국 주식시장 직접환경 전용. 기존 금리·환율 엔진 출력과 독립",
        # Layer 1 기본 점수
        "score": score,
        "score_valid": score is not None,
        "bias": _band(score),
        # 레짐 조정 후 최종 점수
        "final_score": final_score,
        "final_score_valid": final_score is not None,
        "final_bias": _band(final_score),
        "circuit_breaker": circuit_breaker,
        "circuit_breaker_action": regime_output.get("circuit_breaker_action"),
        "data_coverage_pct": data_coverage_pct,
        "active_weight": _round(active_weight, 3),
        "complete_for_market_card": bool(score is not None and data_coverage_pct == 100),
        "stale_sections": stale_sections,
        "components": component_map,
        "weights": COMPONENT_WEIGHTS,
        # Task 1: 반도체 마이크로 팩터
        "semiconductor_cycle": semiconductor,
        # Task 2: 레이어드 듀얼 모델 출력 (Flow Momentum 포함)
        "layered_signal": layered_output,
        # Task 3: PBR 밸류에이션 밴드
        "pbr_valuation_band": pbr_band,
        "pbr_signal_override": pbr_override,
        "score_after_pbr": score_after_pbr,
        # 변동성 레짐 / EMP / 비선형 충격 / 서킷브레이커
        "volatility_regime": regime_output,
        "current_inputs": {
            "kospi200": (fundamentals.get("indices") or {}).get("kospi200") or {},
            "kosdaq150": (fundamentals.get("indices") or {}).get("kosdaq150") or {},
            "credit": raw.get("credit") or {},
        },
        "interpretation": {
            "score_scale": "0~15 매우 불리 · 16~30 불리 · 31~42 약불리 · 43~57 중립 · 58~69 약우호 · 70~84 우호 · 85~100 매우 우호",
            "meaning": "객관적 시장 입력을 규칙기반으로 합성한 직접시장 환경점수이며 수익률 확률이나 보장이 아닙니다.",
            "coverage_rule": "활성 가중치 합이 55% 미만이면 점수를 계산하지 않습니다.",
            "circuit_breaker_rule": "USD/KRW 3일 변화가 3σ 초과 또는 VKOSPI 45 이상 시 서킷브레이커 발동, 방향성 예측 정지",
            "layered_signal_rule": "Layer1(거시 우호≥58) AND Layer2(과매도 탈출 신호) 동시 충족 시에만 BUY_SIGNAL 발생",
        },
        "call_efficiency": {
            "reuses_existing_asset_fundamentals": True,
            "reuses_existing_raw_ecos_gov3y": True,
            "realtime_risk_collector_added": True,
            "history_is_local_committed_json": True,
        },
        "limitations": [
            "이익환경은 공개 forward 성장 대용치를 우선 사용합니다. 비어 있으면 기존 KRX 지수 종가/PER에서 계산한 후행 EPS 대용치 추세를 사용하며 실제 증권사 forward revision으로 오인하지 않습니다.",
            "외국인·기관 수급은 KRX 거래대금 기준이며 기관합계가 없으면 기관 세부주체를 합산합니다.",
            "CDS 프리미엄은 직접 데이터 미확보로 한미 국채 스프레드 + HY OAS로 근사합니다.",
            "VKOSPI는 pykrx 조회 가능 시 실시간 반영, 불가 시 이전 값 재사용합니다.",
            "서킷브레이커 발동 시 환경점수는 무효화되며 위험회피 최고단계 신호만 출력됩니다.",
            "레이어드 기술적 지표는 기존 KRX 지수 종가/PER에서 계산한 KOSPI200 일봉 데이터를 활용합니다.",
            "모든 신규 기능은 이 전용 JSON에만 기록되며 기존 korea_rate_fx_outlook 계열 스키마는 변경하지 않습니다.",
        ],
    }

    write_json(output_dir / "korea_equity_environment.json", result)
    _update_history(history_path, _history_row(raw, fundamentals))
    return result
