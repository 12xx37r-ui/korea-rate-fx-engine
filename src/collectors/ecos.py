from __future__ import annotations
import os
from datetime import date, timedelta
from pathlib import Path
from src.core.http import get_json
from src.core.io import read_json, write_json
from src.core.result import SourceResult

BASE = "https://ecos.bok.or.kr/api/StatisticSearch"

def collect(output_dir: Path, timeout: int, retries: int) -> SourceResult:
    key = os.getenv("ECOS_API_KEY", "").strip()
    config = read_json("config/ecos_series.json")
    series = {n: v for n, v in config.get("series", {}).items() if v.get("enabled")}

    if not key:
        return SourceResult("ecos", "missing_secret", "ECOS_API_KEY가 없습니다.")
    if not series:
        return SourceResult("ecos", "not_configured",
                            "config/ecos_series.json에 실제 ECOS 통계코드를 입력해야 합니다.")

    end = date.today()
    start = end - timedelta(days=90)
    payloads, warnings = {}, []

    for name, item in series.items():
        freq = item["frequency"]
        start_text = start.strftime("%Y%m%d") if freq == "D" else start.strftime("%Y%m")
        end_text = end.strftime("%Y%m%d") if freq == "D" else end.strftime("%Y%m")
        parts = [
            BASE, key, "json", "kr", "1", "1000",
            item["stat_code"], freq, start_text, end_text,
            item.get("item_code1") or "?",
            item.get("item_code2") or "?",
            item.get("item_code3") or "?"
        ]
        url = "/".join(str(x).strip("/") for x in parts)
        try:
            payloads[name] = get_json(url, timeout=timeout, retries=retries)
        except Exception as exc:
            warnings.append(f"{name}: {exc}")

    path = output_dir / "raw_ecos.json"
    write_json(path, payloads)
    if not payloads:
        return SourceResult("ecos", "error", "ECOS 데이터 수집 실패",
                            payload_path=str(path), warnings=warnings)
    return SourceResult("ecos", "ok" if not warnings else "degraded",
                        "ECOS 설정 지표를 수집했습니다.", rows=len(payloads),
                        payload_path=str(path), warnings=warnings)
