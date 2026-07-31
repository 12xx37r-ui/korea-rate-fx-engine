from __future__ import annotations

import math
import os
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import requests

from src.core.io import read_json, write_json

INDEX_CONFIG = {
    "kospi200": {
        "name": "코스피 200",
        "ticker": "1028",
        "indexergo": {"per": "20405", "pbr": "20406", "dividend_yield": "20407"},
    },
    "kosdaq150": {
        "name": "코스닥 150",
        "ticker": "2203",
        "indexergo": {"per": "20505", "pbr": "20506", "dividend_yield": "20507"},
    },
}
REIT_CODE = "329200"
INDEXERGO_BASE = "https://www.indexergo.com/series/?frq=D&idxDetail={}"
FNGUIDE_REIT_URL = (
    "https://comp.fnguide.com/SVO2/ASP/etf_snapshot.asp?NewMenuID=401&gicode=A329200"
)
NAVER_REIT_URL = "https://finance.naver.com/item/main.naver?code=329200"


def _num(value: Any) -> float | None:
    if value is None:
        return None
    raw = str(value).strip().replace(",", "").replace("%", "")
    if not raw or raw in {"-", "--", "N/A", "null", "None"}:
        return None
    try:
        out = float(raw)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


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


def _pykrx_index(cfg: dict[str, Any]) -> dict[str, Any]:
    """Primary source. pykrx v1.2.8 handles the 2026 KRX login/session policy.

    KRX_ID/KRX_PW are optional. With current KRX policy they are usually required;
    failures are retained in diagnostics and public-data fallbacks continue.
    """
    diagnostics: list[str] = []
    try:
        from pykrx import stock  # type: ignore
    except Exception as exc:
        return {"available": False, "diagnostics": [f"pykrx_import:{type(exc).__name__}:{exc}"]}

    # pykrx reads KRX_ID/KRX_PW itself. Do not log secret values.
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
            per = _num(row.get("PER"))
            forward_per = _num(row.get("선행PER"))
            pbr = _num(row.get("PBR"))
            dy = _num(row.get("배당수익률"))
            # KRX occasionally encodes unavailable forward PER as 0.
            if forward_per == 0:
                forward_per = None
            if any(v is not None for v in (per, pbr, dy)):
                return {
                    "per": per,
                    "forward_per": forward_per,
                    "pbr": pbr,
                    "dividend_yield": dy,
                    "as_of": as_of,
                    "source": "KRX 지수 투자지표 (pykrx 세션)",
                    "source_url": "https://data.krx.co.kr/",
                    "source_method": "pykrx_authenticated" if has_login else "pykrx_session",
                    "available": True,
                    "stale": False,
                    "diagnostics": diagnostics,
                }
            diagnostics.append(f"{end}:pykrx_fields_empty:{list(row)}")
        except Exception as exc:
            diagnostics.append(f"{end}:pykrx:{type(exc).__name__}:{str(exc)[:180]}")
    return {"available": False, "diagnostics": diagnostics[-10:]}


def _extract_indexergo_value(html: str, metric_ko: str) -> tuple[float | None, str | None]:
    text = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", " ", html, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;|&#37;|&percnt;", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    # Page header pattern: "PER (22.47)" / "PBR (1.94)" / "배당수익률 (1.09%)"
    patterns = [
        rf"{re.escape(metric_ko)}\s*\(\s*([0-9]+(?:\.[0-9]+)?)\s*%?\s*\)",
        rf"{re.escape(metric_ko)}\s+([0-9]+(?:\.[0-9]+)?)\s*%?\s+(?:전기대비|전일대비|3M|6M|1Y)",
    ]
    value = None
    for pattern in patterns:
        m = re.search(pattern, text, re.I)
        if m:
            value = _num(m.group(1))
            break
    date_matches = re.findall(r"(20\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2})", text)
    as_of = None
    if date_matches:
        y, m, d = date_matches[0]
        as_of = f"{int(y):04d}-{int(m):02d}-{int(d):02d}"
    return value, as_of


def _indexergo_index(session: requests.Session, cfg: dict[str, Any], timeout: int) -> dict[str, Any]:
    values: dict[str, float | None] = {"per": None, "pbr": None, "dividend_yield": None}
    dates: list[str] = []
    diagnostics: list[str] = []
    labels = {"per": "PER", "pbr": "PBR", "dividend_yield": "배당수익률"}
    urls: list[str] = []
    for field, detail in cfg["indexergo"].items():
        url = INDEXERGO_BASE.format(detail)
        urls.append(url)
        try:
            r = session.get(
                url,
                timeout=timeout,
                headers={
                    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131 Safari/537.36",
                    "Accept": "text/html,application/xhtml+xml",
                },
            )
            r.raise_for_status()
            value, as_of = _extract_indexergo_value(_safe_text(r), labels[field])
            values[field] = value
            if as_of:
                dates.append(as_of)
            if value is None:
                diagnostics.append(f"indexergo_no_value:{field}:{detail}")
        except Exception as exc:
            diagnostics.append(f"indexergo:{field}:{type(exc).__name__}:{str(exc)[:160]}")
    available = any(v is not None for v in values.values())
    return {
        **values,
        "forward_per": None,
        "as_of": max(dates) if dates else date.today().isoformat(),
        "source": "INDEXerGO 공개 KRX·KOFIA 재배포자료" if available else None,
        "source_url": urls[0] if available else None,
        "source_method": "public_secondary_fallback" if available else None,
        "available": available,
        "stale": False,
        "diagnostics": diagnostics,
    }


def _merge_sources(*sources: dict[str, Any]) -> dict[str, Any]:
    fields = ("per", "forward_per", "pbr", "dividend_yield")
    out: dict[str, Any] = {field: None for field in fields}
    used: list[dict[str, Any]] = []
    diagnostics: list[str] = []
    for src in sources:
        diagnostics.extend(src.get("diagnostics") or [])
        contributed = False
        for field in fields:
            if out[field] is None and src.get(field) is not None:
                out[field] = src[field]
                contributed = True
        if contributed:
            used.append(src)
    out["available"] = any(out[k] is not None for k in ("per", "pbr", "dividend_yield"))
    out["coverage"] = sum(out[k] is not None for k in ("per", "pbr", "dividend_yield")) / 3
    out["as_of"] = max((str(s.get("as_of")) for s in used if s.get("as_of")), default=date.today().isoformat())
    out["source"] = " + ".join(dict.fromkeys(str(s.get("source")) for s in used if s.get("source"))) or None
    out["source_url"] = next((s.get("source_url") for s in used if s.get("source_url")), None)
    methods = [str(s.get("source_method")) for s in used if s.get("source_method")]
    out["source_method"] = "+".join(dict.fromkeys(methods)) or None
    out["stale"] = False
    out["diagnostics"] = diagnostics[-16:]
    return out


def _naver_price(session: requests.Session, timeout: int) -> float | None:
    try:
        r = session.get(NAVER_REIT_URL, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        text = _safe_text(r)
        m = re.search(r'<p class="no_today">[\s\S]*?<span class="blind">([0-9,]+)</span>', text)
        return _num(m.group(1)) if m else None
    except Exception:
        return None


def _extract_distributions(html: str) -> list[dict[str, Any]]:
    """Parse FnGuide ETF distribution rows without relying on one CSS id."""
    rows: list[dict[str, Any]] = []
    # Common table form: date followed by amount. Keep only plausible ETF distributions.
    normalized = re.sub(r"<br\s*/?>", " ", html, flags=re.I)
    tr_blocks = re.findall(r"<tr[^>]*>([\s\S]*?)</tr>", normalized, flags=re.I)
    for block in tr_blocks:
        text = re.sub(r"<[^>]+>", " ", block)
        text = re.sub(r"\s+", " ", text).strip()
        dm = re.search(r"(20\d{2})[./-](\d{1,2})[./-](\d{1,2})", text)
        if not dm:
            continue
        nums = [_num(x) for x in re.findall(r"(?<!\d)([0-9]{1,4}(?:\.[0-9]+)?)(?!\d)", text)]
        nums = [x for x in nums if x is not None]
        # Remove date fragments and accept an amount of 1~1000 won.
        amount = next((x for x in reversed(nums) if 1 <= x <= 1000 and x not in {20, 2020, 2021, 2022, 2023, 2024, 2025, 2026}), None)
        if amount is None:
            continue
        dt = date(int(dm.group(1)), int(dm.group(2)), int(dm.group(3)))
        rows.append({"date": dt.isoformat(), "amount": amount})
    # Deduplicate date/amount pairs.
    unique = {(x["date"], x["amount"]): x for x in rows}
    return sorted(unique.values(), key=lambda x: x["date"])


def _fnguide_reit(session: requests.Session, timeout: int) -> dict[str, Any]:
    diagnostics: list[str] = []
    price = _naver_price(session, timeout)
    try:
        r = session.get(
            FNGUIDE_REIT_URL,
            timeout=timeout,
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://comp.fnguide.com/"},
        )
        r.raise_for_status()
        html = _safe_text(r)
        distributions = _extract_distributions(html)
        cutoff = date.today() - timedelta(days=370)
        trailing = [x for x in distributions if date.fromisoformat(x["date"]) >= cutoff]
        ttm = sum(float(x["amount"]) for x in trailing) if trailing else None
        dy = (ttm / price * 100) if ttm is not None and price else None
        # FnGuide sometimes exposes only the latest distribution in visible HTML.
        if not trailing:
            m_amount = re.search(r"최근\s*분배금\s*\(원\)[\s\S]{0,220}?([0-9,]+)", re.sub(r"<[^>]+>", " ", html), re.I)
            m_date = re.search(r"최근\s*분배금\s*지급기준일[\s\S]{0,220}?(20\d{2}[./-]\d{1,2}[./-]\d{1,2})", re.sub(r"<[^>]+>", " ", html), re.I)
            latest_amount = _num(m_amount.group(1)) if m_amount else None
            diagnostics.append("full_12m_distribution_table_not_found")
        else:
            latest_amount = trailing[-1]["amount"]
            m_date = None
        return {
            "ticker": REIT_CODE,
            "current_price": price,
            "distribution_yield": dy,
            "trailing_12m_distribution": ttm,
            "distribution_count_12m": len(trailing),
            "latest_distribution": latest_amount,
            "distribution_history_12m": trailing,
            "yield_method": "trailing_12m_cash_distributions/current_price" if dy is not None else "unavailable",
            "as_of": date.today().isoformat(),
            "source": "FnGuide ETF 분배금현황 + NAVER 현재가격",
            "source_url": FNGUIDE_REIT_URL,
            "available": dy is not None,
            "stale": False,
            "diagnostics": diagnostics,
        }
    except Exception as exc:
        return {
            "ticker": REIT_CODE,
            "current_price": price,
            "distribution_yield": None,
            "trailing_12m_distribution": None,
            "yield_method": "unavailable",
            "as_of": date.today().isoformat(),
            "source": None,
            "source_url": FNGUIDE_REIT_URL,
            "available": False,
            "stale": False,
            "diagnostics": [f"fnguide:{type(exc).__name__}:{str(exc)[:180]}"],
        }


def _reuse_last_good(current: dict[str, Any], old: dict[str, Any], label: str) -> tuple[dict[str, Any], bool]:
    if current.get("available"):
        return current, False
    if old.get("available"):
        reused = dict(old)
        reused["stale"] = True
        reused["source_method"] = "last_good_reuse"
        reused["live_diagnostics"] = current.get("diagnostics") or []
        reused["diagnostics"] = (reused.get("diagnostics") or []) + [f"{label}:live_failed_last_good_reused"]
        return reused, True
    return current, False


def collect(output_dir: Path, timeout: int = 25) -> dict[str, Any]:
    previous_path = output_dir / "korea_asset_fundamentals.json"
    previous = read_json(previous_path) if previous_path.exists() else {}
    session = requests.Session()
    session.headers.update({"Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8"})

    indices: dict[str, Any] = {}
    errors: list[str] = []
    used_previous = False
    for key, cfg in INDEX_CONFIG.items():
        primary = _pykrx_index(cfg)
        secondary = _indexergo_index(session, cfg, timeout)
        row = _merge_sources(primary, secondary)
        row, reused = _reuse_last_good(row, ((previous.get("indices") or {}).get(key) or {}), key)
        used_previous = used_previous or reused
        if not row.get("available"):
            errors.append(f"{key}: PER/PBR/dividend_yield unavailable")
        indices[key] = row

    reit_live = _fnguide_reit(session, timeout)
    reit, reused = _reuse_last_good(reit_live, previous.get("reit") or {}, "reit")
    used_previous = used_previous or reused
    if not reit.get("available"):
        errors.append("reit: trailing_12m distribution_yield unavailable")

    result = {
        "schema_version": "1.1.0",
        "engine_version": "korea-asset-fundamentals-v1.1",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "indices": indices,
        "reit": reit,
        "source_status": {
            "krx_pykrx_attempted": True,
            "krx_login_configured": bool(os.getenv("KRX_ID") and os.getenv("KRX_PW")),
            "indexergo_public_fallback_attempted": True,
            "fnguide_reit_distribution_attempted": True,
            "used_previous_results": used_previous,
            "paid_api_required": False,
        },
        "limitations": [
            "KRX 최신값은 pykrx 세션을 우선 사용합니다. 2026년 KRX 정책상 KRX_ID/KRX_PW가 필요할 수 있습니다.",
            "KRX 실패 시 INDEXerGO가 공개하는 KRX·KOFIA 재배포자료를 보조값으로 사용하고 출처·기준일을 표시합니다.",
            "당일 모든 경로 실패 시 마지막 정상값만 stale=true로 재사용합니다.",
            "리츠 분배수익률은 대표 ETF 329200의 최근 12개월 현금분배금 합계 ÷ 현재가격으로 계산합니다.",
        ],
        "errors": errors,
    }
    write_json(previous_path, result)
    print(f"wrote {previous_path} (indices={len(indices)}, errors={len(errors)})")
    for key, row in indices.items():
        print(
            f"{key} available={str(bool(row.get('available'))).lower()} "
            f"coverage={float(row.get('coverage') or 0):.2f} source={row.get('source_method')} stale={row.get('stale')}"
        )
    print(
        f"reit available={str(bool(reit.get('available'))).lower()} "
        f"distribution_count_12m={reit.get('distribution_count_12m', 0)} stale={reit.get('stale')}"
    )
    print(f"korea_asset_fundamentals errors={len(errors)}")
    return result
