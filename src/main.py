from __future__ import annotations

import os
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from src.collectors import ecos, kosis, krx, reb, us_policy
from src.core.io import read_json, write_json
from src.models.krw_strength import build_snapshot
from src.models.korea_policy_v2 import build_fx_forecast_v2, build_rate_forecast_v2


def run_collector(name, collector, output_dir, timeout, retries):
    started = time.monotonic()
    print(f"[START] {name} collector", flush=True)
    try:
        result = collector.collect(output_dir, timeout, retries)
        elapsed = time.monotonic() - started
        print(f"[DONE] {name} collector | status={result.status} | elapsed={elapsed:.1f}s", flush=True)
        return result
    except Exception as exc:
        elapsed = time.monotonic() - started
        print(f"[ERROR] {name} collector | elapsed={elapsed:.1f}s | {type(exc).__name__}: {exc}", flush=True)
        raise


def main():
    settings = read_json("config/settings.json")

    timeout = int(settings["request_timeout_seconds"])
    retries = int(settings["max_retries"])

    tz_name = os.getenv(
        "MODEL_TIMEZONE",
        settings["timezone"],
    )

    now = datetime.now(ZoneInfo(tz_name))

    output_dir = Path("output")
    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    results = [
        run_collector("US_POLICY", us_policy, output_dir, timeout, retries),
        run_collector("ECOS", ecos, output_dir, timeout, retries),
        run_collector("KRX", krx, output_dir, timeout, retries),
        run_collector("KOSIS", kosis, output_dir, timeout, retries),
        run_collector("REB", reb, output_dir, timeout, retries),
    ]


    credential_items = []

    for result in results:
        credential_status = result.metadata.get(
            "credential_status"
        )

        if credential_status:
            credential_items.append(
                {
                    "source": result.source,
                    "secret_name": result.metadata.get(
                        "secret_name"
                    ),
                    "status": credential_status,
                    "action_required": bool(
                        result.metadata.get(
                            "action_required"
                        )
                    ),
                    "action": result.metadata.get(
                        "action"
                    ),
                    "message": (
                        result.metadata.get(
                            "message"
                        )
                        or result.message
                    ),
                }
            )

    credential_alerts = [
        item
        for item in credential_items
        if item["action_required"]
    ]

    health = {
        "schema_version": settings[
            "schema_version"
        ],
        "generated_at": now.isoformat(),
        "sources": {
            result.source: result.to_dict()
            for result in results
        },
        "api_credentials": {
            "status": (
                "action_required"
                if credential_alerts
                else "ok"
            ),
            "action_required": bool(
                credential_alerts
            ),
            "alerts": credential_alerts,
            "items": credential_items,
        },
        "blocking_errors": [
            result.source
            for result in results
            if (
                result.source
                in settings["required_sources"]
                and result.status
                not in {
                    "ok",
                    "degraded",
                    "not_configured",
                }
            )
        ],
        "warnings": [
            f"{result.source}: {result.message}"
            for result in results
            if result.status
            in {
                "degraded",
                "not_configured",
                "missing_secret",
                "credential_error",
            }
        ],
    }

    write_json(
        output_dir / "api_health.json",
        health,
    )

    write_json(
        output_dir / "api_key_status.json",
        {
            "generated_at": now.isoformat(),
            "status": (
                "API_KEY_RENEWAL_REQUIRED"
                if credential_alerts
                else "OK"
            ),
            "renewal_required": bool(
                credential_alerts
            ),
            "alerts": credential_alerts,
            "items": credential_items,
        },
    )

    write_json(
        output_dir / "raw_manifest.json",
        {
            "generated_at": now.isoformat(),
            "files": [
                result.payload_path
                for result in results
                if result.payload_path
            ],
        },
    )

    ecos_path = output_dir / "raw_ecos.json"
    kosis_path = output_dir / "raw_kosis.json"
    us_policy_path = output_dir / "us_input.json"

    ecos_data = (
        read_json(ecos_path)
        if ecos_path.exists()
        else {}
    )

    kosis_data = (
        read_json(kosis_path)
        if kosis_path.exists()
        else {}
    )

    us_policy_data = (
        read_json(us_policy_path)
        if us_policy_path.exists()
        else None
    )

    snapshot = build_snapshot(
        ecos_data,
        kosis_data,
        us_policy_data,
    )

    snapshot["generated_at"] = now.isoformat()

    write_json(
        output_dir
        / "korea_rate_fx_outlook.json",
        snapshot,
    )

    # 기존 파일명을 사용하는 화면과의 호환성을 유지한다.
    write_json(
        output_dir
        / "krw_strength_preview.json",
        snapshot,
    )

    # V2는 기존 한국 엔진 출력과 병행되는 독립 예측·검증 계층이다.
    # 미국 엔진 파일은 읽기 전용 입력으로만 사용한다.
    source_status = {result.source: result.status for result in results}
    rate_v2 = build_rate_forecast_v2(
        ecos_data,
        kosis_data,
        us_policy_data,
        source_status=source_status,
    )
    rate_v2["generated_at"] = now.isoformat()
    fx_v2 = build_fx_forecast_v2(snapshot, rate_v2, ecos_data)
    fx_v2["generated_at"] = now.isoformat()

    write_json(output_dir / "korea_rate_forecast_v2.json", rate_v2)
    write_json(output_dir / "korea_fx_forecast_v2.json", fx_v2)
    write_json(
        output_dir / "korea_validation_v2.json",
        {
            "schema_version": "2.0.0",
            "generated_at": now.isoformat(),
            "rate": rate_v2.get("validation", {}),
            "fx": fx_v2.get("validation", {}),
            "us_engine_modified": False,
            "legacy_outputs_preserved": True,
        },
    )

    # 오늘 시점의 실제 입력·예측을 누적 보관한다. 이 아카이브는 앞으로
    # 실시간 빈티지 백테스트를 가능하게 하며 기존 출력과 완전히 분리된다.
    vintage_dir = output_dir / "vintages"
    vintage_dir.mkdir(parents=True, exist_ok=True)
    vintage_path = vintage_dir / f"{now.date().isoformat()}.json"
    write_json(
        vintage_path,
        {
            "schema_version": "2.2.0",
            "captured_at": now.isoformat(),
            "rate_forecast": rate_v2,
            "fx_forecast": fx_v2,
            "source_status": source_status,
            "us_engine_modified": False,
        },
    )

    print(
        "Generated output/api_health.json"
    )
    print(
        "Generated output/api_key_status.json"
    )
    print(
        "Generated output/raw_manifest.json"
    )
    print(
        "Generated output/korea_rate_fx_outlook.json"
    )
    print(
        "Generated output/krw_strength_preview.json"
    )
    print("Generated output/korea_rate_forecast_v2.json")
    print("Generated output/korea_fx_forecast_v2.json")
    print("Generated output/korea_validation_v2.json")
    print(f"Generated {vintage_path}")


if __name__ == "__main__":
    main()
