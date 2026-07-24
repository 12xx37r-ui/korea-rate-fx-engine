from __future__ import annotations
import os
from pathlib import Path
from src.core.http import get_json
from src.core.io import write_json
from src.core.result import SourceResult

REQUIRED_PATHS = [
    ("fed", "expected_path"),
    ("fed", "next_meeting"),
    ("us_market",),
    ("quality",),
]

def collect(output_dir: Path, timeout: int, retries: int) -> SourceResult:
    url = os.getenv("US_POLICY_JSON_URL", "").strip()
    if not url:
        return SourceResult("us_policy_engine", "not_configured", "US_POLICY_JSON_URL이 설정되지 않았습니다.")

    headers = {}
    token = os.getenv("US_REPO_READ_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        data = get_json(url, headers=headers, timeout=timeout, retries=retries)
        missing = []
        for path in REQUIRED_PATHS:
            node = data
            for key in path:
                if not isinstance(node, dict) or key not in node:
                    missing.append(".".join(path))
                    break
                node = node[key]

        payload_path = output_dir / "us_input.json"
        write_json(payload_path, data)

        if missing:
            return SourceResult("us_policy_engine", "invalid_schema",
                                "미국 엔진 JSON 필수 필드가 없습니다.",
                                payload_path=str(payload_path), warnings=missing)

        return SourceResult("us_policy_engine", "ok",
                            "미국 정책금리 엔진 JSON을 정상적으로 읽었습니다.",
                            rows=1, payload_path=str(payload_path),
                            metadata={"schema_version": data.get("schema_version")})
    except Exception as exc:
        return SourceResult("us_policy_engine", "error", str(exc))
