from src.core.kosis_resolver import _choose_item, _score_candidate


def test_score_candidate_prefers_matching_table():
    good = {"TBL_NM": "소비자물가지수(2020=100)", "CONTENTS": "근원물가"}
    bad = {"TBL_NM": "시도별 소비자물가지수"}
    assert _score_candidate(good, ["소비자물가지수", "근원"], ["시도별"]) > _score_candidate(
        bad, ["소비자물가지수", "근원"], ["시도별"]
    )


def test_choose_item():
    rows = [
        {"ITM_ID": "T1", "ITM_NM": "총지수"},
        {"ITM_ID": "T2", "ITM_NM": "농산물 및 석유류 제외 지수"},
    ]
    chosen = _choose_item(rows, ["농산물 및 석유류 제외"], [])
    assert chosen["ITM_ID"] == "T2"
