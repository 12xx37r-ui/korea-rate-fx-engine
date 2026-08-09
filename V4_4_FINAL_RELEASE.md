# Korea Engine V4.4 Final Low-Call Release

## 이번 최종 수정 범위

- GLOBAL_MARKET
  - FRED 공개지표 13개(기존 11개 + 한국 NEER/REER)를 **1회 CSV batch**로 수집.
  - 최초 미보유 지표는 최근 900일만 bootstrap하고, 이후에는 최근 120일 overlap만 증분 수집 후 로컬 병합.
  - FRED 실패 시 지표별 재시도 금지. 직전 정상 이력을 즉시 재사용.
  - Yahoo USD/KRW는 별도 1회만 사용. GLOBAL_MARKET 외부 요청 최대 2회.

- KOSIS
  - connect/read 대기 장기 반복 제거: timeout 4초, 1회 시도.
  - 첫 네트워크/인증 장애 즉시 circuit breaker를 열어 다음 KOSIS 요청을 생략.
  - 정상 이력이 있으면 최근 18개월만 증분 갱신 후 로컬 병합.
  - URL에 apiKey 문자열이 있다는 이유로 네트워크 timeout을 credential_error로 오분류하지 않음.

- USD/KRW V4
  - 항상 1/3/6/12개월 점예측·확률·구간을 산출.
  - random walk는 실전 중심값 대체가 아니라 benchmark로만 사용.
  - macro factor가 확보되면 broad dollar/CNY/JPY/VIX/HY OAS/oil/commodity/한미2년물 금리차를 후보모형에 추가.
  - 검증력이 약하면 예측을 중단하지 않고 shrinkage와 등급으로 과신을 억제.

- 원화강도
  - 독립 `output/korea_krw_strength_forecast.json` 생성.
  - USD/KRW, NEER/REER, broad dollar/CNY/JPY, 한미2년 금리차, 경상수지, 외환보유액을 그룹화해 중복가중을 줄임.
  - 3/6/12개월 연속 예측 산출.
  - 품질점수는 FX V4 OOS 품질 + 공개요인 coverage 기반이며, 별도 원화강도 목표 OOS를 했다고 과장하지 않음.

- 원화유동성
  - 기존 연속 예측 유지.
  - `model_quality_score`를 예측 적중률로 오해하지 않도록 `input_data_quality_score`와 의미 필드를 추가.

- Unified V3
  - 정책금리 + FX + 원화유동성 + 원화강도를 하나의 `korea_rate_fx_outlook_v3.json`에 포함.
  - GAS가 정상 운용 시 이 JSON 한 개를 한국 예측의 단일 기준원으로 사용 가능.

## 고정 원칙

기능 정확성을 유지하면서 외부 API·UrlFetch·GitHub 호출을 최소화하고, 중복 호출 방지·캐싱·배치 처리·로컬 계산을 우선 적용해 서버 부하를 최소화한다.
