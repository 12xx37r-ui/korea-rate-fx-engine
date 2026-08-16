from __future__ import annotations

import csv
import io
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

from src.core.io import read_json, write_json
from src.core.result import SourceResult

# V4.5: split the optional global panel into three small parallel FRED batches.
# A single 13-series request was repeatedly timing out on GitHub Actions.  Three
# compact requests are still low-call, finish in parallel, and do not fan out into
# per-series retries.  BIS EER is a conditional fallback only when NEER/REER were
# not obtained from FRED.
FRED = {
    "broad_dollar": "DTWEXBGS",
    "us_2y": "DGS2",
    "us_10y": "DGS10",
    "us_breakeven_10y": "T10YIE",
    "vix": "VIXCLS",
    "hy_oas": "BAMLH0A0HYM2",
    "wti": "DCOILWTICO",
    "commodity_index": "PPIACO",
    "usd_cny": "DEXCHUS",
    "usd_jpy": "DEXJPUS",
    "usd_krw_fred": "DEXKOUS",
    "krw_neer": "NBKRBIS",
    "krw_reer": "RBKRBIS",
}

FRED_GROUPS: dict[str, dict[str, str]] = {
    "currency": {
        "broad_dollar": FRED["broad_dollar"],
        "usd_cny": FRED["usd_cny"],
        "usd_jpy": FRED["usd_jpy"],
        "usd_krw_fred": FRED["usd_krw_fred"],
    },
    "rates_risk": {
        "us_2y": FRED["us_2y"],
        "us_10y": FRED["us_10y"],
        "us_breakeven_10y": FRED["us_breakeven_10y"],
        "vix": FRED["vix"],
        "hy_oas": FRED["hy_oas"],
    },
    "commodities": {
        "wti": FRED["wti"],
        "commodity_index": FRED["commodity_index"],
    },
}

BIS_EER_API_URL = "https://stats.bis.org/api/v1/data/WS_EER/M.N+R.B.KR/all"
CONNECT_TIMEOUT_SECONDS = 3
READ_TIMEOUT_SECONDS = 15
BIS_READ_TIMEOUT_SECONDS = 10
BIS_REFRESH_MAX_AGE_DAYS = 45
FRED_BOOTSTRAP_DAYS = 900
OVERLAP_DAYS = 120
HARD_FLOOR = "2018-01-01"

# Task 1: 반도체 마이크로 팩터 - Yahoo Finance 로 수집
# SOX(^SOX), TWII(^TWII), NVDA, TSM 일봉
YAHOO_EQUITY: dict[str, str] = {
    "sox": "^SOX",
    "twii": "^TWII",
    "nvda": "NVDA",
    "tsm": "TSM",
}
YAHOO_EQUITY_DAYS = 90  # 20d/60d 모멘텀 계산에 충분한 기간
NAVER_USDKRW_URL = "https://m.stock.naver.com/front-api/marketIndex/exchange/main"
NO_CACHE_HEADERS = {"Cache-Control": "no-cache, no-store, max-age=0", "Pragma": "no-cache"}


def _safe_previous(path: Path) -> dict[str, Any]:
    try:
        data = read_json(path) if path.exists() else {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _date_text(row: dict[str, Any]) -> str:
    for key in ("date", "DATE", "observation_date", "time", "TIME", "TIME_PERIOD"):
        value = row.get(key)
        if value not in (None, ""):
            return str(value).replace("-", "").replace("/", "")[:8]
    return ""


def _merge_rows(previous: list[dict[str, Any]] | None, fresh: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for row in previous or []:
        if isinstance(row, dict):
            date = _date_text(row)
            if date:
                merged[date] = row
    for row in fresh or []:
        if isinstance(row, dict):
            date = _date_text(row)
            if date:
                merged[date] = row
    return [merged[key] for key in sorted(merged)]


def _bootstrap_start() -> datetime:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    floor = datetime.strptime(HARD_FLOOR, "%Y-%m-%d")
    return max(floor, now - timedelta(days=FRED_BOOTSTRAP_DAYS))


def _group_start(previous: dict[str, Any], group: dict[str, str]) -> tuple[str, str]:
    latest_dates: list[datetime] = []
    missing = False
    for key in group:
        rows = previous.get(key)
        if not isinstance(rows, list) or not rows:
            missing = True
            continue
        date_text = _date_text(rows[-1])
        try:
            latest_dates.append(datetime.strptime(date_text, "%Y%m%d"))
        except (TypeError, ValueError):
            missing = True
    floor = datetime.strptime(HARD_FLOOR, "%Y-%m-%d")
    if missing or len(latest_dates) != len(group):
        return _bootstrap_start().strftime("%Y-%m-%d"), "bootstrap_recent"
    start = min(latest_dates) - timedelta(days=OVERLAP_DAYS)
    if start < floor:
        start = floor
    return start.strftime("%Y-%m-%d"), "incremental"


def _fred_batch(group: dict[str, str], start_date: str) -> dict[str, list[dict[str, Any]]]:
    url = "https://fred.stlouisfed.org/graph/fredgraph.csv"
    series_ids = list(group.values())
    with requests.Session() as session:
        session.headers.update({"User-Agent": "korea-rate-fx-engine/4.5"})
        response = session.get(
            url,
            params={"id": ",".join(series_ids), "cosd": start_date},
            timeout=(CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS),
        )
        response.raise_for_status()
        text = response.text

    reader = csv.DictReader(io.StringIO(text))
    fieldnames = set(reader.fieldnames or [])
    if not ({"observation_date", "DATE", "date"} & fieldnames):
        raise ValueError(f"FRED CSV date column missing: {sorted(fieldnames)[:8]}")
    present_ids = [sid for sid in series_ids if sid in fieldnames]
    if not present_ids:
        raise ValueError("FRED CSV contains none of the requested series")

    by_id: dict[str, list[dict[str, Any]]] = {sid: [] for sid in series_ids}
    for row in reader:
        raw_date = row.get("observation_date") or row.get("DATE") or row.get("date") or ""
        date = str(raw_date).replace("-", "")[:8]
        if not date:
            continue
        for sid in present_ids:
            raw = row.get(sid)
            if raw in (None, "", "."):
                continue
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            by_id[sid].append({"date": date, "value": value, "source": "FRED", "series_id": sid})
    return {key: by_id.get(sid, []) for key, sid in group.items()}


def _parse_bis_eer_csv(text: str) -> dict[str, list[dict[str, Any]]]:
    out = {"krw_neer": [], "krw_reer": []}
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        area = str(row.get("REF_AREA") or row.get("Reference area") or "KR").strip().upper()
        if area not in {"KR", "KOR", "KOREA"} and "KOREA" not in area:
            continue
        freq = str(row.get("FREQ") or row.get("Frequency") or "M").upper()
        if freq and not freq.startswith("M"):
            continue
        basket = str(row.get("EER_BASKET") or row.get("BASKET") or row.get("Basket") or "B").upper()
        if basket and not (basket.startswith("B") or "BROAD" in basket):
            continue
        typ = str(row.get("EER_TYPE") or row.get("TYPE") or row.get("Type") or "").upper()
        if not typ:
            key_text = str(row.get("SERIES_KEY") or row.get("KEY") or row.get("series") or "").upper()
            if ".N.B.KR" in key_text or key_text.startswith("M.N.B.KR"):
                typ = "N"
            elif ".R.B.KR" in key_text or key_text.startswith("M.R.B.KR"):
                typ = "R"
        period = str(row.get("TIME_PERIOD") or row.get("TIME") or row.get("Period") or "")
        raw = row.get("OBS_VALUE") or row.get("value") or row.get("Value")
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        date = period.replace("-", "")[:6]
        if len(date) == 6:
            date += "01"
        if len(date) < 8:
            continue
        key = "krw_neer" if typ.startswith("N") or "NOMINAL" in typ else "krw_reer" if typ.startswith("R") or "REAL" in typ else None
        if key:
            out[key].append({"date": date, "value": value, "source": "BIS", "series_id": "WS_EER"})
    for key in out:
        dedup = {row["date"]: row for row in out[key]}
        out[key] = [dedup[d] for d in sorted(dedup)]
    if not out["krw_neer"] or not out["krw_reer"]:
        raise ValueError(f"BIS API parser incomplete: NEER={len(out['krw_neer'])}, REER={len(out['krw_reer'])}")
    return out


def _eer_start(previous: dict[str, Any]) -> tuple[str, str]:
    latest_dates: list[datetime] = []
    for key in ("krw_neer", "krw_reer"):
        rows = previous.get(key)
        if not isinstance(rows, list) or not rows:
            return "2000-01", "bootstrap"
        try:
            latest_dates.append(datetime.strptime(_date_text(rows[-1])[:6], "%Y%m"))
        except (TypeError, ValueError):
            return "2000-01", "bootstrap"
    latest = min(latest_dates)
    # 24-month overlap handles BIS revisions without re-downloading full history.
    idx = latest.year * 12 + latest.month - 1 - 24
    return f"{idx//12:04d}-{idx%12+1:02d}", "incremental"


def _eer_cache_is_fresh(previous: dict[str, Any]) -> bool:
    dates = []
    for key in ("krw_neer", "krw_reer"):
        rows = previous.get(key)
        if not isinstance(rows, list) or not rows:
            return False
        try:
            dates.append(datetime.strptime(_date_text(rows[-1])[:6] + "01", "%Y%m%d"))
        except (TypeError, ValueError):
            return False
    latest = min(dates)
    # Monthly series: do not call BIS on every GitHub Actions run when cached data
    # is within one publication cycle.
    return (datetime.now(timezone.utc).replace(tzinfo=None) - latest).days <= BIS_REFRESH_MAX_AGE_DAYS


def _bis_eer_api(previous: dict[str, Any]) -> tuple[dict[str, list[dict[str, Any]]], str, str]:
    start_period, mode = _eer_start(previous)
    with requests.Session() as session:
        session.headers.update({
            "User-Agent": "korea-rate-fx-engine/4.7",
            "Accept": "text/csv,application/vnd.sdmx.data+csv;version=1.0.0,*/*;q=0.2",
        })
        response = session.get(
            BIS_EER_API_URL,
            params={"startPeriod": start_period, "detail": "dataonly"},
            timeout=(CONNECT_TIMEOUT_SECONDS, BIS_READ_TIMEOUT_SECONDS),
        )
        response.raise_for_status()
        return _parse_bis_eer_csv(response.text), start_period, mode


def _yahoo_equity_ohlcv(symbol: str, days: int = YAHOO_EQUITY_DAYS) -> list[dict[str, Any]]:
    """Yahoo Finance에서 주식/지수 일봉 종가를 수집한다."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    with requests.Session() as session:
        session.headers.update({"User-Agent": "korea-rate-fx-engine/5.0"})
        response = session.get(
            url,
            params={"range": f"{days}d", "interval": "1d", "events": "history"},
            timeout=(CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS),
        )
        response.raise_for_status()
        payload = response.json()
    result = (((payload or {}).get("chart") or {}).get("result") or [])
    if not result:
        return []
    node = result[0] or {}
    timestamps = node.get("timestamp") or []
    quotes = (((node.get("indicators") or {}).get("quote") or [{}])[0]) or {}
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


def _momentum_pct(rows: list[dict[str, Any]], window: int) -> float | None:
    """window일 전 대비 현재 등락률(%)."""
    if len(rows) < window + 1:
        return None
    try:
        current = float(rows[-1]["value"])
        past = float(rows[-(window + 1)]["value"])
        return round((current / past - 1.0) * 100.0, 4) if past != 0 else None
    except (TypeError, ValueError, KeyError):
        return None


def _collect_yahoo_equity(previous: dict[str, Any]) -> dict[str, Any]:
    """SOX, TWII, NVDA, TSM 일봉 + 20d/60d 모멘텀을 수집한다."""
    rows_by_key: dict[str, list[dict[str, Any]]] = {}
    errors: dict[str, str] = {}

    for key, symbol in YAHOO_EQUITY.items():
        old = list(previous.get(key) or [])
        try:
            fresh = _yahoo_equity_ohlcv(symbol)
            merged = _merge_rows(old, fresh)
            rows_by_key[key] = merged
            if not fresh and old:
                errors[key] = "stale_reused"
        except Exception as exc:
            rows_by_key[key] = old
            errors[key] = f"{type(exc).__name__}: {str(exc)[:120]}"

    momentum: dict[str, Any] = {}
    for key in YAHOO_EQUITY:
        rows = rows_by_key.get(key) or []
        momentum[key] = {
            "latest": rows[-1]["value"] if rows else None,
            "mom_20d_pct": _momentum_pct(rows, 20),
            "mom_60d_pct": _momentum_pct(rows, 60),
        }

    return {"rows": rows_by_key, "momentum": momentum, "errors": errors}


def _yahoo_usdkrw_bundle() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Fetch USD/KRW once and preserve both model history and market snapshot.

    The historical 1d series remains unchanged for model compatibility. Yahoo's
    chart metadata is still retained as a fallback market snapshot. V218 adds an
    independent Naver MarketIndex quote request so the public engine can publish
    a fresher USD/KRW value without changing the historical model input series.
    """
    url = "https://query1.finance.yahoo.com/v8/finance/chart/KRW=X"
    with requests.Session() as session:
        session.headers.update({"User-Agent": "korea-rate-fx-engine/5.2", **NO_CACHE_HEADERS})
        response = session.get(
            url,
            params={"range": "1mo", "interval": "1d", "events": "history", "_ts": str(int(time.time()))},
            timeout=(CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS),
        )
        response.raise_for_status()
        payload = response.json()
    result = (((payload or {}).get("chart") or {}).get("result") or [])
    if not result:
        return [], {}
    node = result[0] or {}
    meta = node.get("meta") or {}
    timestamps = node.get("timestamp") or []
    quotes = ((((node.get("indicators") or {}).get("quote") or [{}])[0]) or {})
    closes = quotes.get("close") or []
    rows: list[dict[str, Any]] = []
    for ts, close in zip(timestamps, closes):
        if close in (None, ""):
            continue
        try:
            value = float(close)
            date = datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y%m%d")
        except (TypeError, ValueError, OSError):
            continue
        if 800.0 <= value <= 2500.0:
            rows.append({"date": date, "value": value, "source": "Yahoo Finance", "symbol": "KRW=X"})

    market_price = meta.get("regularMarketPrice")
    try:
        market_price = float(market_price)
    except (TypeError, ValueError):
        market_price = None
    if market_price is not None and not (800.0 <= market_price <= 2500.0):
        market_price = None

    market_time = meta.get("regularMarketTime")
    market_time_utc = None
    try:
        if market_time not in (None, ""):
            market_time_utc = datetime.fromtimestamp(int(market_time), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        market_time_utc = None

    snapshot = {
        "symbol": "KRW=X",
        "price": market_price,
        "market_time_utc": market_time_utc,
        "exchange": meta.get("exchangeName"),
        "exchange_timezone": meta.get("exchangeTimezoneName"),
        "currency": meta.get("currency"),
        "source": "Yahoo Finance chart metadata",
        "source_url": url,
        "model_history_interval": "1d",
    }
    return rows, snapshot


def _walk_find_naver_usdkrw(node: Any) -> dict[str, Any] | None:
    if isinstance(node, dict):
        code = str(node.get("reutersCode") or node.get("symbolCode") or node.get("code") or "")
        if code == "FX_USDKRW":
            return node
        for value in node.values():
            found = _walk_find_naver_usdkrw(value)
            if found:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _walk_find_naver_usdkrw(value)
            if found:
                return found
    return None


def _parse_naver_datetime(item: dict[str, Any]) -> str | None:
    raw = (item.get("localTradedAt") or item.get("localTradeDate") or item.get("tradeDate")
           or item.get("date") or item.get("localDateTime"))
    text = str(raw or "").strip()
    if not text:
        return None
    candidates = [text, text.replace(".", "-").replace("/", "-")]
    for candidate in candidates:
        try:
            dt = datetime.fromisoformat(candidate)
            if dt.tzinfo is None:
                # Naver MarketIndex timestamps are displayed in Korea local time.
                dt = dt.replace(tzinfo=timezone(timedelta(hours=9)))
            return dt.astimezone(timezone.utc).isoformat()
        except Exception:
            pass
    for fmt in ("%Y%m%d%H%M%S", "%Y%m%d%H%M", "%Y%m%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(text, fmt).replace(tzinfo=timezone(timedelta(hours=9)))
            return dt.astimezone(timezone.utc).isoformat()
        except Exception:
            continue
    return None


def _naver_usdkrw_snapshot() -> dict[str, Any]:
    """Independent latest USD/KRW quote used only as a public-current overlay.

    One additional request per engine run. The model history is never sourced from
    this endpoint; only the current public quote and market status are overlaid.
    """
    with requests.Session() as session:
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (compatible; korea-rate-fx-engine/5.2)",
            "Accept": "application/json,text/plain,*/*",
            **NO_CACHE_HEADERS,
        })
        response = session.get(
            NAVER_USDKRW_URL,
            params={"_ts": str(int(time.time()))},
            timeout=(CONNECT_TIMEOUT_SECONDS, 8),
        )
        response.raise_for_status()
        payload = response.json()
    item = _walk_find_naver_usdkrw(payload)
    if not item:
        raise ValueError("Naver MarketIndex response missing FX_USDKRW")
    raw_price = item.get("closePrice") or item.get("price") or item.get("value")
    try:
        price = float(str(raw_price).replace(",", ""))
    except (TypeError, ValueError):
        raise ValueError(f"Naver FX_USDKRW invalid price: {raw_price!r}")
    if not 800.0 <= price <= 2500.0:
        raise ValueError(f"Naver FX_USDKRW out of range: {price}")
    state = str(item.get("marketStatus") or item.get("marketState") or item.get("status") or "").upper() or None
    return {
        "symbol": "FX_USDKRW",
        "price": price,
        "market_time_utc": _parse_naver_datetime(item),
        "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
        "market_state": state,
        "source": "Naver MarketIndex FX_USDKRW",
        "source_url": NAVER_USDKRW_URL,
        "provider_code": "FX_USDKRW",
    }


def _select_usdkrw_snapshot(naver: dict[str, Any], yahoo: dict[str, Any], previous: dict[str, Any]) -> dict[str, Any]:
    for candidate in (naver, yahoo, previous):
        if isinstance(candidate, dict) and candidate.get("price") is not None:
            return dict(candidate)
    return {}


def _yahoo_usdkrw() -> list[dict[str, Any]]:
    """Backward-compatible historical-series helper."""
    rows, _ = _yahoo_usdkrw_bundle()
    return rows


def collect(output_dir: Path, timeout: int, retries: int) -> SourceResult:
    # Optional factors intentionally ignore broad retry settings. They are bounded
    # by small per-request deadlines and reuse last-good data instead of stalling.
    del timeout, retries

    path = output_dir / "raw_global_market.json"
    previous = _safe_previous(path)
    payload: dict[str, list[dict[str, Any]]] = {key: list(previous.get(key) or []) for key in FRED}
    errors: dict[str, str] = {}
    last_good_reused: list[str] = []
    group_meta: dict[str, Any] = {}
    started = time.monotonic()
    request_count = 0

    # Three compact FRED groups run in parallel. Worst-case wall time is one group
    # timeout rather than three sequential timeouts.
    futures = {}
    with ThreadPoolExecutor(max_workers=len(FRED_GROUPS)) as pool:
        for name, group in FRED_GROUPS.items():
            start_date, mode = _group_start(previous, group)
            group_meta[name] = {"start": start_date, "mode": mode}
            print(f"[GLOBAL_MARKET] FRED {name}: {len(group)} series | start={start_date} | mode={mode}", flush=True)
            futures[pool.submit(_fred_batch, group, start_date)] = (name, group)
            request_count += 1

        for future in as_completed(futures):
            name, group = futures[future]
            try:
                fresh = future.result()
                fresh_nonempty = sum(bool(v) for v in fresh.values())
                group_meta[name]["fresh_series"] = fresh_nonempty
                print(f"[GLOBAL_MARKET] FRED {name}: fresh_series={fresh_nonempty}/{len(group)}", flush=True)
                for key in group:
                    old_rows = previous.get(key) if isinstance(previous.get(key), list) else []
                    fresh_rows = fresh.get(key, [])
                    payload[key] = _merge_rows(old_rows, fresh_rows)
                    if not fresh_rows and old_rows:
                        last_good_reused.append(key)
            except Exception as exc:  # bounded worker exceptions only
                errors[f"fred_{name}"] = f"{type(exc).__name__}: {exc}"
                group_meta[name]["error"] = errors[f"fred_{name}"]
                print(f"[GLOBAL_MARKET] FRED {name}: failed once; no per-series retry | {type(exc).__name__}: {exc}", flush=True)
                for key in group:
                    old_rows = previous.get(key) if isinstance(previous.get(key), list) else []
                    payload[key] = list(old_rows)
                    if old_rows:
                        last_good_reused.append(key)

    # BIS EER is authoritative for KRW NEER/REER. Because it is monthly, reuse
    # a fresh local merged history and only make one SDMX call per release cycle.
    if _eer_cache_is_fresh(previous):
        group_meta["bis_eer"] = {"mode": "fresh_cache", "request_skipped": True}
        print("[GLOBAL_MARKET] BIS EER: fresh monthly cache reused; external request skipped", flush=True)
    else:
        try:
            request_count += 1
            eer, eer_start, eer_mode = _bis_eer_api(previous)
            group_meta["bis_eer"] = {"mode": eer_mode, "start": eer_start, "request_skipped": False}
            for key in ("krw_neer", "krw_reer"):
                old_rows = previous.get(key) if isinstance(previous.get(key), list) else []
                fresh_rows = eer.get(key, [])
                payload[key] = _merge_rows(old_rows, fresh_rows)
            print(f"[GLOBAL_MARKET] BIS EER: NEER={len(eer['krw_neer'])} REER={len(eer['krw_reer'])} mode={eer_mode}", flush=True)
        except Exception as exc:
            errors["bis_eer"] = f"{type(exc).__name__}: {exc}"
            group_meta["bis_eer"] = {"error": errors["bis_eer"], "request_skipped": False}
            for key in ("krw_neer", "krw_reer"):
                if previous.get(key): last_good_reused.append(key)
            print(f"[GLOBAL_MARKET] BIS EER failed once; last-good retained | {type(exc).__name__}: {exc}", flush=True)

    yahoo_old = previous.get("usd_krw_yahoo") if isinstance(previous.get("usd_krw_yahoo"), list) else []
    yahoo_snapshot_old = previous.get("usd_krw_market_snapshot") if isinstance(previous.get("usd_krw_market_snapshot"), dict) else {}
    yahoo_snapshot: dict[str, Any] = {}
    try:
        request_count += 1
        yahoo_fresh, yahoo_snapshot = _yahoo_usdkrw_bundle()
        payload["usd_krw_yahoo"] = _merge_rows(yahoo_old, yahoo_fresh)
        if not yahoo_fresh and yahoo_old:
            last_good_reused.append("usd_krw_yahoo")
    except Exception as exc:
        payload["usd_krw_yahoo"] = list(yahoo_old)
        errors["usd_krw_yahoo"] = f"{type(exc).__name__}: {exc}"
        if yahoo_old:
            last_good_reused.append("usd_krw_yahoo")

    naver_snapshot: dict[str, Any] = {}
    try:
        request_count += 1  # V218: one independent current-quote call per run.
        naver_snapshot = _naver_usdkrw_snapshot()
    except Exception as exc:
        errors["usd_krw_naver"] = f"{type(exc).__name__}: {exc}"

    chosen_snapshot = _select_usdkrw_snapshot(naver_snapshot, yahoo_snapshot, yahoo_snapshot_old)
    payload["usd_krw_market_snapshot"] = chosen_snapshot
    payload["usd_krw_market_snapshot_candidates"] = {
        "naver": naver_snapshot,
        "yahoo": yahoo_snapshot,
    }
    if not naver_snapshot and not yahoo_snapshot and yahoo_snapshot_old:
        last_good_reused.append("usd_krw_market_snapshot")
    print(
        f"[GLOBAL_MARKET] USDKRW current overlay: source={chosen_snapshot.get('source')} "
        f"price={chosen_snapshot.get('price')} state={chosen_snapshot.get('market_state')}",
        flush=True,
    )

    # Task 1: 반도체 마이크로 팩터 수집 (SOX, TWII, NVDA, TSM)
    semi_previous = {k: list(previous.get(k) or []) for k in YAHOO_EQUITY}
    try:
        request_count += len(YAHOO_EQUITY)
        semi_result = _collect_yahoo_equity(semi_previous)
        for key in YAHOO_EQUITY:
            payload[key] = semi_result["rows"].get(key, semi_previous[key])
            if semi_result["errors"].get(key) and semi_previous.get(key):
                last_good_reused.append(key)
        payload["semiconductor_momentum"] = semi_result["momentum"]
        semi_errors = {k: v for k, v in semi_result["errors"].items() if v != "stale_reused"}
        if semi_errors:
            errors.update({f"semi_{k}": v for k, v in semi_errors.items()})
        mom = semi_result["momentum"]
        print(
            f"[GLOBAL_MARKET] semiconductor: sox_20d={mom.get('sox', {}).get('mom_20d_pct')}% "
            f"nvda_20d={mom.get('nvda', {}).get('mom_20d_pct')}% "
            f"tsm_20d={mom.get('tsm', {}).get('mom_20d_pct')}%",
            flush=True,
        )
    except Exception as exc:
        errors["semiconductor"] = f"{type(exc).__name__}: {exc}"
        for key in YAHOO_EQUITY:
            payload[key] = semi_previous.get(key, [])
        payload["semiconductor_momentum"] = {}
        print(f"[GLOBAL_MARKET] semiconductor: failed; last-good reused | {exc}", flush=True)

    write_json(path, payload)

    total_series = len(FRED) + 1 + len(YAHOO_EQUITY)  # FRED + usd_krw_yahoo + 반도체4
    usable_count = sum(bool(values) for key, values in payload.items() if isinstance(values, list))
    critical_live = sum(bool(payload.get(k)) for k in ("broad_dollar", "us_2y", "usd_cny", "usd_jpy", "krw_neer", "krw_reer"))
    semi_live = sum(bool(payload.get(k)) for k in YAHOO_EQUITY)
    if critical_live >= 4 and payload.get("usd_krw_yahoo"):
        status = "ok"
    elif usable_count:
        status = "degraded"
    else:
        status = "error"

    latest = max(
        (str(rows[-1].get("date") or "") for rows in payload.values() if isinstance(rows, list) and rows),
        default=None,
    )
    return SourceResult(
        source="global_market",
        status=status,
        message=f"{usable_count}/{total_series} series usable; semiconductor micro-factors included",
        latest_observation=latest,
        payload_path=str(path),
        warnings=[f"{key}: {value}" for key, value in errors.items()],
        metadata={
            "usable_series": usable_count,
            "series_total": total_series,
            "semiconductor_live": semi_live,
            "semiconductor_symbols": list(YAHOO_EQUITY.keys()),
            "request_count": request_count,
            "bis_monthly_cache_skip_enabled": True,
            "fred_group_count": len(FRED_GROUPS),
            "fred_groups_parallel": True,
            "group_meta": group_meta,
            "bootstrap_days": FRED_BOOTSTRAP_DAYS,
            "overlap_days": OVERLAP_DAYS,
            "last_good_reused": sorted(set(last_good_reused)),
            "errors": errors,
            "elapsed_seconds": round(time.monotonic() - started, 2),
        },
    )
