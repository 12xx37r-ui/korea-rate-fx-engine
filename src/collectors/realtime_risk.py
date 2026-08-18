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
import os
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.core.io import read_json, write_json


COLLECTOR_VERSION = "realtime-risk-collector-v1.4-vkospi-diagnostics"
YAHOO_SYMBOLS = {
    "sox": "^SOX",
    "nvda": "NVDA",
    "soxx": "SOXX",
    "sp500": "^GSPC",
}
YAHOO_TIMEOUT = (3, 9)
VKOSPI_INDEX_CODE = "E62001"
VKOSPI_POLL_URL = "https://polling.finance.naver.com/api/realtime"
VKOSPI_BASIC_URL = "https://m.stock.naver.com/api/index/E62001/basic"
KRX_DERIV_INDEX_URL = "https://data-dbg.krx.co.kr/svc/apis/idx/drvprod_dd_trd"


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


def _vkospi_naver_live() -> dict[str, Any]:
    """Fetch V-KOSPI 200 using the verified KRX/Koscom issue code E62001.

    Identity guard is strict: code must resolve to E62001 and value must be in a
    plausible volatility-index range.  A secondary public endpoint is attempted
    only if the polling endpoint fails, so normal network cost is one request.
    """
    import requests
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json,*/*"}
    errors: list[str] = []
    with requests.Session() as session:
        try:
            response = session.get(
                VKOSPI_POLL_URL,
                params={"query": f"SERVICE_INDEX:{VKOSPI_INDEX_CODE}"},
                headers=headers, timeout=YAHOO_TIMEOUT,
            )
            response.raise_for_status()
            payload = response.json()
            areas = ((payload or {}).get("result") or {}).get("areas") or []
            data = ((areas[0] or {}).get("datas") or [None])[0] if areas else None
            if isinstance(data, dict):
                code = str(data.get("cd") or data.get("code") or "").strip()
                raw = data.get("nv")
                value = _num(raw)
                # NAVER index polling commonly transmits index values *100 as integers.
                if value is not None and value > 150.0:
                    value = value / 100.0
                if code == VKOSPI_INDEX_CODE and value is not None and 5.0 <= value <= 150.0:
                    return {"value": value, "date": date.today().strftime("%Y%m%d"),
                            "source": "NAVER Finance index polling (KRX/Koscom E62001)",
                            "source_validation": "verified", "code": code}
            errors.append("polling_identity_or_value_guard_failed")
        except Exception as exc:
            errors.append(f"polling:{type(exc).__name__}:{str(exc)[:100]}")

        try:
            response = session.get(VKOSPI_BASIC_URL, headers=headers, timeout=YAHOO_TIMEOUT)
            response.raise_for_status()
            payload = response.json()
            code = str(payload.get("itemCode") or payload.get("code") or payload.get("reutersCode") or "").strip()
            name = str(payload.get("stockName") or payload.get("indexName") or payload.get("name") or "")
            raw = payload.get("closePrice") or payload.get("currentPrice") or payload.get("value")
            if isinstance(raw, str): raw = raw.replace(',', '')
            value = _num(raw)
            identity_ok = code == VKOSPI_INDEX_CODE or "KOSPI" in name.upper() and ("V-" in name.upper() or "VOL" in name.upper())
            if identity_ok and value is not None and 5.0 <= value <= 150.0:
                return {"value": value, "date": date.today().strftime("%Y%m%d"),
                        "source": "NAVER Finance index basic (KRX/Koscom E62001)",
                        "source_validation": "verified", "code": VKOSPI_INDEX_CODE}
            errors.append("basic_identity_or_value_guard_failed")
        except Exception as exc:
            errors.append(f"basic:{type(exc).__name__}:{str(exc)[:100]}")
    raise RuntimeError("; ".join(errors[-4:]))



def _vkospi_krx_daily() -> dict[str, Any]:
    """Official KRX daily derivative-index fallback.

    Called only if both NAVER E62001 paths fail. The endpoint is daily/EOD, so
    it intentionally searches only a few recent business dates and accepts a
    row only when KRX itself labels it V-KOSPI 200.
    """
    import requests
    key = os.getenv("KRX_API_KEY", "").strip()
    if not key:
        raise RuntimeError("KRX_API_KEY unavailable")
    errors: list[str] = []
    d = date.today()
    tried = 0
    while tried < 4:
        d -= timedelta(days=1)
        if d.weekday() >= 5:
            continue
        tried += 1
        bas_dd = d.strftime("%Y%m%d")
        try:
            response = requests.get(
                KRX_DERIV_INDEX_URL,
                params={"basDd": bas_dd},
                headers={"AUTH_KEY": key, "Accept": "application/json"},
                timeout=YAHOO_TIMEOUT,
            )
            response.raise_for_status()
            rows = (response.json() or {}).get("OutBlock_1") or []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                name = str(row.get("IDX_NM") or "").upper().replace(" ", "")
                if "V-KOSPI200" not in name and "VKOSPI200" not in name:
                    continue
                raw = str(row.get("CLSPRC_IDX") or "").replace(",", "").strip()
                value = _num(raw)
                if value is not None and 5.0 <= value <= 150.0:
                    return {
                        "value": value,
                        "date": str(row.get("BAS_DD") or bas_dd),
                        "source": "KRX OPEN API derivative-index daily",
                        "source_validation": "verified",
                        "code": VKOSPI_INDEX_CODE,
                        "freshness_class": "EOD",
                    }
            errors.append(f"{bas_dd}:vkospi_row_not_found")
        except Exception as exc:
            errors.append(f"{bas_dd}:{type(exc).__name__}:{str(exc)[:100]}")
    raise RuntimeError("; ".join(errors[-4:]))

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
    verified_previous = _sanitize_verified_vkospi_rows(previous_vkospi)
    errors: list[str] = []
    live = None
    try:
        live = _vkospi_naver_live()
    except Exception as exc:
        errors.append(f"naver:{type(exc).__name__}:{str(exc)[:160]}")
        try:
            live = _vkospi_krx_daily()
        except Exception as krx_exc:
            errors.append(f"krx:{type(krx_exc).__name__}:{str(krx_exc)[:160]}")

    if live is not None:
        by_date = {str(x.get("date")): dict(x) for x in verified_previous if x.get("date")}
        by_date[str(live["date"])] = live
        rows = [by_date[d] for d in sorted(by_date)][-90:]
        live_date = str(live.get("date") or "")
        stale = False
        try:
            live_dt = datetime.strptime(live_date[:8], "%Y%m%d").date()
            stale = (date.today() - live_dt).days > 3
        except Exception:
            pass
        return {"available": True, "rows": rows, "stale": stale,
                "source": live["source"], "source_validation": "verified",
                "reason": "; ".join(errors) if errors else None}

    return {"available": bool(verified_previous), "rows": verified_previous,
            "stale": bool(verified_previous),
            "source": "verified VKOSPI last-good" if verified_previous else None,
            "source_validation": "verified" if verified_previous else "unavailable",
            "reason": "; ".join(errors[-2:]) or "verified VKOSPI source unavailable"}


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
            "identity_code": VKOSPI_INDEX_CODE,
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
