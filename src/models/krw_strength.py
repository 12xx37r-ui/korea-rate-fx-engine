from __future__ import annotations

from dataclasses import dataclass
from math import exp, tanh, sqrt, log
from statistics import mean, median
from typing import Any


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


def _values(
    rows: list[dict[str, Any]],
    keys: tuple[str, ...] = ("DATA_VALUE", "DT"),
) -> list[float]:
    out: list[float] = []

    for row in rows:
        for key in keys:
            try:
                value = row.get(key)

                if value not in (None, "", "-"):
                    out.append(float(value))
                    break

            except (TypeError, ValueError):
                continue

    return out


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
    """Walk-forward weighted trend/mean-reversion ensemble using only information available at each vintage."""
    if len(values) < 140:
        latest = values[-1] if values else None
        return {"mid": latest, "errors": [], "weights": [0.4, 0.35, 0.25], "samples": 0}

    def forecasts(hist: list[float]) -> list[float]:
        x = hist[-1]
        med = median(hist[-252:])
        r20 = hist[-1] / hist[-21] - 1.0 if len(hist) > 21 else 0.0
        r60 = hist[-1] / hist[-61] - 1.0 if len(hist) > 61 else r20
        f1 = x * (1.0 + max(-0.06, min(0.06, r20 * 1.25)))
        f2 = x * (1.0 + max(-0.06, min(0.06, r60 * 0.65)))
        reversion = max(-0.05, min(0.05, (med / x - 1.0) * 0.45)) if x and med else 0.0
        f3 = x * (1.0 + reversion)
        return [f1, f2, f3]

    errors_by_model = [[], [], []]
    start = max(100, len(values) - 360)
    for t in range(start, len(values) - horizon, 10):
        fs = forecasts(values[:t])
        actual = values[t + horizon - 1]
        for i, pred in enumerate(fs):
            errors_by_model[i].append((pred / actual - 1.0) if actual else 0.0)

    rmses = []
    for errs in errors_by_model:
        rmses.append(sqrt(mean([e * e for e in errs])) if errs else 0.05)
    inv = [1.0 / max(0.005, r) for r in rmses]
    total = sum(inv) or 1.0
    weights = [x / total for x in inv]
    current_fs = forecasts(values)
    mid = sum(w * f for w, f in zip(weights, current_fs))
    combined_errors = []
    n = min(len(x) for x in errors_by_model) if all(errors_by_model) else 0
    for j in range(n):
        combined_errors.append(sum(weights[i] * errors_by_model[i][j] for i in range(3)))
    return {"mid": mid, "errors": combined_errors, "weights": weights, "samples": n, "rmse": sqrt(mean([e*e for e in combined_errors])) if combined_errors else None}


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


def build_snapshot(
    ecos: dict[str, list[dict[str, Any]]],
    kosis: dict[str, list[dict[str, Any]]],
    us_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    fx = _values(
        ecos.get("usdkrw", [])
    )

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

    core_cpi = _values(
        kosis.get("cpi_core", [])
    )

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

    rate_prob = _rate_probabilities(
        gap,
        core_cpi,
        industrial,
        us_policy,
    )

    rate_direction = _rate_label(
        rate_prob
    )

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

    # 환율 전망: 세 가지 모형을 과거 워크포워드 오차로 가중한다.
    fx_model = _fx_ensemble(fx, horizon=60)
    fx_mid = fx_model.get("mid")
    fx_bias_pct = (fx_mid / latest_fx - 1.0) if (fx_mid is not None and latest_fx) else 0.0

    # 정책금리차 제약은 작은 보정치로만 사용한다. 시장 추세를 덮어쓰지 않는다.
    us_signal = _us_policy_signal(us_policy)
    if fx_mid is not None and latest_fx is not None:
        differential_adjustment = max(-0.012, min(0.012, us_signal * 0.006))
        fx_mid = fx_mid * (1.0 + differential_adjustment)
        fx_bias_pct = fx_mid / latest_fx - 1.0

    us_connected = _us_policy_connected(
        us_policy
    )

    bt_errors = fx_model.get("errors", [])
    q10 = _quantile(bt_errors, 0.10)
    q90 = _quantile(bt_errors, 0.90)
    if q10 is None or q90 is None:
        q10, q90 = (-0.04, 0.04)
    # 예측오차 e=(예측/실제-1)이므로 실제 범위는 예측/(1+e)로 역산한다.
    fx_range = (
        [
            round(fx_mid / (1.0 + q90), 1),
            round(fx_mid / (1.0 + q10), 1),
        ]
        if fx_mid is not None
        else None
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

    coverage = (
        sum(
            bool(value)
            for value in (
                fx,
                base,
                y2 or y3,
                core_cpi,
                industrial,
            )
        )
        / 5.0
    )

    bt_samples = int(fx_model.get("samples", 0) or 0)
    bt_rmse = fx_model.get("rmse")
    backtest_quality = min(1.0, bt_samples / 24.0)
    error_quality = max(0.0, min(1.0, 1.0 - ((bt_rmse or 0.08) / 0.08)))
    confidence_cap = 0.82 if us_connected else 0.55
    confidence = round(min(confidence_cap, 0.30 + coverage * 0.28 + backtest_quality * 0.12 + error_quality * 0.12), 2)

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
            "krw_strength_score": round(
                future_score,
                4,
            ),
            "krw_strength_grade": (
                future_grade
            ),
            "confidence": confidence,
            "confidence_limit": (
                "미국 정책금리 엔진 연결 반영"
                if us_connected
                else (
                    "미국 정책금리 엔진 "
                    "미연결로 0.55 상한 적용"
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
            "rate_core_cpi_yoy": (round(_yoy(core_cpi, 12), 5) if _yoy(core_cpi, 12) is not None else None),
            "rate_industrial_3m_annualized": (round(_annualized_change(industrial, 3), 5) if _annualized_change(industrial, 3) is not None else None),
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
