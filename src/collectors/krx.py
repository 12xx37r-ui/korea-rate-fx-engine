from __future__ import annotations

"""KRX 데이터 수집기.

Task 2 추가 항목:
  - 외국인 KOSPI 현물 5일/20일 누적 순매수 대금
  - 외국인 KOSPI200 선물 일별 순매수 계약 수 및 누적 추세
  - 시장 베이시스 (선물가격 - 현물가격) 동향
"""

import math
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from src.core.http import get_json
from src.core.io import read_json, write_json
from src.core.result import SourceResult


def _num(value: Any) -> float | None:
    if value is None:
        return None
    raw = str(value).strip().replace(",", "")
    if not raw or raw in {"-", "--", "N/A", "null"}:
        return None
    try:
        out = float(raw)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _round(value: float | None, digits: int = 2) -> float | None:
    return round(float(value), digits) if value is not None and math.isfinite(float(value)) else None


def _safe_read(path: Path, default: Any) -> Any:
    try:
        return read_json(path) if path.exists() else default
    except Exception:
        return default


def _frame_empty(frame: Any) -> bool:
    return frame is None or bool(getattr(frame, "empty", True))


def _find_index_label(frame: Any, candidates: tuple[str, ...]) -> Any | None:
    if _frame_empty(frame):
        return None
    for idx in frame.index:
        label = str(idx).strip().replace(" ", "")
        for c in candidates:
            if c.replace(" ", "") == label or c.replace(" ", "") in label:
                return idx
    return None


def _collect_foreign_spot_flow(stock: Any, window_days: int = 30) -> dict[str, Any]:
    """외국인 KOSPI 현물 5d/20d 누적 순매수 대금 수집."""
    end = date.today()
    start = end - timedelta(days=window_days)
    diagnostics: list[str] = []
    try:
        frame = stock.get_market_trading_value_by_investor(
            start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), "KOSPI"
        )
    except Exception as exc:
        return {"available": False, "diagnostics": [f"spot_flow:{type(exc).__name__}:{str(exc)[:160]}"]}

    if _frame_empty(frame):
        return {"available": False, "diagnostics": ["spot_flow:empty"]}

    foreign_idx = _find_index_label(frame, ("외국인", "외국인합계"))
    if foreign_idx is None:
        return {"available": False, "diagnostics": ["spot_flow:외국인_label_missing"]}

    net = _num(frame.loc[foreign_idx].get("순매수"))
    buy = _num(frame.loc[foreign_idx].get("매수"))
    sell = _num(frame.loc[foreign_idx].get("매도"))

    # 5d/20d 누적 계산을 위해 일별 데이터 수집
    daily_rows: list[dict[str, Any]] = []
    for window in (5, 20):
        w_start = end - timedelta(days=window + 5)
        try:
            w_frame = stock.get_market_trading_value_by_investor(
                w_start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), "KOSPI"
            )
            if not _frame_empty(w_frame):
                f_idx = _find_index_label(w_frame, ("외국인", "외국인합계"))
                if f_idx is not None:
                    w_net = _num(w_frame.loc[f_idx].get("순매수"))
                    daily_rows.append({"window_days": window, "cumulative_net": w_net})
        except Exception as exc:
            diagnostics.append(f"window_{window}d:{type(exc).__name__}")

    cum_5d = next((r["cumulative_net"] for r in daily_rows if r["window_days"] == 5), None)
    cum_20d = next((r["cumulative_net"] for r in daily_rows if r["window_days"] == 20), None)

    return {
        "available": True,
        "period_start": start.strftime("%Y%m%d"),
        "period_end": end.strftime("%Y%m%d"),
        "net_value": _round(net, 0),
        "buy_value": _round(buy, 0),
        "sell_value": _round(sell, 0),
        "cum_5d_net": _round(cum_5d, 0),
        "cum_20d_net": _round(cum_20d, 0),
        "momentum_signal": (
            "강한_순매수" if cum_5d is not None and cum_5d > 0 and cum_20d is not None and cum_20d > 0
            else "강한_순매도" if cum_5d is not None and cum_5d < 0 and cum_20d is not None and cum_20d < 0
            else "혼조"
        ),
        "diagnostics": diagnostics,
        "source": "KRX 투자자별 거래대금 (pykrx)",
    }


def _collect_futures_flow(stock: Any) -> dict[str, Any]:
    """외국인 KOSPI200 선물 일별 순매수 계약 수 수집 (pykrx futures API)."""
    end = date.today()
    start = end - timedelta(days=30)
    diagnostics: list[str] = []

    # pykrx futures 모듈 시도
    try:
        from pykrx import futures as pykrx_futures  # type: ignore
        frame = pykrx_futures.get_market_trading_volume_by_investor(
            start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), "KQ200"
        )
        if not _frame_empty(frame):
            foreign_idx = _find_index_label(frame, ("외국인", "외국인합계"))
            if foreign_idx is not None:
                net_contracts = _num(frame.loc[foreign_idx].get("순매수") or frame.loc[foreign_idx].get("순매수수량"))
                buy_contracts = _num(frame.loc[foreign_idx].get("매수") or frame.loc[foreign_idx].get("매수수량"))
                return {
                    "available": True,
                    "period_start": start.strftime("%Y%m%d"),
                    "period_end": end.strftime("%Y%m%d"),
                    "net_contracts": _round(net_contracts, 0),
                    "buy_contracts": _round(buy_contracts, 0),
                    "trend": "매수우위" if net_contracts is not None and net_contracts > 0 else "매도우위" if net_contracts is not None and net_contracts < 0 else "중립",
                    "source": "pykrx futures KOSPI200 선물",
                    "diagnostics": diagnostics,
                }
    except Exception as exc:
        diagnostics.append(f"pykrx_futures:{type(exc).__name__}:{str(exc)[:160]}")

    # Fallback: KOSPI 현물 외국인 순매수로 선물 추세 대용
    try:
        frame = stock.get_market_trading_value_by_investor(
            start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), "KOSPI"
        )
        if not _frame_empty(frame):
            foreign_idx = _find_index_label(frame, ("외국인", "외국인합계"))
            if foreign_idx is not None:
                net = _num(frame.loc[foreign_idx].get("순매수"))
                return {
                    "available": True,
                    "period_start": start.strftime("%Y%m%d"),
                    "period_end": end.strftime("%Y%m%d"),
                    "net_contracts": None,
                    "net_value_proxy": _round(net, 0),
                    "trend": "매수우위" if net is not None and net > 0 else "매도우위" if net is not None and net < 0 else "중립",
                    "source": "pykrx KOSPI 외국인 현물 (선물 API 불가 시 대용)",
                    "note": "pykrx futures 모듈 미지원 환경: KOSPI 현물 순매수 대용",
                    "diagnostics": diagnostics,
                }
    except Exception as exc:
        diagnostics.append(f"fallback_spot:{type(exc).__name__}:{str(exc)[:160]}")

    return {"available": False, "diagnostics": diagnostics}


def _collect_basis(stock: Any) -> dict[str, Any]:
    """시장 베이시스(KOSPI200 선물가격 - KOSPI200 현물가격) 산출."""
    today = date.today().strftime("%Y%m%d")
    diagnostics: list[str] = []

    try:
        # KOSPI200 현물 지수
        spot_frame = stock.get_index_ohlcv_by_date(today, today, "1028")
        spot_close = None
        if not _frame_empty(spot_frame):
            for idx, row in spot_frame.iterrows():
                spot_close = _num(row.get("종가") or row.get("Close"))
    except Exception as exc:
        diagnostics.append(f"kospi200_spot:{type(exc).__name__}:{str(exc)[:120]}")
        spot_close = None

    futures_close = None
    try:
        from pykrx import futures as pykrx_futures  # type: ignore
        fut_frame = pykrx_futures.get_market_ohlcv_by_date(today, today, "101W9000")
        if not _frame_empty(fut_frame):
            for idx, row in fut_frame.iterrows():
                futures_close = _num(row.get("종가") or row.get("Close"))
    except Exception as exc:
        diagnostics.append(f"k200_futures:{type(exc).__name__}:{str(exc)[:120]}")

    if spot_close is not None and futures_close is not None:
        basis = futures_close - spot_close
        basis_pct = basis / spot_close * 100.0
        return {
            "available": True,
            "date": today,
            "kospi200_spot": _round(spot_close),
            "kospi200_futures": _round(futures_close),
            "basis": _round(basis),
            "basis_pct": _round(basis_pct, 4),
            "signal": "콘탱고(매수세 우위)" if basis > 0 else "백워데이션(매도세 우위)",
            "source": "pykrx",
            "diagnostics": diagnostics,
        }

    # 현물만 있어도 부분 출력
    if spot_close is not None:
        return {
            "available": False,
            "kospi200_spot": _round(spot_close),
            "kospi200_futures": None,
            "reason": "선물가격 조회 실패",
            "diagnostics": diagnostics,
        }

    return {"available": False, "reason": "현물/선물 모두 조회 실패", "diagnostics": diagnostics}


def _collect_pykrx_supply_demand(output_dir: Path) -> dict[str, Any]:
    """pykrx 기반 외국인 수급 데이터 통합 수집."""
    prev_path = output_dir / "raw_krx_supply_demand.json"
    previous = _safe_read(prev_path, {})

    try:
        from pykrx import stock  # type: ignore
    except Exception as exc:
        return {
            "available": False,
            "error": f"pykrx_import:{type(exc).__name__}:{exc}",
            "foreign_spot_flow": previous.get("foreign_spot_flow") or {},
            "futures_flow": previous.get("futures_flow") or {},
            "basis": previous.get("basis") or {},
        }

    foreign_spot_flow = _collect_foreign_spot_flow(stock)
    futures_flow = _collect_futures_flow(stock)
    basis = _collect_basis(stock)

    result = {
        "schema_version": "1.0.0",
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "available": foreign_spot_flow.get("available") or futures_flow.get("available"),
        "foreign_spot_flow": foreign_spot_flow,
        "futures_flow": futures_flow,
        "basis": basis,
        "flow_momentum_summary": {
            "cum_5d_net": foreign_spot_flow.get("cum_5d_net"),
            "cum_20d_net": foreign_spot_flow.get("cum_20d_net"),
            "momentum_signal": foreign_spot_flow.get("momentum_signal"),
            "futures_trend": futures_flow.get("trend"),
            "basis_signal": basis.get("signal"),
        },
    }
    write_json(prev_path, result)
    return result


def collect(output_dir: Path, timeout: int, retries: int) -> SourceResult:
    api_key = os.getenv("KRX_API_KEY", "").strip()
    config = read_json("config/krx_apis.json")
    configured = [(n, v) for n, v in config.items()
                  if not n.startswith("_") and isinstance(v, dict) and v.get("enabled")]

    payloads, warnings = {}, []

    # 기존 KRX API 공식 호출 (API Key 있을 때만)
    if api_key and configured:
        for name, item in configured:
            endpoint = str(item.get("endpoint", "")).strip()
            api_id = str(item.get("api_id", "")).strip()
            if not endpoint or not api_id:
                warnings.append(f"{name}: endpoint 또는 api_id 누락")
                continue
            try:
                payloads[name] = get_json(
                    endpoint,
                    headers={"AUTH_KEY": api_key},
                    params={"basDd": date.today().strftime("%Y%m%d")},
                    timeout=timeout,
                    retries=retries,
                )
            except Exception as exc:
                warnings.append(f"{name}: {exc}")
    elif not api_key:
        warnings.append("KRX_API_KEY 없음: 공식 API 스킵, pykrx 수급 데이터만 수집")

    # Task 2: pykrx 기반 외국인 수급 + 베이시스 수집 (API Key 불필요)
    supply_demand = _collect_pykrx_supply_demand(output_dir)
    payloads["supply_demand"] = supply_demand

    flow_summary = supply_demand.get("flow_momentum_summary") or {}
    print(
        f"[KRX] foreign_spot cum5d={flow_summary.get('cum_5d_net')} "
        f"cum20d={flow_summary.get('cum_20d_net')} "
        f"momentum={flow_summary.get('momentum_signal')} "
        f"futures={flow_summary.get('futures_trend')} "
        f"basis={flow_summary.get('basis_signal')}",
        flush=True,
    )

    path = output_dir / "raw_krx.json"
    write_json(path, payloads)

    supply_ok = bool(supply_demand.get("available"))
    if payloads and (supply_ok or len(payloads) > 1):
        status = "ok" if not warnings else "degraded"
    elif supply_ok:
        status = "degraded"
    else:
        status = "error"

    return SourceResult(
        source="krx",
        status=status,
        message="KRX 공식API + pykrx 외국인수급/선물/베이시스 수집",
        rows=len(payloads),
        payload_path=str(path),
        warnings=warnings,
        metadata={
            "supply_demand_available": supply_ok,
            "flow_momentum_summary": flow_summary,
        },
    )
