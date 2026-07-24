from __future__ import annotations

import ast
import json
import re
import time
from typing import Any

import requests


class HttpError(RuntimeError):
    pass


def _parse_json_like(text: str) -> Any:
    """KOSIS처럼 JSONP 또는 작은따옴표 객체를 반환하는 API도 안전하게 해석한다."""
    raw = (text or "").lstrip("\ufeff").strip()
    if not raw:
        raise ValueError("빈 응답")

    # 표준 JSON
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # callback({...}) 형태 JSONP
    match = re.fullmatch(r"[A-Za-z_$][\w$\.]*\s*\((.*)\)\s*;?", raw, flags=re.DOTALL)
    if match:
        inner = match.group(1).strip()
        try:
            return json.loads(inner)
        except json.JSONDecodeError:
            raw = inner

    # 일부 공공 API가 {'key': 'value'}처럼 Python/JS 유사 객체를 반환하는 경우
    try:
        parsed = ast.literal_eval(raw)
    except (ValueError, SyntaxError) as exc:
        preview = raw[:180].replace("\r", " ").replace("\n", " ")
        raise ValueError(f"JSON 해석 실패. 응답 시작: {preview}") from exc

    if isinstance(parsed, (dict, list)):
        return parsed
    raise ValueError(f"JSON 객체나 배열이 아닙니다: {type(parsed).__name__}")


def get_json(url: str, *, headers=None, params=None, timeout=30, retries=3) -> Any:
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            response = requests.get(url, headers=headers, params=params, timeout=timeout)
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").lower()
            if "html" in content_type:
                preview = response.text[:180].replace("\r", " ").replace("\n", " ")
                raise HttpError(f"API가 JSON 대신 HTML을 반환했습니다. 응답 시작: {preview}")
            return _parse_json_like(response.text)
        except (requests.RequestException, ValueError, HttpError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(2 ** (attempt - 1))
    raise HttpError(f"요청 실패: {last_error}")
