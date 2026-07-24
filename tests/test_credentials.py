from src.core.credentials import credential_issue, credential_metadata


def test_detects_expired_or_invalid_key_message():
    assert credential_issue("ECOS 오류 INFO-100: 인증키가 유효하지 않습니다")
    assert credential_issue("API key expired")


def test_does_not_misclassify_data_error_as_key_error():
    assert not credential_issue("KOSIS 오류 30: 데이터가 존재하지 않습니다")
    assert not credential_issue("잘못된 요청 변수를 호출하였습니다")


def test_metadata_gives_secret_replacement_action():
    data = credential_metadata("ECOS_API_KEY", "renewal_required", "expired")
    assert data["action_required"] is True
    assert "ECOS_API_KEY" in data["action"]
