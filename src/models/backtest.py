from __future__ import annotations

"""백테스트 및 거래 시뮬레이션 모듈 (개선 4번).

신호 기반 실전 수익률, MDD, 승률을 검증하는 거래 시뮬레이션.
외부 API 호출 없이 기존 수집 데이터만 활용.
"""

import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.core.io import read_json


MODEL_VERSION = "backtest-v1.0"


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


def _safe_read(path: Path, default: Any) -> Any:
    try:
        return read_json(path) if path.exists() else default
    except Exception:
        return default


def _extract_price_series(output_dir: Path) -> list[dict[str, Any]]:
    """기존 수집 데이터에서 KOSPI 종가 시계열 추출."""
    equity_raw = _safe_read(output_dir / "raw_korea_equity_environment.json", {})
    hist = (equity_raw.get("valuation_history") or {}).get("kospi200") or {}
    rows = list(hist.get("rows") or [])
    series = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        d = row.get("date")
        v = _num(row.get("close"))
        if d and v is not None and v > 0:
            series.append({"date": str(d), "close": v})
    return sorted(series, key=lambda r: r["date"])


def _extract_vintage_signals(output_dir: Path) -> list[dict[str, Any]]:
    """빈티지 파일에서 과거 신호(score + bias) 추출."""
    vintage_dir = output_dir / "vintages"
    if not vintage_dir.exists():
        return []
    signals = []
    for file in sorted(vintage_dir.glob("*.json")):
        try:
            data = read_json(file)
            date_str = file.stem  # 파일명이 날짜
            # 기존 vintages는 rate/fx 포함; equity environment score는 별도
            equity_score = None
            v3 = data.get("unified_outlook_v3") or {}
            equity_env = v3.get("equity_environment") or {}
            if not equity_env:
                equity_env = _safe_read(output_dir / "korea_equity_environment.json", {})
            score_val = equity_env.get("score") or equity_env.get("final_score")
            if score_val is not None:
                equity_score = _num(score_val)
            # FX 방향도 추출 (환율 상승=원화 약세 여부)
            fx_data = data.get("fx_forecast") or {}
            fx_path = fx_data.get("forecast_path") or []
            fx_direction = None
            if fx_path:
                first = fx_path[0]
                fx_direction = first.get("direction")
            signals.append({
                "date": date_str,
                "equity_score": equity_score,
                "fx_direction": fx_direction,
            })
        except Exception:
            continue
    return signals


def simulate_trades(
    price_series: list[dict[str, Any]],
    signals: list[dict[str, Any]],
    buy_threshold: float = 58.0,
    sell_threshold: float = 43.0,
    holding_days: int = 20,
) -> dict[str, Any]:
    """단순 매수/매도 시뮬레이션.

    규칙:
    - 환경점수 >= buy_threshold: 다음 거래일 매수
    - 환경점수 <  sell_threshold OR 보유 holding_days 초과: 매도
    """
    if len(price_series) < 5 or not signals:
        return {"available": False, "reason": "시계열 데이터 부족"}

    price_map = {row["date"]: row["close"] for row in price_series}
    signal_map = {s["date"]: s.get("equity_score") for s in signals if s.get("equity_score") is not None}
    all_dates = sorted(price_map.keys())

    trades: list[dict[str, Any]] = []
    position = None  # {"entry_date": ..., "entry_price": ..., "days": 0}
    equity_curve: list[float] = [100.0]
    portfolio = 100.0

    for i, d in enumerate(all_dates):
        price = price_map.get(d)
        if price is None:
            continue
        score = signal_map.get(d)

        if position is None:
            # 매수 조건
            if score is not None and score >= buy_threshold:
                position = {"entry_date": d, "entry_price": price, "days": 0}
        else:
            position["days"] += 1
            # 매도 조건
            if score is not None and score < sell_threshold or position["days"] >= holding_days:
                ret_pct = (price / position["entry_price"] - 1.0) * 100.0
                portfolio *= (1.0 + ret_pct / 100.0)
                trades.append({
                    "entry_date": position["entry_date"],
                    "exit_date": d,
                    "entry_price": _round(position["entry_price"]),
                    "exit_price": _round(price),
                    "return_pct": _round(ret_pct),
                    "holding_days": position["days"],
                    "profitable": ret_pct > 0,
                })
                position = None
            equity_curve.append(_round(portfolio))

    if not trades:
        return {"available": False, "reason": "시뮬레이션 기간 내 매매 신호 없음"}

    returns = [t["return_pct"] for t in trades if t["return_pct"] is not None]
    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r <= 0]
    win_rate = len(wins) / len(returns) * 100.0 if returns else 0.0
    avg_return = sum(returns) / len(returns) if returns else 0.0
    total_return = portfolio - 100.0

    # MDD 계산
    peak = equity_curve[0]
    mdd = 0.0
    for val in equity_curve:
        if val > peak:
            peak = val
        drawdown = (val - peak) / peak * 100.0
        if drawdown < mdd:
            mdd = drawdown

    # 기대값 (승률 * 평균수익 - 패율 * 평균손실)
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = abs(sum(losses) / len(losses)) if losses else 0.0
    expected_value = (win_rate / 100.0) * avg_win - ((100.0 - win_rate) / 100.0) * avg_loss

    return {
        "available": True,
        "total_trades": len(trades),
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "win_rate_pct": _round(win_rate),
        "avg_return_pct": _round(avg_return),
        "avg_win_pct": _round(avg_win),
        "avg_loss_pct": _round(-avg_loss),
        "expected_value_pct": _round(expected_value),
        "total_return_pct": _round(total_return),
        "max_drawdown_pct": _round(mdd),
        "buy_threshold": buy_threshold,
        "sell_threshold": sell_threshold,
        "holding_days_max": holding_days,
        "trades": trades[-20:],
    }


def run_backtest(output_dir: Path) -> dict[str, Any]:
    """백테스트 파이프라인 실행."""
    price_series = _extract_price_series(output_dir)
    signals = _extract_vintage_signals(output_dir)

    result_conservative = simulate_trades(price_series, signals, buy_threshold=70.0, sell_threshold=43.0, holding_days=15)
    result_standard = simulate_trades(price_series, signals, buy_threshold=58.0, sell_threshold=43.0, holding_days=20)
    result_aggressive = simulate_trades(price_series, signals, buy_threshold=50.0, sell_threshold=35.0, holding_days=30)

    data_summary = {
        "price_samples": len(price_series),
        "signal_samples": len(signals),
        "price_range": {
            "start": price_series[0]["date"] if price_series else None,
            "end": price_series[-1]["date"] if price_series else None,
        },
    }

    return {
        "model_version": MODEL_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "data_summary": data_summary,
        "scenarios": {
            "conservative": {
                "description": "보수적: 환경점수 70+ 매수, 43 미만 매도, 최대 15일 보유",
                **result_conservative,
            },
            "standard": {
                "description": "표준: 환경점수 58+ 매수, 43 미만 매도, 최대 20일 보유",
                **result_standard,
            },
            "aggressive": {
                "description": "적극적: 환경점수 50+ 매수, 35 미만 매도, 최대 30일 보유",
                **result_aggressive,
            },
        },
        "disclaimer": "과거 시뮬레이션 결과이며 미래 수익을 보장하지 않습니다. 거래비용·슬리피지 미반영.",
    }
