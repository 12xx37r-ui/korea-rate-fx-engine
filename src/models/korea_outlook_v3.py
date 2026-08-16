from __future__ import annotations

"""Unified Korea rate/FX/liquidity/KRW-strength output.

V3 is an integration layer, not a competing forecast model.  FX V4 remains the
single source of truth for USD/KRW point forecasts/probabilities.  Liquidity and KRW
strength are dedicated continuous forecasts generated upstream and embedded here so
GAS can read one GitHub JSON instead of making extra public-data calls.
"""

from datetime import datetime, timezone
from typing import Any


def _float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _iso_age_minutes(value: Any) -> float | None:
    try:
        text = str(value or "").strip()
        if not text:
            return None
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds() / 60.0)
    except Exception:
        return None


def _rebase_fx_forecasts(rows: list[dict[str, Any]], model_anchor: float | None, market_spot: float | None) -> list[dict[str, Any]]:
    """Create an additive market-overlay path without changing the production model path."""
    if not model_anchor or not market_spot or model_anchor <= 0 or market_spot <= 0:
        return []
    scale = market_spot / model_anchor
    out: list[dict[str, Any]] = []
    for row in rows or []:
        # Keep every existing forecast key/type so downstream consumers see the
        # same schema. Only absolute price fields are re-anchored.
        item = dict(row)
        item["model_change_pct"] = row.get("change_pct")
        item["market_spot_anchor"] = round(market_spot, 6)
        for key in ("point_forecast", "mid"):
            value = _float(row.get(key))
            item[key] = round(value * scale, 6) if value is not None else None
        for key in ("range_50", "range_80"):
            band = row.get(key)
            if isinstance(band, list) and len(band) >= 2:
                lo, hi = _float(band[0]), _float(band[1])
                item[key] = [round(lo * scale, 6), round(hi * scale, 6)] if lo is not None and hi is not None else None
            else:
                item[key] = None
        out.append(item)
    return out


def build_v3(
    rate_v2: dict[str, Any],
    fx_v2: dict[str, Any],
    ecos: dict[str, Any],
    kosis: dict[str, Any],
    krx: dict[str, Any],
    global_data: dict[str, Any],
    us: dict[str, Any] | None,
    liquidity: dict[str, Any] | None = None,
    krw_strength: dict[str, Any] | None = None,
) -> dict[str, Any]:
    del ecos, kosis, krx, us  # retained in signature for compatibility/audit

    rate_current = _float((rate_v2.get("current") or {}).get("kr_base_rate_pct")) or 0.0
    rate_path = rate_v2.get("meeting_path") or []
    rate_horizons = []
    for idx, row in enumerate(rate_path[:4]):
        rate_horizons.append(
            {
                "meeting_ahead": idx + 1,
                "label": f"{idx + 1}회 뒤 금통위 직후",
                "expected_rate_pct": row.get("expected_rate_pct"),
                "modal_rate_pct": row.get("modal_rate_pct"),
                "probabilities": row.get("probabilities"),
                "most_likely_action": row.get("most_likely_action"),
            }
        )
    while len(rate_horizons) < 4:
        previous = rate_horizons[-1]["expected_rate_pct"] if rate_horizons else rate_current
        rate_horizons.append(
            {
                "meeting_ahead": len(rate_horizons) + 1,
                "label": f"{len(rate_horizons) + 1}회 뒤 금통위 직후",
                "expected_rate_pct": previous,
                "modal_rate_pct": rate_current,
                "probabilities": None,
                "most_likely_action": "hold",
            }
        )
    rate_month = {
        "3m": rate_horizons[min(1, len(rate_horizons) - 1)]["expected_rate_pct"],
        "6m": rate_horizons[min(2, len(rate_horizons) - 1)]["expected_rate_pct"],
        "12m": rate_horizons[min(3, len(rate_horizons) - 1)]["expected_rate_pct"],
    }

    spot = _float(fx_v2.get("current_usdkrw")) or 0.0
    forecasts = []
    for row in fx_v2.get("forecast_path", []) or []:
        point = row.get("point_forecast", row.get("mid"))
        if point is None:
            band = row.get("range_80")
            if isinstance(band, list) and len(band) >= 2:
                lo, hi = _float(band[0]), _float(band[1])
                if lo is not None and hi is not None:
                    point = (lo + hi) / 2.0
        change_pct = row.get("change_pct")
        if change_pct is None and point is not None and spot:
            change_pct = (float(point) / spot - 1.0) * 100.0
        forecasts.append(
            {
                "months": row.get("months"),
                "point_forecast": point,
                "mid": row.get("mid", point) if row.get("mid") is not None else point,
                "change_pct": change_pct,
                "direction": row.get("direction"),
                "up_probability": row.get("up_probability"),
                "neutral_probability": row.get("neutral_probability"),
                "down_probability": row.get("down_probability"),
                "range_50": row.get("range_50"),
                "range_80": row.get("range_80"),
                "quality_grade": row.get("quality_grade"),
                "model_quality_score": row.get("model_quality_score"),
                "prediction_status": "forecast",
            }
        )

    market_snapshot = global_data.get("usd_krw_market_snapshot") if isinstance(global_data, dict) else {}
    market_snapshot = market_snapshot if isinstance(market_snapshot, dict) else {}
    market_spot = _float(market_snapshot.get("price"))
    market_time_utc = market_snapshot.get("market_time_utc")
    market_age_minutes = _iso_age_minutes(market_time_utc)
    market_state = str(market_snapshot.get("market_state") or "").upper()
    retrieved_at_utc = market_snapshot.get("retrieved_at_utc")
    # V219: a successful refetch does not automatically mean the observation is
    # live. CLOSED/PRE/POST are CACHE, and even an OPEN flag must have a recent
    # market timestamp. This prevents stale provider state from labelling a weekend
    # quote as LIVE while still publishing the newest quote actually retrieved.
    if market_spot is None:
        market_status = "UNAVAILABLE"
    elif market_state in {"CLOSE", "CLOSED", "POST", "PRE"}:
        market_status = "CACHE"
    elif market_state in {"OPEN", "REGULAR", "CONTINUOUS"}:
        market_status = "LIVE" if market_age_minutes is not None and market_age_minutes <= 180 else "CACHE"
    elif market_age_minutes is not None:
        market_status = "LIVE" if market_age_minutes <= 180 else "CACHE"
    else:
        market_status = "CACHE"
    effective_spot = market_spot if market_spot is not None else spot
    rebased_forecasts = _rebase_fx_forecasts(forecasts, spot, effective_spot)
    effective_date = fx_v2.get("current_date")
    try:
        if market_time_utc:
            effective_date = datetime.fromisoformat(str(market_time_utc).replace("Z", "+00:00")).strftime("%Y%m%d")
    except Exception:
        pass

    fx_gate = ((fx_v2.get("validation") or {}).get("quality_gate") or {})
    rate_validation = rate_v2.get("validation") or {}
    rate_gate = (rate_validation.get("quality_gate") or {})
    rate_validation_summary = {
        "samples": rate_validation.get("samples"),
        "brier_score": rate_validation.get("brier_score"),
        "benchmark_brier": rate_validation.get("benchmark_brier"),
        "brier_skill_score": rate_validation.get("brier_skill_score"),
        "accuracy": rate_validation.get("accuracy"),
        "accuracy_wilson_lower_95": rate_validation.get("accuracy_wilson_lower_95"),
        "log_loss": rate_validation.get("log_loss"),
        "release_lag_backtest": rate_validation.get("release_lag_backtest"),
        "walk_forward_backtest": rate_validation.get("walk_forward_backtest"),
        "real_time_vintage": rate_validation.get("real_time_vintage"),
        "real_time_vintage_validation": rate_validation.get("real_time_vintage_validation") or {},
        "market_rate_validation_proxy": rate_validation.get("market_rate_validation_proxy") or {},
    }
    non_copy = any(
        _float(row.get("point_forecast")) is not None
        and abs(float(row.get("point_forecast")) - spot) >= 0.05
        for row in forecasts
    )

    return {
        "schema_version": "3.2.0",
        "status": "ok",
        "engine_scope": "korea_rate_fx_liquidity_strength_unified",
        "us_engine_modified": False,
        "rate": {
            "current_rate_pct": rate_current,
            "next_meeting_expected_rate_pct": rate_horizons[0]["expected_rate_pct"] if rate_horizons else None,
            "meeting_path": rate_horizons,
            "calendar_horizon_estimates": rate_month,
            "quality_gate": rate_gate,
            "validation_summary": rate_validation_summary,
            "explanation": "예상금리는 확률가중 평균이며 modal_rate_pct는 가장 가능성 높은 25bp 정책경로입니다. 재구성 OOS와 실시간 원본 빈티지 엄격검증을 분리합니다.",
        },
        "fx": {
            # V218 compatibility-preserving bug fix: the intended meaning of these
            # existing public fields is "current market value". They now prefer the
            # freshly fetched market overlay; the original model anchor/path remain
            # available below for model audit and reproducibility.
            "current_usdkrw": effective_spot,
            "current_date": effective_date,
            "current_source": market_snapshot.get("source") if market_spot is not None else fx_v2.get("current_source"),
            "recent_change_pct": fx_v2.get("recent_change_pct"),
            "forecast_path": rebased_forecasts if rebased_forecasts else forecasts,
            "quality_gate": fx_gate,
            "point_forecast_is_not_spot_copy": non_copy,
            "production_use": bool(fx_v2.get("forecast_operational", True)),
            "production_model": fx_v2.get("production_model"),
            # V218 audit fields preserve the exact model inputs/outputs.
            "model_anchor_usdkrw": spot,
            "model_forecast_path": forecasts,
            "market_spot": market_spot,
            "market_spot_as_of_utc": market_time_utc,
            "market_spot_source": market_snapshot.get("source"),
            "market_spot_status": market_status,
            "market_state": market_state or None,
            "market_spot_retrieved_at_utc": retrieved_at_utc,
            "market_spot_age_minutes": round(market_age_minutes, 2) if market_age_minutes is not None else None,
            "rebased_forecast_path": rebased_forecasts,
        },
        "krw_liquidity": liquidity or {},
        "krw_strength": krw_strength or {},
        "factor_panel": fx_v2.get("factor_panel") or {},
        "source_freshness": {
            "fx_market": {
                "status": market_status,
                "as_of_utc": market_time_utc,
                "age_minutes": round(market_age_minutes, 2) if market_age_minutes is not None else None,
                "source": market_snapshot.get("source"),
                "model_anchor_preserved": True,
                "public_current_uses_latest_overlay": market_spot is not None,
                "rebased_path_available": bool(rebased_forecasts),
                "market_state": market_state or None,
                "retrieved_at_utc": retrieved_at_utc,
            }
        },
        "certification": {
            "level": fx_gate.get("level", "검증등급 산출"),
            "rate_level": rate_gate.get("level"),
            "fx_level": fx_gate.get("level"),
            "liquidity_level": ((liquidity or {}).get("quality") or {}).get("forecast_quality_grade"),
            "liquidity_quality_semantics": ((liquidity or {}).get("quality") or {}).get("quality_score_semantics"),
            "strength_level": ((krw_strength or {}).get("quality") or {}).get("grade"),
            "production_model": fx_v2.get("production_model"),
            "v3_production_enabled": True,
            "note": "V3는 별도 그림자 FX 예측을 만들지 않고 V4 FX·유동성·원화강도 결과를 하나의 JSON으로 통합합니다.",
        },
    }
