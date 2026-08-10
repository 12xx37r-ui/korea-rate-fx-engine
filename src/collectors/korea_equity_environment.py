from __future__ import annotations

import math
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Any
from urllib.parse import quote

from src.core.http import get_json
from src.core.io import read_json, write_json


SCHEMA_VERSION = "1.0.0"
COLLECTOR_VERSION = "korea-equity-environment-collector-v1.1-full-coverage-bootstrap"
MARKETS = ("KOSPI", "KOSDAQ")
INDEX_TICKERS = {
    "kospi200": "1028",
    "kosdaq150": "2203",
}
CREDIT_CACHE_NAME = "kr_corp_aa_minus_3y_equity_env"
CREDIT_STAT_CODE = "817Y002"
CREDIT_STAT_NAME = "1.3.2.1. 시장금리(일별)"
CREDIT_ITEM_TARGET = "회사채(3년, AA-)"


def _num(value: Any) -> float | None:
    if value is None:
        return None
    raw = str(value).strip().replace(",", "").replace("%", "")
    if not raw or raw in {"-", "--", "N/A", "null", "None", "nan", "NaN"}:
        return None
    try:
        out = float(raw)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _round(value: float | None, digits: int = 6) -> float | None:
    return round(float(value), digits) if value is not None and math.isfinite(float(value)) else None


def _safe_read(path: Path, default: Any) -> Any:
    try:
        return read_json(path) if path.exists() else default
    except Exception:
        return default


def _latest_completed_market_date() -> date:
    now = datetime.now(ZoneInfo("Asia/Seoul"))
    candidate = now.date()
    # KRX final investor data is published after the close; before 19:00 KST use
    # the prior business date so the scheduled morning run never requests an
    # incomplete same-day market snapshot.
    if now.hour < 19:
        candidate -= timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


def _business_window(calendar_days: int = 45) -> tuple[str, str]:
    end = _latest_completed_market_date()
    start = end - timedelta(days=max(28, int(calendar_days)))
    return start.strftime("%Y%m%d"), end.strftime("%Y%m%d")


def _frame_empty(frame: Any) -> bool:
    return frame is None or bool(getattr(frame, "empty", True))


def _row_label(row: Any) -> str:
    return str(row).strip().replace(" ", "")


def _find_index_label(frame: Any, candidates: tuple[str, ...]) -> Any | None:
    if _frame_empty(frame):
        return None
    normalized = {_row_label(idx): idx for idx in frame.index}
    for candidate in candidates:
        key = _row_label(candidate)
        if key in normalized:
            return normalized[key]
    for normalized_label, original in normalized.items():
        for candidate in candidates:
            if _row_label(candidate) in normalized_label:
                return original
    return None


def _collect_flow(stock: Any, start: str, end: str, market: str) -> dict[str, Any]:
    diagnostics: list[str] = []
    try:
        frame = stock.get_market_trading_value_by_investor(start, end, market)
    except Exception as exc:
        return {
            "available": False,
            "market": market,
            "diagnostics": [f"flow:{market}:{type(exc).__name__}:{str(exc)[:180]}"],
        }
    if _frame_empty(frame):
        return {"available": False, "market": market, "diagnostics": [f"flow:{market}:empty"]}

    foreign_label = _find_index_label(frame, ("외국인", "외국인합계"))
    institution_label = _find_index_label(frame, ("기관합계",))

    foreign_net = None
    institution_net = None
    if foreign_label is not None:
        foreign_net = _num(frame.loc[foreign_label].get("순매수"))

    if institution_label is not None:
        institution_net = _num(frame.loc[institution_label].get("순매수"))
    else:
        institution_parts = ("금융투자", "보험", "투신", "사모", "은행", "기타금융", "연기금")
        total = 0.0
        found = 0
        for label in institution_parts:
            idx = _find_index_label(frame, (label,))
            if idx is None:
                continue
            value = _num(frame.loc[idx].get("순매수"))
            if value is not None:
                total += value
                found += 1
        if found:
            institution_net = total
            diagnostics.append(f"institution_aggregate_from_parts:{found}")

    total_buy = 0.0
    buy_found = 0
    institution_detail_labels = {"금융투자", "보험", "투신", "사모", "은행", "기타금융", "연기금"}
    has_institution_detail = any(_row_label(idx) in institution_detail_labels for idx in frame.index)
    for idx, row in frame.iterrows():
        label = _row_label(idx)
        if label in {"전체", "합계"}:
            continue
        # Do not double-count institutions when both 기관합계 and detailed rows exist.
        if label == "기관합계" and has_institution_detail:
            continue
        value = _num(row.get("매수") if hasattr(row, "get") else None)
        if value is not None and value >= 0:
            total_buy += value
            buy_found += 1

    combined_net = None
    if foreign_net is not None or institution_net is not None:
        combined_net = float(foreign_net or 0.0) + float(institution_net or 0.0)
    combined_net_bps = (combined_net / total_buy * 10000.0) if combined_net is not None and total_buy > 0 else None
    foreign_net_bps = (foreign_net / total_buy * 10000.0) if foreign_net is not None and total_buy > 0 else None
    institution_net_bps = (institution_net / total_buy * 10000.0) if institution_net is not None and total_buy > 0 else None

    available = combined_net_bps is not None
    return {
        "available": available,
        "market": market,
        "period_start": start,
        "period_end": end,
        "foreign_net_value": _round(foreign_net, 0),
        "institution_net_value": _round(institution_net, 0),
        "combined_net_value": _round(combined_net, 0),
        "total_buy_value": _round(total_buy, 0) if total_buy > 0 else None,
        "foreign_net_bps_of_turnover": _round(foreign_net_bps, 3),
        "institution_net_bps_of_turnover": _round(institution_net_bps, 3),
        "combined_net_bps_of_turnover": _round(combined_net_bps, 3),
        "source": "KRX 투자자별 거래대금 (pykrx)",
        "source_url": "https://data.krx.co.kr/",
        "diagnostics": diagnostics + ([f"buy_rows:{buy_found}"] if buy_found else []),
    }


def _collect_breadth(stock: Any, start: str, end: str, market: str) -> dict[str, Any]:
    diagnostics: list[str] = []
    frame = None
    try:
        fn = getattr(stock, "get_market_price_change_by_ticker")
        frame = fn(start, end, market=market)
    except Exception as exc:
        diagnostics.append(f"price_change:{market}:{type(exc).__name__}:{str(exc)[:160]}")

    if _frame_empty(frame):
        try:
            fallback = stock.get_market_ohlcv_by_ticker(end, market=market)
            if not _frame_empty(fallback):
                frame = fallback
                diagnostics.append("fallback:single_day_ohlcv")
        except Exception as exc:
            diagnostics.append(f"ohlcv_fallback:{market}:{type(exc).__name__}:{str(exc)[:160]}")

    if _frame_empty(frame):
        return {"available": False, "market": market, "diagnostics": diagnostics + ["breadth_empty"]}

    change_column = None
    for candidate in ("등락률", "등락률(%)", "변동률"):
        if candidate in frame.columns:
            change_column = candidate
            break
    if change_column is None:
        return {
            "available": False,
            "market": market,
            "diagnostics": diagnostics + [f"breadth_change_column_missing:{list(frame.columns)}"],
        }

    values: list[float] = []
    for value in frame[change_column].tolist():
        parsed = _num(value)
        if parsed is not None:
            values.append(parsed)
    if not values:
        return {"available": False, "market": market, "diagnostics": diagnostics + ["breadth_no_values"]}

    advances = sum(1 for value in values if value > 0)
    declines = sum(1 for value in values if value < 0)
    unchanged = sum(1 for value in values if value == 0)
    valid = len(values)
    ad_balance = (advances - declines) / valid if valid else None
    advance_share = advances / valid if valid else None

    return {
        "available": True,
        "market": market,
        "period_start": start,
        "period_end": end,
        "valid_symbols": valid,
        "advances": advances,
        "declines": declines,
        "unchanged": unchanged,
        "advance_share": _round(advance_share, 6),
        "advance_decline_balance": _round(ad_balance, 6),
        "source": "KRX 종목 가격변동 (pykrx)",
        "source_url": "https://data.krx.co.kr/",
        "diagnostics": diagnostics,
    }


def _collect_valuation_history(stock: Any, start: str, end: str, ticker: str, label: str) -> dict[str, Any]:
    try:
        frame = stock.get_index_fundamental(start, end, ticker)
    except Exception as exc:
        return {
            "available": False,
            "label": label,
            "diagnostics": [f"valuation_history:{label}:{type(exc).__name__}:{str(exc)[:180]}"],
        }
    if _frame_empty(frame):
        return {"available": False, "label": label, "diagnostics": [f"valuation_history:{label}:empty"]}

    rows: list[dict[str, Any]] = []
    for idx, row in frame.iterrows():
        per = _num(row.get("PER") if hasattr(row, "get") else None)
        pbr = _num(row.get("PBR") if hasattr(row, "get") else None)
        if per is None and pbr is None:
            continue
        as_of = idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx)[:10]
        rows.append({"date": as_of, "per": _round(per, 4), "pbr": _round(pbr, 4)})
    rows = rows[-320:]
    return {
        "available": bool(rows),
        "label": label,
        "rows": rows,
        "sample_size": len(rows),
        "source": "KRX 지수 투자지표 (pykrx)",
        "source_url": "https://data.krx.co.kr/",
        "diagnostics": [],
    }


def _ecos_rows(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    node = payload.get("StatisticSearch")
    if not isinstance(node, dict):
        return []
    rows = node.get("row")
    return rows if isinstance(rows, list) else []


def _credit_incremental_start(previous_rows: list[dict[str, Any]]) -> str:
    latest = max((str(row.get("TIME") or "") for row in previous_rows if isinstance(row, dict)), default="")
    if len(latest) >= 8:
        try:
            dt = datetime.strptime(latest[:8], "%Y%m%d").date() - timedelta(days=45)
            return dt.strftime("%Y%m%d")
        except ValueError:
            pass
    return (date.today() - timedelta(days=550)).strftime("%Y%m%d")


def _merge_credit_rows(previous_rows: list[dict[str, Any]], fresh_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for row in previous_rows + fresh_rows:
        if not isinstance(row, dict):
            continue
        key = "|".join(
            [
                str(row.get("TIME") or ""),
                str(row.get("ITEM_CODE1") or ""),
                str(row.get("ITEM_NAME1") or ""),
            ]
        )
        merged[key] = row
    return sorted(merged.values(), key=lambda row: str(row.get("TIME") or ""))[-900:]


def _credit_name_key(value: Any) -> str:
    return str(value or "").upper().replace("−", "-").replace("–", "-").replace(" ", "")


def _resolve_credit_item(api_key: str, timeout: int) -> dict[str, str]:
    """Resolve the exact AA- 3Y corporate-bond item without guessing an ECOS code.

    The shared ECOS resolver intentionally strips punctuation for fuzzy matching,
    which can blur AA-/AA/AA+ rating symbols.  This equity-only resolver preserves
    '+'/'-' so the credit spread cannot silently bind to the wrong rating bucket.
    """
    cache_path = Path("cache/korea_equity_credit_resolved.json")
    cached = _safe_read(cache_path, {})
    if isinstance(cached, dict) and cached.get("stat_code") == CREDIT_STAT_CODE and cached.get("item_code1"):
        return {str(k): str(v) for k, v in cached.items()}

    url = "/".join(
        [
            "https://ecos.bok.or.kr/api/StatisticItemList",
            quote(api_key, safe=""),
            "json",
            "kr",
            "1",
            "10000",
            CREDIT_STAT_CODE,
        ]
    )
    payload = get_json(url, timeout=(4, max(6, min(timeout, 12))), retries=1)
    node = payload.get("StatisticItemList") if isinstance(payload, dict) else None
    rows = node.get("row", []) if isinstance(node, dict) else []
    if not isinstance(rows, list):
        rows = []

    exact_candidates: list[dict[str, Any]] = []
    broad_candidates: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("ITEM_NAME") or row.get("ITEM_NAME1") or "").strip()
        code = str(row.get("ITEM_CODE") or row.get("ITEM_CODE1") or "").strip()
        key = _credit_name_key(name)
        if not code or "회사채" not in name or "3년" not in name:
            continue
        if "AA-" in key:
            exact_candidates.append({"name": name, "code": code})
        elif "AA" in key:
            broad_candidates.append({"name": name, "code": code})

    if not exact_candidates:
        seen = ", ".join(item["name"] for item in broad_candidates[:8])
        raise RuntimeError("ECOS 회사채 3년 AA- 항목을 정확히 식별하지 못했습니다." + (f" AA계열 후보={seen}" if seen else ""))
    chosen = exact_candidates[0]
    resolved = {
        "stat_code": CREDIT_STAT_CODE,
        "stat_name": CREDIT_STAT_NAME,
        "cycle": "D",
        "item_code1": chosen["code"],
        "item_name1": chosen["name"],
        "item_code2": "?",
        "item_code3": "?",
    }
    write_json(cache_path, resolved)
    return resolved


def _collect_credit_rate(output_dir: Path, timeout: int) -> dict[str, Any]:
    raw_path = output_dir / "korea_equity_credit_raw.json"
    previous = _safe_read(raw_path, {})
    previous_rows = list(previous.get("rows") or []) if isinstance(previous, dict) else []
    api_key = os.getenv("ECOS_API_KEY", "").strip()
    if not api_key:
        return {
            "available": bool(previous_rows),
            "stale": bool(previous_rows),
            "rows": previous_rows,
            "reason": "ECOS_API_KEY_MISSING",
        }

    diagnostics: list[str] = []
    try:
        resolution = _resolve_credit_item(api_key, timeout)
        start_text = _credit_incremental_start(previous_rows)
        end_text = date.today().strftime("%Y%m%d")
        parts = [
            api_key,
            "json",
            "kr",
            "1",
            "1000",
            resolution["stat_code"],
            resolution["cycle"],
            start_text,
            end_text,
            resolution["item_code1"],
            resolution.get("item_code2") or "?",
            resolution.get("item_code3") or "?",
        ]
        encoded = [quote(str(value).strip("/"), safe="?") for value in parts]
        url = "https://ecos.bok.or.kr/api/StatisticSearch/" + "/".join(encoded)
        fresh_rows = _ecos_rows(get_json(url, timeout=(4, max(6, min(timeout, 12))), retries=1))
        rows = _merge_credit_rows(previous_rows, fresh_rows)
        payload = {
            "schema_version": "1.0.0",
            "series": CREDIT_CACHE_NAME,
            "resolution": resolution,
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "rows": rows,
            "fresh_rows": len(fresh_rows),
            "stale": bool(rows and not fresh_rows),
            "source": "한국은행 ECOS 시장금리(일별)",
            "source_url": "https://ecos.bok.or.kr/",
            "diagnostics": diagnostics,
        }
        write_json(raw_path, payload)
        return {
            "available": bool(rows),
            "stale": bool(rows and not fresh_rows),
            "rows": rows,
            "resolution": resolution,
            "source": payload["source"],
            "source_url": payload["source_url"],
            "diagnostics": diagnostics,
        }
    except Exception as exc:
        diagnostics.append(f"credit_ecos:{type(exc).__name__}:{str(exc)[:220]}")
        return {
            "available": bool(previous_rows),
            "stale": bool(previous_rows),
            "rows": previous_rows,
            "reason": "LIVE_FAILED_LAST_GOOD_REUSED" if previous_rows else "LIVE_FAILED",
            "diagnostics": diagnostics,
        }


def _latest_series_value(rows: list[dict[str, Any]]) -> dict[str, Any]:
    usable: list[tuple[str, float]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        value = _num(row.get("DATA_VALUE"))
        period = str(row.get("TIME") or "")
        if value is not None and period:
            usable.append((period, value))
    if not usable:
        return {"value": None, "time": None}
    period, value = max(usable, key=lambda item: item[0])
    return {"value": value, "time": period}


def _reuse_section(current: dict[str, Any], previous: dict[str, Any], label: str) -> dict[str, Any]:
    if current.get("available"):
        current["stale"] = False
        return current
    if previous.get("available"):
        reused = dict(previous)
        reused["stale"] = True
        reused["live_diagnostics"] = current.get("diagnostics") or []
        reused.setdefault("diagnostics", []).append(f"{label}:live_failed_last_good_reused")
        return reused
    return current


def collect(output_dir: Path, timeout: int = 20) -> dict[str, Any]:
    """Collect only the Korea-equity-specific signals used by the stock-environment card.

    This module is additive by design: it does not modify any existing Korea rate/FX
    JSON contract.  Failure is isolated by the caller and each live section can reuse
    the previous committed section without fabricating fresh values.
    """

    previous_path = output_dir / "raw_korea_equity_environment.json"
    previous = _safe_read(previous_path, {})
    start, end = _business_window(35)

    try:
        from pykrx import stock  # type: ignore
    except Exception as exc:
        stock = None
        import_error = f"pykrx_import:{type(exc).__name__}:{exc}"
    else:
        import_error = ""

    flows: dict[str, Any] = {}
    breadth: dict[str, Any] = {}
    valuation_history: dict[str, Any] = {}

    if stock is not None:
        for market in MARKETS:
            current_flow = _collect_flow(stock, start, end, market)
            flows[market.lower()] = _reuse_section(
                current_flow,
                ((previous.get("flows") or {}).get(market.lower()) or {}),
                f"flow_{market.lower()}",
            )
            current_breadth = _collect_breadth(stock, start, end, market)
            breadth[market.lower()] = _reuse_section(
                current_breadth,
                ((previous.get("breadth") or {}).get(market.lower()) or {}),
                f"breadth_{market.lower()}",
            )

        hist_end_date = datetime.strptime(end, "%Y%m%d").date()
        hist_start = (hist_end_date - timedelta(days=550)).strftime("%Y%m%d")
        for key, ticker in INDEX_TICKERS.items():
            current_hist = _collect_valuation_history(stock, hist_start, end, ticker, key)
            valuation_history[key] = _reuse_section(
                current_hist,
                ((previous.get("valuation_history") or {}).get(key) or {}),
                f"valuation_{key}",
            )
    else:
        for market in MARKETS:
            key = market.lower()
            flows[key] = _reuse_section(
                {"available": False, "market": market, "diagnostics": [import_error]},
                ((previous.get("flows") or {}).get(key) or {}),
                f"flow_{key}",
            )
            breadth[key] = _reuse_section(
                {"available": False, "market": market, "diagnostics": [import_error]},
                ((previous.get("breadth") or {}).get(key) or {}),
                f"breadth_{key}",
            )
        for key in INDEX_TICKERS:
            valuation_history[key] = _reuse_section(
                {"available": False, "label": key, "diagnostics": [import_error]},
                ((previous.get("valuation_history") or {}).get(key) or {}),
                f"valuation_{key}",
            )

    credit = _collect_credit_rate(output_dir, timeout)
    gov_rows = list((_safe_read(output_dir / "raw_ecos.json", {}) or {}).get("kr_gov_3y") or [])
    corp_rows = list(credit.get("rows") or [])
    corp_by_time = {
        str(row.get("TIME") or ""): _num(row.get("DATA_VALUE"))
        for row in corp_rows if isinstance(row, dict) and row.get("TIME")
    }
    gov_by_time = {
        str(row.get("TIME") or ""): _num(row.get("DATA_VALUE"))
        for row in gov_rows if isinstance(row, dict) and row.get("TIME")
    }
    common_times = sorted(
        time_key for time_key in set(corp_by_time).intersection(gov_by_time)
        if corp_by_time.get(time_key) is not None and gov_by_time.get(time_key) is not None
    )
    common_time = common_times[-1] if common_times else None
    corp_latest = {"value": corp_by_time.get(common_time), "time": common_time}
    gov_latest = {"value": gov_by_time.get(common_time), "time": common_time}
    spread = None
    if common_time and corp_latest.get("value") is not None and gov_latest.get("value") is not None:
        spread = float(corp_latest["value"]) - float(gov_latest["value"])

    # Bootstrap the credit-spread percentile immediately from the historical rows
    # already fetched in this same run.  This removes the old 20-run waiting period
    # without adding a new ECOS request: corporate and government yields are paired
    # only on identical dates, so the history is comparable and auditable.
    spread_history = []
    for time_key in common_times[-400:]:
        corp_value = corp_by_time.get(time_key)
        gov_value = gov_by_time.get(time_key)
        if corp_value is None or gov_value is None:
            continue
        spread_history.append({
            "date": time_key,
            "corp_aa_minus_3y_pct": _round(corp_value, 4),
            "gov_3y_pct": _round(gov_value, 4),
            "spread_pct_point": _round(float(corp_value) - float(gov_value), 4),
        })

    result = {
        "schema_version": SCHEMA_VERSION,
        "collector_version": COLLECTOR_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "period_start": start,
        "period_end": end,
        "flows": flows,
        "breadth": breadth,
        "valuation_history": valuation_history,
        "credit": {
            "available": spread is not None,
            "corp_aa_minus_3y_pct": _round(corp_latest.get("value"), 4),
            "corp_as_of": corp_latest.get("time"),
            "gov_3y_pct": _round(gov_latest.get("value"), 4),
            "gov_as_of": gov_latest.get("time"),
            "spread_pct_point": _round(spread, 4),
            "spread_history": spread_history,
            "history_samples": len(spread_history),
            "stale": bool(credit.get("stale")),
            "source": credit.get("source") or "한국은행 ECOS",
            "source_url": credit.get("source_url") or "https://ecos.bok.or.kr/",
            "diagnostics": credit.get("diagnostics") or [],
        },
        "source_status": {
            "pykrx_available": stock is not None,
            "krx_login_configured": bool(os.getenv("KRX_ID") and os.getenv("KRX_PW")),
            "ecos_key_configured": bool(os.getenv("ECOS_API_KEY")),
            "live_call_budget": {
                "krx_flow_calls": 2,
                "krx_breadth_calls": 2,
                "krx_valuation_history_calls": 2,
                "ecos_credit_calls": 1,
                "fallback_calls_only_on_failure": True,
            },
        },
        "limitations": [
            "외국인·기관 수급은 최근 약 1개월(35일 달력창)의 KRX 거래대금을 거래대금 대비 bp로 정규화합니다.",
            "breadth는 KOSPI/KOSDAQ 전체 종목의 기간 등락률 상승·하락 종목 비율을 사용하며, 종목별 200일 이동평균 계산처럼 호출량이 큰 방식은 사용하지 않습니다.",
            "밸류에이션은 KOSPI200/KOSDAQ150의 KRX PER·PBR 과거 분포를 사용합니다.",
            "이익환경은 기존 korea_asset_fundamentals의 forward 성장 대용치를 재사용합니다. 초기 이력 5회 전에는 성장 전망 수준을 사용하고, 이후에는 실제 누적 revision 변화로 자동 전환합니다.",
            "신용스프레드는 한국은행 ECOS의 회사채 3년 AA-와 국고채 3년을 같은 날짜끼리 매칭해 현재값과 과거분포를 한 번에 계산하며, 회사채 항목코드는 ECOS 메타데이터 resolver가 최초 1회 확인 후 캐시합니다.",
            "이 전용 raw 출력은 기존 금리·환율·유동성·원화강도 출력 스키마를 변경하지 않습니다.",
        ],
    }
    write_json(previous_path, result)
    return result
