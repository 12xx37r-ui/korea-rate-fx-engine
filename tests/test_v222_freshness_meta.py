from src.models.krw_strength import _row_period


def test_lowercase_date_is_recognized_for_fred_bis_rows():
    assert _row_period({'date': '2026-08-14', 'value': 1.0}).startswith('20260814')
