from __future__ import annotations
from dataclasses import dataclass

@dataclass
class KrwStrengthResult:
    score: float
    percentile: float | None
    grade: str
    direction_grade: str
    confidence: float

def grade_from_score(score: float) -> str:
    if score >= 0.67:
        return "강강"
    if score >= 0.34:
        return "강약"
    if score >= 0.08:
        return "강중립"
    if score > -0.08:
        return "약중립"
    if score > -0.34:
        return "약강"
    return "약약"

def calculate_placeholder() -> KrwStrengthResult:
    # 실제 데이터 정규화와 백테스트 전에는 임의 예측값을 만들지 않습니다.
    return KrwStrengthResult(0.0, None, "약중립", "약중립", 0.0)
