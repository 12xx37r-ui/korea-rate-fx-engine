from src.collectors.korea_asset_fundamentals import _naver_metric_num

def test_naver_metric_num_units():
    assert _naver_metric_num("26.86배") == 26.86
    assert _naver_metric_num("396원") == 396.0
    assert _naver_metric_num("9.76%") == 9.76
    assert _naver_metric_num("1,411.24원") == 1411.24
    assert _naver_metric_num("-") is None
