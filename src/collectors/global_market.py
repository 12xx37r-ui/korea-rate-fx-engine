from __future__ import annotations

import csv
import io
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

from src.core.io import read_json, write_json
from src.core.result import SourceResult

# Public global factors are optional inputs for the Korea FX/strength models.  The
# whole FRED panel is fetched in ONE CSV request; a FRED outage must never fan out
# into per-series retries.  KRW NEER/REER are added to the same batch, so no extra
# external call is needed for the independent KRW-strength forecast.
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

CONNECT_TIMEOUT_SECONDS = 3
READ_TIMEOUT_SECONDS = 12
# First successful bootstrap only needs enough history to validate the optional
# macro candidate.  Re-downloading from 2018 every run caused runner timeouts.
FRED_BOOTSTRAP_DAYS = 900
OVERLAP_DAYS = 120
HARD_FLOOR = "2018-01-01"


def _safe_previous(path: Path) -> dict[str, Any]:
    try:
        data = read_json(path) if path.exists() else {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _date_text(row: dict[str, Any]) -> str:
    for key in ("date", "DATE", "observation_date", "time", "TIME"):
        value = row.get(key)
        if value not in (None, ""):
            return str(value).replace("-", "").replace("/", "")[:8]
    return ""


def _merge_rows(previous: list[dict[str, Any]] | None, fresh: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Merge incremental observations by date; fresh observations replace revisions."""
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


def _fred_batch_start(previous: dict[str, Any]) -> tuple[str, str]:
    """Return (start_date, mode).

    * If any FRED series has never been bootstrapped, request only the recent
      bootstrap window, not 8+ years.
    * Once all series exist, request a small overlap window so revisions are kept.
    """
    latest_dates: list[datetime] = []
    missing = False
    for key in FRED:
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
    if missing or len(latest_dates) != len(FRED):
        return _bootstrap_start().strftime("%Y-%m-%d"), "bootstrap_recent"
    start = min(latest_dates) - timedelta(days=OVERLAP_DAYS)
    if start < floor:
        start = floor
    return start.strftime("%Y-%m-%d"), "incremental"


def _fred_batch(session: requests.Session, start_date: str) -> dict[str, list[dict[str, Any]]]:
    """Download all FRED series in a single fredgraph CSV request."""
    url = "https://fred.stlouisfed.org/graph/fredgraph.csv"
    series_ids = list(FRED.values())
    response = session.get(
        url,
        params={"id": ",".join(series_ids), "cosd": start_date},
        timeout=(CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS),
    )
    response.raise_for_status()

    reader = csv.DictReader(io.StringIO(response.text))
    fieldnames = set(reader.fieldnames or [])
    if not ({"observation_date", "DATE", "date"} & fieldnames):
        raise ValueError(f"FRED CSV date column missing: {sorted(fieldnames)[:8]}")
    present_ids = [series_id for series_id in series_ids if series_id in fieldnames]
    if not present_ids:
        raise ValueError("FRED CSV contains none of the requested series")

    by_id: dict[str, list[dict[str, Any]]] = {series_id: [] for series_id in series_ids}
    for row in reader:
        raw_date = row.get("observation_date") or row.get("DATE") or row.get("date") or ""
        date = str(raw_date).replace("-", "")[:8]
        if not date:
            continue
        for series_id in present_ids:
            value = row.get(series_id)
            if not value or value == ".":
                continue
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                continue
            by_id[series_id].append(
                {"date": date, "value": numeric, "source": "FRED", "series_id": series_id}
            )

    return {key: by_id.get(series_id, []) for key, series_id in FRED.items()}


def _yahoo_usdkrw(session: requests.Session) -> list[dict[str, Any]]:
    """Small market-price overlay for a fresher USD/KRW spot."""
    url = "https://query1.finance.yahoo.com/v8/finance/chart/KRW=X"
    response = session.get(
        url,
        params={"range": "1mo", "interval": "1d", "events": "history"},
        timeout=(CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS),
    )
    response.raise_for_status()
    payload = response.json()
    result = (((payload or {}).get("chart") or {}).get("result") or [])
    if not result:
        return []
    node = result[0] or {}
    timestamps = node.get("timestamp") or []
    quotes = ((((node.get("indicators") or {}).get("quote") or [{}])[0]) or {})
    closes = quotes.get("close") or []
    rows: list[dict[str, Any]] = []
    for ts, close in zip(timestamps, closes):
        try:
            value = float(close)
            if not 800.0 <= value <= 2500.0:
                continue
            date = datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y%m%d")
        except (TypeError, ValueError, OSError):
            continue
        rows.append({"date": date, "value": value, "source": "Yahoo Finance", "symbol": "KRW=X"})
    return rows


def collect(output_dir: Path, timeout: int, retries: int) -> SourceResult:
    # Optional factors intentionally ignore global retry settings.  Maximum external
    # calls: one FRED batch + one Yahoo spot request.
    del timeout, retries

    path = output_dir / "raw_global_market.json"
    previous = _safe_previous(path)
    payload: dict[str, list[dict[str, Any]]] = {}
    errors: dict[str, str] = {}
    last_good_reused: list[str] = []
    started = time.monotonic()
    request_count = 0

    session = requests.Session()
    session.headers.update({"User-Agent": "korea-rate-fx-engine/4.4"})

    fred_fresh: dict[str, list[dict[str, Any]]] = {}
    fred_start, fred_mode = _fred_batch_start(previous)
    print(f"[GLOBAL_MARKET] FRED batch: {len(FRED)} series | start={fred_start} | mode={fred_mode}", flush=True)
    try:
        request_count += 1
        fred_fresh = _fred_batch(session, fred_start)
        fresh_nonempty = sum(bool(v) for v in fred_fresh.values())
        print(
            f"[GLOBAL_MARKET] FRED batch: fresh_series={fresh_nonempty}/{len(FRED)} | elapsed={time.monotonic()-started:.1f}s",
            flush=True,
        )
    except (requests.RequestException, ValueError, csv.Error) as exc:
        errors["fred_batch"] = f"{type(exc).__name__}: {exc}"
        print(
            f"[GLOBAL_MARKET] FRED batch failed once; no per-series retry | {type(exc).__name__}: {exc}",
            flush=True,
        )

    for key in FRED:
        old_rows = previous.get(key) if isinstance(previous.get(key), list) else []
        fresh_rows = fred_fresh.get(key, [])
        merged = _merge_rows(old_rows, fresh_rows)
        payload[key] = merged
        if not fresh_rows and old_rows:
            last_good_reused.append(key)

    yahoo_old = previous.get("usd_krw_yahoo") if isinstance(previous.get("usd_krw_yahoo"), list) else []
    try:
        request_count += 1
        yahoo_fresh = _yahoo_usdkrw(session)
        payload["usd_krw_yahoo"] = _merge_rows(yahoo_old, yahoo_fresh)
        print(f"[GLOBAL_MARKET] usd_krw_yahoo: fresh_rows={len(yahoo_fresh)}", flush=True)
        if not yahoo_fresh and yahoo_old:
            last_good_reused.append("usd_krw_yahoo")
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        payload["usd_krw_yahoo"] = list(yahoo_old)
        errors["usd_krw_yahoo"] = f"{type(exc).__name__}: {exc}"
        if yahoo_old:
            last_good_reused.append("usd_krw_yahoo")
        print(f"[GLOBAL_MARKET] usd_krw_yahoo: failed once; last-good reused={bool(yahoo_old)}", flush=True)
    finally:
        session.close()

    write_json(path, payload)

    fresh_fred_count = sum(bool(rows) for rows in fred_fresh.values())
    usable_count = sum(bool(values) for values in payload.values())
    # 8 live public factors are enough for the full FX macro panel; NEER/REER and
    # secondary series may lag without disabling the forecast.
    if fresh_fred_count >= 8 and payload.get("usd_krw_yahoo"):
        status = "ok"
    elif usable_count:
        status = "degraded"
    else:
        status = "error"

    latest = max((str(rows[-1].get("date") or "") for rows in payload.values() if rows), default=None)
    return SourceResult(
        source="global_market",
        status=status,
        message=f"{usable_count}/{len(FRED)+1} series usable; max 2 external requests",
        latest_observation=latest,
        payload_path=str(path),
        warnings=[f"{key}: {value}" for key, value in errors.items()],
        metadata={
            "fresh_fred_series": fresh_fred_count,
            "usable_series": usable_count,
            "series_total": len(FRED) + 1,
            "request_count": request_count,
            "max_external_requests": 2,
            "fred_batch_start": fred_start,
            "fred_mode": fred_mode,
            "bootstrap_days": FRED_BOOTSTRAP_DAYS,
            "overlap_days": OVERLAP_DAYS,
            "last_good_reused": sorted(set(last_good_reused)),
            "errors": errors,
        },
    )
