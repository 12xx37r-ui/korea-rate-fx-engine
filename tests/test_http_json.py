from src.core.http import _parse_json_like


def test_parse_standard_json():
    assert _parse_json_like('{"a": 1}') == {"a": 1}


def test_parse_jsonp():
    assert _parse_json_like('callback({"a": 1});') == {"a": 1}


def test_parse_single_quote_object():
    assert _parse_json_like("{'a': 1, 'rows': []}") == {"a": 1, "rows": []}
