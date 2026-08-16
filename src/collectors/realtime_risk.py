from __future__ import annotations

"""실시간 위험 선행 지표 수집기 (개선 1번 - 실시간/단기 선행 지표 결합).

수집 항목:
  - VKOSPI (변동성 지수): pykrx
  - SOX (필라델피아 반도체 지수), NVDA, SOXX ETF: Yahoo Finance
  - USD/KRW 단기 변동성(ATR): 기존 global_market 데이터에서 산출
  - CDS 프록시: 한국-미국 국채 스프레드 (기존 ECOS 데이터 활용)
  - 외국인 선물 순매수: pykrx (가능 시)
"""

import math
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.core.io import read_json, write_json


COLLECTOR_VERSION = "realtime-risk-collector-v1.2-vkospi-source-guard"
YAHOO_SYMBOLS = {
    "sox": "^SOX",
    "nvda": "NVDA",
    "soxx": "SOXX",
    "sp500": "^GSPC",
}
YAHOO_TIMEOUT = (3, 9)
VKOSPI_TICKER = "1024"


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


def _sanitize_verified_vkospi_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep only VKOSPI rows that explicitly came from a verified source.

    Older engine builds used a hard-coded pykrx index ticker as VKOSPI.  That
    mapping is not reliable enough to drive a circuit breaker, so legacy rows
    from that path are deliberately rejected rather than rescaled heuristically.
    """
    out: list[dict[str, Any]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        value = _num(row.get("value"))
        if value is None or not (5.0 <= value <= 150.0):
            continue
        if row.get("source_validation") != "verified":
            continue
        out.append(dict(row))
    return out


def _safe_read(path: Path, default: Any) -> Any:
    try:
        return read_json(path) if path.exists() else default
    except Exception:
        return default


def _yahoo_ohlcv(symbol: str, days: int = 90) -> list[dict[str, Any]]:
    import requests
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    with requests.Session() as session:
        session.headers.update({"User-Agent": "korea-rate-fx-engine/5.0"})
        response = session.get(
            url,
            params={"range": f"{days}d", "interval": "1d", "events": "history"},
            timeout=YAHOO_TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
    result = (((payload or {}).get("chart") or {}).get("result") or [])
    if not result:
        return []
    node = result[0] or {}
    timestamps = node.get("timestamp") or []
    quotes = ((node.get("indicators") or {}).get("quote") or [{}])[0] or {}
    closes = quotes.get("close") or []
    rows: list[dict[str, Any]] = []
    for ts, close in zip(timestamps, closes):
        if close is None:
            continue
        try:
            value = float(close)
            date_str = datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y%m%d")
        except (TypeError, ValueError, OSError):
            continue
        rows.append({"date": date_str, "value": value, "source": "Yahoo Finance", "symbol": symbol})
    return rows


def _collect_global_semiconductor(previous: dict[str, Any]) -> dict[str, Any]:
    results: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for key, symbol in YAHOO_SYMBOLS.items():
        try:
            rows = _yahoo_ohlcv(symbol, days=90)
            if rows:
                results[key] = rows
            else:
                results[key] = list(previous.get(key) or [])
                errors[key] = "empty_response"
        except Exception as exc:
            results[key] = list(previous.get(key) or [])
            errors[key] = f"{type(exc).__name__}: {str(exc)[:120]}"
    return {"data": results, "errors": errors}


def _latest_value(rows: list[dict[str, Any]]) -> float | None:
    if not rows:
        return None
    return _num(rows[-1].get("value"))


def _pct_change(rows: list[dict[str, Any]], days: int = 1) -> float | None:
    if len(rows) < days + 1:
        return None
    current = _num(rows[-1].get("value"))
    prev = _num(rows[-(days + 1)].get("value"))
    if current is None or prev is None or prev == 0:
        return None
    return (current / prev - 1.0) * 100.0


def _rolling_std(rows: list[dict[str, Any]], window: int = 10) -> float | None:
    values = [_num(row.get("value")) for row in rows[-window:] if isinstance(row, dict)]
    values = [v for v in values if v is not None]
    if len(values) < 3:
        return None
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / len(values)
    return math.sqrt(variance)


def _collect_vkospi(previous_vkospi: list[dict[str, Any]]) -> dict[str, Any]:
    """Fail closed until a verifiable VKOSPI source is configured.

    A volatility circuit breaker must never be driven by an unverified index
    ticker.  Preserve only previously verified VKOSPI observations; otherwise
    expose the input as unavailable so the regime model falls back to its other
    risk inputs instead of manufacturing a crisis signal.
    """
    verified_previous = _sanitize_verified_vkospi_rows(previous_vkospi)
    return {
        "available": bool(verified_previous),
        "rows": verified_previous,
        "stale": bool(verified_previous),
        "source": "verified VKOSPI last-good" if verified_previous else None,
        "source_validation": "verified" if verified_previous else "unavailable",
        "reason": (
            "live VKOSPI source disabled: legacy pykrx ticker mapping is not verified for circuit-breaker use"
        ),
    }

def _collect_foreign_futures(previous_futures: dict[str, Any]) -> dict[str, Any]:
    try:
        from pykrx import stock  # type: ignore
        end = date.today()
        start = end - timedelta(days=30)
        frame = stock.get_market_trading_value_by_investor(
            start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), "KOSPI"
        )
        if frame is None or getattr(frame, "empty", True):
            raise ValueError("futures frame empty or unavailable")
        # Try to get futures data; pykrx stock module may not expose this directly.
        # Fall back to reporting kospi foreign net as a proxy.
        foreign_idx = None
        for idx in frame.index:
            if "외국인" in str(idx):
                foreign_idx = idx
                break
        if foreign_idx is not None:
            net = _num(frame.loc[foreign_idx].get("순매수"))
            buy = _num(frame.loc[foreign_idx].get("매수"))
            return {
                "available": True,
                "period_start": start.strftime("%Y%m%d"),
                "period_end": end.strftime("%Y%m%d"),
                "foreign_net_value": net,
                "foreign_buy_value": buy,
                "source": "pykrx KOSPI 외국인 현물 (선물 대용)",
                "note": "pykrx는 선물 직접 조회 미지원 - KOSPI 외국인 현물 순매수를 대용으로 사용",
            }
        raise ValueError("외국인 레이블 없음")
    except Exception as exc:
        return {
            "available": bool(previous_futures.get("available")),
            "stale": True,
            "reason": f"{type(exc).__name__}: {str(exc)[:180]}",
            **{k: v for k, v in previous_futures.items() if k not in ("stale", "reason")},
        }


def _compute_cds_proxy(global_data: dict[str, Any], ecos_data: dict[str, Any]) -> dict[str, Any]:
    us_10y_rows = list(global_data.get("us_10y") or [])
    kr_gov_rows = list(ecos_data.get("kr_gov_10y") or ecos_data.get("kr_gov_3y") or [])

    def _last_val(rows: list[dict[str, Any]]) -> float | None:
        for row in reversed(rows):
            v = _num(row.get("value") or row.get("DATA_VALUE"))
            if v is not None:
                return v
        return None

    us_10y = _last_val(us_10y_rows)
    kr_gov = _last_val(kr_gov_rows)

    if us_10y is None or kr_gov is None:
        return {"available": False, "reason": "한-미 국채 데이터 부재"}

    spread = kr_gov - us_10y
    hy_oas_rows = list(global_data.get("hy_oas") or [])
    hy_oas = _last_val(hy_oas_rows)

    # CDS proxy = 한국 국채 - 미국 국채 스프레드 + HY OAS 가중치 (글로벌 위험 요소)
    # 스프레드가 넓을수록 위험 압력 증가
    cds_proxy = spread + (hy_oas * 0.2 if hy_oas is not None else 0.0)
    return {
        "available": True,
        "kr_gov_latest_pct": _round(kr_gov),
        "us_10y_latest_pct": _round(us_10y),
        "kr_us_spread_pct": _round(spread),
        "hy_oas_pct": _round(hy_oas),
        "cds_proxy_score": _round(cds_proxy),
        "note": "CDS 직접 데이터 미확보: 한미 국채 스프레드 + HY OAS 가중치로 근사",
    }


def collect(output_dir: Path, timeout: int = 20) -> dict[str, Any]:
    prev_path = output_dir / "raw_realtime_risk.json"
    previous = _safe_read(prev_path, {})
    started = time.monotonic()

    global_data = _safe_read(output_dir / "raw_global_market.json", {})
    ecos_data = _safe_read(output_dir / "raw_ecos.json", {})

    # 1. VKOSPI 수집
    vkospi_result = _collect_vkospi(list(previous.get("vkospi_rows") or []))

    # 2. 글로벌 반도체/기술주 수집
    semi_previous = {k: list(previous.get("semiconductor", {}).get(k) or []) for k in YAHOO_SYMBOLS}
    semi_result = _collect_global_semiconductor(semi_previous)

    # 3. 외국인 선물(대용) 수집
    futures_result = _collect_foreign_futures(previous.get("foreign_futures") or {})

    # 4. CDS 프록시 계산
    cds_proxy = _compute_cds_proxy(global_data, ecos_data)

    # 5. USD/KRW 단기 변동성 산출
    usdkrw_rows = list(global_data.get("usd_krw_yahoo") or global_data.get("usd_krw_fred") or [])
    usdkrw_std_5d = _rolling_std(usdkrw_rows, window=5)
    usdkrw_std_10d = _rolling_std(usdkrw_rows, window=10)
    usdkrw_chg_1d = _pct_change(usdkrw_rows, days=1)
    usdkrw_chg_3d = _pct_change(usdkrw_rows, days=3)
    usdkrw_latest = _latest_value(usdkrw_rows)

    # 6. 반도체 지수 모멘텀 요약
    sox_rows = semi_result["data"].get("sox") or []
    sox_1d = _pct_change(sox_rows, days=1)
    sox_5d = _pct_change(sox_rows, days=5)
    sox_latest = _latest_value(sox_rows)
    nvda_rows = semi_result["data"].get("nvda") or []
    nvda_1d = _pct_change(nvda_rows, days=1)

    # 7. VKOSPI 최신값
    vkospi_rows = vkospi_result.get("rows") or []
    vkospi_latest = _latest_value(vkospi_rows)
    vkospi_std_10d = _rolling_std(vkospi_rows, window=10)

    result = {
        "schema_version": "1.0.0",
        "collector_version": COLLECTOR_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "vkospi": {
            "available": vkospi_result.get("available"),
            "latest": _round(vkospi_latest),
            "std_10d": _round(vkospi_std_10d),
            "rows": vkospi_rows[-60:],
            "stale": vkospi_result.get("stale", False),
            "source": vkospi_result.get("source"),
            "source_validation": vkospi_result.get("source_validation"),
            "reason": vkospi_result.get("reason"),
        },
        "semiconductor": {
            "sox_latest": _round(sox_latest),
            "sox_1d_pct": _round(sox_1d),
            "sox_5d_pct": _round(sox_5d),
            "nvda_1d_pct": _round(nvda_1d),
            "errors": semi_result.get("errors") or {},
        },
        "usdkrw_volatility": {
            "latest_rate": _round(usdkrw_latest),
            "change_1d_pct": _round(usdkrw_chg_1d),
            "change_3d_pct": _round(usdkrw_chg_3d),
            "std_5d": _round(usdkrw_std_5d),
            "std_10d": _round(usdkrw_std_10d),
        },
        "cds_proxy": cds_proxy,
        "foreign_futures": futures_result,
        "semiconductor_raw": {k: v[-60:] for k, v in semi_result["data"].items() if v},
        "vkospi_rows": vkospi_rows[-60:],
        "elapsed_seconds": round(time.monotonic() - started, 2),
    }
    write_json(prev_path, result)
    return result
