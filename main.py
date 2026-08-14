from __future__ import annotations

import os
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Any

from src.collectors import ecos, global_market, kosis, krx, reb, us_policy
from src.collectors import korea_asset_fundamentals, korea_equity_environment
from src.core.io import read_json, write_json
from src.core.result import SourceResult
from src.models.krw_strength import build_snapshot, build_krw_strength_forecast
from src.models.korea_policy_v2 import build_fx_forecast_v2, build_rate_forecast_v2
from src.models.korea_outlook_v3 import build_v3
from src.models.krw_liquidity import build_krw_liquidity_forecast
from src.models.rate_validation import evaluate_rate_vintage_snapshots
from src.models.korea_equity_environment import build_and_write as build_korea_equity_environment
from src.models.korea_comprehensive_market import build_and_write as build_comprehensive_market


ENGINE_VERSION = "4.8.0-long-history-rate-oos-vintage-accumulator"


def _safe_read(path: Path, default: Any) -> Any:
    try:
        return read_json(path) if path.exists() else default
    except Exception:
        return default


def _merge_series_payload(current: Any, previous: Any) -> dict[str, Any]:
    """Keep last-good series when a collector fails or returns an empty subseries.

    GitHub Actions checks out the previous committed output before each run, so this
    continuity layer can preserve historical inputs without inventing values.
    """
    current = current if isinstance(current, dict) else {}
    previous = previous if isinstance(previous, dict) else {}
    merged: dict[str, Any] = dict(previous)
    for key, value in current.items():
        if isinstance(value, list):
            if value:
                merged[key] = value
            elif key not in merged:
                merged[key] = []
        elif value not in (None, "", {}):
            merged[key] = value
        elif key not in merged:
            merged[key] = value
    return merged


def run_collector(name: str, collector, output_dir: Path, timeout: int, retries: int) -> SourceResult:
    """A single source outage must never terminate the Korea forecast pipeline."""
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
        return SourceResult(
            source=name.lower(),
            status="error",
            message=f"collector exception: {type(exc).__name__}: {exc}",
            metadata={"continuity_fallback_allowed": True},
        )


def _forecast_continuity(current: dict[str, Any], previous: dict[str, Any], now_iso: str, kind: str) -> tuple[dict[str, Any], bool]:
    if kind == "fx":
        usable = bool(current.get("forecast_operational")) and bool(current.get("forecast_path"))
    elif kind == "rate":
        usable = bool((current.get("current") or {}).get("kr_base_rate_pct") is not None) and bool(current.get("meeting_path"))
    else:
        usable = bool(current.get("forecast_operational")) and bool(current.get("forecast_path"))
    if usable or not isinstance(previous, dict) or not previous:
        return current, False
    carried = dict(previous)
    carried["generated_at"] = now_iso
    carried["continuity_mode"] = "previous_committed_forecast"
    carried["continuity_reason"] = "current run lacked enough raw history; retained last committed forecast rather than fabricating values"
    carried["forecast_operational"] = True
    return carried, True


def main() -> None:
    settings = read_json("config/settings.json")
    timeout = int(settings["request_timeout_seconds"])
    retries = int(settings["max_retries"])
    tz_name = os.getenv("MODEL_TIMEZONE", settings["timezone"])
    now = datetime.now(ZoneInfo(tz_name))
    now_iso = now.isoformat()

    output_dir = Path("output")
    output_dir.mkdir(parents=True, exist_ok=True)

    # Snapshot committed inputs/forecasts before collectors can overwrite them.
    paths = {
        "ecos": output_dir / "raw_ecos.json",
        "kosis": output_dir / "raw_kosis.json",
        "global": output_dir / "raw_global_market.json",
        "krx": output_dir / "raw_krx.json",
        "us": output_dir / "us_input.json",
        "fx": output_dir / "korea_fx_forecast_v2.json",
        "rate": output_dir / "korea_rate_forecast_v2.json",
        "liquidity": output_dir / "korea_krw_liquidity_forecast.json",
        "strength": output_dir / "korea_krw_strength_forecast.json",
    }
    previous = {key: _safe_read(path, {}) for key, path in paths.items()}

    results = [
        run_collector("US_POLICY_ENGINE", us_policy, output_dir, timeout, retries),
        run_collector("ECOS", ecos, output_dir, timeout, retries),
        run_collector("GLOBAL_MARKET", global_market, output_dir, timeout, retries),
        run_collector("KRX", krx, output_dir, timeout, retries),
        run_collector("KOSIS", kosis, output_dir, timeout, retries),
        run_collector("REB", reb, output_dir, timeout, retries),
    ]

    try:
        korea_asset_fundamentals.collect(output_dir, timeout=min(timeout, 20))
    except Exception as exc:
        print(f"[WARN] KOREA_ASSET_FUNDAMENTALS | {type(exc).__name__}: {exc}", flush=True)

    # Merge current raw data with last-good committed series. Empty current blocks do
    # not erase usable history.
    ecos_data = _merge_series_payload(_safe_read(paths["ecos"], {}), previous["ecos"])
    kosis_data = _merge_series_payload(_safe_read(paths["kosis"], {}), previous["kosis"])
    global_data = _merge_series_payload(_safe_read(paths["global"], {}), previous["global"])
    krx_data = _merge_series_payload(_safe_read(paths["krx"], {}), previous["krx"])
    us_current = _safe_read(paths["us"], {})
    us_policy_data = us_current if isinstance(us_current, dict) and us_current else previous["us"]

    write_json(paths["ecos"], ecos_data)
    write_json(paths["kosis"], kosis_data)
    write_json(paths["global"], global_data)
    if krx_data:
        write_json(paths["krx"], krx_data)
    if us_policy_data:
        write_json(paths["us"], us_policy_data)

    # Korea-equity-specific sub-engine. Keep it isolated from all existing rate/FX
    # contracts. It runs AFTER continuity-merged raw files are written so credit
    # spread and other reused inputs see the same last-good data as the core engine.
    try:
        equity_raw = korea_equity_environment.collect(output_dir, timeout=min(timeout, 20))
        equity_environment = build_korea_equity_environment(output_dir, equity_raw)
        print(
            f"[DONE] KOREA_EQUITY_ENVIRONMENT | score={equity_environment.get('score')} "
            f"bias={equity_environment.get('bias')} coverage={equity_environment.get('data_coverage_pct')}%",
            flush=True,
        )
    except Exception as exc:
        print(f"[WARN] KOREA_EQUITY_ENVIRONMENT | {type(exc).__name__}: {exc}", flush=True)

    # 한국증시 종합환경 엔진 (보강 확장 모듈) — 기존 출력 파일 재사용, 신규 API 없음
    try:
        comprehensive = build_comprehensive_market(output_dir)
        cur_s = (comprehensive.get("summary") or {}).get("current_score")
        trn_s = (comprehensive.get("summary") or {}).get("trend_score")
        fwd_s = (comprehensive.get("summary") or {}).get("forward_overall")
        print(
            f"[DONE] KOREA_COMPREHENSIVE_MARKET | "
            f"current={cur_s} trend={trn_s:+d} forward={fwd_s}"
            if all(v is not None for v in (cur_s, trn_s, fwd_s))
            else f"[DONE] KOREA_COMPREHENSIVE_MARKET | current={cur_s} trend={trn_s} forward={fwd_s}",
            flush=True,
        )
    except Exception as exc:
        print(f"[WARN] KOREA_COMPREHENSIVE_MARKET | {type(exc).__name__}: {exc}", flush=True)

    source_status = {result.source.lower(): result.status for result in results}
    # Normalise collector aliases used by the modelling code.
    if "us_policy_engine" not in source_status:
        source_status["us_policy_engine"] = source_status.get("us_policy_engine", source_status.get("us_policy", "error"))

    snapshot = build_snapshot(ecos_data, kosis_data, us_policy_data, global_data=global_data)
    snapshot["generated_at"] = now_iso
    snapshot["engine_version"] = ENGINE_VERSION
    write_json(output_dir / "korea_rate_fx_outlook.json", snapshot)
    write_json(output_dir / "krw_strength_preview.json", snapshot)

    vintage_validation = evaluate_rate_vintage_snapshots(
        output_dir / "vintages",
        ecos_data.get("kr_base_rate", []),
        min_matured_samples=24,
    )
    rate_v2 = build_rate_forecast_v2(
        ecos_data,
        kosis_data,
        us_policy_data,
        source_status=source_status,
        vintage_validation=vintage_validation,
    )
    rate_v2["generated_at"] = now_iso
    rate_v2["engine_version"] = ENGINE_VERSION + "/rate"
    rate_v2, rate_continuity = _forecast_continuity(rate_v2, previous["rate"], now_iso, "rate")

    fx_v2 = build_fx_forecast_v2(
        snapshot,
        rate_v2,
        ecos_data,
        global_data=global_data,
    )
    fx_v2["generated_at"] = now_iso
    fx_v2, fx_continuity = _forecast_continuity(fx_v2, previous["fx"], now_iso, "fx")

    liquidity = build_krw_liquidity_forecast(ecos_data, rate_v2)
    liquidity["generated_at"] = now_iso
    liquidity, liquidity_continuity = _forecast_continuity(liquidity, previous["liquidity"], now_iso, "liquidity")

    strength = build_krw_strength_forecast(ecos_data, global_data, fx_v2, rate_v2)
    strength["generated_at"] = now_iso
    strength, strength_continuity = _forecast_continuity(strength, previous["strength"], now_iso, "strength")

    write_json(paths["rate"], rate_v2)
    write_json(paths["fx"], fx_v2)
    write_json(paths["liquidity"], liquidity)
    write_json(paths["strength"], strength)
    # Backward-compatible filename now carries the dedicated strength forecast rather
    # than a duplicate of the broad snapshot.
    write_json(output_dir / "krw_strength_preview.json", strength)

    v3 = build_v3(
        rate_v2,
        fx_v2,
        ecos_data,
        kosis_data,
        krx_data,
        global_data,
        us_policy_data,
        liquidity=liquidity,
        krw_strength=strength,
    )
    v3["generated_at"] = now_iso
    v3["engine_version"] = ENGINE_VERSION
    write_json(output_dir / "korea_rate_fx_outlook_v3.json", v3)

    # API/source health is diagnostic only; it no longer determines whether an
    # operational forecast exists.
    credential_items = []
    for result in results:
        credential_status = result.metadata.get("credential_status")
        if credential_status:
            credential_items.append(
                {
                    "source": result.source,
                    "secret_name": result.metadata.get("secret_name"),
                    "status": credential_status,
                    "action_required": bool(result.metadata.get("action_required")),
                    "action": result.metadata.get("action"),
                    "message": result.metadata.get("message") or result.message,
                }
            )
    credential_alerts = [item for item in credential_items if item["action_required"]]
    required = {str(x).lower() for x in settings.get("required_sources", [])}
    blocking = [
        result.source
        for result in results
        if result.source.lower() in required and result.status not in {"ok", "degraded", "not_configured"}
    ]
    health = {
        "schema_version": settings["schema_version"],
        "engine_version": ENGINE_VERSION,
        "generated_at": now_iso,
        "sources": {result.source: result.to_dict() for result in results},
        "api_credentials": {
            "status": "action_required" if credential_alerts else "ok",
            "action_required": bool(credential_alerts),
            "alerts": credential_alerts,
            "items": credential_items,
        },
        "blocking_errors": blocking,
        "forecast_engine_operational": bool(fx_v2.get("forecast_operational")) and bool(liquidity.get("forecast_operational")) and bool(strength.get("forecast_operational")),
        "continuity": {
            "rate_previous_forecast_used": rate_continuity,
            "fx_previous_forecast_used": fx_continuity,
            "liquidity_previous_forecast_used": liquidity_continuity,
            "strength_previous_forecast_used": strength_continuity,
            "raw_series_merge_enabled": True,
        },
        "warnings": [
            f"{result.source}: {result.message}"
            for result in results
            if result.status in {"degraded", "not_configured", "missing_secret", "credential_error", "error"}
        ],
    }
    write_json(output_dir / "api_health.json", health)
    write_json(
        output_dir / "api_key_status.json",
        {
            "generated_at": now_iso,
            "status": "API_KEY_RENEWAL_REQUIRED" if credential_alerts else "OK",
            "renewal_required": bool(credential_alerts),
            "alerts": credential_alerts,
            "items": credential_items,
        },
    )
    write_json(
        output_dir / "raw_manifest.json",
        {
            "generated_at": now_iso,
            "files": [
                "output/raw_ecos.json",
                "output/raw_kosis.json",
                "output/raw_global_market.json",
                "output/raw_krx.json" if (output_dir / "raw_krx.json").exists() else None,
                "output/us_input.json" if (output_dir / "us_input.json").exists() else None,
            ],
        },
    )

    fx_gate = (fx_v2.get("validation") or {}).get("quality_gate") or {}
    rate_gate = (rate_v2.get("validation") or {}).get("quality_gate") or {}
    production_readiness = {
        "schema_version": "2.0.0",
        "engine_version": ENGINE_VERSION,
        "generated_at": now_iso,
        "overall_level": "운영 예측 정상" if fx_v2.get("forecast_operational") else "연속성 예측 사용",
        "rate": {
            "level": rate_gate.get("level", "검증등급 산출"),
            "passed": bool(rate_gate.get("passed")),
            "candidate": bool(rate_gate.get("candidate")),
        },
        "fx": {
            "primary_level": fx_gate.get("level", "검증등급 산출"),
            "primary_passed": bool(fx_gate.get("passed")),
            "strict_passed": bool(fx_gate.get("strict_passed")),
            "passed_horizons": fx_gate.get("passed_horizons", []),
            "strict_passed_horizons": fx_gate.get("strict_passed_horizons", []),
            "forecast_operational": bool(fx_v2.get("forecast_operational")),
            "production_model": fx_v2.get("production_model"),
        },
        "liquidity": {
            "forecast_operational": bool(liquidity.get("forecast_operational")),
            "forecast_quality_score": (liquidity.get("quality") or {}).get("forecast_quality_score"),
            "forecast_quality_grade": (liquidity.get("quality") or {}).get("forecast_quality_grade"),
            "input_data_quality_score": (liquidity.get("quality") or {}).get("input_data_quality_score"),
            "quality_score_semantics": (liquidity.get("quality") or {}).get("quality_score_semantics"),
            "data_mode": liquidity.get("data_mode"),
        },
        "krw_strength": {
            "forecast_operational": bool(strength.get("forecast_operational")),
            "model_quality_score": (strength.get("quality") or {}).get("model_quality_score"),
            "quality_score_semantics": (strength.get("quality") or {}).get("quality_score_semantics"),
            "separate_oos_validated": bool((strength.get("quality") or {}).get("separate_oos_validated")),
            "independent_oos_primary_grade": (strength.get("quality") or {}).get("independent_oos_primary_grade"),
            "independent_oos_quality_score": (strength.get("quality") or {}).get("independent_oos_quality_score"),
            "weighted_group_coverage": ((strength.get("factor_panel") or {}).get("weighted_group_coverage")),
            "neer_available": ((strength.get("current") or {}).get("neer") is not None),
            "reer_available": ((strength.get("current") or {}).get("reer") is not None),
        },
        "continuity": health["continuity"],
        "certification_rule": "예측은 항상 산출하되 검증성적은 등급·확률·구간으로 분리 공개한다.",
    }
    write_json(output_dir / "korea_validation_v2.json", {
        "schema_version": "4.0.0",
        "engine_version": ENGINE_VERSION,
        "generated_at": now_iso,
        "rate": rate_v2.get("validation", {}),
        "fx": fx_v2.get("validation", {}),
        "liquidity": liquidity.get("quality", {}),
        "krw_strength": strength.get("quality", {}),
        "us_engine_modified": False,
        "legacy_outputs_preserved": True,
    })
    write_json(output_dir / "production_readiness_v2.json", production_readiness)

    vintage_dir = output_dir / "vintages"
    vintage_dir.mkdir(parents=True, exist_ok=True)
    vintage_path = vintage_dir / f"{now.date().isoformat()}.json"
    write_json(
        vintage_path,
        {
            "schema_version": "4.0.0",
            "captured_at": now_iso,
            "rate_forecast": rate_v2,
            "fx_forecast": fx_v2,
            "krw_liquidity_forecast": liquidity,
            "krw_strength_forecast": strength,
            "unified_outlook_v3": v3,
            "source_status": source_status,
            "continuity": health["continuity"],
            "us_engine_modified": False,
        },
    )

    print("Generated continuous Korea rate/FX/liquidity/strength outputs", flush=True)
    print(f"Generated {vintage_path}", flush=True)


if __name__ == "__main__":
    main()
