from __future__ import annotations
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from src.collectors import ecos, kosis, krx, reb, us_policy
from src.core.io import read_json, write_json
from src.models.krw_strength import build_snapshot


def main():
    settings = read_json("config/settings.json")
    timeout = int(settings["request_timeout_seconds"])
    retries = int(settings["max_retries"])
    tz_name = os.getenv("MODEL_TIMEZONE", settings["timezone"])
    now = datetime.now(ZoneInfo(tz_name))
    output_dir = Path("output")
    output_dir.mkdir(parents=True, exist_ok=True)

    results = [
        us_policy.collect(output_dir, timeout, retries),
        ecos.collect(output_dir, timeout, retries),
        krx.collect(output_dir, timeout, retries),
        kosis.collect(output_dir, timeout, retries),
        reb.collect(output_dir, timeout, retries),
    ]

    credential_items = []
    for result in results:
        credential_status = result.metadata.get("credential_status")
        if credential_status:
            credential_items.append({
                "source": result.source,
                "secret_name": result.metadata.get("secret_name"),
                "status": credential_status,
                "action_required": bool(result.metadata.get("action_required")),
                "action": result.metadata.get("action"),
                "message": result.metadata.get("message") or result.message,
            })

    credential_alerts = [item for item in credential_items if item["action_required"]]
    health = {
        "schema_version": settings["schema_version"],
        "generated_at": now.isoformat(),
        "sources": {r.source: r.to_dict() for r in results},
        "api_credentials": {
            "status": "action_required" if credential_alerts else "ok",
            "action_required": bool(credential_alerts),
            "alerts": credential_alerts,
            "items": credential_items,
        },
        "blocking_errors": [r.source for r in results if r.source in settings["required_sources"] and r.status not in {"ok", "degraded", "not_configured"}],
        "warnings": [f"{r.source}: {r.message}" for r in results if r.status in {"degraded", "not_configured", "missing_secret", "credential_error"}],
    }
    write_json(output_dir / "api_health.json", health)
    write_json(output_dir / "api_key_status.json", {
        "generated_at": now.isoformat(),
        "status": "API_KEY_RENEWAL_REQUIRED" if credential_alerts else "OK",
        "renewal_required": bool(credential_alerts),
        "alerts": credential_alerts,
        "items": credential_items,
    })
    write_json(output_dir / "raw_manifest.json", {"generated_at": now.isoformat(), "files": [r.payload_path for r in results if r.payload_path]})

    ecos_data = read_json(output_dir / "raw_ecos.json") if (output_dir / "raw_ecos.json").exists() else {}
    kosis_data = read_json(output_dir / "raw_kosis.json") if (output_dir / "raw_kosis.json").exists() else {}
    us_policy_data = read_json(output_dir / "raw_us_policy.json") if (output_dir / "raw_us_policy.json").exists() else None
    snapshot = build_snapshot(ecos_data, kosis_data, us_policy_data)
    snapshot["generated_at"] = now.isoformat()
    write_json(output_dir / "korea_rate_fx_outlook.json", snapshot)
    # Keep the old filename for backward compatibility.
    write_json(output_dir / "krw_strength_preview.json", snapshot)

    print("Generated output/api_health.json")
    print("Generated output/api_key_status.json")
    print("Generated output/raw_manifest.json")
    print("Generated output/korea_rate_fx_outlook.json")
    print("Generated output/krw_strength_preview.json")


if __name__ == "__main__":
    main()
