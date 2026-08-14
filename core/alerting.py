"""Active alerting — Telegram / Slack webhook.

환경변수로 채널을 설정한다. 미설정 채널은 무음 처리.
  TELEGRAM_BOT_TOKEN   — 봇 토큰
  TELEGRAM_CHAT_ID     — 채팅 ID (음수 허용, 예: -100xxxxxxxxx)
  SLACK_WEBHOOK_URL    — Incoming Webhook URL

트리거 조건:
  1. 소스별 연속 실패 횟수가 임계값(기본 3회) 이상
  2. Data Quality Gate 이슈 발생
  3. alert_level이 이전 실행 대비 상승 (NOTICE→WARNING, WARNING→ALERT)

파이프라인 중단 없음: 모든 전송 실패는 stderr 로그 후 무시.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

import requests

_TELEGRAM_URL = "https://api.telegram.org/bot{token}/sendMessage"
_CONNECT_TIMEOUT = 5
_READ_TIMEOUT    = 10
_ALERT_THRESHOLD = 3   # consecutive failures → WARNING+


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _telegram_send(text: str) -> bool:
    token   = _env("TELEGRAM_BOT_TOKEN")
    chat_id = _env("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False
    url = _TELEGRAM_URL.format(token=token)
    try:
        r = requests.post(
            url,
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
        )
        r.raise_for_status()
        return True
    except Exception as exc:
        print(f"[ALERT-WARN] Telegram 전송 실패: {type(exc).__name__}: {exc}", flush=True)
        return False


def _slack_send(text: str) -> bool:
    url = _env("SLACK_WEBHOOK_URL")
    if not url:
        return False
    try:
        r = requests.post(
            url,
            json={"text": text},
            timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
        )
        r.raise_for_status()
        return True
    except Exception as exc:
        print(f"[ALERT-WARN] Slack 전송 실패: {type(exc).__name__}: {exc}", flush=True)
        return False


def _format_failure_alert(
    failing: list[tuple[str, dict[str, Any]]],
    dq_issues: list[str],
    engine_version: str,
    now_iso: str,
) -> str:
    lines = [
        "🚨 <b>[한국시장엔진] API 장애 알림</b>",
        f"⏰ {now_iso[:19].replace('T', ' ')}",
        f"🔧 {engine_version}",
        "",
    ]

    if failing:
        lines.append("📡 <b>연속 실패 소스:</b>")
        for src, ft in failing:
            cnt   = ft["consecutive_failure_count"]
            level = ft["alert_level"]
            lines.append(f"  • <b>{src}</b> — {cnt}회 연속 실패 [{level}]")
        lines.append("")

    if dq_issues:
        lines.append("⚠️ <b>Data Quality 이슈:</b>")
        for issue in dq_issues[:10]:
            lines.append(f"  • {issue}")
        if len(dq_issues) > 10:
            lines.append(f"  … 외 {len(dq_issues)-10}건")
        lines.append("")

    lines.append("→ GitHub Actions 로그 확인 요망")
    return "\n".join(lines)


def _level_rank(level: str) -> int:
    return {"OK": 0, "NOTICE": 1, "WARNING": 2, "ALERT": 3}.get(level, 0)


def send_alerts(
    failure_tracking: dict[str, dict[str, Any]],
    dq_summary: dict[str, Any],
    engine_version: str,
    now_iso: str,
    prev_health: dict[str, Any] | None = None,
    threshold: int = _ALERT_THRESHOLD,
) -> None:
    """연속 실패 임계 초과 또는 DQ 이슈 발생 시 Telegram/Slack 발송."""
    # Collect sources at WARNING+ and at/above threshold
    failing: list[tuple[str, dict[str, Any]]] = []
    for src, ft in failure_tracking.items():
        cnt   = ft.get("consecutive_failure_count", 0)
        level = ft.get("alert_level", "OK")
        if cnt >= threshold or _level_rank(level) >= _level_rank("WARNING"):
            # Only escalate (new alert or worsened level)
            prev_src = (prev_health or {}).get("failure_tracking", {}).get(src, {})
            prev_level = prev_src.get("alert_level", "OK")
            if _level_rank(level) >= _level_rank("WARNING") and _level_rank(level) >= _level_rank(prev_level):
                failing.append((src, ft))

    dq_issues = dq_summary.get("all_issues", [])

    if not failing and not dq_issues:
        return

    text = _format_failure_alert(failing, dq_issues, engine_version, now_iso)

    sent_any = False
    sent_any |= _telegram_send(text)
    # Slack uses plain text (strip HTML tags for readability)
    plain = text.replace("<b>", "*").replace("</b>", "*").replace("<br>", "\n")
    sent_any |= _slack_send(plain)

    if sent_any:
        triggered = [s for s, _ in failing]
        print(
            f"[ALERT] 알림 발송 완료 | "
            f"실패소스={triggered} dq_issues={len(dq_issues)}건",
            flush=True,
        )
    else:
        print(
            f"[ALERT-SKIP] 웹훅 미설정 (TELEGRAM_BOT_TOKEN / SLACK_WEBHOOK_URL)"
            f" - 알림 조건 충족했으나 전송 채널 없음",
            flush=True,
        )
