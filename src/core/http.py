from __future__ import annotations
import time
from typing import Any
import requests

class HttpError(RuntimeError):
    pass

def get_json(url: str, *, headers=None, params=None, timeout=30, retries=3) -> Any:
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            response = requests.get(url, headers=headers, params=params, timeout=timeout)
            response.raise_for_status()
            if "html" in response.headers.get("content-type", "").lower():
                raise HttpError("API가 JSON 대신 HTML을 반환했습니다.")
            return response.json()
        except (requests.RequestException, ValueError, HttpError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(2 ** (attempt - 1))
    raise HttpError(f"요청 실패: {last_error}")
