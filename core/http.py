from __future__ import annotations

import ast
import json
import random
import re
import time
from typing import Any

import requests


class HttpError(RuntimeError):
    pass


def _parse_json_like(text: str) -> Any:
    """표준 JSON과 JSONP 그리고 작은따옴표 객체 응답을 해석한다."""
    raw = (text or "").lstrip("﻿").strip()
    if not raw:
        raise ValueError("빈 응답")

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    match = re.fullmatch(r"[A-Za-z_$][\w$\.]*\s*\((.*)\)\s*;?", raw, flags=re.DOTALL)
    if match:
        inner = match.group(1).strip()
        try:
            return json.loads(inner)
        except json.JSONDecodeError:
            raw = inner

    try:
        parsed = ast.literal_eval(raw)
    except (ValueError, SyntaxError) as exc:
        preview = raw[:180].replace("\r", " ").replace("\n", " ")
        raise ValueError(f"JSON 해석 실패. 응답 시작: {preview}") from exc

    if isinstance(parsed, (dict, list)):
        return parsed
    raise ValueError(f"JSON 객체나 배열이 아닙니다: {type(parsed).__name__}")


def _backoff(attempt: int, base: float = 2.0, cap: float = 60.0) -> float:
    """Exponential backoff with ±25% jitter, capped at `cap` seconds."""
    raw = base ** (attempt - 1)
    return min(raw * random.uniform(0.75, 1.25), cap)


def get_json(url: str, *, headers=None, params=None, timeout=30, retries=3) -> Any:
    """응답 헤더가 잘못된 API도 본문이 JSON이면 정상 처리한다.

    KOSIS 일부 응답은 실제 본문이 JSON 배열이어도 Content-Type을
    text/html로 반환한다. 따라서 Content-Type만 보고 차단하지 않고
    본문을 먼저 파싱한다. 실제 HTML 문서일 때만 명확한 오류로 처리한다.

    HTTP 429 (Too Many Requests): Retry-After 헤더를 파싱해 대기 후 재시도.
    HTTP 5xx: 지수 백오프 + jitter로 재시도.
    HTTP 4xx (429 제외): 재시도 없음 (클라이언트 오류, 영구적).
    """
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            response = requests.get(url, headers=headers, params=params, timeout=timeout)

            if response.status_code == 429:
                retry_after = int(response.headers.get("Retry-After", 60))
                retry_after = max(1, min(retry_after, 300))
                last_error = HttpError(f"HTTP 429 Too Many Requests (Retry-After={retry_after}s)")
                if attempt < retries:
                    time.sleep(retry_after + _backoff(attempt, base=1.5, cap=10.0))
                continue

            if response.status_code >= 500:
                last_error = HttpError(f"HTTP {response.status_code} 서버 오류")
                if attempt < retries:
                    time.sleep(_backoff(attempt))
                continue

            if 400 <= response.status_code < 500:
                raise HttpError(f"HTTP {response.status_code} 클라이언트 오류 (재시도 없음)")

            raw = (response.text or "").lstrip("﻿").strip()
            if raw.startswith("<"):
                preview = raw[:180].replace("\r", " ").replace("\n", " ")
                raise HttpError(f"API가 HTML 문서를 반환했습니다. 응답 시작: {preview}")

            return _parse_json_like(raw)

        except HttpError:
            raise
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(_backoff(attempt))

    raise HttpError(f"요청 실패 ({retries}회 시도): {last_error}")
