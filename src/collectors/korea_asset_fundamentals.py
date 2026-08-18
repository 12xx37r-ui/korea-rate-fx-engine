from __future__ import annotations

import math
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

from src.core.io import read_json, write_json

INDEX_CONFIG = {
    "kospi200": {
        "name": "코스피 200",
        "ticker": "1028",
        "market": "KOSPI",
        "max_constituents": 24,
        "minimum_market_cap_coverage": 0.30,
        "minimum_samples": 5,
    },
    "kosdaq150": {
        "name": "코스닥 150",
        "ticker": "2203",
        "market": "KOSDAQ",
        "max_constituents": 24,
        "minimum_market_cap_coverage": 0.25,
        "minimum_samples": 5,
    },
}

REIT_CODE = "329200"
REIT_ISIN = "KR7329200000"
NAVER_INTEGRATION_URL = "https://m.stock.naver.com/api/stock/{code}/integration"
NAVER_REALTIME_URL = "https://polling.finance.naver.com/api/realtime/domestic/stock/{code}"
NAVER_REIT_PAGE = "https://finance.naver.com/item/main.naver?code=329200"
INVESTING_REIT_URL = "https://kr.investing.com/etfs/mirae-asset-tiger-reit-infra-hidiv"
THERICH_REIT_URL = "https://www.therich.io/stock?country=KR&ticker=329200"

MIRAE_DISTRIBUTION_ENDPOINTS = (
    (
        "https://investments.miraeasset.com/tigeretf/ko/product/search/detail/refDiv.do",
        "https://investments.miraeasset.com/tigeretf/ko/product/search/detail/index.do?ksdFund=" + REIT_ISIN,
    ),
    (
        "https://www.tigeretf.com/ko/product/search/detail/refDiv.do",
        "https://www.tigeretf.com/ko/product/search/detail/index.do?ksdFund=" + REIT_ISIN,
    ),
)


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




def _naver_metric_num(value: Any) -> float | None:
    """Parse NAVER metric strings such as '26.86배', '396원', '9.7%'.

    This is deliberately separate from the generic numeric parser so market-cap
    strings containing 조/억 are never accidentally interpreted as plain numbers.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return _num(value)
    raw = str(value).strip().replace(',', '')
    if not raw or raw in {'-', '--', 'N/A', 'null', 'None', 'nan', 'NaN'}:
        return None
    m = re.search(r'[-+]?(?:\d+(?:\.\d*)?|\.\d+)', raw)
    if not m:
        return None
    try:
        v = float(m.group(0))
    except ValueError:
        return None
    return v if math.isfinite(v) else None

def _positive_num(value: Any) -> float | None:
    out = _num(value)
    return out if out is not None and out > 0 else None


def _round(value: float | None, digits: int = 4) -> float | None:
    return round(float(value), digits) if value is not None else None


def _business_dates(days: int = 14) -> list[str]:
    out: list[str] = []
    d = date.today() + timedelta(days=1)
    for _ in range(days * 3):
        d -= timedelta(days=1)
        if d.weekday() < 5:
            out.append(d.strftime("%Y%m%d"))
            if len(out) >= days:
                break
    return out


def _safe_text(response: requests.Response) -> str:
    response.encoding = response.apparent_encoding or response.encoding or "utf-8"
    return response.text


def _latest_dataframe_row(df: Any) -> tuple[str, dict[str, Any]] | None:
    if df is None or getattr(df, "empty", True):
        return None
    row = df.iloc[-1]
    idx = df.index[-1]
    as_of = idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx)[:10]
    return as_of, {str(k): row[k] for k in df.columns}


def _find_metric(row: dict[str, Any], keys: list[str]) -> float | None:
    for key in keys:
        value = _positive_num(row.get(key))
        if value is not None:
            return value
    return None


def _pykrx_index(cfg: dict[str, Any]) -> dict[str, Any]:
    """Collect official current index PER/PBR/dividend yield from KRX."""
    diagnostics: list[str] = []
    try:
        from pykrx import stock  # type: ignore
    except Exception as exc:
        return {
            "available": False,
            "diagnostics": [f"pykrx_import:{type(exc).__name__}:{exc}"],
        }

    has_login = bool(os.getenv("KRX_ID") and os.getenv("KRX_PW"))
    for end in _business_dates(12):
        start = (datetime.strptime(end, "%Y%m%d") - timedelta(days=10)).strftime("%Y%m%d")
        try:
            df = stock.get_index_fundamental(start, end, cfg["ticker"])
            latest = _latest_dataframe_row(df)
            if not latest:
                diagnostics.append(f"{end}:pykrx_empty")
                continue
            as_of, row = latest
            columns = list(row.keys())
            diagnostics.append(f"{end}:pykrx_columns:{columns}")

            per = _find_metric(row, ["PER", "지수PER", "PER(배)", "PER(배율)"])
            pbr = _find_metric(row, ["PBR", "지수PBR", "PBR(배)", "PBR(배율)"])
            dividend_yield = _find_metric(row, ["배당수익률", "배당수익률(%)", "배당률"])
            official_forward = _find_metric(row, ["선행PER", "12개월선행PER", "Forward PER"])

            if any(v is not None for v in (per, pbr, dividend_yield)):
                return {
                    "per": _round(per),
                    "official_forward_per": _round(official_forward),
                    "pbr": _round(pbr),
                    "dividend_yield": _round(dividend_yield),
                    "as_of": as_of,
                    "source": "KRX 지수 투자지표 (pykrx 세션)",
                    "source_url": "https://data.krx.co.kr/",
                    "source_method": "pykrx_authenticated" if has_login else "pykrx_session",
                    "available": True,
                    "stale": False,
                    "diagnostics": diagnostics[-10:],
                }
            diagnostics.append(f"{end}:pykrx_fields_empty:{columns}")
        except Exception as exc:
            diagnostics.append(f"{end}:pykrx:{type(exc).__name__}:{str(exc)[:180]}")
    return {"available": False, "diagnostics": diagnostics[-12:]}


def _extract_naver_total_infos(payload: dict[str, Any]) -> dict[str, Any]:
    rows = payload.get("totalInfos") or payload.get("totalInfo") or []
    out: dict[str, Any] = {}
    if isinstance(rows, dict):
        rows = rows.values()
    for row in rows if isinstance(rows, (list, tuple)) else []:
        if not isinstance(row, dict):
            continue
        key = str(row.get("code") or row.get("key") or "").strip()
        if key:
            value = row.get("value")
            out[key] = value
            out[key.lower()] = value
    return out


def _info_value(infos: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in infos:
            return infos[key]
        lowered = key.lower()
        if lowered in infos:
            return infos[lowered]
    return None


def _naver_integration_metrics(
    code: str,
    timeout: int,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    diagnostics: list[str] = []
    own_session = session is None
    client = session or requests.Session()
    try:
        response = client.get(
            NAVER_INTEGRATION_URL.format(code=code),
            timeout=max(5, min(timeout, 12)),
            headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131 Safari/537.36",
                "Accept": "application/json,text/plain,*/*",
                "Referer": f"https://m.stock.naver.com/domestic/stock/{code}/total",
            },
        )
        response.raise_for_status()
        payload = response.json()
        infos = _extract_naver_total_infos(payload)
        return {
            "code": code,
            "per": _naver_metric_num(_info_value(infos, "per")),
            "eps": _naver_metric_num(_info_value(infos, "eps")),
            "forward_per": _naver_metric_num(_info_value(infos, "cnsPer", "forwardPer")),
            "forward_eps": _naver_metric_num(_info_value(infos, "cnsEps", "forwardEps")),
            "pbr": _naver_metric_num(_info_value(infos, "pbr")),
            "dividend_yield": _naver_metric_num(
                _info_value(infos, "dividendYieldRatio", "dividendYield")
            ),
            "dividend": _naver_metric_num(_info_value(infos, "dividend", "dividendPerShare")),
            "last_close_price": _naver_metric_num(
                _info_value(infos, "lastClosePrice", "closePrice")
            ),
            "available": bool(infos),
            "diagnostics": diagnostics,
        }
    except Exception as exc:
        diagnostics.append(f"naver_integration:{code}:{type(exc).__name__}:{str(exc)[:150]}")
        return {"code": code, "available": False, "diagnostics": diagnostics}
    finally:
        if own_session:
            client.close()


def _get_index_constituents_and_caps(
    cfg: dict[str, Any],
    as_of: str,
) -> dict[str, Any]:
    diagnostics: list[str] = []
    try:
        from pykrx import stock  # type: ignore
    except Exception as exc:
        return {
            "available": False,
            "diagnostics": [f"pykrx_import:{type(exc).__name__}:{exc}"],
        }

    compact = re.sub(r"[^0-9]", "", as_of or "")
    constituents: list[str] = []
    try:
        if compact:
            try:
                constituents = list(stock.get_index_portfolio_deposit_file(cfg["ticker"], compact))
            except TypeError:
                constituents = list(stock.get_index_portfolio_deposit_file(cfg["ticker"]))
        else:
            constituents = list(stock.get_index_portfolio_deposit_file(cfg["ticker"]))
    except Exception as exc:
        diagnostics.append(f"index_constituents:{type(exc).__name__}:{str(exc)[:160]}")

    constituents = [str(x).zfill(6) for x in constituents if str(x).strip()]
    if not constituents:
        return {"available": False, "diagnostics": diagnostics + ["index_constituents_empty"]}

    market_caps: dict[str, float] = {}
    candidate_dates = [compact] if compact else []
    candidate_dates += [x for x in _business_dates(5) if x not in candidate_dates]
    for trading_date in candidate_dates:
        try:
            frame = stock.get_market_cap_by_ticker(trading_date, market=cfg["market"])
            if frame is None or getattr(frame, "empty", True):
                diagnostics.append(f"{trading_date}:market_cap_empty")
                continue
            normalized_rows: dict[str, Any] = {}
            for raw_index, raw_row in frame.iterrows():
                normalized_rows[str(raw_index).strip().zfill(6)] = raw_row
            for ticker in constituents:
                row = normalized_rows.get(ticker)
                if row is None:
                    continue
                value = _positive_num(row.get("시가총액") if hasattr(row, "get") else None)
                if value is not None:
                    market_caps[ticker] = value
            if market_caps:
                compact = trading_date
                break
        except Exception as exc:
            diagnostics.append(f"{trading_date}:market_cap:{type(exc).__name__}:{str(exc)[:150]}")

    if not market_caps:
        return {
            "available": False,
            "constituent_count": len(constituents),
            "diagnostics": diagnostics + ["market_cap_unavailable"],
        }

    ranked = sorted(market_caps.items(), key=lambda item: item[1], reverse=True)
    return {
        "available": True,
        "as_of": f"{compact[:4]}-{compact[4:6]}-{compact[6:8]}" if len(compact) == 8 else as_of,
        "constituent_count": len(constituents),
        "ranked_caps": ranked,
        "total_market_cap": sum(market_caps.values()),
        "diagnostics": diagnostics[-8:],
    }


def _aggregate_forward_proxy(
    ranked_caps: list[tuple[str, float]],
    total_market_cap: float,
    metrics_by_code: dict[str, dict[str, Any]],
    minimum_coverage: float,
    minimum_samples: int,
) -> dict[str, Any]:
    usable_forward: list[tuple[str, float, float]] = []
    usable_growth: list[tuple[str, float, float, float]] = []
    diagnostics: list[str] = []

    for code, cap in ranked_caps:
        row = metrics_by_code.get(code) or {}
        forward_per = _positive_num(row.get("forward_per"))
        current_per = _positive_num(row.get("per"))
        if forward_per is not None and 1 <= forward_per <= 300:
            usable_forward.append((code, cap, forward_per))
        if (
            forward_per is not None
            and current_per is not None
            and 1 <= current_per <= 500
            and 1 <= forward_per <= 300
        ):
            usable_growth.append((code, cap, current_per, forward_per))
        diagnostics.extend(row.get("diagnostics") or [])

    covered_cap = sum(cap for _, cap, _ in usable_forward)
    coverage = covered_cap / total_market_cap if total_market_cap > 0 else 0.0
    sample_size = len(usable_forward)

    forward_per = None
    if sample_size >= minimum_samples and coverage >= minimum_coverage:
        earnings_forward = sum(cap / pe for _, cap, pe in usable_forward)
        if earnings_forward > 0:
            forward_per = covered_cap / earnings_forward

    eps_growth_pct = None
    growth_covered_cap = sum(cap for _, cap, _, _ in usable_growth)
    growth_coverage = growth_covered_cap / total_market_cap if total_market_cap > 0 else 0.0
    if len(usable_growth) >= minimum_samples and growth_coverage >= minimum_coverage:
        current_earnings = sum(cap / current_pe for _, cap, current_pe, _ in usable_growth)
        forward_earnings = sum(cap / forward_pe for _, cap, _, forward_pe in usable_growth)
        if current_earnings > 0 and forward_earnings > 0:
            growth = (forward_earnings / current_earnings - 1.0) * 100.0
            if -80 <= growth <= 300:
                eps_growth_pct = growth

    available = forward_per is not None or eps_growth_pct is not None
    if not available:
        diagnostics.append(
            f"forward_proxy_insufficient:sample={sample_size},coverage={coverage:.3f},"
            f"required_sample={minimum_samples},required_coverage={minimum_coverage:.3f}"
        )

    return {
        "forward_per": _round(forward_per),
        "eps_growth_pct": _round(eps_growth_pct),
        "available": available,
        "sample_size": sample_size,
        "market_cap_coverage": _round(coverage),
        "growth_market_cap_coverage": _round(growth_coverage),
        "proxy_symbols": [code for code, _, _ in usable_forward],
        "source": "KRX 지수 구성종목 + NAVER 공개 컨센서스" if available else None,
        "source_url": "https://m.stock.naver.com/" if available else None,
        "source_method": "constituent_market_cap_weighted_consensus" if available else None,
        "diagnostics": diagnostics[-16:],
    }


def _collect_forward_proxy(cfg: dict[str, Any], current_as_of: str, timeout: int) -> dict[str, Any]:
    universe = _get_index_constituents_and_caps(cfg, current_as_of)
    if not universe.get("available"):
        return {
            "available": False,
            "forward_per": None,
            "eps_growth_pct": None,
            "proxy_symbols": [],
            "sample_size": 0,
            "market_cap_coverage": 0.0,
            "diagnostics": universe.get("diagnostics") or [],
        }

    ranked_all = list(universe.get("ranked_caps") or [])
    selected = ranked_all[: int(cfg.get("max_constituents") or 24)]
    metrics_by_code: dict[str, dict[str, Any]] = {}
    workers = min(4, max(2, len(selected)))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(_naver_integration_metrics, code, timeout): code
            for code, _ in selected
        }
        for future in as_completed(futures):
            code = futures[future]
            try:
                metrics_by_code[code] = future.result()
            except Exception as exc:
                metrics_by_code[code] = {
                    "code": code,
                    "available": False,
                    "diagnostics": [f"naver_future:{type(exc).__name__}:{str(exc)[:120]}"],
                }

    aggregate = _aggregate_forward_proxy(
        selected,
        float(universe.get("total_market_cap") or 0),
        metrics_by_code,
        float(cfg.get("minimum_market_cap_coverage") or 0.25),
        int(cfg.get("minimum_samples") or 5),
    )
    aggregate["constituent_count"] = int(universe.get("constituent_count") or 0)
    aggregate["queried_constituent_count"] = len(selected)
    aggregate["as_of"] = universe.get("as_of") or current_as_of
    aggregate["diagnostics"] = (
        (universe.get("diagnostics") or []) + (aggregate.get("diagnostics") or [])
    )[-20:]
    return aggregate


def _merge_index_sources(current: dict[str, Any], forward_proxy: dict[str, Any]) -> dict[str, Any]:
    per = _positive_num(current.get("per"))
    pbr = _positive_num(current.get("pbr"))
    dividend_yield = _positive_num(current.get("dividend_yield"))
    official_forward = _positive_num(current.get("official_forward_per"))
    proxy_forward = _positive_num(forward_proxy.get("forward_per"))
    forward_per = official_forward or proxy_forward
    eps_growth_pct = _num(forward_proxy.get("eps_growth_pct"))
    if eps_growth_pct is not None and not (-80 <= eps_growth_pct <= 300):
        eps_growth_pct = None

    growth_adjusted_per = None
    if forward_per is not None and eps_growth_pct is not None and eps_growth_pct > 0:
        growth_adjusted_per = forward_per / eps_growth_pct

    diagnostics = (current.get("diagnostics") or []) + (forward_proxy.get("diagnostics") or [])
    source_names = [current.get("source")]
    source_methods = [current.get("source_method")]
    if official_forward is None and forward_proxy.get("available"):
        source_names.append(forward_proxy.get("source"))
        source_methods.append(forward_proxy.get("source_method"))

    coverage_fields = (per, pbr, dividend_yield)
    return {
        "per": _round(per),
        "forward_per": _round(forward_per),
        "eps_growth_pct": _round(eps_growth_pct),
        "growth_adjusted_per": _round(growth_adjusted_per),
        "pbr": _round(pbr),
        "dividend_yield": _round(dividend_yield),
        "available": any(v is not None for v in coverage_fields),
        "coverage": sum(v is not None for v in coverage_fields) / 3,
        "forward_data_available": forward_per is not None or eps_growth_pct is not None,
        "forward_data_is_official": official_forward is not None,
        "forward_proxy_sample_size": int(forward_proxy.get("sample_size") or 0),
        "forward_proxy_market_cap_coverage": _round(_num(forward_proxy.get("market_cap_coverage"))),
        "forward_proxy_growth_market_cap_coverage": _round(
            _num(forward_proxy.get("growth_market_cap_coverage"))
        ),
        "as_of": current.get("as_of") or forward_proxy.get("as_of") or date.today().isoformat(),
        "source": " + ".join(dict.fromkeys(str(x) for x in source_names if x)) or None,
        "source_url": current.get("source_url") or forward_proxy.get("source_url"),
        "source_method": "+".join(dict.fromkeys(str(x) for x in source_methods if x)) or None,
        "proxy_symbols": forward_proxy.get("proxy_symbols") or [],
        "stale": False,
        "diagnostics": diagnostics[-24:],
    }


def _extract_distributions(html: str) -> list[dict[str, Any]]:
    """Parse ETF distribution date/amount rows from official table HTML."""
    rows: list[dict[str, Any]] = []
    normalized = re.sub(r"<br\s*/?>", " ", html, flags=re.I)
    tr_blocks = re.findall(r"<tr[^>]*>([\s\S]*?)</tr>", normalized, flags=re.I)

    for block in tr_blocks:
        cells = [
            re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", cell)).strip()
            for cell in re.findall(r"<t[dh][^>]*>([\s\S]*?)</t[dh]>", block, flags=re.I)
        ]
        text = " | ".join(cells) if cells else re.sub(r"<[^>]+>", " ", block)
        text = re.sub(r"\s+", " ", text).strip()
        date_match = re.search(r"(20\d{2})[./-](\d{1,2})[./-](\d{1,2})", text)
        if not date_match:
            continue

        try:
            dt = date(
                int(date_match.group(1)),
                int(date_match.group(2)),
                int(date_match.group(3)),
            )
        except ValueError:
            continue

        amount: float | None = None
        # Official table order: base date, payment date, cash amount, tax base.
        # Prefer the penultimate/last plausible amount cells, excluding rates.
        for cell in reversed(cells[1:]):
            if "%" in cell:
                continue
            values = [_positive_num(x) for x in re.findall(r"[0-9,]+(?:\.[0-9]+)?", cell)]
            plausible = [x for x in values if x is not None and 1 <= x <= 5000]
            if plausible:
                amount = plausible[-1]
                break
        if amount is None:
            continue
        rows.append({"date": dt.isoformat(), "amount": float(amount)})

    unique = {(item["date"], item["amount"]): item for item in rows}
    return sorted(unique.values(), key=lambda item: item["date"])


def _fetch_mirae_distribution_history(
    session: requests.Session,
    timeout: int,
) -> dict[str, Any]:
    diagnostics: list[str] = []
    for endpoint, referer in MIRAE_DISTRIBUTION_ENDPOINTS:
        try:
            # Prime cookies. Failure here does not block the AJAX endpoint attempt.
            try:
                session.get(
                    referer,
                    timeout=max(5, min(timeout, 10)),
                    headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html,*/*"},
                )
            except Exception as exc:
                diagnostics.append(f"mirae_prime:{type(exc).__name__}:{str(exc)[:100]}")

            response = session.get(
                endpoint,
                params={"ksdFund": REIT_ISIN, "listCnt": 100, "pageIndex": 1},
                timeout=max(5, min(timeout, 15)),
                headers={
                    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131 Safari/537.36",
                    "Accept": "text/html,application/xhtml+xml,application/json,text/plain,*/*",
                    "Referer": referer,
                    "X-Requested-With": "XMLHttpRequest",
                    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
                },
            )
            response.raise_for_status()
            html = _safe_text(response)
            rows = _extract_distributions(html)
            if rows:
                return {
                    "available": True,
                    "rows": rows,
                    "source": "미래에셋 TIGER 공식 분배금 지급현황",
                    "source_url": str(response.url),
                    "source_method": "mirae_official_distribution_history",
                    "diagnostics": diagnostics,
                }
            diagnostics.append(f"mirae_official_rows_empty:{endpoint}")
        except Exception as exc:
            diagnostics.append(f"mirae_official:{type(exc).__name__}:{str(exc)[:160]}")
    return {"available": False, "rows": [], "diagnostics": diagnostics[-10:]}


def _naver_realtime_price(session: requests.Session, timeout: int) -> float | None:
    try:
        response = session.get(
            NAVER_REALTIME_URL.format(code=REIT_CODE),
            timeout=max(5, min(timeout, 10)),
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json,*/*"},
        )
        response.raise_for_status()
        payload = response.json()
        rows = payload.get("datas") or []
        if rows:
            return _positive_num(rows[0].get("closePrice"))
    except Exception:
        pass
    return None


def _strip_html(html: str) -> str:
    text = re.sub(r"<script[^>]*>[\s\S]*?</script>", " ", html, flags=re.I)
    text = re.sub(r"<style[^>]*>[\s\S]*?</style>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _extract_public_reit_snapshot(html: str) -> dict[str, float | None]:
    text = _strip_html(html)
    yield_value: float | None = None
    ttm_distribution: float | None = None
    price: float | None = None

    yield_patterns = (
        r'"marketDividendRatio"\s*:\s*"?([0-9]+(?:\.[0-9]+)?)',
        r'"dividendYield(?:Ratio)?"\s*:\s*"?([0-9]+(?:\.[0-9]+)?)',
        r'배당수익률.{0,100}?([0-9]+(?:\.[0-9]+)?)\s*%',
        r'Dividend Yield.{0,100}?([0-9]+(?:\.[0-9]+)?)\s*%',
    )
    distribution_patterns = (
        r'"dividendPerShare"\s*:\s*"?([0-9,]+(?:\.[0-9]+)?)',
        r'배당금\s*\(최근\s*12개월\).{0,100}?([0-9,]+(?:\.[0-9]+)?)',
        r'Annualized Payout.{0,100}?([0-9,]+(?:\.[0-9]+)?)',
    )
    price_patterns = (
        r'"closePrice"\s*:\s*"?([0-9,]+(?:\.[0-9]+)?)',
        r'"currentPrice"\s*:\s*"?([0-9,]+(?:\.[0-9]+)?)',
    )

    for pattern in yield_patterns:
        match = re.search(pattern, html if pattern.startswith('"') else text, flags=re.I)
        if match:
            candidate = _positive_num(match.group(1))
            if candidate is not None and 0 < candidate < 100:
                yield_value = candidate
                break
    for pattern in distribution_patterns:
        match = re.search(pattern, html if pattern.startswith('"') else text, flags=re.I)
        if match:
            candidate = _positive_num(match.group(1))
            if candidate is not None and 1 <= candidate <= 10000:
                ttm_distribution = candidate
                break
    for pattern in price_patterns:
        match = re.search(pattern, html, flags=re.I)
        if match:
            candidate = _positive_num(match.group(1))
            if candidate is not None and 100 <= candidate <= 1000000:
                price = candidate
                break

    return {
        "distribution_yield": yield_value,
        "trailing_12m_distribution": ttm_distribution,
        "current_price": price,
    }


def _fetch_public_reit_snapshot(
    session: requests.Session,
    timeout: int,
) -> dict[str, Any]:
    diagnostics: list[str] = []
    sources = (
        (INVESTING_REIT_URL, "Investing.com 공개 ETF 지표", "investing_public_reit_snapshot"),
        (THERICH_REIT_URL, "더리치 공개 ETF 지표", "therich_public_reit_snapshot"),
    )
    for url, source, method in sources:
        try:
            response = session.get(
                url,
                timeout=max(5, min(timeout, 12)),
                headers={
                    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131 Safari/537.36",
                    "Accept": "text/html,application/xhtml+xml,*/*",
                    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
                },
            )
            response.raise_for_status()
            snapshot = _extract_public_reit_snapshot(_safe_text(response))
            if snapshot.get("distribution_yield") is not None:
                return {
                    "available": True,
                    **snapshot,
                    "source": source,
                    "source_url": str(response.url),
                    "source_method": method,
                    "diagnostics": diagnostics,
                }
            diagnostics.append(f"public_reit_snapshot_empty:{url}")
        except Exception as exc:
            diagnostics.append(f"public_reit_snapshot:{type(exc).__name__}:{str(exc)[:150]}")
    return {"available": False, "diagnostics": diagnostics[-8:]}


def _collect_reit(session: requests.Session, timeout: int) -> dict[str, Any]:
    official = _fetch_mirae_distribution_history(session, timeout)
    snapshot = _naver_integration_metrics(REIT_CODE, timeout, session=session)
    diagnostics = (official.get("diagnostics") or []) + (snapshot.get("diagnostics") or [])

    price = _naver_realtime_price(session, timeout) or _positive_num(snapshot.get("last_close_price"))
    rows = list(official.get("rows") or [])
    cutoff = date.today() - timedelta(days=370)
    trailing = [item for item in rows if date.fromisoformat(item["date"]) >= cutoff]
    ttm = sum(float(item["amount"]) for item in trailing) if trailing else None
    distribution_yield = (ttm / price * 100.0) if ttm is not None and price else None
    latest = trailing[-1]["amount"] if trailing else None

    source = official.get("source")
    source_url = official.get("source_url")
    source_method = official.get("source_method")
    yield_method = "trailing_12m_cash_distributions/current_price" if distribution_yield is not None else None
    naver_fallback_attempted = False
    public_fallback_attempted = False

    if distribution_yield is None:
        naver_fallback_attempted = True
        naver_yield = _positive_num(snapshot.get("dividend_yield"))
        naver_dividend = _positive_num(snapshot.get("dividend"))
        implied_yield = (naver_dividend / price * 100.0) if naver_dividend and price else None
        consistent = (
            naver_yield is not None
            and implied_yield is not None
            and abs(implied_yield - naver_yield) <= max(0.35, naver_yield * 0.15)
        )
        if consistent:
            ttm = naver_dividend
            distribution_yield = implied_yield
            yield_method = "naver_public_snapshot_dps/current_price_cross_checked"
        elif naver_yield is not None:
            distribution_yield = naver_yield
            if naver_dividend is not None:
                ttm = naver_dividend
            yield_method = "naver_public_distribution_yield"
        if distribution_yield is not None:
            source = "NAVER 증권 공개 ETF 분배지표"
            source_url = NAVER_INTEGRATION_URL.format(code=REIT_CODE)
            source_method = "naver_public_reit_snapshot_fallback"
            diagnostics.append("official_history_unavailable_naver_snapshot_used")

    if distribution_yield is None:
        public_fallback_attempted = True
        public_snapshot = _fetch_public_reit_snapshot(session, timeout)
        diagnostics.extend(public_snapshot.get("diagnostics") or [])
        if public_snapshot.get("available"):
            public_price = _positive_num(public_snapshot.get("current_price"))
            if price is None and public_price is not None:
                price = public_price
            public_ttm = _positive_num(public_snapshot.get("trailing_12m_distribution"))
            public_yield = _positive_num(public_snapshot.get("distribution_yield"))
            implied_public_yield = (public_ttm / price * 100.0) if public_ttm and price else None
            if public_yield is not None and implied_public_yield is not None:
                if abs(implied_public_yield - public_yield) <= max(0.45, public_yield * 0.20):
                    distribution_yield = implied_public_yield
                    ttm = public_ttm
                    yield_method = "public_snapshot_dps/current_price_cross_checked"
                else:
                    distribution_yield = public_yield
                    yield_method = "public_distribution_yield"
                    diagnostics.append("public_snapshot_dps_yield_crosscheck_mismatch")
            elif public_yield is not None:
                distribution_yield = public_yield
                ttm = public_ttm
                yield_method = "public_distribution_yield"
            if distribution_yield is not None:
                source = public_snapshot.get("source")
                source_url = public_snapshot.get("source_url")
                source_method = public_snapshot.get("source_method")
                diagnostics.append("official_and_naver_unavailable_public_snapshot_used")

    return {
        "ticker": REIT_CODE,
        "current_price": _round(price, 2),
        "distribution_yield": _round(distribution_yield),
        "trailing_12m_distribution": _round(ttm, 2),
        "distribution_count_12m": len(trailing),
        "latest_distribution": _round(latest, 2),
        "distribution_history_12m": trailing,
        "yield_method": yield_method or "unavailable",
        "as_of": date.today().isoformat(),
        "source": source,
        "source_url": source_url or NAVER_REIT_PAGE,
        "source_method": source_method,
        "official_history_available": bool(trailing),
        "naver_fallback_attempted": naver_fallback_attempted,
        "public_fallback_attempted": public_fallback_attempted,
        "available": distribution_yield is not None,
        "stale": False,
        "diagnostics": diagnostics[-18:],
    }

def _reuse_last_good(
    current: dict[str, Any], old: dict[str, Any], label: str
) -> tuple[dict[str, Any], bool]:
    if current.get("available"):
        return current, False
    if old.get("available"):
        reused = dict(old)
        reused["stale"] = True
        reused["source_method"] = "last_good_reuse"
        reused["live_diagnostics"] = current.get("diagnostics") or []
        reused["diagnostics"] = (reused.get("diagnostics") or []) + [
            f"{label}:live_failed_last_good_reused"
        ]
        return reused, True
    return current, False


def collect(output_dir: Path, timeout: int = 25) -> dict[str, Any]:
    previous_path = output_dir / "korea_asset_fundamentals.json"
    previous = read_json(previous_path) if previous_path.exists() else {}
    session = requests.Session()
    session.headers.update({"Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8"})

    indices: dict[str, Any] = {}
    errors: list[str] = []
    warnings: list[str] = []
    used_previous = False

    for key, cfg in INDEX_CONFIG.items():
        current = _pykrx_index(cfg)
        forward_proxy = _collect_forward_proxy(
            cfg,
            str(current.get("as_of") or date.today().isoformat()),
            timeout,
        )
        row = _merge_index_sources(current, forward_proxy)
        row, reused = _reuse_last_good(
            row,
            ((previous.get("indices") or {}).get(key) or {}),
            key,
        )
        used_previous = used_previous or reused
        if not row.get("available"):
            errors.append(f"{key}: PER/PBR/dividend_yield unavailable")
        if not row.get("forward_data_available"):
            warnings.append(f"{key}: forward consensus unavailable")
        indices[key] = row

    reit_live = _collect_reit(session, timeout)
    reit, reused = _reuse_last_good(reit_live, previous.get("reit") or {}, "reit")
    used_previous = used_previous or reused
    if not reit.get("available"):
        errors.append("reit: distribution_yield unavailable")
    elif not reit.get("official_history_available"):
        warnings.append("reit: official distribution history unavailable; NAVER snapshot fallback used")

    result = {
        "schema_version": "1.3.0",
        "engine_version": "korea-asset-fundamentals-v1.3",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "indices": indices,
        "reit": reit,
        "source_status": {
            "krx_pykrx_attempted": True,
            "krx_login_configured": bool(os.getenv("KRX_ID") and os.getenv("KRX_PW")),
            "naver_constituent_consensus_attempted": True,
            "mirae_official_reit_history_attempted": True,
            "naver_reit_snapshot_fallback_attempted": bool(
                reit_live.get("naver_fallback_attempted")
            ),
            "public_reit_snapshot_fallback_attempted": bool(
                reit_live.get("public_fallback_attempted")
            ),
            "used_previous_results": used_previous,
            "paid_api_required": False,
        },
        "limitations": [
            "현재 PER·PBR·배당수익률은 KRX 인증 세션의 공식 지수 투자지표를 사용합니다.",
            "KRX가 공식 선행 PER을 제공하지 않을 때는 KRX 지수 구성종목 중 시가총액 상위 종목의 NAVER 공개 컨센서스를 시가총액·이익 기준으로 합산합니다.",
            "선행 대용치는 구성종목 표본수와 시가총액 커버리지를 함께 공개하며, 기준 미달 시 null을 유지합니다.",
            "리츠는 TIGER 공식 최근 12개월 분배금 이력을 우선 사용하고, 공식 페이지 차단 시 NAVER 공개 분배지표와 복수 공개 ETF 지표로 교차 대체합니다.",
            "당일 모든 경로 실패 시 마지막 정상값만 stale=true로 재사용합니다.",
        ],
        "warnings": warnings,
        "errors": errors,
    }
    write_json(previous_path, result)

    print(f"wrote {previous_path} (indices={len(indices)}, errors={len(errors)})")
    for key, row in indices.items():
        print(
            f"{key} available={str(bool(row.get('available'))).lower()} "
            f"coverage={float(row.get('coverage') or 0):.2f} "
            f"forward_available={str(bool(row.get('forward_data_available'))).lower()} "
            f"forward_sample={row.get('forward_proxy_sample_size', 0)} "
            f"forward_cap_coverage={float(row.get('forward_proxy_market_cap_coverage') or 0):.3f} "
            f"source={row.get('source_method')} stale={row.get('stale')}"
        )
    print(
        f"reit available={str(bool(reit.get('available'))).lower()} "
        f"official_history={str(bool(reit.get('official_history_available'))).lower()} "
        f"distribution_count_12m={reit.get('distribution_count_12m', 0)} "
        f"source={reit.get('source_method')} stale={reit.get('stale')}"
    )
    print(f"korea_asset_fundamentals warnings={len(warnings)} errors={len(errors)}")
    session.close()
    return result
