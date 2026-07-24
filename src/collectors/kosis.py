from __future__ import annotations
import os
from pathlib import Path
from src.core.http import get_json
from src.core.io import read_json, write_json
from src.core.result import SourceResult

URL = "https://kosis.kr/openapi/Param/statisticsParameterData.do"

def collect(output_dir: Path, timeout: int, retries: int) -> SourceResult:
    key = os.getenv("KOSIS_API_KEY", "").strip()
    config = read_json("config/kosis_series.json")
    series = {n: v for n, v in config.get("series", {}).items() if v.get("enabled")}

    if not key:
        return SourceResult("kosis", "missing_secret", "KOSIS_API_KEY가 없습니다.")
    if not series:
        return SourceResult("kosis", "not_configured",
                            "config/kosis_series.json에 실제 통계코드를 입력해야 합니다.")

    payloads, warnings = {}, []
    for name, item in series.items():
        params = {
            "method": "getList", "apiKey": key, "orgId": item["orgId"],
            "tblId": item["tblId"], "itmId": item["itmId"],
            "objL1": item["objL1"], "prdSe": item["prdSe"],
            "newEstPrdCnt": "24", "format": "json", "jsonVD": "Y"
        }
        if item.get("objL2"):
            params["objL2"] = item["objL2"]
        try:
            payloads[name] = get_json(URL, params=params, timeout=timeout, retries=retries)
        except Exception as exc:
            warnings.append(f"{name}: {exc}")

    path = output_dir / "raw_kosis.json"
    write_json(path, payloads)
    if not payloads:
        return SourceResult("kosis", "error", "KOSIS 데이터 수집 실패",
                            payload_path=str(path), warnings=warnings)
    return SourceResult("kosis", "ok" if not warnings else "degraded",
                        "KOSIS 설정 지표를 수집했습니다.", rows=len(payloads),
                        payload_path=str(path), warnings=warnings)
