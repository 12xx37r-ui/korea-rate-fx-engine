# Korea rate/FX engine V2.7 — objective validation final guardrails

- 한국 기준금리 검증을 발표시차 반영 expanding walk-forward 고정 사양으로 변경했습니다.
- 기준모형은 전체 표본 빈도를 미리 보는 방식이 아니라, 각 원점 이전 결과만 사용하는 순차 빈도(Laplace smoothing)입니다.
- 95% Wilson 방향정확도 하한을 추가했습니다.
- KRX/REB는 고정 기준금리 모형에 실제 사용되지 않으므로 인증 점수에서 제외하고 선택적 보강자료로 분리했습니다.
- 인증 입력 완전성은 ECOS·KOSIS·미국정책경로·검증무결성만 평가합니다.
- 실시간 빈티지가 없는 점은 계속 명시하되, 발표시차 반영 재구성 OOS를 준기관급 인증 근거로 사용합니다.
- production readiness는 최소입력, 전체 고정모형 입력, 선택적 보강자료 완전성을 분리합니다.
- 환율 3개월·6개월 고정모형과 게이트는 변경하지 않았습니다.
