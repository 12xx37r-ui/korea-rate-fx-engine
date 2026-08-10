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


def test_selective_fx_validation_reports_active_signal_metrics():
    from src.models.rate_validation import fx_walk_forward_validation

    rows = []
    value = 1200.0
    for i in range(1100):
        # Alternating medium-term swings create both active and abstention periods.
        phase = (i // 80) % 2
        value += 1.1 if phase == 0 else -0.9
        rows.append({"TIME": f"2020{i//250+1:04d}{i%250+1:04d}", "DATA_VALUE": value})
    out = fx_walk_forward_validation(rows)
    assert "active_signal_coverage" in out
    assert out["model_specification"]["activation_threshold_abs_return"] == 0.03
    assert out["horizons"]["3m"]["model"] == "selective_60d_contrarian_shrunk"


def test_rate_validation_extends_history_with_3y_before_2y():
    from src.models.rate_validation import combine_market_rate_rows_for_backtest, numeric_series

    y3 = [
        {"TIME": "20190131", "DATA_VALUE": "2.00"},
        {"TIME": "20200131", "DATA_VALUE": "1.80"},
        {"TIME": "20210131", "DATA_VALUE": "1.60"},
        {"TIME": "20210310", "DATA_VALUE": "1.90"},
    ]
    y2 = [
        {"TIME": "20210310", "DATA_VALUE": "1.70"},
        {"TIME": "20210401", "DATA_VALUE": "1.75"},
    ]
    rows, meta = combine_market_rate_rows_for_backtest(y2, y3)
    series = dict(numeric_series(rows))
    assert meta["mode"] == "kr_gov_3y_pre_2y_then_2y_fixed_proxy"
    assert meta["first_2y_date"] == "20210310"
    assert series["20210131"] == 1.60
    assert series["20210310"] == 1.70  # 2Y replaces same-day 3Y from inception onward.


def test_saved_vintage_gate_requires_matured_monthly_snapshots(tmp_path):
    import json
    from src.models.rate_validation import evaluate_rate_vintage_snapshots

    base = []
    # 36 months of unchanged policy rate -> matured labels are all hold.
    for i in range(36):
        y = 2020 + i // 12
        m = i % 12 + 1
        base.append({"TIME": f"{y}{m:02d}28", "DATA_VALUE": "2.50"})
    vint = tmp_path / "vintages"
    vint.mkdir()
    for i in range(24):
        y = 2020 + i // 12
        m = i % 12 + 1
        obj = {
            "captured_at": f"{y}-{m:02d}-20T12:00:00+09:00",
            "rate_forecast": {
                "status": "ok",
                "meeting_path": [{"probabilities": {"hold": 0.8, "hike": 0.1, "cut": 0.1}}],
            },
        }
        (vint / f"{y}-{m:02d}-20.json").write_text(json.dumps(obj), encoding="utf-8")
    out = evaluate_rate_vintage_snapshots(vint, base, min_matured_samples=24)
    assert out["samples"] == 24
    assert out["qualified"] is True
    assert out["network_calls_added"] == 0
    assert out["accuracy"] == 1.0
