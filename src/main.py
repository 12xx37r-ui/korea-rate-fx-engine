from __future__ import annotations

import os
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from src.collectors import ecos, kosis, krx, reb, us_policy
from src.core.io import read_json, write_json
from src.models.krw_strength import build_snapshot
from src.models.korea_policy_v2 import (
    build_fx_forecast_v2,
    build_rate_forecast_v2,
)


def run_collector(
    name,
    collector,
    output_dir,
    timeout,
    retries,
):
    started = time.monotonic()

    print(
        f"[START] {name} collector",
        flush=True,
    )

    try:
        result = collector.collect(
            output_dir,
            timeout,
            retries,
        )

        elapsed = time.monotonic() - started

        print(
            (
                f"[DONE] {name} collector "
                f"| status={result.status} "
                f"| elapsed={elapsed:.1f}s"
            ),
            flush=True,
        )

        return result

    except Exception as exc:
        elapsed = time.monotonic() - started

        print(
            (
                f"[ERROR] {name} collector "
                f"| elapsed={elapsed:.1f}s "
                f"| {type(exc).__name__}: {exc}"
            ),
            flush=True,
        )

        raise


def main():
    total_started = time.monotonic()

    print(
        "[ENGINE] Korea Rate FX Engine started",
        flush=True,
    )

    settings = read_json(
        "config/settings.json"
    )

    timeout = int(
        settings["request_timeout_seconds"]
    )

    retries = int(
        settings["max_retries"]
    )

    tz_name = os.getenv(
        "MODEL_TIMEZONE",
        settings["timezone"],
    )

    now = datetime.now(
        ZoneInfo(tz_name)
    )

    output_dir = Path("output")

    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    print(
        (
            f"[CONFIG] timeout={timeout}s "
            f"| retries={retries} "
            f"| timezone={tz_name}"
        ),
        flush=True,
    )

    results = []

    results.append(
        run_collector(
            "US_POLICY",
            us_policy,
            output_dir,
            timeout,
            retries,
        )
    )

    results.append(
        run_collector(
            "ECOS",
            ecos,
            output_dir,
            timeout,
            retries,
        )
    )

    results.append(
        run_collector(
            "KRX",
            krx,
            output_dir,
            timeout,
            retries,
        )
    )

    results.append(
        run_collector(
            "KOSIS",
            kosis,
            output_dir,
            timeout,
            retries,
        )
    )

    results.append(
        run_collector(
            "REB",
            reb,
            output_dir,
            timeout,
            retries,
        )
    )

    print(
        "[ENGINE] All collectors finished",
        flush=True,
    )

    credential_items = []

    for result in results:
        credential_status = (
            result.metadata.get(
                "credential_status"
            )
        )

        if credential_status:
            credential_items.append(
                {
                    "source": result.source,
                    "secret_name": (
                        result.metadata.get(
                            "secret_name"
                        )
                    ),
                    "status": (
                        credential_status
                    ),
                    "action_required": bool(
                        result.metadata.get(
                            "action_required"
                        )
                    ),
                    "action": (
                        result.metadata.get(
                            "action"
                        )
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
            result.source: (
                result.to_dict()
            )
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
                in settings[
                    "required_sources"
                ]
                and result.status
                not in {
                    "ok",
                    "degraded",
                    "not_configured",
                }
            )
        ],
        "warnings": [
            (
                f"{result.source}: "
                f"{result.message}"
            )
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

    print(
        "[WRITE] api_health.json",
        flush=True,
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

    print(
        "[WRITE] api_key_status.json",
        flush=True,
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

    print(
        "[WRITE] raw_manifest.json",
        flush=True,
    )

    ecos_path = (
        output_dir / "raw_ecos.json"
    )

    kosis_path = (
        output_dir / "raw_kosis.json"
    )

    us_policy_path = (
        output_dir / "us_input.json"
    )

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

    print(
        "[MODEL] Building legacy snapshot",
        flush=True,
    )

    snapshot = build_snapshot(
        ecos_data,
        kosis_data,
        us_policy_data,
    )

    snapshot["generated_at"] = (
        now.isoformat()
    )

    write_json(
        output_dir
        / "korea_rate_fx_outlook.json",
        snapshot,
    )

    print(
        "[WRITE] korea_rate_fx_outlook.json",
        flush=True,
    )

    write_json(
        output_dir
        / "krw_strength_preview.json",
        snapshot,
    )

    print(
        "[WRITE] krw_strength_preview.json",
        flush=True,
    )

    print(
        "[MODEL] Building Korea rate V2",
        flush=True,
    )

    rate_v2 = build_rate_forecast_v2(
        ecos_data,
        kosis_data,
        us_policy_data,
    )

    rate_v2["generated_at"] = (
        now.isoformat()
    )

    print(
        "[MODEL] Building Korea FX V2",
        flush=True,
    )

    fx_v2 = build_fx_forecast_v2(
        snapshot,
        rate_v2,
    )

    fx_v2["generated_at"] = (
        now.isoformat()
    )

    write_json(
        output_dir
        / "korea_rate_forecast_v2.json",
        rate_v2,
    )

    print(
        "[WRITE] korea_rate_forecast_v2.json",
        flush=True,
    )

    write_json(
        output_dir
        / "korea_fx_forecast_v2.json",
        fx_v2,
    )

    print(
        "[WRITE] korea_fx_forecast_v2.json",
        flush=True,
    )

    write_json(
        output_dir
        / "korea_validation_v2.json",
        {
            "schema_version": "2.0.0",
            "generated_at": (
                now.isoformat()
            ),
            "rate": rate_v2.get(
                "validation",
                {},
            ),
            "fx": fx_v2.get(
                "validation",
                {},
            ),
            "us_engine_modified": False,
            "legacy_outputs_preserved": True,
        },
    )

    print(
        "[WRITE] korea_validation_v2.json",
        flush=True,
    )

    elapsed = (
        time.monotonic()
        - total_started
    )

    print(
        (
            "[SUCCESS] Korea Rate FX Engine "
            f"finished in {elapsed:.1f}s"
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
