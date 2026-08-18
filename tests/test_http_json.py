from unittest.mock import Mock, patch

from src.core.http import _parse_json_like, get_json


def test_parse_standard_json_array():
    assert _parse_json_like('[{"a": 1}]') == [{"a": 1}]


def test_parse_jsonp():
    assert _parse_json_like('callback({"a": 1});') == {"a": 1}


def test_parse_single_quoted_object():
    assert _parse_json_like("{'a': 1}") == {"a": 1}


def test_get_json_accepts_json_body_with_html_content_type():
    response = Mock()
    response.status_code = 200
    response.text = '[{"ORG_ID":"101","TBL_ID":"DT_TEST"}]'
    response.headers = {"content-type": "text/html; charset=UTF-8"}
    response.raise_for_status.return_value = None

    with patch("src.core.http.requests.get", return_value=response):
        payload = get_json("https://example.test", retries=1)

    assert payload == [{"ORG_ID": "101", "TBL_ID": "DT_TEST"}]
