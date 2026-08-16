from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output"


def read_json(path: Path) -> dict[str, Any]:
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def parse_dt(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        pass
    for fmt in ("%Y%m%d", "%Y-%m-%d", "%Y%m", "%Y-%m"):
        try:
            dt = datetime.strptime(text[:10], fmt).replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            continue
    return None


def age_hours(value: Any) -> float | None:
    dt = parse_dt(value)
    if dt is None:
        return None
    return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0)


def classify(*, available: bool, reused: bool = False, fallback: bool = False, age: float | None = None, max_age: float | None = None) -> str:
    if not available:
        return "UNAVAILABLE"
    if fallback:
        return "FALLBACK"
    if reused:
        return "LKG"
    if age is not None and max_age is not None and age > max_age:
        return "CACHE"
    return "LIVE"


def main() -> None:
    health = read_json(OUT / "api_health.json")
    v3 = read_json(OUT / "korea_rate_fx_outlook_v3.json")
    global_raw = read_json(OUT / "raw_global_market.json")
    sources = health.get("sources") if isinstance(health.get("sources"), dict) else {}

    items: list[dict[str, Any]] = []
    specs = {
        "us_policy_engine": (72.0, "event/daily"),
        "ecos": (96.0, "daily/monthly"),
        "global_market": (96.0, "daily/monthly"),
        "krx": (96.0, "trading-day"),
        "kosis": (24.0 * 45, "monthly"),
    }
    for name, (max_age, cadence) in specs.items():
        src = sources.get(name) if isinstance(sources.get(name), dict) else {}
        latest = src.get("latest_observation")
        age = age_hours(latest)
        meta = src.get("metadata") if isinstance(src.get("metadata"), dict) else {}
        reused = bool(meta.get("last_good_reused") or meta.get("circuit_last_good_reused"))
        status = classify(
            available=src.get("status") in {"ok", "degraded", "not_configured"} and bool(src),
            reused=reused,
            fallback=src.get("status") == "not_configured",
            age=age,
            max_age=max_age,
        )
        items.append({
            "source": name,
            "status": status,
            "collector_status": src.get("status"),
            "cadence": cadence,
            "observation_at": latest,
            "age_hours": round(age, 2) if age is not None else None,
            "max_age_hours": max_age,
            "last_good_reused": reused,
            "warnings": src.get("warnings") or [],
        })

    snap = global_raw.get("usd_krw_market_snapshot") if isinstance(global_raw.get("usd_krw_market_snapshot"), dict) else {}
    snap_age = age_hours(snap.get("market_time_utc"))
    items.append({
        "source": "usdkrw_market_snapshot",
        "status": classify(available=snap.get("price") is not None, age=snap_age, max_age=3.0),
        "cadence": "intraday-when-market-open",
        "observation_at": snap.get("market_time_utc"),
        "age_hours": round(snap_age, 2) if snap_age is not None else None,
        "max_age_hours": 3.0,
        "value": snap.get("price"),
        "provider": snap.get("source"),
        "note": "모델 기준 일봉과 분리된 보조 market snapshot이며 기존 current_usdkrw 의미를 변경하지 않습니다.",
    })

    payload = {
        "schema_version": "1.0.0",
        "patch_version": "V217-additive-freshness-contract",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "engine_generated_at": v3.get("generated_at"),
        "compatibility": {
            "existing_keys_removed": False,
            "existing_field_semantics_changed": False,
            "new_network_calls": 0,
            "model_formulas_changed": False,
        },
        "items": items,
        "summary": {
            "live": sum(x["status"] == "LIVE" for x in items),
            "cache": sum(x["status"] == "CACHE" for x in items),
            "lkg": sum(x["status"] == "LKG" for x in items),
            "fallback": sum(x["status"] == "FALLBACK" for x in items),
            "unavailable": sum(x["status"] == "UNAVAILABLE" for x in items),
        },
    }
    (OUT / "freshness_status.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload["summary"], ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
