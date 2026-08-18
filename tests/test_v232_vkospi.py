from unittest.mock import Mock, patch
from src.collectors.realtime_risk import _collect_vkospi

def test_vkospi_accepts_only_verified_live():
    live={"value":22.4,"date":"20260818","source":"test E62001","source_validation":"verified","code":"E62001"}
    with patch("src.collectors.realtime_risk._vkospi_naver_live", return_value=live):
        out=_collect_vkospi([])
    assert out["available"] is True
    assert out["rows"][-1]["value"] == 22.4
    assert out["source_validation"] == "verified"

def test_vkospi_failure_does_not_reuse_unverified_legacy():
    previous=[{"date":"20260817","value":20.0,"source":"pykrx","ticker":"1024"}]
    with patch("src.collectors.realtime_risk._vkospi_naver_live", side_effect=RuntimeError("down")):
        out=_collect_vkospi(previous)
    assert out["available"] is False
    assert out["rows"] == []
