from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from src.core.http import get_json
from src.core.io import write_json
from src.core.result import SourceResult


REQUIRED_PATHS = [
    ("fed", "expected_path"),
    ("fed", "next_meeting"),
    ("us_market",),
    ("quality",),
]


def _has_path(data: dict[str, Any], path: tuple[str, ...]) -> bool:
    node: Any = data

    for key in path:
        if not isinstance(node, dict) or key not in node:
            return False
        node = node[key]

    return True


def _convert_us_engine_schema(data: dict[str, Any]) -> dict[str, Any]:
    """
    미국 정책금리 엔진의 최신 출력 구조를
    한국 금리·환율 엔진이 사용하는 호환 구조로 변환한다.

    미국 엔진 주요 필드:
    - meeting_path
    - market_path
    - next_fomc
    - probabilities
    - features
    - curves
    - confidence

    한국 엔진 호환 필드:
    - fed.expected_path
    - fed.next_meeting
    - us_market
    - quality

    원본 필드는 삭제하지 않고 그대로 보존한다.
    """

    normalized = dict(data)

    meeting_path = data.get("meeting_path")
    if not isinstance(meeting_path, list):
        meeting_path = []

    market_path = data.get("market_path")
    if not isinstance(market_path, dict):
        market_path = {}

    probabilities = data.get("probabilities")
    if not isinstance(probabilities, dict):
        probabilities = {}

    market_action_probabilities = data.get(
        "market_implied_action_probabilities"
    )
    if not isinstance(market_action_probabilities, dict):
        market_action_probabilities = {}

    features = data.get("features")
    if not isinstance(features, dict):
        features = {}

    curves = data.get("curves")
    if not isinstance(curves, dict):
        curves = {}

    confidence = data.get("confidence")
    if not isinstance(confidence, dict):
        confidence = {}

    next_fomc = data.get("next_fomc")

    next_meeting = {
        "date": next_fomc,
        "meeting": market_path.get("meeting", next_fomc),
        "current_effective_rate": data.get("current_effective_rate"),
        "current_effective_rate_source": data.get(
            "current_effective_rate_source"
        ),
        "expected_post_meeting_rate": market_path.get(
            "expected_post_meeting_rate"
        ),
        "expected_change_bps": market_path.get("expected_change_bps"),
        "contract_symbol": market_path.get("contract_symbol"),
        "probabilities": probabilities,
        "action_probabilities": (
            market_path.get("action_probabilities")
            if isinstance(
                market_path.get("action_probabilities"),
                dict,
            )
            else market_action_probabilities
        ),
        "target_rate_probabilities": (
            market_path.get("target_rate_probabilities")
            if isinstance(
                market_path.get("target_rate_probabilities"),
                dict,
            )
            else data.get("market_implied_target_probabilities", {})
        ),
    }

    normalized["schema_version"] = data.get(
        "schema_version",
        data.get("engine_version", "unknown"),
    )

    normalized["fed"] = {
        "expected_path": meeting_path,
        "next_meeting": next_meeting,
        "current_effective_rate": data.get("current_effective_rate"),
        "current_effective_rate_source": data.get(
            "current_effective_rate_source"
        ),
        "probabilities": probabilities,
        "market_path": market_path,
    }

    normalized["us_market"] = {
        "features": features,
        "weights": data.get("weights", {}),
        "curves": curves,
        "macro_blocks": data.get("macro_blocks", {}),
        "market_path": market_path,
        "meeting_path": meeting_path,
        "current_effective_rate": data.get("current_effective_rate"),
        "probabilities": probabilities,
    }

    normalized["quality"] = {
        "score": confidence.get("score"),
        "grade": confidence.get("grade"),
        "confidence": confidence,
        "warnings": data.get("warnings", []),
        "generated_at": data.get(
            "generated_at",
            data.get("generated_at_utc"),
        ),
        "engine_version": data.get("engine_version"),
    }

    return normalized


def collect(
    output_dir: Path,
    timeout: int,
    retries: int,
) -> SourceResult:
    url = os.getenv("US_POLICY_JSON_URL", "").strip()

    if not url:
        return SourceResult(
            "us_policy_engine",
            "not_configured",
            "US_POLICY_JSON_URL이 설정되지 않았습니다.",
        )

    headers: dict[str, str] = {}

    token = os.getenv("US_REPO_READ_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        raw_data = get_json(
            url,
            headers=headers,
            timeout=timeout,
            retries=retries,
        )

        if not isinstance(raw_data, dict):
            return SourceResult(
                "us_policy_engine",
                "invalid_schema",
                "미국 엔진 응답이 JSON 객체 형식이 아닙니다.",
            )

        data = _convert_us_engine_schema(raw_data)

        missing: list[str] = []

        for path in REQUIRED_PATHS:
            if not _has_path(data, path):
                missing.append(".".join(path))

        payload_path = output_dir / "us_input.json"
        write_json(payload_path, data)

        if missing:
            return SourceResult(
                "us_policy_engine",
                "invalid_schema",
                "미국 엔진 JSON 필수 필드가 없습니다.",
                payload_path=str(payload_path),
                warnings=missing,
                metadata={
                    "source_engine_version": raw_data.get(
                        "engine_version"
                    ),
                    "conversion_applied": True,
                },
            )

        expected_path = data["fed"]["expected_path"]

        return SourceResult(
            "us_policy_engine",
            "ok",
            "미국 정책금리 엔진 JSON을 정상적으로 읽고 호환 구조로 변환했습니다.",
            rows=len(expected_path),
            latest_observation=data["fed"]["next_meeting"].get("date"),
            payload_path=str(payload_path),
            metadata={
                "schema_version": data.get("schema_version"),
                "source_engine_version": raw_data.get(
                    "engine_version"
                ),
                "conversion_applied": True,
                "next_fomc": raw_data.get("next_fomc"),
            },
        )

    except Exception as exc:
        return SourceResult(
            "us_policy_engine",
            "error",
            f"{type(exc).__name__}: {exc}",
        )
