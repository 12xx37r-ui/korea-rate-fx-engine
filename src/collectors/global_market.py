from __future__ import annotations

import csv
import io
import time
import zipfile
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
        "krw_neer": FRED["krw_neer"],
        "krw_reer": FRED["krw_reer"],
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

BIS_EER_BULK_URL = "https://data.bis.org/static/bulk/WS_EER_csv_flat.zip"
CONNECT_TIMEOUT_SECONDS = 3
READ_TIMEOUT_SECONDS = 9
BIS_READ_TIMEOUT_SECONDS = 12
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


def _bis_eer_fallback() -> dict[str, list[dict[str, Any]]]:
    """Fetch Korea broad NEER/REER from the BIS bulk EER file in one request.

    The parser accepts both SDMX-style code columns and human-readable aliases so
    minor BIS CSV schema changes do not break the collector.  This function is only
    called when FRED did not provide NEER/REER and therefore does not add a normal
    steady-state request.
    """
    with requests.Session() as session:
        session.headers.update({"User-Agent": "korea-rate-fx-engine/4.5"})
        response = session.get(
            BIS_EER_BULK_URL,
            timeout=(CONNECT_TIMEOUT_SECONDS, BIS_READ_TIMEOUT_SECONDS),
        )
        response.raise_for_status()
        content = response.content

    out = {"krw_neer": [], "krw_reer": []}
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        names = [name for name in zf.namelist() if name.lower().endswith(".csv")]
        if not names:
            raise ValueError("BIS EER ZIP contains no CSV")
        # Flat CSV is normally a single file. If multiple files exist, parse all.
        for name in names:
            with zf.open(name) as fh:
                text = io.TextIOWrapper(fh, encoding="utf-8-sig", errors="replace")
                reader = csv.DictReader(text)
                for row in reader:
                    area = str(row.get("REF_AREA") or row.get("Reference area") or row.get("REF_AREA_CODE") or "").strip()
                    if area.upper() not in {"KR", "KOR", "KOREA"} and "KOREA" not in area.upper():
                        continue
                    freq = str(row.get("FREQ") or row.get("Frequency") or "M").upper()
                    if freq and not freq.startswith("M"):
                        continue
                    basket = str(row.get("BASKET") or row.get("Basket") or row.get("EER_BASKET") or "B").upper()
                    if basket and not (basket.startswith("B") or "BROAD" in basket):
                        continue
                    typ = str(row.get("EER_TYPE") or row.get("TYPE") or row.get("Type") or row.get("EER_TYPE_CODE") or "").upper()
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
                    if typ.startswith("N") or "NOMINAL" in typ:
                        key = "krw_neer"
                    elif typ.startswith("R") or "REAL" in typ:
                        key = "krw_reer"
                    else:
                        continue
                    out[key].append({"date": date, "value": value, "source": "BIS", "series_id": "WS_EER"})
    for key in out:
        dedup = {row["date"]: row for row in out[key]}
        out[key] = [dedup[d] for d in sorted(dedup)]
    if not out["krw_neer"] and not out["krw_reer"]:
        raise ValueError("BIS EER parser found no Korea broad NEER/REER")
    return out


def _yahoo_usdkrw() -> list[dict[str, Any]]:
    url = "https://query1.finance.yahoo.com/v8/finance/chart/KRW=X"
    with requests.Session() as session:
        session.headers.update({"User-Agent": "korea-rate-fx-engine/4.5"})
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
        if close in (None, ""):
            continue
        try:
            value = float(close)
            date = datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y%m%d")
        except (TypeError, ValueError, OSError):
            continue
        if 800.0 <= value <= 2500.0:
            rows.append({"date": date, "value": value, "source": "Yahoo Finance", "symbol": "KRW=X"})
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

    # Conditional official BIS fallback for Korea EER only.
    if not payload.get("krw_neer") or not payload.get("krw_reer"):
        try:
            request_count += 1
            eer = _bis_eer_fallback()
            for key in ("krw_neer", "krw_reer"):
                old_rows = previous.get(key) if isinstance(previous.get(key), list) else []
                fresh_rows = eer.get(key, [])
                payload[key] = _merge_rows(old_rows, fresh_rows)
            print(
                f"[GLOBAL_MARKET] BIS EER fallback: NEER={len(eer.get('krw_neer', []))} REER={len(eer.get('krw_reer', []))}",
                flush=True,
            )
        except Exception as exc:
            errors["bis_eer"] = f"{type(exc).__name__}: {exc}"
            print(f"[GLOBAL_MARKET] BIS EER fallback failed once | {type(exc).__name__}: {exc}", flush=True)

    yahoo_old = previous.get("usd_krw_yahoo") if isinstance(previous.get("usd_krw_yahoo"), list) else []
    try:
        request_count += 1
        yahoo_fresh = _yahoo_usdkrw()
        payload["usd_krw_yahoo"] = _merge_rows(yahoo_old, yahoo_fresh)
        print(f"[GLOBAL_MARKET] usd_krw_yahoo: fresh_rows={len(yahoo_fresh)}", flush=True)
        if not yahoo_fresh and yahoo_old:
            last_good_reused.append("usd_krw_yahoo")
    except Exception as exc:
        payload["usd_krw_yahoo"] = list(yahoo_old)
        errors["usd_krw_yahoo"] = f"{type(exc).__name__}: {exc}"
        if yahoo_old:
            last_good_reused.append("usd_krw_yahoo")
        print(f"[GLOBAL_MARKET] usd_krw_yahoo: failed once; last-good reused={bool(yahoo_old)}", flush=True)

    write_json(path, payload)

    fresh_fred_count = sum(bool(group_meta.get(g, {}).get("fresh_series")) for g in FRED_GROUPS)
    usable_count = sum(bool(values) for values in payload.values())
    # Forecasting does not depend on this status: the FX engine has ECOS macro
    # factors as a validated fallback. This status only describes optional global data.
    critical_live = sum(bool(payload.get(k)) for k in ("broad_dollar", "us_2y", "usd_cny", "usd_jpy", "krw_neer", "krw_reer"))
    if critical_live >= 4 and payload.get("usd_krw_yahoo"):
        status = "ok"
    elif usable_count:
        status = "degraded"
    else:
        status = "error"

    latest = max((str(rows[-1].get("date") or "") for rows in payload.values() if isinstance(rows, list) and rows), default=None)
    return SourceResult(
        source="global_market",
        status=status,
        message=f"{usable_count}/{len(FRED)+1} series usable; <=5 bounded requests, 3 FRED groups parallel",
        latest_observation=latest,
        payload_path=str(path),
        warnings=[f"{key}: {value}" for key, value in errors.items()],
        metadata={
            "usable_series": usable_count,
            "series_total": len(FRED) + 1,
            "request_count": request_count,
            "max_external_requests": 5,
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
