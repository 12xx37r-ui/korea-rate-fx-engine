from __future__ import annotations

import ast
import copy
import json
import random
import re
import threading
import time
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlparse

import requests

_HTTP_LOCK = threading.Lock()
_HTTP_MEMO: dict[str, Any] = {}
_LAST_CALL_AT: dict[str, float] = {}
_HOST_MIN_INTERVAL = {
    "api.stlouisfed.org": 0.60,
    "fred.stlouisfed.org": 0.60,
    "ecos.bok.or.kr": 0.15,
    "kosis.kr": 0.15,
}
_DEFAULT_MIN_INTERVAL = 0.10


def _prepared_url(url: str, params: Any) -> str:
    return requests.Request("GET", url, params=params).prepare().url or url


def _memo_key(url: str, headers: Any, params: Any) -> str:
    prepared = _prepared_url(url, params)
    header_items = tuple(sorted((str(k).lower(), str(v)) for k, v in (headers or {}).items()))
    return repr((prepared, header_items))


def _pace(url: str) -> None:
    host = (urlparse(url).hostname or "").lower()
    interval = _HOST_MIN_INTERVAL.get(host, _DEFAULT_MIN_INTERVAL)
    with _HTTP_LOCK:
        last = _LAST_CALL_AT.get(host, 0.0)
    wait = interval - (time.monotonic() - last)
    if wait > 0:
        time.sleep(wait)
    with _HTTP_LOCK:
        _LAST_CALL_AT[host] = time.monotonic()


def _retry_after_seconds(response: requests.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        try:
            dt = parsedate_to_datetime(str(value))
            return max(0.0, dt.timestamp() - time.time())
        except Exception:
            return None


class HttpError(RuntimeError):
    pass


def _parse_json_like(text: str) -> Any:
    """표준 JSON과 JSONP 그리고 작은따옴표 객체 응답을 해석한다."""
    raw = (text or "").lstrip("\ufeff").strip()
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


def get_json(url: str, *, headers=None, params=None, timeout=30, retries=3) -> Any:
    """JSON/JSONP tolerant GET with in-run dedupe and bounded retry.

    Successful identical requests are shared only inside this Python process, so
    cross-run freshness is unchanged.  429 follows Retry-After first; 5xx and
    timeouts use bounded exponential backoff with jitter.
    """
    key = _memo_key(url, headers, params)
    with _HTTP_LOCK:
        if key in _HTTP_MEMO:
            return copy.deepcopy(_HTTP_MEMO[key])

    last_error: Exception | None = None
    attempts = max(1, int(retries))
    for attempt in range(1, attempts + 1):
        response: requests.Response | None = None
        try:
            _pace(url)
            response = requests.get(url, headers=headers, params=params, timeout=timeout)

            if response.status_code == 429:
                if attempt >= attempts:
                    response.raise_for_status()
                retry_after = _retry_after_seconds(response)
                delay = retry_after if retry_after is not None else min(8.0, 0.75 * (2 ** (attempt - 1)) + random.uniform(0.05, 0.35))
                time.sleep(delay)
                continue

            if 500 <= response.status_code <= 599:
                if attempt >= attempts:
                    response.raise_for_status()
                time.sleep(min(5.0, 0.5 * (2 ** (attempt - 1)) + random.uniform(0.05, 0.30)))
                continue

            response.raise_for_status()
            raw = (response.text or "").lstrip("\ufeff").strip()
            if raw.startswith("<"):
                preview = raw[:180].replace("\r", " ").replace("\n", " ")
                raise HttpError(f"API가 HTML 문서를 반환했습니다. 응답 시작: {preview}")

            parsed = _parse_json_like(raw)
            with _HTTP_LOCK:
                _HTTP_MEMO[key] = copy.deepcopy(parsed)
            return parsed
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(min(4.0, 0.4 * (2 ** (attempt - 1)) + random.uniform(0.05, 0.25)))
                continue
        except (requests.RequestException, ValueError, HttpError) as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(min(4.0, 0.4 * (2 ** (attempt - 1)) + random.uniform(0.05, 0.25)))
                continue
        break
    raise HttpError(f"요청 실패: {last_error}")
