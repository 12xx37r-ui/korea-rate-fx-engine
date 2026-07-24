from __future__ import annotations
import os
from datetime import date
from pathlib import Path
from src.core.http import get_json
from src.core.io import read_json, write_json
from src.core.result import SourceResult

def collect(output_dir: Path, timeout: int, retries: int) -> SourceResult:
    api_key = os.getenv("KRX_API_KEY", "").strip()
    config = read_json("config/krx_apis.json")
    configured = [(n, v) for n, v in config.items()
                  if not n.startswith("_") and isinstance(v, dict) and v.get("enabled")]

    if not api_key:
        return SourceResult("krx", "missing_secret", "KRX_API_KEY가 없습니다.")
    if not configured:
        return SourceResult("krx", "not_configured",
                            "config/krx_apis.json에 승인된 실제 endpoint와 API ID를 입력해야 합니다.")

    payloads, warnings = {}, []
    for name, item in configured:
        endpoint = str(item.get("endpoint", "")).strip()
        api_id = str(item.get("api_id", "")).strip()
        if not endpoint or not api_id:
            warnings.append(f"{name}: endpoint 또는 api_id 누락")
            continue
        try:
            payloads[name] = get_json(
                endpoint,
                headers={"AUTH_KEY": api_key},
                params={"basDd": date.today().strftime("%Y%m%d")},
                timeout=timeout,
                retries=retries,
            )
        except Exception as exc:
            warnings.append(f"{name}: {exc}")

    path = output_dir / "raw_krx.json"
    write_json(path, payloads)
    if not payloads:
        return SourceResult("krx", "error", "KRX 데이터 수집 실패",
                            payload_path=str(path), warnings=warnings)
    return SourceResult("krx", "ok" if not warnings else "degraded",
                        "KRX 승인 API를 호출했습니다.", rows=len(payloads),
                        payload_path=str(path), warnings=warnings)
