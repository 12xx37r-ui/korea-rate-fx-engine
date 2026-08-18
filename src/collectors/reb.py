from __future__ import annotations
import os
import re
from pathlib import Path
import requests
from src.core.http import get_json
from src.core.io import read_json, write_json
from src.core.result import SourceResult

BASE = "https://www.reb.or.kr/r-one/openapi/SttsApiTblData.do"
PORTAL = "https://www.reb.or.kr/r-one/portal/main/indexPage.do"

def _headline_fallback(timeout: int) -> dict:
    response = requests.get(PORTAL, timeout=max(5, min(timeout, 12)),
                            headers={"User-Agent":"Mozilla/5.0", "Accept":"text/html,*/*"})
    response.raise_for_status()
    text = re.sub(r"<[^>]+>", " ", response.text)
    text = re.sub(r"\s+", " ", text)
    # Official portal headline: 전국주택가격동향조사 ... 매매가격지수 변동률 (%) ... 아파트 N.NN
    patterns = [
        r"전국주택가격동향조사.{0,220}?매매가격지수\s*변동률.{0,120}?아파트\s*([-+]?\d+(?:\.\d+)?)",
        r"매매가격지수\s*변동률.{0,160}?아파트\s*([-+]?\d+(?:\.\d+)?)",
    ]
    value = None
    for p in patterns:
        m = re.search(p, text, flags=re.I)
        if m:
            value=float(m.group(1)); break
    if value is None or not (-20.0 <= value <= 20.0):
        raise ValueError("REB portal apartment sale-change headline not found")
    month = None
    m = re.search(r"전국주택가격동향조사\s*(20\d{2})년\s*(\d{1,2})월", text)
    if m: month=f"{m.group(1)}-{int(m.group(2)):02d}"
    return {
        "headline_apartment_sale_change_pct": value,
        "observation_period": month,
        "source": "한국부동산원 R-ONE 공식 포털",
        "source_url": PORTAL,
        "source_method": "official_portal_headline_fallback",
    }

def collect(output_dir: Path, timeout: int, retries: int) -> SourceResult:
    key = os.getenv("REB_API_KEY", "").strip()
    config = read_json("config/reb_series.json")
    series = {n: v for n, v in config.get("series", {}).items() if v.get("enabled")}
    payloads, warnings = {}, []

    if key and series:
        for name, item in series.items():
            params = {"KEY": key, "Type": "json", "pIndex": 1, "pSize": 1000,
                      "STATBL_ID": item["statbl_id"], "DTACYCLE_CD": item["cycle"]}
            params.update(item.get("item_filter", {}))
            try:
                payloads[name] = get_json(BASE, params=params, timeout=timeout, retries=retries)
            except Exception as exc:
                warnings.append(f"{name}: {exc}")

    # If detailed R-ONE series are not configured (or all failed), preserve a real
    # official REB housing signal instead of reporting not_configured. One request.
    if not payloads:
        try:
            payloads["official_headline"] = _headline_fallback(timeout)
            if not key:
                warnings.append("REB_API_KEY absent: official portal headline fallback used")
            elif not series:
                # Valid official headline fallback is an intentional coverage mode,
                # not a degraded collection failure. Keep status=ok and expose the
                # mode in the message instead of manufacturing a warning.
                pass
        except Exception as exc:
            warnings.append(f"official_headline: {type(exc).__name__}: {str(exc)[:160]}")

    path = output_dir / "raw_reb.json"
    if payloads:
        write_json(path, payloads)
        mode = "상세 R-ONE 시리즈" if series and key else "공식 포털 headline fallback"
        return SourceResult("reb", "ok" if not warnings else "degraded",
                            f"R-ONE 공식 부동산 지표를 수집했습니다. ({mode})", rows=len(payloads),
                            payload_path=str(path), warnings=warnings)

    # Do not overwrite last-good raw_reb.json with an empty object.
    return SourceResult("reb", "error", "R-ONE 데이터 수집 실패; 기존 last-good 파일 유지",
                        payload_path=str(path) if path.exists() else None, warnings=warnings)
