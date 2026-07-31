from __future__ import annotations

import json
import math
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

from src.core.io import read_json, write_json

KRX_URL = "https://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd"
KRX_SOURCE_URL = "https://data.krx.co.kr/contents/MDC/MAIN/main/index.cmd"
NAVER_INDEX_URLS = {
    "kospi200": [
        "https://m.stock.naver.com/api/index/KOSPI200/basic",
        "https://m.stock.naver.com/api/index/KPI200/basic",
    ],
    "kosdaq150": [
        "https://m.stock.naver.com/api/index/KOSDAQ150/basic",
    ],
}
INDEX_CONFIG = {
    "kospi200": {"name": "코스피 200", "code": "1028", "group": "01"},
    "kosdaq150": {"name": "코스닥 150", "code": "2203", "group": "02"},
}
REIT_CODES = ["329200", "476800"]


def _num(value: Any) -> float | None:
    if value is None:
        return None
    raw = str(value).strip().replace(",", "").replace("%", "")
    if not raw or raw in {"-", "--", "N/A", "null", "None"}:
        return None
    try:
        out = float(raw)
    except ValueError:
        return None
    return out if math.isfinite(out) else None


def _business_dates(days: int = 10) -> list[str]:
    out: list[str] = []
    d = date.today()
    for _ in range(days * 2):
        d -= timedelta(days=1)
        if d.weekday() < 5:
            out.append(d.strftime("%Y%m%d"))
            if len(out) >= days:
                break
    return out


def _rows(payload: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict):
                found.append(item)
    elif isinstance(payload, dict):
        for value in payload.values():
            if isinstance(value, list):
                found.extend(x for x in value if isinstance(x, dict))
    return found


def _pick(row: dict[str, Any], names: list[str]) -> float | None:
    for name in names:
        value = _num(row.get(name))
        if value is not None:
            return value
    return None


def _krx_index(session: requests.Session, cfg: dict[str, str], timeout: int) -> dict[str, Any] | None:
    errors: list[str] = []
    for trd_dd in _business_dates(10):
        payloads = [
            {
                "bld": "dbms/MDC/STAT/standard/MDCSTAT00601",
                "trdDd": trd_dd,
                "idxIndMidclssCd": cfg["group"],
                "idxIndMidclssCd2": "",
                "share": "1",
                "money": "1",
                "csvxls_isNo": "false",
            },
            {
                "bld": "dbms/MDC/STAT/standard/MDCSTAT00601",
                "trdDd": trd_dd,
                "idxIndMidclssCd": "",
                "idxIndMidclssCd2": "",
                "share": "1",
                "money": "1",
                "csvxls_isNo": "false",
            },
        ]
        for payload in payloads:
            try:
                response = session.post(
                    KRX_URL,
                    data=payload,
                    timeout=timeout,
                    headers={
                        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
                        "Referer": "https://data.krx.co.kr/",
                        "Origin": "https://data.krx.co.kr",
                        "Accept": "application/json,text/plain,*/*",
                    },
                )
                response.raise_for_status()
                obj = response.json()
                rows = _rows(obj)
                target = None
                wanted = re.sub(r"\s+", "", cfg["name"]).upper()
                for row in rows:
                    name = str(row.get("IDX_NM") or row.get("IDX_NM1") or row.get("IND_NM") or "")
                    code = str(row.get("IDX_CD") or row.get("IND_CD") or "")
                    if wanted in re.sub(r"\s+", "", name).upper() or code == cfg["code"]:
                        target = row
                        break
                if not target:
                    errors.append(f"{trd_dd}:row_not_found:{len(rows)}")
                    continue
                per = _pick(target, ["PER", "PER_VAL", "IDX_PER", "PER1"])
                pbr = _pick(target, ["PBR", "PBR_VAL", "IDX_PBR", "PBR1"])
                dy = _pick(target, ["DIV_YD", "DVD_YLD", "DIVIDEND_YIELD", "DY", "DVD_YD"])
                if per is not None or pbr is not None or dy is not None:
                    return {
                        "per": per,
                        "pbr": pbr,
                        "dividend_yield": dy,
                        "as_of": trd_dd[:4] + "-" + trd_dd[4:6] + "-" + trd_dd[6:],
                        "source": "KRX 정보데이터시스템",
                        "source_url": KRX_SOURCE_URL,
                        "source_method": "official_web_dataset",
                        "available": True,
                        "stale": False,
                        "diagnostics": [],
                    }
                errors.append(f"{trd_dd}:fields_not_found")
            except Exception as exc:
                errors.append(f"{trd_dd}:{type(exc).__name__}:{exc}")
    return {"available": False, "diagnostics": errors[-8:]}


def _walk(obj: Any, keys: set[str]) -> float | None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k).lower() in keys:
                n = _num(v.get("value") if isinstance(v, dict) else v)
                if n is not None:
                    return n
        for v in obj.values():
            n = _walk(v, keys)
            if n is not None:
                return n
    elif isinstance(obj, list):
        for v in obj:
            n = _walk(v, keys)
            if n is not None:
                return n
    return None


def _naver_index(session: requests.Session, key: str, timeout: int) -> dict[str, Any] | None:
    errors: list[str] = []
    for url in NAVER_INDEX_URLS.get(key, []):
        try:
            r = session.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
            r.raise_for_status()
            obj = r.json()
            per = _walk(obj, {"per", "trailingpe", "priceearningratio"})
            pbr = _walk(obj, {"pbr", "pricetobookratio", "pricebookratio"})
            dy = _walk(obj, {"dividendyield", "dividendrate", "dvr", "dividend_yield"})
            as_of = str(obj.get("localTradedAt") or obj.get("tradeDate") or date.today().isoformat())[:10]
            if per is not None or pbr is not None or dy is not None:
                return {
                    "per": per,
                    "pbr": pbr,
                    "dividend_yield": dy,
                    "as_of": as_of,
                    "source": "NAVER 증권 지수정보 보조자료",
                    "source_url": url,
                    "source_method": "public_json_fallback",
                    "available": True,
                    "stale": False,
                    "diagnostics": errors,
                }
            errors.append(f"no_fields:{url}")
        except Exception as exc:
            errors.append(f"{type(exc).__name__}:{exc}:{url}")
    return {"available": False, "diagnostics": errors}


def _merge(primary: dict[str, Any] | None, fallback: dict[str, Any] | None) -> dict[str, Any]:
    p = primary or {}
    f = fallback or {}
    out = {
        "per": p.get("per") if p.get("per") is not None else f.get("per"),
        "pbr": p.get("pbr") if p.get("pbr") is not None else f.get("pbr"),
        "dividend_yield": p.get("dividend_yield") if p.get("dividend_yield") is not None else f.get("dividend_yield"),
        "as_of": p.get("as_of") or f.get("as_of") or date.today().isoformat(),
        "source": p.get("source") if p.get("available") else f.get("source"),
        "source_url": p.get("source_url") if p.get("available") else f.get("source_url"),
        "source_method": p.get("source_method") if p.get("available") else f.get("source_method"),
        "stale": False,
    }
    out["available"] = any(out[k] is not None for k in ("per", "pbr", "dividend_yield"))
    out["coverage"] = sum(out[k] is not None for k in ("per", "pbr", "dividend_yield")) / 3
    out["diagnostics"] = list(p.get("diagnostics") or []) + list(f.get("diagnostics") or [])
    return out


def _naver_reit(session: requests.Session, code: str, timeout: int) -> dict[str, Any] | None:
    url = f"https://finance.naver.com/item/main.naver?code={code}"
    try:
        r = session.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        text = r.content.decode("euc-kr", errors="ignore")
        price_match = re.search(r'<p class="no_today">[\s\S]*?<span class="blind">([0-9,]+)</span>', text)
        current = _num(price_match.group(1)) if price_match else None
        dy_match = re.search(r'id=["\']_dvr["\'][^>]*>\s*([0-9.,-]+)', text, re.I)
        dy = _num(dy_match.group(1)) if dy_match else None
        if dy is None:
            # Naver often prints dividend yield in a labelled table without a stable id.
            dy_match = re.search(r'배당수익률[\s\S]{0,260}?([0-9]+(?:\.[0-9]+)?)\s*%', text, re.I)
            dy = _num(dy_match.group(1)) if dy_match else None
        if current is None and dy is None:
            return None
        return {
            "ticker": code,
            "current_price": current,
            "distribution_yield": dy,
            "trailing_12m_distribution": (current * dy / 100) if current is not None and dy is not None else None,
            "yield_method": "reported_yield" if dy is not None else "unavailable",
            "as_of": date.today().isoformat(),
            "source": "NAVER 증권 ETF 투자지표",
            "source_url": url,
            "available": dy is not None,
            "stale": False,
            "diagnostics": [],
        }
    except Exception as exc:
        return {"available": False, "ticker": code, "diagnostics": [f"{type(exc).__name__}:{exc}"]}


def collect(output_dir: Path, timeout: int = 20) -> dict[str, Any]:
    previous_path = output_dir / "korea_asset_fundamentals.json"
    previous = read_json(previous_path) if previous_path.exists() else {}
    session = requests.Session()
    session.headers.update({"Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8"})

    indices: dict[str, Any] = {}
    errors: list[str] = []
    used_previous = False
    for key, cfg in INDEX_CONFIG.items():
        krx = _krx_index(session, cfg, timeout)
        naver = _naver_index(session, key, timeout)
        row = _merge(krx, naver)
        if not row["available"]:
            old = ((previous.get("indices") or {}).get(key) or {})
            if old.get("available"):
                row = dict(old)
                row["stale"] = True
                row["source_method"] = "last_good_reuse"
                row["diagnostics"] = (row.get("diagnostics") or []) + ["live_collection_failed_last_good_reused"]
                used_previous = True
            else:
                errors.append(f"{key}: PER/PBR/dividend_yield unavailable")
        indices[key] = row

    reit_candidates = [_naver_reit(session, code, timeout) for code in REIT_CODES]
    reit = next((x for x in reit_candidates if x and x.get("available")), None)
    if reit is None:
        old_reit = previous.get("reit") or {}
        if old_reit.get("available"):
            reit = dict(old_reit)
            reit["stale"] = True
            reit["source_method"] = "last_good_reuse"
            used_previous = True
        else:
            reit = reit_candidates[0] or {"available": False, "ticker": REIT_CODES[0], "diagnostics": []}
            errors.append("reit: distribution_yield unavailable")

    result = {
        "schema_version": "1.0.0",
        "engine_version": "korea-asset-fundamentals-v1.0",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "indices": indices,
        "reit": reit,
        "source_status": {
            "krx_official_web_attempted": True,
            "naver_public_fallback_attempted": True,
            "used_previous_results": used_previous,
            "paid_api_required": False,
        },
        "limitations": [
            "KRX 정보데이터시스템의 공개 웹 데이터셋을 우선 사용하고, 필드 결측 시 NAVER 공개 지수정보를 보조로 사용합니다.",
            "당일 수집 실패 시 마지막 정상값을 재사용하고 stale=true로 표시합니다.",
            "리츠 분배수익률은 대표 ETF 공개 투자지표를 사용하며 개별 리츠 전체의 가중평균 배당률은 아닙니다.",
        ],
        "errors": errors,
    }
    write_json(previous_path, result)
    print(f"wrote {previous_path} (indices={len(indices)}, errors={len(errors)})")
    return result
