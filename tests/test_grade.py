from src.models.krw_strength import grade_from_score

def test_grade_boundaries():
    assert grade_from_score(0.8) == "강강"
    assert grade_from_score(0.5) == "강약"
    assert grade_from_score(0.1) == "강중립"
    assert grade_from_score(0.0) == "약중립"
    assert grade_from_score(-0.2) == "약강"
    assert grade_from_score(-0.5) == "약약"
