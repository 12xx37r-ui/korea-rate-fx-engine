from src.models.krw_strength import _values, _validated_macro_yoy, _fx_ensemble

def test_values_sorts_descending_api_rows():
    rows=[{"TIME":"202402","DATA_VALUE":"102"},{"TIME":"202401","DATA_VALUE":"100"}]
    assert _values(rows)==[100.0,102.0]

def test_macro_sanity_rejects_impossible_yoy():
    vals=[100.0]*12+[200.0]
    value,ok=_validated_macro_yoy(vals,12,-0.03,0.12)
    assert value is None and ok is False

def test_fx_ensemble_has_many_walkforward_samples():
    vals=[1200+i*0.1 for i in range(1000)]
    result=_fx_ensemble(vals,60)
    assert result["samples"] >= 80
