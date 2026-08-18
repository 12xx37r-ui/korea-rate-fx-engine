from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUT = Path(os.getenv("FRESHNESS_OUTPUT_DIR", str(ROOT / "output")))


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
    compact = text.replace("-", "").replace("/", "")
    for fmt, width in (("%Y%m%d", 8), ("%Y%m", 6)):
        try:
            return datetime.strptime(compact[:width], fmt).replace(tzinfo=timezone.utc)
        except Exception:
            pass
    return None


def age_hours(value: Any) -> float | None:
    dt = parse_dt(value)
    if dt is None:
        return None
    return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0)


def last_date(rows: Any) -> str | None:
    if not isinstance(rows, list):
        return None
    for row in reversed(rows):
        if isinstance(row, dict):
            for key in ("market_time_utc", "observed_at", "observation_at", "date", "TIME_PERIOD"):
                if row.get(key):
                    return str(row[key])
    return None


def series_status(name: str, observation: Any, *, value_present: bool, cadence: str, max_age_h: float) -> dict[str, Any]:
    age = age_hours(observation)
    if not value_present:
        state = "UNAVAILABLE"
    elif cadence == "monthly":
        state = "LIVE" if age is not None and age <= max_age_h else "CACHE"
    elif cadence == "event":
        # 정책금리는 '값이 안 바뀌는 것' 자체가 정상이다. 원천 재조회 여부는 api_health에서 별도 표시한다.
        state = "CACHE"
    else:
        state = "LIVE" if age is not None and age <= max_age_h else "CACHE"
    return {
        "source": name,
        "status": state,
        "cadence": cadence,
        "observation_at": observation,
        "age_hours": round(age, 2) if age is not None else None,
        "max_age_hours": max_age_h,
    }


def main() -> None:
    health = read_json(OUT / "api_health.json")
    v3 = read_json(OUT / "korea_rate_fx_outlook_v3.json")
    global_raw = read_json(OUT / "raw_global_market.json")
    sources = health.get("sources") if isinstance(health.get("sources"), dict) else {}

    items: list[dict[str, Any]] = []

    # 1) 실제 provider 수집 상태. 발표주기상 의도적 skip은 CACHE, 장애 후 LKG는 LKG.
    provider_specs = {
        "us_policy_engine": (72.0, "event/daily"),
        "ecos": (96.0, "daily/monthly"),
        "global_market": (96.0, "daily/market"),
        "krx": (96.0, "trading-day"),
        "kosis": (24.0 * 45.0, "monthly"),
    }
    for name, (max_age, cadence) in provider_specs.items():
        src = sources.get(name) if isinstance(sources.get(name), dict) else {}
        meta = src.get("metadata") if isinstance(src.get("metadata"), dict) else {}
        latest = src.get("latest_observation") or meta.get("latest_observation")
        age = age_hours(latest)
        reused = bool(meta.get("last_good_reused") or meta.get("circuit_last_good_reused"))
        cadence_skip = bool(meta.get("cadence_skips") or meta.get("cadence_skip"))
        collector_status = str(src.get("status") or "")
        if not src or collector_status == "error":
            state = "UNAVAILABLE"
        elif collector_status == "not_configured":
            state = "FALLBACK"
        elif reused and not cadence_skip:
            state = "LKG"
        elif cadence_skip:
            state = "CACHE"
        elif age is not None and age > max_age:
            state = "CACHE"
        else:
            state = "LIVE"
        items.append({
            "source": name,
            "status": state,
            "collector_status": collector_status or None,
            "cadence": cadence,
            "observation_at": latest,
            "age_hours": round(age, 2) if age is not None else None,
            "max_age_hours": max_age,
            "last_good_reused": reused,
            "cadence_skip": cadence_skip,
            "warnings": src.get("warnings") or [],
        })

    # 2) USD/KRW 실제 시장 snapshot. 추가 호출 없이 collector가 이미 저장한 값을 감사한다.
    fx = v3.get("fx") if isinstance(v3.get("fx"), dict) else {}
    snap = global_raw.get("usd_krw_market_snapshot") if isinstance(global_raw.get("usd_krw_market_snapshot"), dict) else {}
    spot_value = fx.get("market_spot") if fx.get("market_spot") is not None else snap.get("price")
    spot_obs = fx.get("market_spot_as_of_utc") or snap.get("market_time_utc")
    spot_retrieved = fx.get("market_spot_retrieved_at_utc") or snap.get("retrieved_at_utc")
    state = str(fx.get("market_state") or snap.get("market_state") or "").upper()
    spot_age = age_hours(spot_obs)
    if spot_value is None:
        spot_status = "UNAVAILABLE"
    elif state in {"CLOSE", "CLOSED", "POST", "PRE"}:
        spot_status = "CACHE"
    elif state in {"OPEN", "REGULAR", "CONTINUOUS"}:
        spot_status = "LIVE" if spot_age is not None and spot_age <= 3.0 else "CACHE"
    else:
        spot_status = "LIVE" if spot_age is not None and spot_age <= 3.0 else "CACHE"
    items.append({
        "source": "usdkrw_market_spot",
        "status": spot_status,
        "cadence": "intraday-when-market-open",
        "observation_at": spot_obs,
        "retrieved_at": spot_retrieved,
        "market_state": state or None,
        "age_hours": round(spot_age, 2) if spot_age is not None else None,
        "max_age_hours": 3.0,
        "value": spot_value,
        "provider": fx.get("market_spot_source") or snap.get("source") or fx.get("current_source"),
    })

    # 3) 한국 정책금리: event-driven. 현재 유효값과 엔진 생성시각을 분리한다.
    rate = v3.get("rate") if isinstance(v3.get("rate"), dict) else {}
    rate_value = rate.get("current_rate_pct")
    items.append({
        **series_status("kr_policy_rate", v3.get("generated_at"), value_present=rate_value is not None, cadence="event", max_age_h=24.0 * 45.0),
        "value": rate_value,
        "note": "정책금리는 이벤트형 값이라 엔진 생성시각이 오래됐다는 이유만으로 값 자체를 stale로 보지 않습니다. provider 재조회 상태는 ecos 행을 확인합니다.",
    })

    # 4) 이미 수집된 글로벌/한국 시장 원자료 전체. 새 네트워크 요청 없음.
    series_specs = {
        "broad_dollar": ("trading-day", 96.0),
        "us_2y": ("trading-day", 96.0),
        "us_10y": ("trading-day", 96.0),
        "us_breakeven_10y": ("trading-day", 96.0),
        "vix": ("trading-day", 96.0),
        "hy_oas": ("trading-day", 120.0),
        "wti": ("trading-day", 96.0),
        "commodity_index": ("monthly", 24.0 * 45.0),
        "usd_cny": ("trading-day", 96.0),
        "usd_jpy": ("trading-day", 96.0),
        "usd_krw_fred": ("trading-day", 120.0),
        "krw_neer": ("monthly", 24.0 * 45.0),
        "krw_reer": ("monthly", 24.0 * 45.0),
        "sox": ("trading-day", 96.0),
        "twii": ("trading-day", 96.0),
        "nvda": ("trading-day", 96.0),
        "tsm": ("trading-day", 96.0),
    }
    for key, (cadence, max_age) in series_specs.items():
        rows = global_raw.get(key)
        obs = last_date(rows)
        value = rows[-1].get("value") if isinstance(rows, list) and rows and isinstance(rows[-1], dict) else None
        item = series_status(key, obs, value_present=value is not None, cadence=cadence, max_age_h=max_age)
        item["value"] = value
        items.append(item)

    # 5) 파생 모델은 원천 freshness와 분리해 명시. 새 값이 아니라 계산 결과임을 표시한다.
    for key in ("krw_liquidity", "krw_strength"):
        obj = v3.get(key) if isinstance(v3.get(key), dict) else {}
        current = obj.get("current") if isinstance(obj.get("current"), dict) else {}
        items.append({
            "source": key,
            "status": "LIVE" if current and v3.get("generated_at") else "UNAVAILABLE",
            "cadence": "derived-from-collected-inputs",
            "observation_at": v3.get("generated_at"),
            "age_hours": round(age_hours(v3.get("generated_at")), 2) if age_hours(v3.get("generated_at")) is not None else None,
            "derived": True,
            "note": "실측 입력에서 계산된 파생지표. 원천별 상태는 위 항목을 사용합니다.",
        })

    payload = {
        "schema_version": "1.1.0",
        "patch_version": "V228-zero-call-freshness-unification",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "engine_generated_at": v3.get("generated_at"),
        "compatibility": {
            "existing_keys_removed": False,
            "existing_field_semantics_changed": False,
            "new_network_calls_added_by_patch": 0,
            "collector_network_policy_changed": False,
            "model_formulas_changed": False,
            "output_values_changed": False,
        },
        "items": items,
        "summary": {k: sum(x.get("status") == k for x in items) for k in ("LIVE", "CACHE", "LKG", "FALLBACK", "UNAVAILABLE")},
    }
    (OUT / "freshness_status.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload["summary"], ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
