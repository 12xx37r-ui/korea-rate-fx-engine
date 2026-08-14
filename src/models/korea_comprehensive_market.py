from __future__ import annotations

"""
한국증시 종합환경 엔진 (보강 확장 모듈 v1.0)

기존 엔진 출력 파일을 재사용하여 신규 API 호출 없이 운영합니다.
기존 엔진의 함수·파일·스키마를 변경하지 않고 독립 모듈로 추가합니다.

재사용 파일:
  korea_equity_environment.json        (flow/breadth/valuation/earnings/credit)
  raw_korea_equity_environment.json    (종가 이동평균 계산)
  raw_ecos.json                        (기준금리·국고채 시계열)
  raw_global_market.json               (USD/KRW·VIX·HY OAS·달러인덱스)
  korea_rate_forecast_v2.json          (기준금리 방향 신호)
  korea_krw_strength_forecast.json     (원화강도 예측)
  korea_krw_liquidity_forecast.json    (유동성 예측)
  korea_asset_fundamentals.json        (지수 PER/PBR)

출력: output/korea_comprehensive_environment.json (신규, 기존 파일 불변)
"""

import math
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from src.core.io import read_json, write_json
from src.core.pit_database import pit_coverage_check


SCHEMA_VERSION = "1.0.0"
MODEL_VERSION = "korea-comprehensive-market-v1.0-zero-new-api"

# ── Current Score 가중치 (합계=1.0) ──────────────────────────────────────────
CURRENT_WEIGHTS: dict[str, float] = {
    "price_trend":            0.12,   # A. 주가추세
    "breadth":                0.12,   # B. 시장 Breadth
    "earnings":               0.12,   # C. 기업이익
    "valuation":              0.10,   # D. 밸류에이션
    "flow":                   0.15,   # E. 수급
    "rate_liquidity_credit":  0.14,   # F. 금리·유동성·신용
    "fx":                     0.10,   # G. 환율
    "market_risk":            0.08,   # H. 시장위험
    "external":               0.07,   # I. 대외시장환경
}

MIN_COVERAGE = 0.50   # 활성 가중치 합이 50% 미만이면 점수 미산출


# ── 공통 유틸 ─────────────────────────────────────────────────────────────────
def _num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _r(v: float | None, d: int = 4) -> float | None:
    if v is None:
        return None
    try:
        fv = float(v)
    except (TypeError, ValueError):
        return None
    return round(fv, d) if math.isfinite(fv) else None


def _safe_read(path: Path, default: Any) -> Any:
    try:
        return read_json(path) if path.exists() else default
    except Exception:
        return default


def _percentile(values: list[float], current: float) -> float | None:
    clean = [float(v) for v in values if math.isfinite(float(v))]
    if len(clean) < 20:
        return None
    below = sum(1 for v in clean if v < current)
    equal = sum(1 for v in clean if v == current)
    return (below + 0.5 * equal) / len(clean)


def _ecos_series(rows: list[Any]) -> list[tuple[str, float]]:
    """ECOS 시계열 → (TIME, DATA_VALUE) 정렬 리스트."""
    out: list[tuple[str, float]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        t = str(row.get("TIME") or "")
        v = _num(row.get("DATA_VALUE"))
        if t and v is not None:
            out.append((t, v))
    return sorted(out, key=lambda x: x[0])


def _global_series(rows: list[Any]) -> list[tuple[str, float]]:
    """global_market 시계열 → (date, value) 정렬 리스트."""
    out: list[tuple[str, float]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        d = str(row.get("date") or "")
        v = _num(row.get("value"))
        if d and v is not None and v > 0:
            out.append((d, v))
    return sorted(out, key=lambda x: x[0])


def _current_regime(score: int | None) -> str:
    if score is None:
        return "데이터 부족"
    if score >= 90:
        return "과열"
    if score >= 80:
        return "강한 호황"
    if score >= 70:
        return "호황"
    if score >= 60:
        return "약호황"
    if score >= 50:
        return "중립"
    if score >= 40:
        return "약간 불리"
    if score >= 30:
        return "약세"
    if score >= 15:
        return "불황"
    return "극심한 불황"


def _trend_direction(score: int | None) -> str:
    if score is None:
        return "데이터 부족"
    if score >= 20:
        return "강한 개선"
    if score >= 8:
        return "개선"
    if score >= -7:
        return "횡보"
    if score >= -20:
        return "악화"
    return "강한 악화"


def _forward_direction(score: int | None) -> str:
    if score is None:
        return "데이터 부족"
    if score >= 80:
        return "강한 상승 우세"
    if score >= 65:
        return "상승 우세"
    if score >= 55:
        return "소폭 상승 우세"
    if score >= 45:
        return "중립"
    if score >= 35:
        return "소폭 하락 우세"
    if score >= 20:
        return "하락 우세"
    return "강한 하락 우세"


def _forward_bias(score: int | None) -> str:
    """유불리 표현 (상세 단계)."""
    if score is None:
        return "판단불가"
    if score >= 80:
        return "강강"
    if score >= 70:
        return "강약"
    if score >= 60:
        return "약강"
    if score >= 50:
        return "중립"
    if score >= 40:
        return "약약"
    if score >= 30:
        return "약불리"
    return "불리"


# ── 팩터 A: 주가추세 ─────────────────────────────────────────────────────────
def _factor_price_trend(raw_equity: dict[str, Any]) -> dict[str, Any]:
    """KOSPI200 종가 시계열 기반 이동평균·수익률 신호. 추가 API 없음."""
    hist = raw_equity.get("valuation_history") or {}
    kospi_hist = hist.get("kospi200") or {}
    rows = list(kospi_hist.get("rows") or [])

    pairs: list[tuple[str, float]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        d = str(row.get("date") or "")
        c = _num(row.get("close"))
        if d and c is not None and c > 0:
            pairs.append((d, c))
    pairs.sort(key=lambda x: x[0])
    prices = [p for _, p in pairs]
    n = len(prices)

    if n < 25:
        return {
            "available": False,
            "score_normalized": None,
            "reason": "KOSPI200 가격 시계열 표본 부족 (<25거래일)",
            "samples": n,
            "data_status": "DATA_PENDING",
        }

    current_price = prices[-1]
    signals: list[float] = []
    evidence: dict[str, Any] = {"current_close": _r(current_price, 2), "samples": n}

    # 수익률 신호
    for label, lookback, scale in [
        ("1m", 20, 0.05), ("3m", 60, 0.10), ("6m", 125, 0.15), ("12m", 250, 0.20)
    ]:
        if n > lookback:
            ret = current_price / prices[-(lookback + 1)] - 1.0
            sig = math.tanh(ret / scale)
            signals.append(sig)
            evidence[f"return_{label}_pct"] = _r(ret * 100, 2)
            evidence[f"signal_{label}"] = _r(sig, 4)

    # 이동평균 신호
    for label, window in [("ma50", 50), ("ma200", 200)]:
        if n >= window:
            ma = sum(prices[-window:]) / window
            sig = 0.5 if current_price > ma else -0.5
            signals.append(sig)
            evidence[label] = _r(ma, 2)
            evidence[f"above_{label}"] = current_price > ma
            evidence[f"signal_{label}"] = sig

    if not signals:
        return {
            "available": False,
            "score_normalized": None,
            "reason": "신호 계산 불가",
            "data_status": "DATA_PENDING",
        }

    score = _clamp(sum(signals) / len(signals), -1, 1)
    return {
        "available": True,
        "score_normalized": _r(score),
        "evidence": evidence,
        "signal_count": len(signals),
        "source": "raw_korea_equity_environment.json → valuation_history.kospi200",
        "detail": "KOSPI200 종가 MA50·MA200 신호 + 1M/3M/6M/12M 수익률 신호",
    }


# ── 팩터 B/C/D/E: equity_env 재사용 ─────────────────────────────────────────
def _reuse_equity_component(equity_env: dict[str, Any], key: str) -> dict[str, Any]:
    comp = (equity_env.get("components") or {}).get(key) or {}
    if not comp.get("available"):
        return {
            "available": False,
            "score_normalized": None,
            "reason": f"korea_equity_environment.{key} 미수집 또는 연결대기",
            "data_status": "DATA_PENDING",
            "source": "korea_equity_environment.json",
        }
    return {**comp, "source": "korea_equity_environment.json"}


# ── 팩터 B 보강: KOSPI vs KOSDAQ Breadth 이원화 ───────────────────────────────
def _factor_breadth_dualtrack(equity_env: dict[str, Any], raw_equity: dict[str, Any]) -> dict[str, Any]:
    """KOSPI(수출·반도체 대형주) vs KOSDAQ(내수·중소형주) Breadth 이원화.

    composite score_normalized는 기존 equity_env 값을 유지하여
    Current Score 가중합 계산에 영향을 주지 않는다.
    dual_track은 디커플링 분석용 보조 출력이다.
    """
    comp = (equity_env.get("components") or {}).get("breadth") or {}
    if not comp.get("available"):
        return {
            "available": False, "score_normalized": None,
            "reason": "Breadth 데이터 미수집", "data_status": "DATA_PENDING",
            "source": "korea_equity_environment.json",
        }

    raw_br = raw_equity.get("breadth") or {}
    kospi_br  = raw_br.get("kospi") or {}
    kosdaq_br = raw_br.get("kosdaq") or {}

    dual_track: dict[str, Any] = {}

    # KOSPI -반도체·수출 대형주 편중 (삼성전자·SK하이닉스 시총비중 30~35%)
    k_adv = int(kospi_br.get("advances") or 0)
    k_dec = int(kospi_br.get("declines") or 0)
    k_tot = k_adv + k_dec
    if k_tot > 0:
        k_adb = (k_adv - k_dec) / k_tot
        dual_track["export_large_cap"] = {
            "market": "KOSPI",
            "advances": k_adv, "declines": k_dec,
            "valid_symbols": kospi_br.get("valid_symbols"),
            "ad_balance": _r(k_adb, 4),
            "signal": _r(_clamp(k_adb, -1, 1), 4),
            "note": "반도체·수출 대형주 편중 -지수 왜곡 위험 구간",
        }

    # KOSDAQ -내수·중소형·바이오 성장주
    q_adv = int(kosdaq_br.get("advances") or 0)
    q_dec = int(kosdaq_br.get("declines") or 0)
    q_tot = q_adv + q_dec
    if q_tot > 0:
        q_adb = (q_adv - q_dec) / q_tot
        dual_track["domestic_small_cap"] = {
            "market": "KOSDAQ",
            "advances": q_adv, "declines": q_dec,
            "valid_symbols": kosdaq_br.get("valid_symbols"),
            "ad_balance": _r(q_adb, 4),
            "signal": _r(_clamp(q_adb, -1, 1), 4),
            "note": "내수·바이오·중소형 성장주 -체감 시장 온도계",
        }

    # 디커플링 탐지: KOSPI 강세인데 KOSDAQ 약세 → 반도체 쏠림 착시 경보
    if "export_large_cap" in dual_track and "domestic_small_cap" in dual_track:
        k_sig = dual_track["export_large_cap"]["signal"]
        q_sig = dual_track["domestic_small_cap"]["signal"]
        divergence = _r(q_sig - k_sig, 4)   # 양수=KOSDAQ우세, 음수=KOSPI우세
        decoupling = abs(divergence) > 0.30
        dual_track["divergence"] = {
            "kosdaq_minus_kospi_signal": divergence,
            "decoupling_detected": decoupling,
            "interpretation": (
                "KOSDAQ 우세 -내수·중소형 체감 강세" if divergence > 0.20 else
                "KOSPI 우세 -반도체·수출 주도 (체감 괴리 위험)" if divergence < -0.20 else
                "동조 -디커플링 없음"
            ),
            "alert": (
                "반도체 쏠림 착시 경보: 종합지수 강세이나 내수·중소형 약세 가능성"
                if k_sig > 0.20 and q_sig < -0.10 else None
            ),
        }

    return {
        **comp,
        "dual_track": dual_track,
        "source": "korea_equity_environment.json + raw_korea_equity_environment.json",
        "detail": comp.get("detail", "") + " | KOSPI vs KOSDAQ Breadth 이원화",
    }


# ── 팩터 C 보강: KOSPI200 vs KOSDAQ150 이익 이원화 ───────────────────────────
def _factor_earnings_dualtrack(equity_env: dict[str, Any]) -> dict[str, Any]:
    """KOSPI200(반도체·대형) vs KOSDAQ150(내수·성장) 이익 성장 이원화.

    composite score_normalized는 기존 equity_env 값 유지.
    """
    comp = (equity_env.get("components") or {}).get("earnings_revision") or {}
    if not comp.get("available"):
        return {
            "available": False, "score_normalized": None,
            "reason": "이익 데이터 미수집", "data_status": "DATA_PENDING",
            "source": "korea_equity_environment.json",
        }

    evidence_list = comp.get("evidence") or []
    dual_track: dict[str, Any] = {}

    for item in evidence_list:
        if not isinstance(item, dict) or not item.get("available"):
            continue
        idx = str(item.get("index") or "").lower()
        growth = _num(item.get("growth_pct"))
        if "kospi" in idx:
            dual_track["export_large_cap"] = {
                "index": item.get("index"),
                "growth_pct": _r(growth, 2),
                "eps_proxy_recent": _r(_num(item.get("recent_eps_proxy")), 4),
                "samples": item.get("samples"),
                "note": "KOSPI200 -반도체·수출 대형주 이익 집중",
            }
        elif "kosdaq" in idx:
            dual_track["domestic_small_cap"] = {
                "index": item.get("index"),
                "growth_pct": _r(growth, 2),
                "eps_proxy_recent": _r(_num(item.get("recent_eps_proxy")), 4),
                "samples": item.get("samples"),
                "note": "KOSDAQ150 -내수·바이오·성장주 이익",
            }

    # 이익 성장 디커플링
    if "export_large_cap" in dual_track and "domestic_small_cap" in dual_track:
        ex_g = _num(dual_track["export_large_cap"]["growth_pct"]) or 0.0
        do_g = _num(dual_track["domestic_small_cap"]["growth_pct"]) or 0.0
        spread = _r(do_g - ex_g, 2)
        dual_track["earnings_divergence"] = {
            "kosdaq150_minus_kospi200_growth_pct": spread,
            "interpretation": (
                "내수·중소형 이익 우위 -반수출 국면" if spread > 5 else
                "반도체·수출 이익 독주 -지수 왜곡 위험" if spread < -10 else
                "이익 성장 동조"
            ),
        }

    return {
        **comp,
        "dual_track": dual_track,
        "source": "korea_equity_environment.json",
        "detail": comp.get("detail", "") + " | KOSPI200 vs KOSDAQ150 이익 이원화",
    }


# ── 팩터 F: 금리·유동성·신용 ─────────────────────────────────────────────────
def _factor_rate_liquidity_credit(
    ecos: dict[str, Any],
    rate_forecast: dict[str, Any],
    liquidity: dict[str, Any],
    equity_env: dict[str, Any],
) -> dict[str, Any]:
    signals: list[float] = []
    evidence: dict[str, Any] = {}
    pending: list[str] = []

    # 1. 기준금리 방향 신호 (다음 금통위 cut-hike 확률 차이)
    path = list(rate_forecast.get("meeting_path") or [])
    if path:
        p0 = path[0].get("probabilities") or {}
        cut_p = _num(p0.get("cut")) or 0.0
        hike_p = _num(p0.get("hike")) or 0.0
        rate_signal = _clamp(cut_p - hike_p, -1, 1)
        signals.append(rate_signal)
        evidence["rate_cut_prob"] = _r(cut_p, 3)
        evidence["rate_hike_prob"] = _r(hike_p, 3)
        evidence["rate_direction_signal"] = _r(rate_signal, 4)
        evidence["next_meeting_action"] = (path[0] or {}).get("most_likely_action")
    else:
        pending.append("RATE_FORECAST_미수집")

    # 2. 장단기금리차 (10y - 3y): 가파를수록 성장 기대 긍정
    gov_3y = _ecos_series(list(ecos.get("kr_gov_3y") or []))
    gov_10y = _ecos_series(list(ecos.get("kr_gov_10y") or []))
    if gov_3y and gov_10y:
        _, g3 = gov_3y[-1]
        _, g10 = gov_10y[-1]
        term_spread = g10 - g3
        # 역사적 장단기 금리차 분포
        s3 = {t: v for t, v in gov_3y}
        s10 = {t: v for t, v in gov_10y}
        common = sorted(set(s3) & set(s10))
        hist_spreads = [s10[t] - s3[t] for t in common[-400:] if math.isfinite(s10[t] - s3[t])]
        pct = _percentile(hist_spreads, term_spread) if len(hist_spreads) >= 20 else None
        if pct is not None:
            # 중립(50%)보다 높으면 긍정, 역전(-) 이면 부정
            ts_signal = _clamp(term_spread / 1.5, -1, 1)
        else:
            ts_signal = _clamp(term_spread / 1.5, -1, 1)
        signals.append(ts_signal)
        evidence["term_spread_pct"] = _r(term_spread, 3)
        evidence["term_spread_signal"] = _r(ts_signal, 4)
        if pct is not None:
            evidence["term_spread_percentile"] = _r(pct, 3)
    else:
        pending.append("ECOS_GOV_RATE_미수집")

    # 3. 신용스프레드 신호 (equity_env 재사용)
    credit_comp = (equity_env.get("components") or {}).get("credit_spread") or {}
    if credit_comp.get("available"):
        cs = _num(credit_comp.get("score_normalized"))
        if cs is not None:
            signals.append(cs)
            evidence["credit_spread_signal"] = _r(cs, 4)
            evidence["credit_spread_pct"] = _num(credit_comp.get("spread_pct_point"))
    else:
        pending.append("CREDIT_SPREAD_미수집")

    # 4. 유동성 신호 (korea_krw_liquidity_forecast 재사용)
    liq_current = liquidity.get("current") or {}
    liq_score_raw = _num(liq_current.get("liquidity_score"))
    if liq_score_raw is not None:
        liq_signal = _clamp(liq_score_raw / 0.5, -1, 1)
        signals.append(liq_signal)
        evidence["liquidity_score"] = _r(liq_score_raw, 4)
        evidence["liquidity_grade"] = liq_current.get("grade")
        evidence["liquidity_signal"] = _r(liq_signal, 4)
    else:
        pending.append("LIQUIDITY_미수집")

    if not signals:
        return {
            "available": False, "score_normalized": None,
            "reason": "금리·유동성·신용 데이터 미수집/연결대기",
            "data_status": "DATA_PENDING",
            "pending": pending,
        }

    score = _clamp(sum(signals) / len(signals), -1, 1)
    return {
        "available": True,
        "score_normalized": _r(score),
        "signal_count": len(signals),
        "evidence": evidence,
        "pending": pending,
        "source": "korea_rate_forecast_v2 + raw_ecos + korea_equity_environment + korea_krw_liquidity",
        "detail": "기준금리 방향·장단기금리차·AA- 신용스프레드·M2 유동성 복합신호",
    }


# ── 팩터 G: 환율 ─────────────────────────────────────────────────────────────
def _factor_fx(
    global_data: dict[str, Any],
    strength_forecast: dict[str, Any],
) -> dict[str, Any]:
    signals: list[float] = []
    evidence: dict[str, Any] = {}
    pending: list[str] = []

    # 1. USD/KRW 방향 (하락=원화강세=주식에 긍정)
    krw_series = _global_series(list(global_data.get("usd_krw_yahoo") or []))
    if len(krw_series) >= 5:
        current_krw = krw_series[-1][1]
        evidence["usd_krw_current"] = _r(current_krw, 2)
        if len(krw_series) >= 21:
            ret_1m = (current_krw / krw_series[-21][1] - 1.0) * 100
            sig = math.tanh(-ret_1m / 3.0)
            signals.append(sig)
            evidence["usd_krw_1m_change_pct"] = _r(ret_1m, 2)
            evidence["fx_1m_signal"] = _r(sig, 4)
        if len(krw_series) >= 63:
            ret_3m = (current_krw / krw_series[-63][1] - 1.0) * 100
            sig_3m = math.tanh(-ret_3m / 5.0)
            signals.append(sig_3m)
            evidence["usd_krw_3m_change_pct"] = _r(ret_3m, 2)
            evidence["fx_3m_signal"] = _r(sig_3m, 4)
    else:
        pending.append("USD_KRW_미수집")

    # 2. 원화강도 예측 신호
    str_current = strength_forecast.get("current") or {}
    str_score = _num(str_current.get("strength_score"))
    if str_score is not None:
        str_signal = _clamp((str_score - 50) / 50, -1, 1)
        signals.append(str_signal)
        evidence["krw_strength_score"] = _r(str_score, 2)
        evidence["krw_strength_grade"] = str_current.get("grade")
        evidence["krw_strength_signal"] = _r(str_signal, 4)
    else:
        pending.append("KRW_STRENGTH_미수집")

    # 3. 달러인덱스 방향 (FRED - 있으면 활용)
    bd_series = _global_series(list(global_data.get("broad_dollar") or []))
    if len(bd_series) >= 21:
        current_bd = bd_series[-1][1]
        bd_1m_ago = bd_series[-21][1]
        bd_change = (current_bd / bd_1m_ago - 1.0) * 100
        bd_signal = math.tanh(-bd_change / 3.0)
        signals.append(bd_signal)
        evidence["broad_dollar_1m_change_pct"] = _r(bd_change, 2)
        evidence["dollar_signal"] = _r(bd_signal, 4)
    else:
        pending.append("BROAD_DOLLAR_FRED_미수집")

    if not signals:
        return {
            "available": False, "score_normalized": None,
            "reason": "환율 데이터 미수집/연결대기",
            "data_status": "DATA_PENDING", "pending": pending,
        }

    raw_score = _clamp(sum(signals) / len(signals), -1, 1)

    # ── 비선형 FX 레짐 탐지 ────────────────────────────────────────────────
    # 한국 증시 환율 특성: 완만한 원화약세=수출호재 / 임계치 돌파=외국인 패닉셀
    # 선형 신호에 비선형 배율을 적용해 위기 국면 가중치를 급증시킨다.
    fx_regime = "NORMAL"
    regime_multiplier = 1.0

    if len(krw_series) >= 22:
        current_krw = krw_series[-1][1]

        # 1개월 롤링 변동성 (일별 수익률 표준편차 × √252, 연환산 %)
        recent = [v for _, v in krw_series[-23:]]
        daily_rets = [recent[i] / recent[i - 1] - 1.0 for i in range(1, len(recent))]
        if len(daily_rets) >= 15:
            mu = sum(daily_rets) / len(daily_rets)
            vol_ann = math.sqrt(sum((r - mu) ** 2 for r in daily_rets) / len(daily_rets) * 252) * 100
            evidence["usd_krw_vol_1m_ann_pct"] = _r(vol_ann, 2)

        # 역사적 레벨 분위수
        hist_prices = [v for _, v in krw_series[-400:]]
        pct_level = _percentile(hist_prices, current_krw)
        if pct_level is not None:
            evidence["usd_krw_level_percentile"] = _r(pct_level, 3)

        # 임계값 체계 (원·달러 기준)
        # 1,360 초과=경계 / 1,420 초과=위기 / 변동성 40% 초과=추가 경계
        high_vol = evidence.get("usd_krw_vol_1m_ann_pct", 0) > 40.0
        if current_krw >= 1420.0 or (current_krw >= 1380.0 and high_vol):
            fx_regime = "CRISIS"
            regime_multiplier = 2.5   # 음수 신호 2.5배 증폭 (외국인 패닉셀 반영)
        elif current_krw >= 1360.0 or high_vol:
            fx_regime = "CAUTION"
            regime_multiplier = 1.5

        evidence["fx_regime"] = fx_regime
        evidence["fx_regime_multiplier"] = regime_multiplier if fx_regime != "NORMAL" else None
        evidence["fx_crisis_thresholds"] = {"caution_krw": 1360, "crisis_krw": 1420}

    # 비선형 배율 적용: 음수(원화약세·악재) 신호만 증폭
    if fx_regime != "NORMAL" and raw_score < 0:
        score = _clamp(raw_score * regime_multiplier, -1, 1)
    else:
        score = raw_score

    return {
        "available": True,
        "score_normalized": _r(score),
        "raw_score_before_regime": _r(raw_score, 4),
        "signal_count": len(signals),
        "evidence": evidence,
        "pending": pending,
        "source": "raw_global_market + korea_krw_strength_forecast",
        "detail": (
            "USD/KRW 1M/3M 추세·원화강도예측·달러인덱스 복합신호 "
            "+ 비선형 레짐 탐지(NORMAL/CAUTION/CRISIS)"
        ),
    }


# ── 파생수급 오버레이 (E. 수급 보조) ─────────────────────────────────────────
def _overlay_derivative_flow(krx_data: dict[str, Any]) -> dict[str, Any]:
    """KRX 파생·수급 오버레이: 외국인 선물 순매수·프로그램매매·신용융자 비율.

    단기 수급 선행 신호를 E. 수급 팩터 보조 레이어로 편입한다.
    Current Score 가중합(CURRENT_WEIGHTS)에는 포함되지 않으며
    별도 overlay_score로 출력한다.

    활성화 조건 (krx_apis.json futures_daily_non_equity enabled=true 설정 후):
      krx_data["futures_daily"]      : 외국인 KOSPI200 선물 누적 순매수
      krx_data["program_trading"]    : 차익/비차익 잔고
      krx_data["customer_deposit"]   : 고객예탁금
      krx_data["margin_loan"]        : 신용융자 잔고
    """
    signals: list[float] = []
    evidence: dict[str, Any] = {}
    pending: list[str] = []

    # 1. 외국인 KOSPI200 선물 누적 순매수 방향
    futures_rows = krx_data.get("futures_daily") or []
    if futures_rows:
        net_positions = [_num(r.get("foreign_net")) for r in futures_rows[-20:] if isinstance(r, dict)]
        net_vals = [v for v in net_positions if v is not None]
        if net_vals:
            recent_net = net_vals[-1]
            avg_net = sum(net_vals) / len(net_vals)
            fut_signal = _clamp(math.tanh((recent_net - avg_net) / max(abs(avg_net), 1e-6)), -1, 1)
            signals.append(fut_signal)
            evidence["foreign_futures_net_signal"] = _r(fut_signal, 4)
    else:
        pending.append("KOSPI200_선물_외국인수급_미수집 (krx_apis.json futures_daily_non_equity 활성화 필요)")

    # 2. 프로그램매매 차익잔고 방향 (차익잔고 증가=매도압력↑)
    program_rows = krx_data.get("program_trading") or []
    if program_rows:
        arb_balance = [_num(r.get("arbitrage_balance")) for r in program_rows[-10:] if isinstance(r, dict)]
        arb_vals = [v for v in arb_balance if v is not None]
        if len(arb_vals) >= 5:
            arb_change = arb_vals[-1] - arb_vals[0]
            arb_signal = _clamp(math.tanh(-arb_change / 1e12), -1, 1)
            signals.append(arb_signal)
            evidence["program_arb_balance_signal"] = _r(arb_signal, 4)
    else:
        pending.append("프로그램매매_차익잔고_미수집")

    # 3. 고객예탁금 대비 신용융자 비율 (비율 급증=과열·역전 위험)
    deposit_rows = krx_data.get("customer_deposit") or []
    margin_rows  = krx_data.get("margin_loan") or []
    if deposit_rows and margin_rows:
        dep = _num((deposit_rows[-1] or {}).get("value"))
        mar = _num((margin_rows[-1] or {}).get("value"))
        if dep and mar and dep > 0:
            ratio = mar / dep
            # 0.5 이상(예탁금의 50% 신용융자)=과열, 0.2 미만=건강
            ratio_signal = _clamp(-(ratio - 0.35) / 0.15, -1, 1)
            signals.append(ratio_signal)
            evidence["margin_deposit_ratio"] = _r(ratio, 4)
            evidence["margin_ratio_signal"] = _r(ratio_signal, 4)
    else:
        pending.append("고객예탁금_신용융자비율_미수집")

    if not signals:
        return {
            "available": False,
            "overlay_active": False,
            "score_normalized": None,
            "data_status": "DATA_PENDING",
            "pending": pending,
            "activation_guide": (
                "krx_apis.json 내 futures_daily_non_equity enabled=true 및 API ID 설정 후 자동 활성화. "
                "DATA_PENDING 상태에서는 Current Score에 영향 없음."
            ),
        }

    score = _clamp(sum(signals) / len(signals), -1, 1)
    return {
        "available": True,
        "overlay_active": True,
        "score_normalized": _r(score),
        "signal_count": len(signals),
        "evidence": evidence,
        "pending": pending,
        "source": "raw_krx.json (파생수급)",
        "detail": "KOSPI200 선물 외국인순매수 · 차익잔고 · 신용융자비율 단기 수급 오버레이",
    }


# ── 팩터 H: 시장위험 ─────────────────────────────────────────────────────────
def _factor_market_risk(global_data: dict[str, Any]) -> dict[str, Any]:
    signals: list[float] = []
    evidence: dict[str, Any] = {}
    pending: list[str] = []

    for key, label, lo_good in [("vix", "VIX", True), ("hy_oas", "HY_OAS", True)]:
        series = _global_series(list(global_data.get(key) or []))
        if len(series) >= 20:
            current_val = series[-1][1]
            hist_vals = [v for _, v in series[-400:]]
            pct = _percentile(hist_vals, current_val)
            if pct is not None:
                sig = (1.0 - 2.0 * pct) if lo_good else (2.0 * pct - 1.0)
                signals.append(sig)
                evidence[f"{key}_current"] = _r(current_val, 3)
                evidence[f"{key}_percentile"] = _r(pct, 3)
                evidence[f"{key}_signal"] = _r(sig, 4)
        else:
            pending.append(f"{label}_FRED_미수집")

    if not signals:
        return {
            "available": False, "score_normalized": None,
            "reason": "시장위험 지표 미수집/연결대기 (FRED VIX·HY OAS)",
            "data_status": "DATA_PENDING", "pending": pending,
        }

    score = _clamp(sum(signals) / len(signals), -1, 1)
    return {
        "available": True,
        "score_normalized": _r(score),
        "signal_count": len(signals),
        "evidence": evidence,
        "pending": pending,
        "source": "raw_global_market (FRED)",
        "detail": "VIX·HY OAS 과거분포 백분위 기반 시장위험 신호",
    }


# ── 팩터 I: 대외시장환경 ─────────────────────────────────────────────────────
def _factor_external(
    global_data: dict[str, Any],
    us_policy: dict[str, Any],
) -> dict[str, Any]:
    signals: list[float] = []
    evidence: dict[str, Any] = {}
    pending: list[str] = []

    # 1. 미국 금리정책 방향
    if isinstance(us_policy, dict) and us_policy:
        us_path = us_policy.get("meeting_path") or us_policy.get("rate_path") or []
        if us_path:
            p0 = (us_path[0] or {}).get("probabilities") or {}
            cut_p = _num(p0.get("cut") or p0.get("lower")) or 0.0
            hike_p = _num(p0.get("hike") or p0.get("raise") or p0.get("higher")) or 0.0
            if cut_p + hike_p > 0.01:
                us_sig = _clamp(cut_p - hike_p, -1, 1)
                signals.append(us_sig)
                evidence["us_cut_prob"] = _r(cut_p, 3)
                evidence["us_rate_signal"] = _r(us_sig, 4)

    # 2. 미국 10년 금리 방향 (FRED)
    us10y = _global_series(list(global_data.get("us_10y") or []))
    if len(us10y) >= 21:
        curr_10y = us10y[-1][1]
        prev_10y = us10y[-21][1]
        change_10y = curr_10y - prev_10y
        us10y_sig = math.tanh(-change_10y / 0.5)
        signals.append(us10y_sig)
        evidence["us_10y_current"] = _r(curr_10y, 3)
        evidence["us_10y_1m_change"] = _r(change_10y, 3)
        evidence["us10y_signal"] = _r(us10y_sig, 4)
    else:
        pending.append("US_10Y_FRED_미수집")

    # 3. USD/CNY 방향 (CNY 약세=한국 수출에 부정적)
    cny_series = _global_series(list(global_data.get("usd_cny") or []))
    if len(cny_series) >= 21:
        curr_cny = cny_series[-1][1]
        prev_cny = cny_series[-21][1]
        cny_change = (curr_cny / prev_cny - 1.0) * 100
        cny_sig = math.tanh(-cny_change / 2.0)
        signals.append(cny_sig)
        evidence["usd_cny_current"] = _r(curr_cny, 4)
        evidence["usd_cny_1m_change_pct"] = _r(cny_change, 2)
        evidence["cny_signal"] = _r(cny_sig, 4)
    else:
        pending.append("USD_CNY_FRED_미수집")

    if not signals:
        return {
            "available": False, "score_normalized": None,
            "reason": "대외시장환경 데이터 미수집/연결대기",
            "data_status": "DATA_PENDING", "pending": pending,
        }

    score = _clamp(sum(signals) / len(signals), -1, 1)
    return {
        "available": True,
        "score_normalized": _r(score),
        "signal_count": len(signals),
        "evidence": evidence,
        "pending": pending,
        "source": "us_input + raw_global_market",
        "detail": "미국 금리정책 방향·미국 10Y·USD/CNY 복합신호",
    }


# ── Current Score 종합 ────────────────────────────────────────────────────────
def _build_current_score(factors: dict[str, dict]) -> dict[str, Any]:
    weighted_sum = 0.0
    active_weight = 0.0
    for name, weight in CURRENT_WEIGHTS.items():
        f = factors.get(name) or {}
        s = _num(f.get("score_normalized"))
        if f.get("available") and s is not None:
            weighted_sum += s * weight
            active_weight += weight

    score: int | None = None
    coverage = round(active_weight * 100)
    if active_weight >= MIN_COVERAGE:
        norm = weighted_sum / active_weight
        score = round(_clamp(50.0 + norm * 50.0, 0, 100))

    pos_factors: list[str] = []
    neg_factors: list[str] = []
    for name, f in factors.items():
        if not f.get("available"):
            continue
        s = _num(f.get("score_normalized"))
        if s is None:
            continue
        if s > 0.25:
            pos_factors.append(name)
        elif s < -0.25:
            neg_factors.append(name)

    return {
        "score": score,
        "score_valid": score is not None,
        "regime": _current_regime(score),
        "coverage_pct": coverage,
        "positive_factors": pos_factors,
        "negative_factors": neg_factors,
        "weights": CURRENT_WEIGHTS,
        "factors": factors,
        "interpretation": (
            "0~14:극심한불황 · 15~29:불황 · 30~39:약세 · 40~49:약간불리 · 50~59:중립 "
            "· 60~69:약호황 · 70~79:호황 · 80~89:강한호황 · 90~100:과열"
        ),
        "coverage_rule": f"활성 가중치 합 {round(active_weight*100)}% (50% 미만 시 점수 미산출)",
    }


# ── Trend Score ───────────────────────────────────────────────────────────────
def _build_trend_score(
    factors: dict[str, dict],
    global_data: dict[str, Any],
    equity_env: dict[str, Any],
    history: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    추세점수: 현재 시장환경이 과거보다 개선·악화 중인가.
    1차: committed history 비교 (5건 이상 누적 시 자동 전환)
    2차: 내재 시계열 방향신호 (초기 운영 또는 history 부족 시)
    """
    trend_signals: list[float] = []
    period_detail: dict[str, Any] = {}
    method_used = "embedded_series"

    # ── History 기반 (5건 이상 누적 시 사용) ──
    if len(history) >= 5:
        today = date.today().isoformat()
        current_score_val = None
        factor_scores_now: dict[str, float] = {}
        for name, f in factors.items():
            s = _num(f.get("score_normalized"))
            if s is not None:
                factor_scores_now[name] = s

        for period_label, lookback_days in [("1m", 20), ("3m", 60), ("6m", 125), ("12m", 252)]:
            target_date_str = None
            target_row = None
            for row in reversed(history):
                if not isinstance(row, dict):
                    continue
                d = str(row.get("date") or "")
                if not d:
                    continue
                from datetime import datetime as _dt
                try:
                    delta = (_dt.fromisoformat(today) - _dt.fromisoformat(d)).days
                except ValueError:
                    continue
                if delta >= lookback_days:
                    target_row = row
                    target_date_str = d
                    break
            if target_row is None:
                continue
            past_factor_scores: dict[str, float] = target_row.get("factor_scores") or {}
            if not past_factor_scores:
                continue
            period_changes: list[float] = []
            for name, curr_val in factor_scores_now.items():
                past_val = _num(past_factor_scores.get(name))
                if past_val is None:
                    continue
                change = curr_val - past_val
                weight = CURRENT_WEIGHTS.get(name, 0.10)
                period_changes.append(change * weight / 0.12)
            if period_changes:
                period_avg = sum(period_changes) / len(period_changes)
                trend_signals.append(period_avg)
                period_detail[period_label] = {
                    "compared_to": target_date_str,
                    "trend_signal": _r(period_avg, 4),
                }
        if trend_signals:
            method_used = "committed_history"

    # ── 내재 시계열 방향신호 (fallback 또는 보완) ──
    embedded_signals: list[float] = []

    # 주가추세 수익률 신호 (그 자체가 방향 신호)
    price_ev = (factors.get("price_trend") or {}).get("evidence") or {}
    for label, wt in [("1m", 0.20), ("3m", 0.30), ("6m", 0.30), ("12m", 0.20)]:
        sig = _num(price_ev.get(f"signal_{label}"))
        if sig is not None:
            embedded_signals.append(sig * (wt / 0.25))

    # 수급 방향 (현재 수급방향 자체가 추세 신호)
    flow_f = factors.get("flow") or {}
    if flow_f.get("available"):
        fs = _num(flow_f.get("score_normalized"))
        if fs is not None:
            embedded_signals.append(fs)

    # 신용스프레드 추세 (spread_history 내 최근 vs 이전 구간)
    spread_hist = list(
        ((equity_env.get("current_inputs") or {}).get("credit") or {}).get("spread_history") or []
    )
    if len(spread_hist) >= 30:
        recent_rows = [_num(r.get("spread_pct_point")) for r in spread_hist[-10:] if isinstance(r, dict)]
        earlier_rows = [_num(r.get("spread_pct_point")) for r in spread_hist[-30:-20] if isinstance(r, dict)]
        recent_vals = [v for v in recent_rows if v is not None]
        earlier_vals = [v for v in earlier_rows if v is not None]
        if recent_vals and earlier_vals:
            recent_avg = sum(recent_vals) / len(recent_vals)
            earlier_avg = sum(earlier_vals) / len(earlier_vals)
            if earlier_avg > 0:
                spread_change = (recent_avg - earlier_avg) / earlier_avg
                spread_trend = math.tanh(-spread_change / 0.1)
                embedded_signals.append(spread_trend)
                period_detail["credit_spread_trend_signal"] = _r(spread_trend, 4)

    # USD/KRW 1M 방향
    krw_s = _global_series(list(global_data.get("usd_krw_yahoo") or []))
    if len(krw_s) >= 21:
        ret = (krw_s[-1][1] / krw_s[-21][1] - 1.0)
        krw_trend = math.tanh(-ret / 0.03)
        embedded_signals.append(krw_trend)
        period_detail["usd_krw_1m_trend_signal"] = _r(krw_trend, 4)

    # 금리 방향
    rate_ev = (factors.get("rate_liquidity_credit") or {}).get("evidence") or {}
    rate_dir = _num(rate_ev.get("rate_direction_signal"))
    if rate_dir is not None:
        embedded_signals.append(rate_dir)

    # 이익 방향
    earnings_f = factors.get("earnings") or {}
    if earnings_f.get("available"):
        es = _num(earnings_f.get("score_normalized"))
        if es is not None:
            embedded_signals.append(es)

    if not trend_signals and not embedded_signals:
        return {
            "score": None, "score_valid": False,
            "direction": "데이터 부족",
            "reason": "추세 신호 미수집/연결대기",
        }

    all_signals = trend_signals if trend_signals else embedded_signals
    raw = sum(all_signals) / len(all_signals)
    trend_score = round(_clamp(raw * 100, -100, 100))

    return {
        "score": trend_score,
        "score_valid": True,
        "direction": _trend_direction(trend_score),
        "signal_count": len(all_signals),
        "method": method_used,
        "history_samples": len(history),
        "period_detail": period_detail,
        "embedded_signal_count": len(embedded_signals),
        "note": (
            "추세 점수는 현재 시계열 방향신호의 복합 평균입니다. "
            "committed history 5건 이상 누적 후 자동으로 이력 비교 방식으로 전환됩니다."
        ),
    }


# ── Forward Score ─────────────────────────────────────────────────────────────
def _forward_period(
    period_months: int,
    factors: dict[str, dict],
    rate_forecast: dict[str, Any],
    liquidity: dict[str, Any],
    strength: dict[str, Any],
) -> dict[str, Any]:
    """기간별 전망 점수 산출."""
    signals: list[float] = []
    evidence: dict[str, Any] = {}
    price_ev = (factors.get("price_trend") or {}).get("evidence") or {}

    if period_months == 1:
        # 1개월: 단기 모멘텀 중심
        for label, w in [("1m", 2.0), ("3m", 1.0)]:
            sig = _num(price_ev.get(f"signal_{label}"))
            if sig is not None:
                signals.append(sig * w)

        for fname in ("breadth", "flow"):
            f = factors.get(fname) or {}
            if f.get("available"):
                s = _num(f.get("score_normalized"))
                if s is not None:
                    signals.append(s)

        risk_f = factors.get("market_risk") or {}
        if risk_f.get("available"):
            rs = _num(risk_f.get("score_normalized"))
            if rs is not None:
                signals.append(rs)

        # KRW 단기 (strength forecast 3M 첫 번째 항목)
        str_path = list(strength.get("forecast_path") or [])
        if str_path:
            ss = _num(str_path[0].get("strength_score"))
            if ss is not None:
                signals.append(_clamp((ss - 50) / 50, -1, 1) * 0.5)

    elif period_months == 3:
        # 3개월: 정책 + 이익 + 유동성
        for label in ("3m", "6m"):
            sig = _num(price_ev.get(f"signal_{label}"))
            if sig is not None:
                signals.append(sig)

        for fname in ("earnings", "valuation"):
            f = factors.get(fname) or {}
            if f.get("available"):
                s = _num(f.get("score_normalized"))
                if s is not None:
                    signals.append(s)

        rlc_ev = (factors.get("rate_liquidity_credit") or {}).get("evidence") or {}
        rate_sig = _num(rlc_ev.get("rate_direction_signal"))
        if rate_sig is not None:
            signals.append(rate_sig)
            evidence["rate_3m_signal"] = _r(rate_sig, 4)

        liq_path = list(liquidity.get("forecast_path") or [])
        if liq_path:
            ls = _num(liq_path[0].get("liquidity_score"))
            if ls is not None:
                liq_sig = _clamp(ls / 0.5, -1, 1)
                signals.append(liq_sig)
                evidence["liq_3m_signal"] = _r(liq_sig, 4)

        str_path = list(strength.get("forecast_path") or [])
        if str_path:
            ss = _num(str_path[0].get("strength_score"))
            if ss is not None:
                signals.append(_clamp((ss - 50) / 50, -1, 1))

        credit_ev = (factors.get("rate_liquidity_credit") or {}).get("evidence") or {}
        cs = _num(credit_ev.get("credit_spread_signal"))
        if cs is not None:
            signals.append(cs)

    elif period_months == 6:
        # 6개월: 이익 + 밸류에이션 + 유동성 + KRW
        for label in ("6m", "12m"):
            sig = _num(price_ev.get(f"signal_{label}"))
            if sig is not None:
                signals.append(sig)

        earn_f = factors.get("earnings") or {}
        if earn_f.get("available"):
            es = _num(earn_f.get("score_normalized"))
            if es is not None:
                signals.append(es * 1.5)

        val_f = factors.get("valuation") or {}
        if val_f.get("available"):
            vs = _num(val_f.get("score_normalized"))
            if vs is not None:
                signals.append(vs)

        liq_path = list(liquidity.get("forecast_path") or [])
        liq_6m = next((r for r in liq_path if _num(r.get("months")) == 6), liq_path[-1] if len(liq_path) >= 2 else None)
        if liq_6m:
            ls = _num(liq_6m.get("liquidity_score"))
            if ls is not None:
                signals.append(_clamp(ls / 0.5, -1, 1))

        str_path = list(strength.get("forecast_path") or [])
        str_6m = next((r for r in str_path if _num(r.get("months")) == 6), str_path[-1] if len(str_path) >= 2 else None)
        if str_6m:
            ss = _num(str_6m.get("strength_score"))
            if ss is not None:
                signals.append(_clamp((ss - 50) / 50, -1, 1))

        rlc_f = factors.get("rate_liquidity_credit") or {}
        rlc_s = _num(rlc_f.get("score_normalized"))
        if rlc_s is not None:
            signals.append(rlc_s)

    else:  # 12M
        # 12개월: 밸류에이션 + 이익 + 금리 사이클
        val_f = factors.get("valuation") or {}
        if val_f.get("available"):
            vs = _num(val_f.get("score_normalized"))
            if vs is not None:
                signals.append(vs * 2.0)

        earn_f = factors.get("earnings") or {}
        if earn_f.get("available"):
            es = _num(earn_f.get("score_normalized"))
            if es is not None:
                signals.append(es * 1.5)

        sig_12m = _num(price_ev.get("signal_12m"))
        if sig_12m is not None:
            signals.append(sig_12m)

        rlc_f = factors.get("rate_liquidity_credit") or {}
        rlc_s = _num(rlc_f.get("score_normalized"))
        if rlc_s is not None:
            signals.append(rlc_s)

        ext_f = factors.get("external") or {}
        if ext_f.get("available"):
            ext_s = _num(ext_f.get("score_normalized"))
            if ext_s is not None:
                signals.append(ext_s)

        str_path = list(strength.get("forecast_path") or [])
        if str_path:
            ss = _num(str_path[-1].get("strength_score"))
            if ss is not None:
                signals.append(_clamp((ss - 50) / 50, -1, 1))

    if not signals:
        return {
            "available": False, "score": None,
            "direction": "데이터 부족",
            "data_status": "DATA_PENDING",
        }

    avg = sum(signals) / len(signals)
    score = round(_clamp(50.0 + avg * 50.0, 0, 100))
    return {
        "available": True,
        "score": score,
        "direction": _forward_direction(score),
        "bias": _forward_bias(score),
        "signal_count": len(signals),
        "evidence": evidence,
    }


def _build_forward_score(
    factors: dict[str, dict],
    rate_forecast: dict[str, Any],
    liquidity: dict[str, Any],
    strength: dict[str, Any],
) -> dict[str, Any]:
    periods: dict[str, Any] = {}
    for months in (1, 3, 6, 12):
        key = f"{months}m"
        periods[key] = _forward_period(months, factors, rate_forecast, liquidity, strength)

    valid_scores = [p["score"] for p in periods.values() if p.get("available") and p.get("score") is not None]
    overall_score: int | None = None
    if valid_scores:
        overall_score = round(sum(valid_scores) / len(valid_scores))

    # 신뢰도 = 유효 팩터 비율 × 유효 기간 비율
    active_factors = sum(1 for f in factors.values() if f.get("available"))
    total_factors = len(factors)
    valid_periods = len(valid_scores)
    confidence = round((active_factors / max(total_factors, 1)) * (valid_periods / 4) * 100)

    return {
        "overall": {
            "score": overall_score,
            "score_valid": overall_score is not None,
            "regime": _current_regime(overall_score),
            "direction": _forward_direction(overall_score),
            "confidence_pct": confidence,
        },
        "periods": periods,
        "coverage_note": f"유효 팩터 {active_factors}/{total_factors} · 유효 기간 {valid_periods}/4",
    }


# ── 히스토리 관리 ─────────────────────────────────────────────────────────────
def _update_history(path: Path, current_factors: dict, current_score: dict) -> list[dict]:
    existing = _safe_read(path, [])
    rows: list[dict] = list(existing) if isinstance(existing, list) else []
    today = date.today().isoformat()
    rows = [r for r in rows if isinstance(r, dict) and str(r.get("date") or "") != today]

    factor_scores = {
        name: _num(f.get("score_normalized"))
        for name, f in current_factors.items()
        if f.get("available") and _num(f.get("score_normalized")) is not None
    }

    rows.append({
        "date": today,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "current_score": current_score.get("score"),
        "factor_scores": factor_scores,
    })
    rows = sorted(rows, key=lambda r: str(r.get("date") or ""))[-400:]
    write_json(path, rows)
    return rows


# ── Data Quality Gate ─────────────────────────────────────────────────────────
def _dq_check(
    equity_env: dict,
    raw_equity: dict,
    ecos: dict,
    global_data: dict,
) -> dict[str, Any]:
    issues: list[str] = []
    warnings: list[str] = []

    # 기존 equity_env 정합성
    if not (equity_env.get("score_valid") or equity_env.get("score") is not None):
        warnings.append("equity_env 점수 미산출 상태")

    # ECOS 데이터 존재 여부
    for key in ("kr_base_rate", "kr_gov_3y", "kr_gov_10y"):
        if not list(ecos.get(key) or []):
            warnings.append(f"ECOS {key} 시계열 없음")

    # KOSPI200 종가 시계열
    k_rows = list(((raw_equity.get("valuation_history") or {}).get("kospi200") or {}).get("rows") or [])
    if len(k_rows) < 25:
        issues.append(f"KOSPI200 종가 시계열 부족 ({len(k_rows)}건 < 25건 필요)")

    # Credit spread history
    cred_hist = list(
        ((equity_env.get("current_inputs") or {}).get("credit") or {}).get("spread_history") or []
    )
    if len(cred_hist) < 20:
        warnings.append(f"신용스프레드 이력 부족 ({len(cred_hist)}건)")

    # USD/KRW Yahoo 시계열
    krw_s = list(global_data.get("usd_krw_yahoo") or [])
    if len(krw_s) < 5:
        warnings.append(f"USD/KRW Yahoo 시계열 부족 ({len(krw_s)}건)")

    return {
        "passed": not issues,
        "issues": issues,
        "warnings": warnings,
    }


# ── 메인 진입점 ──────────────────────────────────────────────────────────────
def build_and_write(output_dir: Path) -> dict[str, Any]:
    """
    기존 output 파일을 읽어 종합환경 점수를 산출하고
    output/korea_comprehensive_environment.json 에 기록합니다.
    새로운 외부 API 호출은 없습니다.
    """
    # ── 기존 파일 읽기 (모두 재사용, 신규 수집 없음) ──
    equity_env = _safe_read(output_dir / "korea_equity_environment.json", {})
    raw_equity = _safe_read(output_dir / "raw_korea_equity_environment.json", {})
    ecos = _safe_read(output_dir / "raw_ecos.json", {})
    global_data = _safe_read(output_dir / "raw_global_market.json", {})
    rate_forecast = _safe_read(output_dir / "korea_rate_forecast_v2.json", {})
    strength = _safe_read(output_dir / "korea_krw_strength_forecast.json", {})
    liquidity = _safe_read(output_dir / "korea_krw_liquidity_forecast.json", {})
    us_policy = _safe_read(output_dir / "us_input.json", {})
    krx_data  = _safe_read(output_dir / "raw_krx.json", {})

    # ── Data Quality Gate ──
    dq = _dq_check(equity_env, raw_equity, ecos, global_data)

    # ── PiT 커버리지 검사 (live 모드에서는 필터링 없음, 백테스트에서만 활용) ──
    pit_report = pit_coverage_check(
        {k: v for k, v in ecos.items() if isinstance(v, list)},
        as_of_date=None,   # live 실행
    )

    # ── 팩터 계산 (각 팩터 독립, 기존 equity_env 최대 재사용) ──
    # B. Breadth: 이원화 (KOSPI 수출·대형 vs KOSDAQ 내수·중소형)
    # C. Earnings: 이원화 (KOSPI200 vs KOSDAQ150)
    # G. FX: 비선형 레짐 (NORMAL/CAUTION/CRISIS) 적용
    factors: dict[str, dict] = {
        "price_trend":           _factor_price_trend(raw_equity),
        "breadth":               _factor_breadth_dualtrack(equity_env, raw_equity),
        "earnings":              _factor_earnings_dualtrack(equity_env),
        "valuation":             _reuse_equity_component(equity_env, "valuation"),
        "flow":                  _reuse_equity_component(equity_env, "flow"),
        "rate_liquidity_credit": _factor_rate_liquidity_credit(ecos, rate_forecast, liquidity, equity_env),
        "fx":                    _factor_fx(global_data, strength),
        "market_risk":           _factor_market_risk(global_data),
        "external":              _factor_external(global_data, us_policy),
    }

    # 파생수급 오버레이 (E. 수급 보조, CURRENT_WEIGHTS 외 별도 출력)
    derivative_overlay = _overlay_derivative_flow(krx_data)

    # ── Current Score ──
    current_score = _build_current_score(factors)

    # ── 히스토리 업데이트 & Trend Score ──
    hist_path = output_dir / "korea_comprehensive_environment_history.json"
    history = _safe_read(hist_path, [])
    history = list(history) if isinstance(history, list) else []
    trend_score = _build_trend_score(factors, global_data, equity_env, history)
    history = _update_history(hist_path, factors, current_score)

    # ── Forward Score ──
    forward_score = _build_forward_score(factors, rate_forecast, liquidity, strength)

    # ── 요약 ──
    cur_s = current_score.get("score")
    fwd_s = (forward_score.get("overall") or {}).get("score")
    trn_s = trend_score.get("score")
    summary = {
        "current_score": cur_s,
        "current_regime": _current_regime(cur_s),
        "trend_score": trn_s,
        "trend_direction": _trend_direction(trn_s),
        "forward_overall": fwd_s,
        "forward_regime": _current_regime(fwd_s),
        "forward_confidence_pct": (forward_score.get("overall") or {}).get("confidence_pct"),
        "positive_factors": current_score.get("positive_factors", []),
        "negative_factors": current_score.get("negative_factors", []),
        "data_quality": dq,
    }

    # ── 최종 출력 ──
    gen_at = datetime.now(timezone.utc).isoformat()
    periods = forward_score.get("periods") or {}
    def _fp(key: str, field: str):
        p = periods.get(key) or {}
        return p.get(field) if p.get("available") else None

    # GAS 대시보드용 플랫 스키마 (_dashboard) -중첩 없이 1-depth 키만 사용
    dashboard_flat: dict[str, Any] = {
        "generated_at_utc":       gen_at,
        "current_score":          cur_s,
        "current_regime":         _current_regime(cur_s),
        "trend_score":            trn_s,
        "trend_direction":        _trend_direction(trn_s),
        "trend_method":           trend_score.get("method_used", "embedded_series"),
        "forward_overall":        fwd_s,
        "forward_confidence_pct": (forward_score.get("overall") or {}).get("confidence_pct"),
        "forward_1m_score":       _fp("1m", "score"),
        "forward_1m_direction":   _fp("1m", "direction"),
        "forward_3m_score":       _fp("3m", "score"),
        "forward_3m_direction":   _fp("3m", "direction"),
        "forward_6m_score":       _fp("6m", "score"),
        "forward_6m_direction":   _fp("6m", "direction"),
        "forward_12m_score":      _fp("12m", "score"),
        "forward_12m_direction":  _fp("12m", "direction"),
        "coverage_pct":           current_score.get("coverage_pct"),
        "factors_available":      current_score.get("positive_factors", []) + [
            n for n, f in factors.items()
            if f.get("available") and n not in current_score.get("positive_factors", [])
               and n not in current_score.get("negative_factors", [])
        ],
        "factors_positive":       current_score.get("positive_factors", []),
        "factors_negative":       current_score.get("negative_factors", []),
        "factors_pending":        [n for n, f in factors.items() if not f.get("available")],
        "dq_passed":              dq.get("passed", True),
        "dq_issues_count":        len(dq.get("issues", [])),
        "dq_warnings_count":      len(dq.get("warnings", [])),
    }

    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "model_version": MODEL_VERSION,
        "generated_at_utc": gen_at,
        "scope": (
            "한국증시 종합환경 엔진. 기존 금리·환율·유동성·원화강도·주식환경 엔진 출력을 재사용. "
            "신규 외부 API 호출 없음. 기존 스키마 변경 없음."
        ),
        "summary": summary,
        "current_score": current_score,
        "trend_score": trend_score,
        "forward_score": forward_score,
        "derivative_overlay": derivative_overlay,
        "pit_coverage": pit_report,
        "data_quality_gate": dq,
        "_dashboard": dashboard_flat,
        "data_sources": {
            "new_api_calls": 0,
            "reused_output_files": [
                "korea_equity_environment.json",
                "raw_korea_equity_environment.json",
                "raw_ecos.json",
                "raw_global_market.json",
                "korea_rate_forecast_v2.json",
                "korea_krw_strength_forecast.json",
                "korea_krw_liquidity_forecast.json",
                "us_input.json",
            ],
            "history_file": "korea_comprehensive_environment_history.json",
            "output_file": "korea_comprehensive_environment.json",
        },
        "pending_data_note": (
            "DATA_PENDING 팩터는 데이터 미수집/연결대기 상태이며 0점으로 대체하지 않습니다. "
            "활성 가중치 합이 50% 이상인 경우에만 Current Score를 산출합니다."
        ),
    }

    write_json(output_dir / "korea_comprehensive_environment.json", result)
    return result
