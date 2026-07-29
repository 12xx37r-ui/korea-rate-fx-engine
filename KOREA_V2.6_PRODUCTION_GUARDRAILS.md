# 한국 V2.6 실전 안전패치

- 기존 V2.5 금리·환율 계산식과 JSON 스키마를 변경하지 않는다.
- KOSIS 확정 캐시를 유지한다.
- 필수 수집원(`required_sources`)이 `not_configured`이면 `api_health.blocking_errors`에 포함한다.
- `output/production_readiness_v2.json`을 추가한다.
- 기준금리, 환율 1·3·6·12개월을 각 품질 게이트대로 별도 표시한다.
- 기준금리 게이트 미통과 상태를 환율 3·6개월 통과와 합쳐 전체 준기관급으로 오인하지 않는다.
- 현재 방향 신호 비활성과 과거 OOS 검증 통과를 분리한다.
