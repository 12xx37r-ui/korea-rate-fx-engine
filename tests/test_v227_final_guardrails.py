from src.collectors.ecos import _redact_secret_text as redact_ecos
from src.collectors.kosis import _redact_secret_text as redact_kosis
from src.models.krw_strength import _strength_oos_candidates


def test_ecos_secret_redaction(monkeypatch):
    monkeypatch.setenv('ECOS_API_KEY', 'SECRET123')
    text = 'HTTPSConnectionPool url: /api/StatisticSearch/SECRET123/json/kr/1/1000/X'
    redacted = redact_ecos(text)
    assert 'SECRET123' not in redacted
    assert '/api/StatisticSearch/***/json/' in redacted


def test_kosis_secret_redaction(monkeypatch):
    monkeypatch.setenv('KOSIS_API_KEY', 'SECRET456')
    text = 'https://kosis.kr/x?method=getList&apiKey=SECRET456&orgId=101'
    redacted = redact_kosis(text)
    assert 'SECRET456' not in redacted
    assert 'apiKey=***' in redacted


def test_krw_strength_guardrail_candidate_universe():
    levels = [1.0 + i * 0.001 for i in range(40)]
    legacy = _strength_oos_candidates(levels, 30, 3, enhanced=False)
    enhanced = _strength_oos_candidates(levels, 30, 12, enhanced=True)
    assert 'contrarian6' not in legacy
    assert 'contrarian12' not in legacy
    assert 'reversal_blend' not in legacy
    assert {'contrarian6', 'contrarian12', 'reversal_blend'} <= set(enhanced)
