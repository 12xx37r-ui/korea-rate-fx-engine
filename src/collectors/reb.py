from __future__ import annotations
import os
from pathlib import Path
from src.core.http import get_json
from src.core.io import read_json, write_json
from src.core.result import SourceResult

BASE = "https://www.reb.or.kr/r-one/openapi/SttsApiTblData.do"

def collect(output_dir: Path, timeout: int, retries: int) -> SourceResult:
    key = os.getenv("REB_API_KEY", "").strip()
    config = read_json("config/reb_series.json")
    series = {n: v for n, v in config.get("series", {}).items() if v.get("enabled")}

    if not key:
        return SourceResult("reb", "missing_secret", "REB_API_KEY가 없습니다.")
    if not series:
        return SourceResult("reb", "not_configured",
                            "config/reb_series.json에 실제 R-ONE 통계코드를 입력해야 합니다.")

    payloads, warnings = {}, []
    for name, item in series.items():
        params = {
            "KEY": key, "Type": "json", "pIndex": 1, "pSize": 1000,
            "STATBL_ID": item["statbl_id"], "DTACYCLE_CD": item["cycle"]
        }
        params.update(item.get("item_filter", {}))
        try:
            payloads[name] = get_json(BASE, params=params, timeout=timeout, retries=retries)
        except Exception as exc:
            warnings.append(f"{name}: {exc}")

    path = output_dir / "raw_reb.json"
    write_json(path, payloads)
    if not payloads:
        return SourceResult("reb", "error", "R-ONE 데이터 수집 실패",
                            payload_path=str(path), warnings=warnings)
    return SourceResult("reb", "ok" if not warnings else "degraded",
                        "R-ONE 설정 통계를 수집했습니다.", rows=len(payloads),
                        payload_path=str(path), warnings=warnings)
