from __future__ import annotations

import math
import os
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

from src.core.io import read_json, write_json

INDEX_CONFIG = {
    "kospi200": {
        "name": "코스피 200",
        "ticker": "1028",
        "representative_etfs": ["069500.KS", "102110.KS"],
    },
    "kosdaq150": {
        "name": "코스닥 150",
        "ticker": "2203",
        "representative_etfs": ["229200.KS", "232080.KS"],
    },
}

REIT_CODE = "329200"
REIT_ISIN = "KR7329200000"
TIGER_REIT_URL = (
    "https://www.tigeretf.com/ko/product/search/detail/index.do?ksdFund=" + REIT_ISIN
)
FUNETF_REIT_URL = "https://www.funetf.co.kr/product/etf/view/" + REIT_ISIN
FNGUIDE_REIT_URL = (
    "https://comp.fnguide.com/SVO2/ASP/etf_snapshot.asp?NewMenuID=401&gicode=A329200"
)
NAVER_REIT_URL = "https://finance.naver.com/item/main.naver?code=329200"
YAHOO_QUOTE_URLS = (
    "https://query1.finance.yahoo.com/v7/finance/quote",
    "https://query2.finance.yahoo.com/v7/finance/quote",
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
    """KRX current valuation metrics through pykrx authenticated session."""
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
            dividend_yield = _find_metric(
                row, ["배당수익률", "배당수익률(%)", "배당률"]
            )

            if any(v is not None for v in (per, pbr, dividend_yield)):
                return {
                    "per": _round(per),
                    "pbr": _round(pbr),
                    "dividend_yield": _round(dividend_yield),
                    "as_of": as_of,
                    "source": "KRX 지수 투자지표 (pykrx 세션)",
                    "source_url": "https://data.krx.co.kr/",
                    "source_method": (
                        "pykrx_authenticated" if has_login else "pykrx_session"
                    ),
                    "available": True,
                    "stale": False,
                    "diagnostics": diagnostics[-10:],
                }
            diagnostics.append(f"{end}:pykrx_fields_empty:{columns}")
        except Exception as exc:
            diagnostics.append(f"{end}:pykrx:{type(exc).__name__}:{str(exc)[:180]}")
    return {"available": False, "diagnostics": diagnostics[-12:]}


def _yahoo_forward_proxy(
    session: requests.Session, cfg: dict[str, Any], timeout: int
) -> dict[str, Any]:
    """Optional representative-ETF proxy for forward PER and EPS growth.

    This is never labelled as an official index value. Missing values remain null.
    """
    diagnostics: list[str] = []
    results: list[dict[str, Any]] = []
    symbols = ",".join(cfg.get("representative_etfs") or [])
    if not symbols:
        return {"available": False, "diagnostics": ["yahoo:no_symbols"]}

    for base_url in YAHOO_QUOTE_URLS:
        try:
            response = session.get(
                base_url,
                params={"symbols": symbols},
                timeout=timeout,
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Accept": "application/json,text/plain,*/*",
                },
            )
            response.raise_for_status()
            payload = response.json()
            results = ((payload.get("quoteResponse") or {}).get("result") or [])
            if results:
                break
            diagnostics.append(f"yahoo_empty:{base_url}")
        except Exception as exc:
            diagnostics.append(f"yahoo:{type(exc).__name__}:{str(exc)[:160]}")

    forward_pes: list[float] = []
    growth_rates: list[float] = []
    used_symbols: list[str] = []
    for row in results:
        forward_pe = _positive_num(row.get("forwardPE"))
        eps_forward = _positive_num(row.get("epsForward"))
        eps_trailing = _positive_num(row.get("epsTrailingTwelveMonths"))
        if forward_pe is not None:
            forward_pes.append(forward_pe)
            used_symbols.append(str(row.get("symbol") or ""))
        if eps_forward is not None and eps_trailing is not None:
            growth = (eps_forward / eps_trailing - 1.0) * 100.0
            if math.isfinite(growth) and -80 <= growth <= 300:
                growth_rates.append(growth)

    def median(values: list[float]) -> float | None:
        if not values:
            return None
        ordered = sorted(values)
        middle = len(ordered) // 2
        if len(ordered) % 2:
            return ordered[middle]
        return (ordered[middle - 1] + ordered[middle]) / 2.0

    forward_per = median(forward_pes)
    eps_growth_pct = median(growth_rates)
    available = forward_per is not None or eps_growth_pct is not None
    return {
        "forward_per": _round(forward_per),
        "eps_growth_pct": _round(eps_growth_pct),
        "available": available,
        "source": "대표 ETF Yahoo 공개지표 대용치" if available else None,
        "source_url": "https://finance.yahoo.com/" if available else None,
        "source_method": "representative_etf_proxy" if available else None,
        "proxy_symbols": sorted(set(x for x in used_symbols if x)),
        "diagnostics": diagnostics[-8:],
    }


def _merge_index_sources(
    current: dict[str, Any], forward_proxy: dict[str, Any]
) -> dict[str, Any]:
    per = _positive_num(current.get("per"))
    pbr = _positive_num(current.get("pbr"))
    dividend_yield = _positive_num(current.get("dividend_yield"))
    forward_per = _positive_num(forward_proxy.get("forward_per"))
    eps_growth_pct = _num(forward_proxy.get("eps_growth_pct"))
    if eps_growth_pct is not None and not (-80 <= eps_growth_pct <= 300):
        eps_growth_pct = None

    growth_adjusted_per = None
    if forward_per is not None and eps_growth_pct is not None and eps_growth_pct > 0:
        growth_adjusted_per = forward_per / eps_growth_pct

    coverage_fields = (per, pbr, dividend_yield)
    diagnostics = (current.get("diagnostics") or []) + (
        forward_proxy.get("diagnostics") or []
    )
    sources = [current.get("source"), forward_proxy.get("source")]
    methods = [current.get("source_method"), forward_proxy.get("source_method")]

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
        "as_of": current.get("as_of") or date.today().isoformat(),
        "source": " + ".join(dict.fromkeys(str(x) for x in sources if x)) or None,
        "source_url": current.get("source_url") or forward_proxy.get("source_url"),
        "source_method": "+".join(dict.fromkeys(str(x) for x in methods if x)) or None,
        "proxy_symbols": forward_proxy.get("proxy_symbols") or [],
        "stale": False,
        "diagnostics": diagnostics[-16:],
    }


def _naver_price(session: requests.Session, timeout: int) -> float | None:
    try:
        response = session.get(
            NAVER_REIT_URL,
            timeout=timeout,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        response.raise_for_status()
        text = _safe_text(response)
        match = re.search(
            r'<p class="no_today">[\s\S]*?<span class="blind">([0-9,]+)</span>',
            text,
        )
        return _positive_num(match.group(1)) if match else None
    except Exception:
        return None


def _extract_distributions(html: str) -> list[dict[str, Any]]:
    """Parse date/amount rows used by official ETF and public ETF pages."""
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

        # Prefer the last table cell because ETF distribution tables put the
        # cash amount after the distribution rate.
        candidates: list[float] = []
        for cell in reversed(cells):
            values = [_positive_num(x) for x in re.findall(r"[0-9,]+(?:\.[0-9]+)?", cell)]
            candidates.extend(x for x in values if x is not None)
        if not candidates:
            values = [_positive_num(x) for x in re.findall(r"[0-9,]+(?:\.[0-9]+)?", text)]
            candidates.extend(x for x in values if x is not None)

        amount = next((x for x in candidates if 1 <= x <= 5000), None)
        if amount is None:
            continue
        try:
            dt = date(
                int(date_match.group(1)),
                int(date_match.group(2)),
                int(date_match.group(3)),
            )
        except ValueError:
            continue
        rows.append({"date": dt.isoformat(), "amount": float(amount)})

    unique = {(item["date"], item["amount"]): item for item in rows}
    return sorted(unique.values(), key=lambda item: item["date"])


def _parse_current_price(html: str) -> float | None:
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text)
    patterns = [
        r"현재가\s*([0-9,]+)\s*원",
        r"현재가[\s\S]{0,80}?([0-9,]+)\s*원",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            value = _positive_num(match.group(1))
            if value is not None:
                return value
    return None


def _fetch_distribution_source(
    session: requests.Session,
    url: str,
    label: str,
    method: str,
    timeout: int,
) -> dict[str, Any]:
    try:
        response = session.get(
            url,
            timeout=timeout,
            headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml",
                "Referer": url,
            },
        )
        response.raise_for_status()
        html = _safe_text(response)
        rows = _extract_distributions(html)
        return {
            "rows": rows,
            "price": _parse_current_price(html),
            "source": label,
            "source_url": url,
            "source_method": method,
            "available": bool(rows),
            "diagnostics": [] if rows else [f"{method}:distribution_rows_not_found"],
        }
    except Exception as exc:
        return {
            "rows": [],
            "price": None,
            "source": None,
            "source_url": url,
            "source_method": None,
            "available": False,
            "diagnostics": [f"{method}:{type(exc).__name__}:{str(exc)[:160]}"],
        }


def _collect_reit(session: requests.Session, timeout: int) -> dict[str, Any]:
    diagnostics: list[str] = []
    sources = [
        _fetch_distribution_source(
            session,
            TIGER_REIT_URL,
            "TIGER 공식 상품정보",
            "tiger_official_distribution",
            timeout,
        ),
        _fetch_distribution_source(
            session,
            FUNETF_REIT_URL,
            "FunETF 공개 분배금 정보",
            "funetf_distribution_fallback",
            timeout,
        ),
        _fetch_distribution_source(
            session,
            FNGUIDE_REIT_URL,
            "FnGuide ETF 분배금현황",
            "fnguide_distribution_fallback",
            timeout,
        ),
    ]
    selected = next((src for src in sources if src.get("available")), None)
    for src in sources:
        diagnostics.extend(src.get("diagnostics") or [])

    price = _naver_price(session, timeout)
    if price is None:
        price = next(
            (_positive_num(src.get("price")) for src in sources if src.get("price")),
            None,
        )

    rows = list((selected or {}).get("rows") or [])
    cutoff = date.today() - timedelta(days=370)
    trailing = [item for item in rows if date.fromisoformat(item["date"]) >= cutoff]
    ttm = sum(float(item["amount"]) for item in trailing) if trailing else None
    distribution_yield = (ttm / price * 100.0) if ttm is not None and price else None
    latest = trailing[-1]["amount"] if trailing else None

    return {
        "ticker": REIT_CODE,
        "current_price": _round(price, 2),
        "distribution_yield": _round(distribution_yield),
        "trailing_12m_distribution": _round(ttm, 2),
        "distribution_count_12m": len(trailing),
        "latest_distribution": _round(latest, 2),
        "distribution_history_12m": trailing,
        "yield_method": (
            "trailing_12m_cash_distributions/current_price"
            if distribution_yield is not None
            else "unavailable"
        ),
        "as_of": date.today().isoformat(),
        "source": (selected or {}).get("source"),
        "source_url": (selected or {}).get("source_url") or TIGER_REIT_URL,
        "source_method": (selected or {}).get("source_method"),
        "available": distribution_yield is not None,
        "stale": False,
        "diagnostics": diagnostics[-12:],
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
        forward_proxy = _yahoo_forward_proxy(session, cfg, timeout)
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
            warnings.append(f"{key}: forward_per/eps_growth proxy unavailable")
        indices[key] = row

    reit_live = _collect_reit(session, timeout)
    reit, reused = _reuse_last_good(reit_live, previous.get("reit") or {}, "reit")
    used_previous = used_previous or reused
    if not reit.get("available"):
        errors.append("reit: trailing_12m distribution_yield unavailable")

    result = {
        "schema_version": "1.2.0",
        "engine_version": "korea-asset-fundamentals-v1.2",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "indices": indices,
        "reit": reit,
        "source_status": {
            "krx_pykrx_attempted": True,
            "krx_login_configured": bool(os.getenv("KRX_ID") and os.getenv("KRX_PW")),
            "yahoo_forward_proxy_attempted": True,
            "tiger_official_reit_attempted": True,
            "funetf_reit_fallback_attempted": True,
            "fnguide_reit_fallback_attempted": True,
            "used_previous_results": used_previous,
            "paid_api_required": False,
        },
        "limitations": [
            "현재 PER·PBR·배당수익률은 KRX 인증 세션의 공식 지수 투자지표를 사용합니다.",
            "선행 PER·EPS 성장률은 공식 지수값이 아니라 대표 ETF 공개지표 대용치이며, 확보되지 않으면 null을 유지합니다.",
            "리츠 분배수익률은 대표 ETF 329200의 최근 12개월 실제 현금분배금 합계 ÷ 현재가격으로 계산합니다.",
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
            f"source={row.get('source_method')} stale={row.get('stale')}"
        )
    print(
        f"reit available={str(bool(reit.get('available'))).lower()} "
        f"distribution_count_12m={reit.get('distribution_count_12m', 0)} "
        f"source={reit.get('source_method')} stale={reit.get('stale')}"
    )
    print(f"korea_asset_fundamentals warnings={len(warnings)} errors={len(errors)}")
    return result
