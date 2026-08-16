from src.models.rate_validation import probability_candidates_from_features, RATE_PROBABILITY_SPECS
from src.models.krw_strength import _strength_oos_candidates
from src.models.fx_forecast_v4 import _candidate_returns, _global_lookups


def test_rate_candidate_specs_are_additive_and_probabilities_sum_to_one():
    candidates = probability_candidates_from_features(0.5, 0.025, 0.02)
    assert set(candidates) == set(RATE_PROBABILITY_SPECS)
    assert "baseline" in candidates
    for p in candidates.values():
        assert abs(sum(p.values()) - 1.0) < 1e-9
        assert set(p) == {"hold", "hike", "cut"}


def test_strength_candidate_tournament_contains_zero_and_multiple_reversal_specs():
    levels = [i * 0.001 for i in range(40)]
    c = _strength_oos_candidates(levels, 30, 3)
    assert c["zero"] == 0.0
    assert "contrarian3" in c and "contrarian6" in c and "contrarian12" in c
    assert "reversal_blend" in c


def test_fx_candidates_add_economic_specs_when_inputs_exist():
    values = [1200.0 + i * 0.2 for i in range(400)]
    dates = [f"2025{1 + (i//28)%12:02d}{1 + i%28:02d}" for i in range(400)]
    rows = lambda v: [{"date": dates[-1], "value": v}]
    global_data = {"us_2y": rows(4.1), "vix": rows(22.0), "hy_oas": rows(4.4)}
    ecos = {"kr_gov_2y": rows(3.2), "kr_base_rate": rows(2.75)}
    lookups = _global_lookups(global_data, ecos)
    c, _ = _candidate_returns(values, dates, 399, 63, lookups)
    assert "rate_gap_carry" in c
    assert "risk_regime" in c
    assert "contrarian_120" in c
