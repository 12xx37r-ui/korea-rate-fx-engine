from src.models.rate_validation import core_cpi_yoy_series, calibrate_probabilities


def test_core_cpi_monthly_rates_compound_to_yoy():
    rows = []
    for month in range(1, 13):
        rows.append({
            "PRD_DE": f"2025{month:02d}",
            "DT": "0.2",
            "ITM_NM": "전월비",
            "C1_NM": "농산물및석유류제외지수",
            "UNIT_NM": "%",
        })
    series, meta = core_cpi_yoy_series(rows)
    assert meta["valid"] is True
    assert meta["source_mode"] == "monthly_compounded_yoy"
    assert 0.023 < series[-1][1] < 0.025


def test_core_cpi_prefers_official_yoy():
    rows = []
    for month in range(1, 13):
        rows.append({
            "PRD_DE": f"2025{month:02d}",
            "DT": "2.3",
            "ITM_NM": "전년동월비",
            "C1_NM": "농산물및석유류제외지수",
            "UNIT_NM": "%",
        })
    series, meta = core_cpi_yoy_series(rows)
    assert meta["source_mode"] == "official_yoy"
    assert series[-1][1] == 0.023


def test_calibration_shrinks_unvalidated_probability():
    probs, alpha = calibrate_probabilities(
        {"hold": 0.4, "hike": 0.4, "cut": 0.2},
        {"samples": 10, "class_frequency": {"hold": 0.8, "hike": 0.1, "cut": 0.1}},
    )
    assert alpha == 0.55
    assert probs["hold"] > 0.4
