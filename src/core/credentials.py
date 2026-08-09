from __future__ import annotations

import re
from typing import Any

_AUTH_PATTERNS = (
    "인증키", "api key", "apikey", "api_key", "invalid key", "invalid auth",
    "unauthorized", "forbidden", "expired", "만료", "유효하지", "등록되지",
    "승인되지", "접근권한", "인증 실패", "authentication", "authorization",
)

# Common public-data API error codes. Text matching remains the primary guard
# because providers occasionally change the exact code while keeping the message.
_AUTH_CODES = {
    "INFO-100", "INFO-200", "ERROR-100", "ERROR-200",
}


def credential_issue(exc_or_text: Any) -> bool:
    text = str(exc_or_text or "").strip().lower()
    if not text:
        return False
    # Network exceptions often include the request URL and therefore the literal
    # query parameter ``apiKey=...``.  That must never be classified as an invalid
    # credential.  Transport failures are diagnostic/degraded, not credential_error.
    network_markers = (
        "connecttimeouterror", "readtimeout", "read timed out", "connect timeout",
        "connection to", "connectionerror", "max retries exceeded", "name resolution",
        "temporary failure", "network is unreachable",
    )
    if any(marker in text for marker in network_markers):
        return False
    if any(pattern in text for pattern in _AUTH_PATTERNS):
        return True
    codes = set(re.findall(r"(?:info|error)-\d+|\b\d{1,3}\b", text, flags=re.I))
    normalized = {code.upper() for code in codes}
    return bool(normalized & _AUTH_CODES) and any(word in text for word in ("오류", "error", "result"))


def credential_metadata(secret_name: str, status: str, message: str = "") -> dict[str, Any]:
    action_required = status in {"missing", "renewal_required"}
    if status == "valid":
        action = "없음"
    else:
        action = f"GitHub Settings → Secrets and variables → Actions에서 {secret_name} 값을 새 API 키로 교체한 뒤 workflow를 다시 실행하세요."
    return {
        "credential_status": status,
        "secret_name": secret_name,
        "action_required": action_required,
        "action": action,
        "message": message,
    }
