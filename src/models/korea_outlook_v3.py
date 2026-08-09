from __future__ import annotations

"""Unified Korea rate/FX/liquidity/KRW-strength output.

V3 is an integration layer, not a competing forecast model.  FX V4 remains the
single source of truth for USD/KRW point forecasts/probabilities.  Liquidity and KRW
strength are dedicated continuous forecasts generated upstream and embedded here so
GAS can read one GitHub JSON instead of making extra public-data calls.
"""

from typing import Any


def _float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


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
    del ecos, kosis, krx, global_data, us  # retained in signature for compatibility/audit

    rate_current = _float((rate_v2.get("current") or {}).get("kr_base_rate_pct")) or 0.0
    rate_path = rate_v2.get("meeting_path") or []
    rate_horizons = []
    for idx, row in enumerate(rate_path[:4]):
        rate_horizons.append(
            {
                "meeting_ahead": idx + 1,
                "label": f"{idx + 1}회 뒤 금통위 직후",
                "expected_rate_pct": row.get("expected_rate_pct"),
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

    fx_gate = ((fx_v2.get("validation") or {}).get("quality_gate") or {})
    rate_gate = ((rate_v2.get("validation") or {}).get("quality_gate") or {})
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
            "explanation": "예상금리는 동결·인상·인하 확률의 확률가중 평균입니다.",
        },
        "fx": {
            "current_usdkrw": spot,
            "current_date": fx_v2.get("current_date"),
            "current_source": fx_v2.get("current_source"),
            "recent_change_pct": fx_v2.get("recent_change_pct"),
            "forecast_path": forecasts,
            "quality_gate": fx_gate,
            "point_forecast_is_not_spot_copy": non_copy,
            "production_use": bool(fx_v2.get("forecast_operational", True)),
            "production_model": fx_v2.get("production_model"),
        },
        "krw_liquidity": liquidity or {},
        "krw_strength": krw_strength or {},
        "factor_panel": fx_v2.get("factor_panel") or {},
        "certification": {
            "level": fx_gate.get("level", "검증등급 산출"),
            "rate_level": rate_gate.get("level"),
            "fx_level": fx_gate.get("level"),
            "liquidity_quality_semantics": ((liquidity or {}).get("quality") or {}).get("quality_score_semantics"),
            "strength_level": ((krw_strength or {}).get("quality") or {}).get("grade"),
            "production_model": fx_v2.get("production_model"),
            "v3_production_enabled": True,
            "note": "V3는 별도 그림자 FX 예측을 만들지 않고 V4 FX·유동성·원화강도 결과를 하나의 JSON으로 통합합니다.",
        },
    }
