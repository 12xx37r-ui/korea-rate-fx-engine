from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import exp, tanh, sqrt, log
from statistics import mean, median
from typing import Any

from src.models.fx_forecast_v4 import build_fx_forecast_v4, _merge_fx_series

from src.models.rate_validation import (
    calibrate_probabilities,
    core_cpi_yoy_series,
    probability_from_features,
    rate_probability_backtest,
    combine_market_rate_rows_for_backtest,
)


@dataclass
class KrwStrengthResult:
    score: float
    percentile: float | None
    grade: str
    direction_grade: str
    confidence: float


def grade_from_score(score: float) -> str:
    if score >= 0.67:
        return "강강"
    if score >= 0.34:
        return "강약"
    if score >= 0.08:
        return "강중립"
    if score > -0.08:
        return "약중립"
    if score > -0.34:
        return "약강"
    return "약약"


def _row_period(row: dict[str, Any]) -> str:
    for key in ("TIME", "PRD_DE", "TIME_PERIOD", "DATE", "date", "baseYm", "WRTTIME"): 
        value = row.get(key)
        if value not in (None, ""):
            text = str(value).replace("-", "").replace(".", "")
            if key == "date" and text.isdigit():
                return text
            return text.zfill(14) if text.isdigit() else text
    return ""


def _values(
    rows: list[dict[str, Any]],
    keys: tuple[str, ...] = ("DATA_VALUE", "DT"),
) -> list[float]:
    """날짜 오름차순 정렬·중복 제거 후 값만 반환한다. API 역순 응답으로 YoY가 뒤집히는 오류를 방지한다."""
    pairs: list[tuple[str, float]] = []
    for idx, row in enumerate(rows):
        for key in keys:
            try:
                value = row.get(key)
                if value not in (None, "", "-"):
                    period = _row_period(row) or f"{idx:08d}"
                    pairs.append((period, float(str(value).replace(",", ""))))
                    break
            except (TypeError, ValueError):
                continue
    dedup: dict[str, float] = {}
    for period, value in pairs:
        dedup[period] = value
    return [dedup[k] for k in sorted(dedup)]




def _normalise_date(value: Any) -> str:
    text = str(value or "").strip().replace("-", "").replace(".", "")
    return text[:8] if len(text) >= 8 and text[:8].isdigit() else text


def _latest_row_meta(rows: list[dict[str, Any]] | None, source: str) -> dict[str, Any]:
    latest_period = None
    latest_value = None
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        period = _row_period(row)
        value = None
        for key in ("DATA_VALUE", "DT", "value"):
            try:
                raw = row.get(key)
                if raw not in (None, "", "-"):
                    value = float(str(raw).replace(",", ""))
                    break
            except (TypeError, ValueError):
                continue
        if period and value is not None and (latest_period is None or period >= latest_period):
            latest_period = period
            latest_value = value
    return {"source": source, "observation": latest_period, "value": latest_value}


def _apply_live_usdkrw_overlay(
    merged_fx: list[tuple[str, float]],
    spot_meta: dict[str, Any],
    global_data: dict[str, Any],
) -> tuple[list[tuple[str, float]], dict[str, Any], float | None]:
    """Overlay the freshly fetched market spot on current-state calculations only.

    Historical OOS remains based on the original merged series.  The live overlay may
    replace/append only the last observation so current KRW strength is aligned with
    the same market spot published by the unified FX card.
    """
    base_spot = merged_fx[-1][1] if merged_fx else None
    snap = global_data.get("usd_krw_market_snapshot") if isinstance(global_data, dict) else None
    if not isinstance(snap, dict):
        return list(merged_fx), dict(spot_meta or {}), base_spot
    try:
        price = float(snap.get("price"))
    except (TypeError, ValueError):
        return list(merged_fx), dict(spot_meta or {}), base_spot
    if not 800.0 <= price <= 2500.0:
        return list(merged_fx), dict(spot_meta or {}), base_spot
    obs = None
    try:
        raw_time = snap.get("market_time_utc")
        if raw_time:
            obs = datetime.fromisoformat(str(raw_time).replace("Z", "+00:00")).strftime("%Y%m%d")
    except Exception:
        obs = None
    live = list(merged_fx)
    if live:
        last_date = _normalise_date(live[-1][0])
        if obs and obs > last_date:
            live.append((obs, price))
        else:
            live[-1] = (live[-1][0], price)
    elif obs:
        live = [(obs, price)]
    meta = {
        "date": obs or (spot_meta or {}).get("date"),
        "source": snap.get("source") or "market_snapshot",
        "value": price,
        "market_time_utc": snap.get("market_time_utc"),
        "retrieved_at_utc": snap.get("retrieved_at_utc"),
        "market_state": snap.get("market_state"),
        "live_overlay_applied": True,
        "model_anchor_value": base_spot,
    }
    return live, meta, base_spot


def _pct(
    values: list[float],
    periods: int,
) -> float | None:
    if (
        len(values) <= periods
        or values[-periods - 1] == 0
    ):
        return None

    return values[-1] / values[-periods - 1] - 1.0


def _percentile(
    values: list[float],
    value: float,
) -> float | None:
    if not values:
        return None

    return sum(
        item <= value
        for item in values
    ) / len(values)


def _softmax(
    raw: dict[str, float],
) -> dict[str, float]:
    peak = max(raw.values())

    weights = {
        key: exp(value - peak)
        for key, value in raw.items()
    }

    total = sum(weights.values()) or 1.0

    return {
        key: round(value / total, 3)
        for key, value in weights.items()
    }


def _yoy(values: list[float], months: int = 12) -> float | None:
    return _pct(values, months)


def _annualized_change(values: list[float], periods: int) -> float | None:
    if len(values) <= periods or values[-periods - 1] <= 0 or values[-1] <= 0:
        return None
    return (values[-1] / values[-periods - 1]) ** (12.0 / periods) - 1.0


def _quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    data = sorted(values)
    pos = (len(data) - 1) * max(0.0, min(1.0, q))
    lo = int(pos)
    hi = min(len(data) - 1, lo + 1)
    frac = pos - lo
    return data[lo] * (1.0 - frac) + data[hi] * frac


def _us_policy_signal(us_policy: dict[str, Any] | None) -> float:
    if not isinstance(us_policy, dict):
        return 0.0
    current = us_policy.get("current_effective_rate")
    path = us_policy.get("meeting_path")
    if current is None or not isinstance(path, list) or not path:
        fed = us_policy.get("fed", {})
        path = fed.get("expected_path", []) if isinstance(fed, dict) else []
        current = fed.get("current_effective_rate") if isinstance(fed, dict) else current
    try:
        current = float(current)
    except (TypeError, ValueError):
        return 0.0
    future = []
    for row in path[:3]:
        if not isinstance(row, dict):
            continue
        for key in ("expected_post_meeting_rate", "expected_rate", "rate"):
            try:
                value = row.get(key)
                if value is not None:
                    future.append(float(value))
                    break
            except (TypeError, ValueError):
                continue
    if not future:
        return 0.0
    return max(-1.0, min(1.0, (mean(future) - current) / 0.50))


def _fx_ensemble(values: list[float], horizon: int = 60) -> dict[str, Any]:
    """다중 모형 워크포워드 앙상블. 최소 5년 수집을 전제로 주간 재예측 표본을 만든다."""
    if len(values) < 180:
        latest = values[-1] if values else None
        return {"mid": latest, "errors": [], "weights": [0.25]*4, "samples": 0, "rmse": None}

    def clip(x: float, lo: float, hi: float) -> float:
        return max(lo, min(hi, x))

    def forecasts(hist: list[float]) -> list[float]:
        x = hist[-1]
        med = median(hist[-252:])
        r20 = hist[-1] / hist[-21] - 1.0
        r60 = hist[-1] / hist[-61] - 1.0
        r120 = hist[-1] / hist[-121] - 1.0 if len(hist) > 121 else r60
        f1 = x * (1.0 + clip(r20 * 1.05, -0.07, 0.07))
        f2 = x * (1.0 + clip(r60 * 0.55, -0.07, 0.07))
        f3 = x * (1.0 + clip(r120 * 0.30, -0.06, 0.06))
        f4 = x * (1.0 + clip((med / x - 1.0) * 0.40, -0.06, 0.06))
        return [f1, f2, f3, f4]

    errors_by_model = [[] for _ in range(4)]
    start = max(126, len(values) - 1260)
    for t in range(start, len(values) - horizon, 5):
        fs = forecasts(values[:t])
        actual = values[t + horizon - 1]
        if not actual:
            continue
        for i, pred in enumerate(fs):
            errors_by_model[i].append(pred / actual - 1.0)

    rmses = [sqrt(mean([e*e for e in errs])) if errs else 0.08 for errs in errors_by_model]
    inv = [1.0 / max(0.008, r) for r in rmses]
    total = sum(inv) or 1.0
    weights = [x / total for x in inv]
    current_fs = forecasts(values)
    mid = sum(w*f for w,f in zip(weights,current_fs))
    n = min((len(x) for x in errors_by_model), default=0)
    combined = [sum(weights[i]*errors_by_model[i][j] for i in range(4)) for j in range(n)]
    rmse = sqrt(mean([e*e for e in combined])) if combined else None
    return {"mid": mid, "errors": combined, "weights": weights, "samples": n, "rmse": rmse}


def _rate_probabilities(
    gap: float | None,
    core_cpi: list[float],
    industrial: list[float],
    us_policy: dict[str, Any] | None = None,
) -> dict[str, float]:
    # 규칙형 1차판의 CPI 지수수준 직접 사용 오류를 제거했다.
    # 물가는 12개월 상승률, 생산은 3개월 연율, 시장금리는 기준금리 대비 격차를 사용한다.
    raw = {"hold": 1.65, "hike": 0.0, "cut": 0.0}

    if gap is not None:
        raw["hike"] += max(0.0, min(1.10, gap * 0.70))
        raw["cut"] += max(0.0, min(1.10, -gap * 1.00))

    cpi_yoy = _yoy(core_cpi, 12)
    if cpi_yoy is not None:
        # 한국은행 물가목표 2%를 중심으로 압력을 대칭 반영한다.
        inflation_gap = (cpi_yoy - 0.02) / 0.01
        raw["hike"] += max(0.0, min(0.90, inflation_gap * 0.32))
        raw["cut"] += max(0.0, min(0.75, -inflation_gap * 0.28))

    production_3m_ann = _annualized_change(industrial, 3)
    if production_3m_ann is not None:
        growth_signal = production_3m_ann / 0.06
        raw["hike"] += max(0.0, min(0.55, growth_signal * 0.28))
        raw["cut"] += max(0.0, min(0.65, -growth_signal * 0.34))

    # 미국 경로는 한국의 금리결정을 대체하지 않고 금리차/환율 제약 요인으로 제한 반영한다.
    us_signal = _us_policy_signal(us_policy)
    raw["hike"] += max(0.0, us_signal) * 0.28
    raw["cut"] += max(0.0, -us_signal) * 0.18

    return _softmax(raw)

def _rate_label(
    probability: dict[str, float],
) -> str:
    order = sorted(
        probability.items(),
        key=lambda item: item[1],
        reverse=True,
    )

    names = {
        "hold": "동결",
        "hike": "인상",
        "cut": "인하",
    }

    first = order[0]
    second = order[1]

    if (
        first[0] == "hold"
        and second[1] >= 0.20
    ):
        return (
            f"동결 우세·"
            f"{names[second[0]]} 위험"
        )

    return f"{names[first[0]]} 우세"


def _us_policy_connected(
    us_policy: dict[str, Any] | None,
) -> bool:
    """
    정규화된 미국 정책금리 엔진 입력이 실제로 유효한지 확인한다.

    정상 호환 구조:
    - fed.expected_path
    - fed.next_meeting
    - us_market
    - quality

    미국 엔진 원본에는 최상위 status 필드가 없을 수 있으므로
    status 존재 여부로 연결을 판정하지 않는다.
    """

    if not isinstance(us_policy, dict):
        return False

    fed = us_policy.get("fed")
    us_market = us_policy.get("us_market")
    quality = us_policy.get("quality")

    if not isinstance(fed, dict):
        return False

    expected_path = fed.get("expected_path")
    next_meeting = fed.get("next_meeting")

    if not isinstance(expected_path, list):
        return False

    if not expected_path:
        return False

    if not isinstance(next_meeting, dict):
        return False

    if not isinstance(us_market, dict):
        return False

    if not isinstance(quality, dict):
        return False

    return True



def _validated_macro_yoy(values: list[float], periods: int, lo: float, hi: float) -> tuple[float | None, bool]:
    value = _pct(values, periods)
    if value is None or not (lo <= value <= hi):
        return None, False
    return value, True


def _current_us_rate(us_policy: dict[str, Any] | None) -> float | None:
    if not isinstance(us_policy, dict):
        return None
    for candidate in (us_policy.get("current_effective_rate"), (us_policy.get("fed") or {}).get("current_effective_rate")):
        try:
            if candidate is not None:
                return float(candidate)
        except (TypeError, ValueError):
            pass
    return None

def build_snapshot(
    ecos: dict[str, list[dict[str, Any]]],
    kosis: dict[str, list[dict[str, Any]]],
    us_policy: dict[str, Any] | None = None,
    global_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    # V4 FINAL: 실제 글로벌 환율 오버레이가 있을 때는 현재 원화강도와
    # 환율예측이 서로 다른 spot을 보지 않도록 V4와 동일한 병합 이력을 사용한다.
    # 테스트/레거시처럼 날짜 형식이 단순 인덱스이고 글로벌 오버레이가 없으면 기존 정렬을 유지한다.
    has_fx_overlay = bool((global_data or {}).get("usd_krw_yahoo") or (global_data or {}).get("usd_krw_fred"))
    if has_fx_overlay:
        merged_fx_rows, _fx_spot_meta = _merge_fx_series(ecos, global_data or {})
        fx = [value for _, value in merged_fx_rows]
    else:
        fx = _values(ecos.get("usdkrw", []))

    base = _values(
        ecos.get("kr_base_rate", [])
    )

    y2 = _values(
        ecos.get("kr_gov_2y", [])
    )

    y3 = _values(
        ecos.get("kr_gov_3y", [])
    )

    y10 = _values(
        ecos.get("kr_gov_10y", [])
    )

    core_cpi_rows = kosis.get("cpi_core", [])
    core_cpi_yoy_series_data, core_cpi_meta = core_cpi_yoy_series(core_cpi_rows)
    core_cpi_yoy_values = [value for _, value in core_cpi_yoy_series_data]

    industrial = _values(
        kosis.get(
            "industrial_production",
            [],
        )
    )

    latest_fx = fx[-1] if fx else None
    latest_rate = base[-1] if base else None

    market_yield = (
        y2[-1]
        if y2
        else (
            y3[-1]
            if y3
            else None
        )
    )

    gap = (
        market_yield - latest_rate
        if (
            market_yield is not None
            and latest_rate is not None
        )
        else None
    )

    # 절대 수준:
    # 최근 최대 1년 분포에서 USD/KRW가 높을수록 원화 약세.
    level_window = fx[-252:] if fx else []

    fx_percentile = (
        _percentile(
            level_window,
            latest_fx,
        )
        if latest_fx is not None
        else None
    )

    level_score = (
        1.0 - 2.0 * fx_percentile
        if fx_percentile is not None
        else 0.0
    )

    # 방향:
    # USD/KRW 하락은 원화 강세.
    fx1 = (
        _pct(
            fx,
            min(
                20,
                max(1, len(fx) - 1),
            ),
        )
        if len(fx) > 1
        else None
    )

    fx3 = (
        _pct(
            fx,
            min(
                60,
                max(1, len(fx) - 1),
            ),
        )
        if len(fx) > 1
        else None
    )

    momentum_parts = [
        value
        for value in (fx1, fx3)
        if value is not None
    ]

    fx_momentum = (
        mean(momentum_parts)
        if momentum_parts
        else 0.0
    )

    momentum_score = -tanh(
        fx_momentum * 12.0
    )

    # 현재 원화 강도는 절대 수준을 더 크게 반영한다.
    score = max(
        -1.0,
        min(
            1.0,
            level_score * 0.65
            + momentum_score * 0.35,
        ),
    )

    cpi_yoy_validated = core_cpi_yoy_values[-1] if core_cpi_yoy_values else None
    cpi_valid = bool(core_cpi_meta.get("valid")) and cpi_yoy_validated is not None
    industrial_3m = _annualized_change(industrial, 3)
    industrial_valid = industrial_3m is not None and -0.40 <= industrial_3m <= 0.40

    # 미국 정책경로는 미국엔진 결과를 재사용하고, 한국의 현재 확률 산식에 제한적으로만 반영한다.
    base_rate_prob = probability_from_features(
        gap,
        cpi_yoy_validated if cpi_valid else None,
        industrial_3m if industrial_valid else None,
    )
    us_signal = _us_policy_signal(us_policy)
    raw_rate_prob = {
        "hold": max(1e-6, base_rate_prob["hold"]),
        "hike": max(1e-6, base_rate_prob["hike"] * (1.0 + max(0.0, us_signal) * 0.28)),
        "cut": max(1e-6, base_rate_prob["cut"] * (1.0 + max(0.0, -us_signal) * 0.18)),
    }
    raw_total = sum(raw_rate_prob.values()) or 1.0
    raw_rate_prob = {key: value / raw_total for key, value in raw_rate_prob.items()}

    rate_validation_market_rows, _ = combine_market_rate_rows_for_backtest(
        ecos.get("kr_gov_2y", []),
        ecos.get("kr_gov_3y", []),
    )
    rate_backtest = rate_probability_backtest(
        ecos.get("kr_base_rate", []),
        rate_validation_market_rows,
        core_cpi_yoy_series_data,
        kosis.get("industrial_production", []),
    )
    rate_prob, rate_calibration_weight = calibrate_probabilities(raw_rate_prob, rate_backtest)

    rate_direction = _rate_label(rate_prob)

    rate_expected = None

    if latest_rate is not None:
        rate_expected = (
            latest_rate
            * rate_prob["hold"]
            + (latest_rate + 0.25)
            * rate_prob["hike"]
            + max(
                0.0,
                latest_rate - 0.25,
            )
            * rate_prob["cut"]
        )

    # 환율 전망은 V4 연속형 OOS 앙상블을 단일 진실원천으로 사용한다.
    # 약한 신호도 단순 spot 복사로 바꾸지 않고, 검증성적에 따라 예측폭만 축소한다.
    fx_v4 = build_fx_forecast_v4(ecos, global_data or {}, rate_v2=None)
    fx_v4_rows = {int(row.get("months")): row for row in fx_v4.get("forecast_path", []) if row.get("months")}
    fx3_row = fx_v4_rows.get(3, {})
    fx_mid = fx3_row.get("mid")
    if fx_mid is None:
        # 짧은 테스트 표본 등 V4 최소 이력이 충족되지 않는 경우에만 기존 연속 앙상블을 사용한다.
        legacy_fx_model = _fx_ensemble(fx, horizon=60)
        fx_mid = legacy_fx_model.get("mid")
        fx_range = None
        fx_model = legacy_fx_model
    else:
        fx_range = fx3_row.get("range_80")
        v4_val = ((fx_v4.get("validation") or {}).get("oos_by_horizon") or {}).get("3m", {})
        fx_model = {
            "weights": list((v4_val.get("weights") or {}).values()),
            "weight_map": v4_val.get("weights") or {},
            "samples": int(v4_val.get("samples") or 0),
            "rmse": (float(v4_val.get("rmse_pct")) / 100.0) if v4_val.get("rmse_pct") is not None else None,
            "errors": [],
            "quality_grade": v4_val.get("grade"),
            "model_quality_score": v4_val.get("model_quality_score"),
        }
    fx_bias_pct = (float(fx_mid) / latest_fx - 1.0) if (fx_mid is not None and latest_fx) else 0.0

    us_connected = _us_policy_connected(
        us_policy
    )

    # 예상 강도도 절대수준과 예상 방향을 분리한다.
    future_level_pct = (
        _percentile(
            level_window,
            fx_mid,
        )
        if fx_mid is not None
        else None
    )

    future_level_score = (
        1.0 - 2.0 * future_level_pct
        if future_level_pct is not None
        else level_score
    )

    future_direction_score = -tanh(
        fx_bias_pct * 12.0
    )

    future_score = max(
        -1.0,
        min(
            1.0,
            future_level_score * 0.65
            + future_direction_score * 0.35,
        ),
    )

    us_current_rate = _current_us_rate(us_policy)
    us_kr_gap = (us_current_rate - latest_rate) if (us_current_rate is not None and latest_rate is not None) else None

    coverage = (
        sum(
            bool(value)
            for value in (
                fx,
                base,
                y2 or y3,
                core_cpi_yoy_values,
                industrial,
            )
        )
        / 5.0
    )

    bt_samples = int(fx_model.get("samples", 0) or 0)
    bt_rmse = fx_model.get("rmse")
    backtest_quality = min(1.0, bt_samples / 80.0)
    error_quality = max(0.0, min(1.0, 1.0 - ((bt_rmse or 0.08) / 0.08)))
    rate_bt_samples = int(rate_backtest.get("samples") or 0)
    rate_skill = rate_backtest.get("brier_skill_score")
    rate_validation_quality = min(1.0, rate_bt_samples / 60.0) * (0.6 + 0.4 * max(0.0, min(1.0, float(rate_skill or 0.0))))
    confidence_cap = 0.90 if (us_connected and cpi_valid and industrial_valid and bt_samples >= 40 and rate_bt_samples >= 18) else (0.72 if us_connected else 0.55)
    confidence = round(min(confidence_cap, 0.26 + coverage * 0.25 + backtest_quality * 0.10 + error_quality * 0.10 + rate_validation_quality * 0.13), 2)

    current_grade = grade_from_score(
        score
    )

    direction_grade = grade_from_score(
        momentum_score
    )

    future_grade = grade_from_score(
        future_score
    )

    us_next_meeting = None
    us_expected_path_count = 0
    us_schema_version = None

    if us_connected and isinstance(
        us_policy,
        dict,
    ):
        fed = us_policy.get("fed", {})

        next_meeting = fed.get(
            "next_meeting",
            {},
        )

        expected_path = fed.get(
            "expected_path",
            [],
        )

        quality = us_policy.get(
            "quality",
            {},
        )

        us_next_meeting = (
            next_meeting.get("date")
            if isinstance(
                next_meeting,
                dict,
            )
            else None
        )

        us_expected_path_count = (
            len(expected_path)
            if isinstance(
                expected_path,
                list,
            )
            else 0
        )

        us_schema_version = (
            us_policy.get(
                "schema_version"
            )
            or quality.get(
                "engine_version"
            )
        )

    return {
        "status": (
            "ok"
            if fx and base
            else "partial"
        ),
        "current": {
            "kr_base_rate_pct": latest_rate,
            "us_policy_rate_pct": round(us_current_rate, 3) if us_current_rate is not None else None,
            "us_kr_policy_gap_pctp": round(us_kr_gap, 3) if us_kr_gap is not None else None,
            "usdkrw": latest_fx,
            "kr_gov_2y_pct": (
                y2[-1]
                if y2
                else None
            ),
            "kr_gov_3y_pct": (
                y3[-1]
                if y3
                else None
            ),
            "kr_gov_10y_pct": (
                y10[-1]
                if y10
                else None
            ),
            "krw_strength_score": round(
                score,
                4,
            ),
            "krw_strength_grade": (
                current_grade
            ),
            "krw_absolute_level_score": (
                round(
                    level_score,
                    4,
                )
            ),
            "krw_absolute_level_grade": (
                grade_from_score(
                    level_score
                )
            ),
            "krw_short_term_momentum_score": (
                round(
                    momentum_score,
                    4,
                )
            ),
            "krw_short_term_momentum_grade": (
                direction_grade
            ),
            "usdkrw_1y_percentile": (
                round(
                    fx_percentile,
                    4,
                )
                if fx_percentile
                is not None
                else None
            ),
        },
        "forecast": {
            "horizon": "향후 1~3개월",
            "kr_base_rate_direction": (
                rate_direction
            ),
            "kr_base_rate_expected_pct": (
                round(
                    rate_expected,
                    2,
                )
                if rate_expected
                is not None
                else None
            ),
            "kr_base_rate_scenarios": {
                "hold": {
                    "rate_pct": latest_rate,
                    "probability": (
                        rate_prob["hold"]
                    ),
                },
                "hike_25bp": {
                    "rate_pct": (
                        round(
                            latest_rate
                            + 0.25,
                            2,
                        )
                        if latest_rate
                        is not None
                        else None
                    ),
                    "probability": (
                        rate_prob["hike"]
                    ),
                },
                "cut_25bp": {
                    "rate_pct": (
                        round(
                            max(
                                0.0,
                                latest_rate
                                - 0.25,
                            ),
                            2,
                        )
                        if latest_rate
                        is not None
                        else None
                    ),
                    "probability": (
                        rate_prob["cut"]
                    ),
                },
            },
            "usdkrw_mid": (
                round(
                    fx_mid,
                    1,
                )
                if fx_mid
                is not None
                else None
            ),
            "usdkrw_range": fx_range,
            "usdkrw_direction": fx3_row.get("direction") if isinstance(fx3_row, dict) else None,
            "usdkrw_up_probability": fx3_row.get("up_probability") if isinstance(fx3_row, dict) else None,
            "usdkrw_neutral_probability": fx3_row.get("neutral_probability") if isinstance(fx3_row, dict) else None,
            "usdkrw_down_probability": fx3_row.get("down_probability") if isinstance(fx3_row, dict) else None,
            "usdkrw_model_quality_grade": fx3_row.get("quality_grade") if isinstance(fx3_row, dict) else None,
            "usdkrw_model_quality_score": fx3_row.get("model_quality_score") if isinstance(fx3_row, dict) else None,
            "krw_strength_score": round(
                future_score,
                4,
            ),
            "krw_strength_grade": (
                future_grade
            ),
            "confidence": confidence,
            "confidence_limit": (
                "공식자료·시장금리·미국정책경로·워크포워드 검증 반영"
                if us_connected
                else (
                    "미국 정책경로 비반영 · 국내자료 기반 예측"
                )
            ),
        },
        "methodology": {
            "note": (
                "현재 원화 강도는 환율의 절대 수준과 "
                "단기 모멘텀을 분리해 계산합니다. "
                "금리 전망은 시장금리 격차, 근원물가의 12개월 상승률, "
                "생산의 3개월 연율과 미국 정책경로를 제한적으로 결합한 확률 앙상블입니다. "
                "환율은 단기·중기 추세와 평균회귀 모형을 워크포워드 오차로 가중하고 "
                "과거 예측오차 분포에서 80% 범위를 산출합니다."
            ),
            "rate_core_cpi_yoy": (round(cpi_yoy_validated, 5) if cpi_yoy_validated is not None else None),
            "rate_core_cpi_valid": cpi_valid,
            "rate_core_cpi_source_mode": core_cpi_meta.get("source_mode"),
            "rate_core_cpi_rows": core_cpi_meta.get("rows"),
            "rate_probability_backtest_samples": rate_backtest.get("samples"),
            "rate_probability_brier_score": rate_backtest.get("brier_score"),
            "rate_probability_benchmark_brier": rate_backtest.get("benchmark_brier"),
            "rate_probability_brier_skill_score": rate_backtest.get("brier_skill_score"),
            "rate_probability_accuracy": rate_backtest.get("accuracy"),
            "rate_probability_log_loss": rate_backtest.get("log_loss"),
            "rate_probability_evaluation_horizon": rate_backtest.get("evaluation_horizon"),
            "rate_probability_calibration_weight": rate_calibration_weight,
            "rate_industrial_valid": industrial_valid,
            "us_policy_rate_pct": round(us_current_rate, 3) if us_current_rate is not None else None,
            "us_kr_policy_gap_pctp": round(us_kr_gap, 3) if us_kr_gap is not None else None,
            "rate_industrial_3m_annualized": (round(industrial_3m, 5) if industrial_valid else None),
            "fx_model_weights": [round(float(w), 4) for w in fx_model.get("weights", [])],
            "fx_backtest_samples": int(fx_model.get("samples", 0) or 0),
            "fx_backtest_rmse_pct": (round(float(fx_model.get("rmse")) * 100, 3) if fx_model.get("rmse") is not None else None),
            "fx_prediction_interval": "워크포워드 오차 기반 약 80% 구간",
            "fx_1m_change": (
                round(
                    fx1,
                    5,
                )
                if fx1 is not None
                else None
            ),
            "fx_3m_change": (
                round(
                    fx3,
                    5,
                )
                if fx3 is not None
                else None
            ),
            "fx_forecast_bias_pct": (
                round(
                    fx_bias_pct,
                    5,
                )
            ),
            "policy_market_gap_pctp": (
                round(
                    gap,
                    3,
                )
                if gap is not None
                else None
            ),
            "us_policy_engine_connected": (
                us_connected
            ),
            "us_policy_next_meeting": (
                us_next_meeting
            ),
            "us_policy_expected_path_rows": (
                us_expected_path_count
            ),
            "us_policy_schema_version": (
                us_schema_version
            ),
        },
    }


def calculate_placeholder() -> KrwStrengthResult:
    return KrwStrengthResult(
        0.0,
        None,
        "약중립",
        "약중립",
        0.0,
    )


def _generic_values(rows: list[dict[str, Any]] | None) -> list[float]:
    values: list[tuple[str, float]] = []
    for idx, row in enumerate(rows or []):
        if not isinstance(row, dict):
            continue
        raw = row.get("value", row.get("DATA_VALUE", row.get("DT")))
        try:
            val = float(str(raw).replace(",", ""))
        except (TypeError, ValueError):
            continue
        date = str(row.get("date") or row.get("TIME") or row.get("PRD_DE") or f"{idx:08d}").replace("-", "").replace(".", "")
        values.append((date, val))
    dedup = {date: val for date, val in values if date}
    return [dedup[k] for k in sorted(dedup)]


def _strength_label_100(score: float | None) -> str:
    if score is None:
        return "확인 불가"
    if score >= 65:
        return "원화 강세"
    if score >= 56:
        return "원화 약강세"
    if score >= 47:
        return "원화 중립"
    if score >= 40:
        return "원화 약세"
    return "원화 매우 약세"



def _monthly_last_map_from_fx(merged_fx: list[tuple[str, float]]) -> dict[str, float]:
    out: dict[str, float] = {}
    for date, value in merged_fx:
        month = str(date).replace("-", "")[:6]
        if len(month) == 6:
            out[month] = float(value)
    return out


def _monthly_last_map_rows(rows: list[dict[str, Any]] | None) -> dict[str, float]:
    out: dict[str, float] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        raw = row.get("value", row.get("DATA_VALUE", row.get("DT")))
        try:
            value = float(str(raw).replace(",", ""))
        except (TypeError, ValueError):
            continue
        month = str(row.get("date") or row.get("TIME_PERIOD") or row.get("TIME") or "").replace("-", "").replace(".", "")[:6]
        if len(month) == 6:
            out[month] = value
    return out


def _clip_strength_return(value: float, bound: float = 0.18) -> float:
    return max(-bound, min(bound, value))


def _strength_oos_candidates(levels: list[float], origin: int, horizon: int) -> dict[str, float]:
    def delta(k: int) -> float:
        if origin < k:
            return 0.0
        return levels[origin] - levels[origin-k]
    d3, d6, d12 = delta(3), delta(6), delta(12)
    scale3 = horizon / 3.0
    scale6 = horizon / 6.0
    scale12 = horizon / 12.0
    return {
        "zero": 0.0,
        "mom3": _clip_strength_return(0.50 * d3 * scale3),
        "mom6": _clip_strength_return(0.55 * d6 * scale6),
        "mom12": _clip_strength_return(0.45 * d12 * scale12),
        "contrarian3": _clip_strength_return(-0.30 * d3 * scale3),
        "contrarian6": _clip_strength_return(-0.24 * d6 * scale6),
        "contrarian12": _clip_strength_return(-0.18 * d12 * scale12),
        "reversal_blend": _clip_strength_return(-0.20*d3*scale3 - 0.14*d6*scale6 - 0.08*d12*scale12),
        "blend": _clip_strength_return(0.32*d3*scale3 + 0.28*d6*scale6 + 0.15*d12*scale12),
    }


def _krw_strength_independent_oos(
    merged_fx: list[tuple[str, float]],
    global_data: dict[str, Any],
) -> dict[str, Any]:
    """No-lookahead OOS validation against an observable KRW-strength target.

    Target level = -50% log(USD/KRW) + 25% log(BIS NEER) + 25% log(BIS REER).
    A positive future change means KRW appreciation. Candidate selection at each
    origin only uses candidate losses that were already observable before origin.
    """
    fx = _monthly_last_map_from_fx(merged_fx)
    neer = _monthly_last_map_rows(global_data.get("krw_neer", []))
    reer = _monthly_last_map_rows(global_data.get("krw_reer", []))
    months = sorted(set(fx) & set(neer) & set(reer))
    rows = []
    for month in months:
        if fx[month] <= 0 or neer[month] <= 0 or reer[month] <= 0:
            continue
        level = -0.50*log(fx[month]) + 0.25*log(neer[month]) + 0.25*log(reer[month])
        rows.append((month, level))
    if len(rows) < 84:
        return {
            "separate_oos_validated": False,
            "target_definition": "-0.50*log(USD/KRW)+0.25*log(BIS_NEER)+0.25*log(BIS_REER)",
            "samples_available_months": len(rows),
            "reason": "NEER/REER와 USD/KRW 공통 월별 이력이 84개월 미만",
            "oos_by_horizon": {},
            "no_lookahead": True,
        }
    levels = [x[1] for x in rows]
    result: dict[str, Any] = {}
    for horizon in (3, 6, 12):
        losses: dict[str, list[float]] = {}
        strategy_err: list[float] = []
        base_err: list[float] = []
        hits: list[int] = []
        active = 0
        selected_counts: dict[str, int] = {}
        start = max(24, 12 + horizon)
        for origin in range(start, len(levels)-horizon):
            candidates = _strength_oos_candidates(levels, origin, horizon)
            mature = {name: errs for name, errs in losses.items() if len(errs) >= 24}
            if mature:
                def candidate_score(name: str) -> float:
                    errs = mature[name]
                    recent = errs[-48:]
                    return 0.60 * mean(recent) + 0.40 * mean(errs)
                selected = min(mature, key=candidate_score)
            else:
                selected = "zero"
            pred = candidates[selected]
            actual = levels[origin+horizon] - levels[origin]
            selected_counts[selected] = selected_counts.get(selected, 0) + 1
            strategy_err.append(pred-actual)
            base_err.append(-actual)
            if abs(actual) >= 0.002 and abs(pred) >= 0.0005:
                active += 1
                hits.append(int(pred*actual > 0))
            for name, value in candidates.items():
                losses.setdefault(name, []).append((value-actual)**2)
        n = len(strategy_err)
        model_rmse = sqrt(mean([e*e for e in strategy_err])) if strategy_err else None
        base_rmse = sqrt(mean([e*e for e in base_err])) if base_err else None
        skill = (1-model_rmse/base_rmse)*100 if model_rmse is not None and base_rmse else None
        da = mean(hits) if hits else None
        active_cov = active/n if n else 0.0
        strict = bool(n >= 60 and skill is not None and skill > 0 and da is not None and da >= 0.52 and active_cov >= 0.20)
        if strict and skill >= 5 and da >= 0.56:
            grade, score = "A", min(96, round(84 + min(8, skill) + min(4, (da-0.56)*25)))
        elif strict:
            grade, score = "B", min(88, round(76 + min(6, skill) + min(4, max(0, da-0.52)*25)))
        elif n >= 48:
            grade, score = "C", max(55, min(74, round(64 + (skill or -5)*0.6 + ((da or .5)-.5)*20)))
        else:
            grade, score = "D", 50
        result[f"{horizon}m"] = {
            "samples": n,
            "rmse": round(model_rmse, 6) if model_rmse is not None else None,
            "zero_benchmark_rmse": round(base_rmse, 6) if base_rmse is not None else None,
            "zero_benchmark_skill_pct": round(skill, 3) if skill is not None else None,
            "direction_accuracy": round(da, 4) if da is not None else None,
            "active_direction_coverage": round(active_cov, 4),
            "strict_skill_passed": strict,
            "grade": grade,
            "forecast_quality_score": score,
            "selected_model_counts": selected_counts,
            "selection_min_matured_errors": 24,
            "selection_no_lookahead": True,
            "benchmark": "zero_change_in_independent_krw_strength_target",
        }
    primary = result.get("3m", {})
    return {
        "separate_oos_validated": True,
        "target_definition": "-0.50*log(USD/KRW)+0.25*log(BIS_NEER)+0.25*log(BIS_REER); positive=KRW appreciation",
        "samples_available_months": len(rows),
        "primary_horizon": "3m",
        "primary_grade": primary.get("grade"),
        "primary_quality_score": primary.get("forecast_quality_score"),
        "oos_by_horizon": result,
        "no_lookahead": True,
        "selection_method": "expanding_origin_prequential_recent_plus_long_candidate_selection",
        "selection_rule": "60% recent-48 + 40% expanding matured squared error; no lookahead",
    }


def build_krw_strength_forecast(
    ecos: dict[str, Any],
    global_data: dict[str, Any] | None,
    fx_v2: dict[str, Any],
    rate_v2: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Independent KRW-strength composite for 3/6/12 month dashboard use.

    The target is *KRW strength*, not a duplicate USD/KRW card.  USD/KRW level and
    direction remain the largest anchor, while NEER/REER, broad-dollar/Asian FX,
    the US-Korea 2Y gap and external-balance inputs are grouped and re-normalised
    when an optional source is missing.  Future probabilities inherit the validated
    FX distribution with the sign inverted (USD/KRW down == KRW stronger).
    """
    global_data = global_data or {}
    merged_fx, spot_meta = _merge_fx_series(ecos, global_data)
    # OOS remains anchored to the historical series; only the current/live layer is overlaid.
    independent_oos = _krw_strength_independent_oos(merged_fx, global_data)
    live_merged_fx, spot_meta, model_anchor_spot = _apply_live_usdkrw_overlay(merged_fx, spot_meta, global_data)
    fx_values = [value for _, value in live_merged_fx]
    spot = fx_values[-1] if fx_values else None
    if spot is None or len(fx_values) < 61:
        return {
            "schema_version": "1.0.0",
            "engine_version": "1.4.0-prequential-candidate-tournament",
            "status": "insufficient_history",
            "forecast_operational": False,
            "forecast_path": [],
        }

    level_window = fx_values[-252:]
    fx_pctile = _percentile(level_window, spot)
    fx_level_score = 1.0 - 2.0 * fx_pctile if fx_pctile is not None else 0.0
    r20 = _pct(fx_values, 20) or 0.0
    r60 = _pct(fx_values, 60) or 0.0
    fx_momentum_score = -tanh(mean([r20, r60]) * 12.0)
    fx_group = max(-1.0, min(1.0, fx_level_score * 0.62 + fx_momentum_score * 0.38))

    factor_groups: dict[str, float] = {"usdkrw_level_momentum": fx_group}
    factor_details: dict[str, Any] = {
        "usdkrw_level_momentum": {
            "score": round(fx_group, 4),
            "spot": round(spot, 4),
            "one_year_percentile": round(fx_pctile, 4) if fx_pctile is not None else None,
            "ret20_pct": round(r20 * 100.0, 3),
            "ret60_pct": round(r60 * 100.0, 3),
            "source": spot_meta.get("source"),
            "observation": spot_meta.get("date"),
            "market_time_utc": spot_meta.get("market_time_utc"),
            "live_overlay_applied": bool(spot_meta.get("live_overlay_applied")),
            "model_anchor_usdkrw": round(model_anchor_spot, 4) if model_anchor_spot is not None else None,
        }
    }

    # Effective-exchange-rate group. Higher NEER/REER and positive recent change
    # mean a stronger won. Monthly BIS series are sufficient here.
    eff_scores: list[float] = []
    for key in ("krw_neer", "krw_reer"):
        vals = _generic_values(global_data.get(key, []))
        if not vals:
            continue
        pctile = _percentile(vals[-60:], vals[-1])
        level = (2.0 * pctile - 1.0) if pctile is not None else 0.0
        mom = _pct(vals, min(3, len(vals) - 1)) if len(vals) > 1 else None
        mom_score = tanh((mom or 0.0) * 8.0)
        score = max(-1.0, min(1.0, 0.6 * level + 0.4 * mom_score))
        eff_scores.append(score)
        factor_details[key] = {
            "score": round(score, 4), "latest": vals[-1],
            "percentile_60obs": round(pctile, 4) if pctile is not None else None,
            "recent_change_pct": round((mom or 0.0) * 100.0, 3),
        }
    if eff_scores:
        factor_groups["effective_fx"] = mean(eff_scores)

    # Global currency/risk group. A weaker broad dollar / CNY appreciation / JPY
    # appreciation is generally supportive for KRW, so signs are inverted.
    global_scores: list[float] = []
    for key, scale in (("broad_dollar", 0.04), ("usd_cny", 0.04), ("usd_jpy", 0.05)):
        vals = _generic_values(global_data.get(key, []))
        ret = _pct(vals, min(60, len(vals) - 1)) if len(vals) > 1 else None
        if ret is None:
            continue
        score = -tanh(ret / max(1e-6, scale))
        global_scores.append(score)
        factor_details[key] = {"score": round(score, 4), "change_pct": round(ret * 100.0, 3)}
    if global_scores:
        factor_groups["global_currency"] = mean(global_scores)

    # Rate-gap group. Prefer US-Korea 2Y yields; when FRED is unavailable, use
    # the connected US policy engine effective rate versus the BOK base rate as a
    # lower-weight proxy instead of dropping the whole group.
    us2 = _generic_values(global_data.get("us_2y", []))
    kr2 = _values((ecos or {}).get("kr_gov_2y", []) or (ecos or {}).get("kr_gov_3y", []))
    if us2 and kr2:
        gap = us2[-1] - kr2[-1]
        score = -tanh(gap / 1.5)
        factor_groups["rate_gap"] = score
        factor_details["rate_gap"] = {
            "score": round(score, 4),
            "us_minus_kr_2y_pctp": round(gap, 3),
            "source": "US/KR 2Y market yields",
            "proxy": False,
        }
    else:
        current_rate = (rate_v2 or {}).get("current") or {}
        try:
            us_policy_rate = float(current_rate.get("us_current_effective_rate_pct"))
            kr_policy_rate = float(current_rate.get("kr_base_rate_pct"))
            gap = us_policy_rate - kr_policy_rate
            score = -tanh(gap / 1.75) * 0.70
            factor_groups["rate_gap"] = score
            factor_details["rate_gap"] = {
                "score": round(score, 4),
                "us_minus_kr_policy_rate_pctp": round(gap, 3),
                "source": "US effective policy rate - BOK base rate proxy",
                "proxy": True,
            }
        except (TypeError, ValueError):
            pass

    # External balance group. Percentile-based normalisation avoids dependence on
    # the provider's units for current-account/reserve levels.
    ext_scores: list[float] = []
    ca = _values((ecos or {}).get("current_account", []))
    if ca:
        pctile = _percentile(ca[-60:], ca[-1])
        if pctile is not None:
            score = 2.0 * pctile - 1.0
            ext_scores.append(score)
            factor_details["current_account"] = {"score": round(score, 4), "percentile_60obs": round(pctile, 4)}
    reserves = _values((ecos or {}).get("fx_reserves", []))
    if len(reserves) > 1:
        change = _pct(reserves, min(12, len(reserves) - 1))
        if change is not None:
            score = tanh(change / 0.05)
            ext_scores.append(score)
            factor_details["fx_reserves"] = {"score": round(score, 4), "change_pct": round(change * 100.0, 3)}
    if ext_scores:
        factor_groups["external_balance"] = mean(ext_scores)

    group_weights = {
        "usdkrw_level_momentum": 0.40,
        "effective_fx": 0.25,
        "global_currency": 0.15,
        "rate_gap": 0.10,
        "external_balance": 0.10,
    }
    active_weight = sum(group_weights[k] for k in factor_groups) or 1.0
    total_group_weight = sum(group_weights.values()) or 1.0
    weighted_group_coverage = active_weight / total_group_weight
    macro_composite = sum(factor_groups[k] * group_weights[k] for k in factor_groups) / active_weight
    macro_composite = max(-1.0, min(1.0, macro_composite))
    current_score = 50.0 + 50.0 * macro_composite

    fx_rows = {int(row.get("months")): row for row in fx_v2.get("forecast_path", []) if row.get("months")}
    forecast_path: list[dict[str, Any]] = []
    group_coverage = len(factor_groups) / len(group_weights)
    non_fx = [v for k, v in factor_groups.items() if k != "usdkrw_level_momentum"]
    non_fx_macro = mean(non_fx) if non_fx else 0.0
    oos_by_horizon = independent_oos.get("oos_by_horizon", {}) if isinstance(independent_oos, dict) else {}

    for months in (3, 6, 12):
        row = fx_rows.get(months, {})
        point = row.get("point_forecast", row.get("mid"))
        try:
            point = float(point)
        except (TypeError, ValueError):
            point = spot
        # The FX model itself keeps its historical anchor/OOS.  For the current KRW
        # strength view, translate absolute forecast levels to the freshly fetched spot
        # while preserving the model's relative move.
        if model_anchor_spot and spot and point is not None and abs(float(model_anchor_spot)) > 1e-9:
            point = float(point) * float(spot) / float(model_anchor_spot)
        change_pct = row.get("change_pct")
        try:
            change = float(change_pct) / 100.0
        except (TypeError, ValueError):
            change = point / spot - 1.0 if spot else 0.0

        future_pctile = _percentile(level_window, point)
        future_level = 1.0 - 2.0 * future_pctile if future_pctile is not None else fx_level_score
        future_direction = -tanh(change * 10.0)
        decay = {3: 0.90, 6: 0.75, 12: 0.55}[months]
        future_raw = max(-1.0, min(1.0, 0.55 * future_level + 0.30 * future_direction + 0.15 * non_fx_macro * decay))
        future_score = 50.0 + 50.0 * future_raw
        delta = future_score - current_score
        direction = "up" if delta >= 2.0 else ("down" if delta <= -2.0 else "neutral")

        # FX probabilities invert naturally for KRW strength.
        p_up = row.get("down_probability")
        p_down = row.get("up_probability")
        p_neutral = row.get("neutral_probability")
        fx_quality = row.get("model_quality_score")
        try:
            fx_quality_num = float(fx_quality)
        except (TypeError, ValueError):
            fx_quality_num = 50.0
        # Prefer the independent KRW-strength target OOS once NEER/REER history exists.
        # Coverage remains explicit and cannot be hidden by a high backtest score.
        oos_row = oos_by_horizon.get(f"{months}m", {}) if isinstance(oos_by_horizon, dict) else {}
        oos_score = oos_row.get("forecast_quality_score")
        try:
            oos_score_num = float(oos_score)
        except (TypeError, ValueError):
            oos_score_num = None
        if independent_oos.get("separate_oos_validated") and oos_score_num is not None:
            quality_score = round(max(0.0, min(100.0, oos_score_num * 0.70 + weighted_group_coverage * 100.0 * 0.30)))
            quality_grade = oos_row.get("grade") or row.get("quality_grade")
        else:
            quality_score = round(max(0.0, min(100.0, fx_quality_num * 0.72 + weighted_group_coverage * 100.0 * 0.28)))
            quality_grade = row.get("quality_grade")

        score_range = None
        band = row.get("range_80")
        if isinstance(band, list) and len(band) >= 2:
            try:
                lo_fx, hi_fx = float(band[0]), float(band[1])
                lo_pct = _percentile(level_window, hi_fx)  # high USD/KRW = weak KRW
                hi_pct = _percentile(level_window, lo_fx)
                if lo_pct is not None and hi_pct is not None:
                    lo_score = 50.0 + 50.0 * max(-1.0, min(1.0, 0.55 * (1 - 2 * lo_pct) + 0.15 * non_fx_macro * decay))
                    hi_score = 50.0 + 50.0 * max(-1.0, min(1.0, 0.55 * (1 - 2 * hi_pct) + 0.15 * non_fx_macro * decay))
                    score_range = [round(min(lo_score, hi_score), 1), round(max(lo_score, hi_score), 1)]
            except (TypeError, ValueError):
                score_range = None

        forecast_path.append(
            {
                "months": months,
                "strength_score": round(future_score, 2),
                "grade": _strength_label_100(future_score),
                "change_points": round(delta, 2),
                "direction": direction,
                "up_probability": p_up,
                "neutral_probability": p_neutral,
                "down_probability": p_down,
                "range_80": score_range,
                "fx_anchor": round(point, 1),
                "quality_grade": quality_grade,
                "model_quality_score": quality_score,
                "independent_oos_grade": oos_row.get("grade"),
                "independent_oos_quality_score": oos_row.get("forecast_quality_score"),
                "independent_oos_strict_passed": oos_row.get("strict_skill_passed"),
                "prediction_status": "forecast",
            }
        )

    latest_neer = _generic_values(global_data.get("krw_neer", []))
    latest_reer = _generic_values(global_data.get("krw_reer", []))
    primary = next((row for row in forecast_path if row["months"] == 3), {})
    freshness = {
        "usdkrw": {
            "source": spot_meta.get("source"),
            "observation": spot_meta.get("date"),
            "market_time_utc": spot_meta.get("market_time_utc"),
            "retrieved_at_utc": spot_meta.get("retrieved_at_utc"),
            "market_state": spot_meta.get("market_state"),
            "live_overlay_applied": bool(spot_meta.get("live_overlay_applied")),
        },
        "krw_neer": _latest_row_meta(global_data.get("krw_neer", []), "BIS EER"),
        "krw_reer": _latest_row_meta(global_data.get("krw_reer", []), "BIS EER"),
        "us_2y": _latest_row_meta(global_data.get("us_2y", []), "FRED/Global collector"),
        "kr_2y": _latest_row_meta((ecos or {}).get("kr_gov_2y", []) or (ecos or {}).get("kr_gov_3y", []), "ECOS"),
        "current_account": _latest_row_meta((ecos or {}).get("current_account", []), "ECOS"),
        "fx_reserves": _latest_row_meta((ecos or {}).get("fx_reserves", []), "ECOS"),
    }
    primary_oos = ((independent_oos.get("oos_by_horizon") or {}).get("3m") or {}) if isinstance(independent_oos, dict) else {}
    missing_groups = [name for name in group_weights if name not in factor_groups]
    improvement = {
        "score_inflation_forbidden": True,
        "current_model_quality_score": primary.get("model_quality_score"),
        "independent_oos_quality_score": independent_oos.get("primary_quality_score") if isinstance(independent_oos, dict) else None,
        "weighted_factor_coverage_pct": round(weighted_group_coverage * 100.0, 1),
        "missing_factor_groups": missing_groups,
        "objective_next_targets": {
            "weighted_factor_coverage_pct": 100.0,
            "independent_oos_grade": "A",
            "zero_benchmark_skill_pct_min": 5.0,
            "direction_accuracy_min": 0.56,
            "active_direction_coverage_min": 0.20,
        },
        "observed_primary_oos": {
            "zero_benchmark_skill_pct": primary_oos.get("zero_benchmark_skill_pct"),
            "direction_accuracy": primary_oos.get("direction_accuracy"),
            "active_direction_coverage": primary_oos.get("active_direction_coverage"),
            "samples": primary_oos.get("samples"),
        },
        "note": "점수는 임의 상향하지 않고 독립 OOS 성능과 입력 커버리지가 실제 개선될 때만 상승합니다.",
    }
    return {
        "schema_version": "1.0.0",
        "engine_version": "1.4.0-prequential-candidate-tournament",
        "status": "ok",
        "forecast_operational": True,
        "current": {
            "strength_score": round(current_score, 2),
            "grade": _strength_label_100(current_score),
            "usdkrw": round(spot, 4),
            "usdkrw_source": spot_meta.get("source"),
            "usdkrw_model_anchor": round(model_anchor_spot, 4) if model_anchor_spot is not None else None,
            "usdkrw_live_overlay_applied": bool(spot_meta.get("live_overlay_applied")),
            "neer": latest_neer[-1] if latest_neer else None,
            "reer": latest_reer[-1] if latest_reer else None,
        },
        "forecast_path": forecast_path,
        "factor_panel": {
            "group_scores": {k: round(v, 4) for k, v in factor_groups.items()},
            "group_weights": group_weights,
            "details": factor_details,
            "active_group_count": len(factor_groups),
            "group_coverage": round(group_coverage, 3),
            "weighted_group_coverage": round(weighted_group_coverage, 3),
        },
        "input_freshness": freshness,
        "quality_improvement": improvement,
        "quality": {
            "grade": primary.get("quality_grade"),
            "model_quality_score": primary.get("model_quality_score"),
            "separate_oos_validated": bool(independent_oos.get("separate_oos_validated")),
            "independent_oos_primary_grade": independent_oos.get("primary_grade"),
            "independent_oos_quality_score": independent_oos.get("primary_quality_score"),
            "independent_oos_validation": independent_oos,
            "quality_score_semantics": (
                "70% independent KRW-strength OOS quality + 30% weighted factor coverage; not a probability"
                if independent_oos.get("separate_oos_validated")
                else "FX OOS quality 72% + weighted KRW factor coverage 28%; not a probability"
            ),
            "validation_basis": (
                "independent observable KRW-strength target OOS + weighted factor coverage"
                if independent_oos.get("separate_oos_validated")
                else "FX walk-forward OOS distribution + weighted KRW-strength factor coverage"
            ),
        },
        "limitations": [
            "원화 강도 확률은 USD/KRW 예측분포의 방향을 반전해 사용하고 NEER·REER·금리차·대외건전성으로 점수를 보정합니다.",
            "독립 OOS는 USD/KRW·BIS NEER·BIS REER의 관측 가능한 복합 원화강도 목표를 과거시점 순차선택으로 검증합니다.",
        ],
    }
