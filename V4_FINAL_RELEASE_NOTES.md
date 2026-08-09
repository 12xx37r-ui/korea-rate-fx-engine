# V4 Final — 연속형 한국 금리·환율·원화강도·원화유동성 엔진

## 목적

이 버전은 한국 엔진이 외부 예측 JSON이나 일부 선택 데이터가 비어 있다는 이유로 `보류`, `연결대기`, `자료미확보`, `현재값 그대로`를 실전 예측값으로 내보내는 구조를 제거한다.

## 핵심 변경

1. **USD/KRW는 항상 확률 예측**
   - 1·3·6·12개월 점예측, 상승/중립/하락 확률, 50%/80% 구간을 산출한다.
   - 랜덤워크(현재값 유지)는 검증용 벤치마크로만 남긴다.
   - 검증력이 약하면 예측을 중단하지 않고 예측폭을 축소하고 품질등급을 낮춘다.

2. **환율 단일 예측원**
   - 기존 V2 파일명은 유지하지만 내부 계산은 `continuous_oos_weighted_ensemble_v4`로 통일한다.
   - V3는 별도 그림자 환율모형을 만들지 않고 V4 결과를 그대로 통합한다.

3. **실제 원자료 기반 요인 결합**
   - 기술요인: 20·60·120일 추세, 60일 역추세, 252일 평균회귀, 추세가속도.
   - 공개 글로벌 요인: Broad Dollar, CNY, JPY, VIX, HY OAS, WTI, 원자재, 한미 2년물 금리차.
   - 각 모형 가중치는 과거 시점 순차 walk-forward 성능으로 동적으로 결정한다.

4. **최신 USD/KRW 소스 계층**
   - 장기 기준: ECOS.
   - 보조: FRED DEXKOUS.
   - 최신 시장가격 overlay: Yahoo `KRW=X`.
   - Yahoo/FRED 실패는 엔진 정지 사유가 아니다.

5. **원화강도 예측 개선**
   - 원화강도 1~3개월 예상값이 V4 USD/KRW 예측을 단일 입력으로 사용한다.
   - 과거의 별도 환율 앙상블과 V2/V3 간 상충을 제거한다.

6. **원화유동성 예측 추가**
   - M1·M2·Lf를 우선 사용한다.
   - 통화량이 일시적으로 수집되지 않아도 기준금리·국고채·예상 정책경로로 연속 예측한다.
   - 출력: `output/korea_krw_liquidity_forecast.json`.

7. **수집기 장애 연속성**
   - 한 데이터원 오류가 전체 workflow를 중단시키지 않는다.
   - 새 수집값이 비어 있으면 이전 GitHub commit의 정상 원자료 시계열을 유지한다.
   - 이전 정상 예측을 사용하는 경우에도 새 값을 임의 생성하지 않고 continuity metadata를 남긴다.

8. **GLOBAL_MARKET 실제 실행**
   - 기존 저장소에는 collector가 있었지만 `src.main`에서 호출되지 않아 `raw_global_market.json`이 비어 있을 수 있었다.
   - V4에서는 매 실행 시 GLOBAL_MARKET collector를 실제 호출한다.

9. **품질표현 정리**
   - `신뢰도 61%`처럼 확률로 오해할 수 있는 표현 대신 `model_quality_score`와 A~D 검증등급을 사용한다.
   - `quality_gate.passed`는 하위 대시보드 호환을 위한 운영 가능 여부이며, 엄격한 통계우위는 `strict_passed`로 별도 공개한다.

## 출력 파일

- `output/korea_rate_forecast_v2.json`
- `output/korea_fx_forecast_v2.json` — V4 연속형 FX 엔진 결과
- `output/korea_rate_fx_outlook.json`
- `output/korea_rate_fx_outlook_v3.json` — V4 통합 결과
- `output/krw_strength_preview.json`
- `output/korea_krw_liquidity_forecast.json`
- `output/korea_validation_v2.json`
- `output/production_readiness_v2.json`
- `output/api_health.json`

## 검증

- Python 문법검사 수행.
- 전체 pytest 통과를 배포 조건으로 한다.
- 환율 랜덤워크는 예측모형이 아닌 성능 비교 기준으로만 사용한다.

## 적용

GitHub 저장소의 파일을 이 압축본으로 교체한 뒤 Actions에서 `Korea Rate FX Engine` workflow를 1회 수동 실행한다. 이후 평일 예약 실행으로 출력이 갱신된다.

## Final bridge integration

- GAS K/K+ 표시부는 `forecast_operational`과 엄격 OOS 인증을 분리한다.
- 환율의 작은 변화는 `방향 보류`가 아니라 실제 모델 결과인 `중립 예상`으로 표시한다.
- 3개월 상승/중립/하락 확률과 A~D 검증등급, `model_quality_score`를 화면에 전달한다.
- GitHub 환율 출력이 일시 실패해도 GAS는 최신 가격 기반 walk-forward OOS 축소모형으로 연속 예측하며 `current=forecast`를 만들지 않는다.
- 원화강도와 V4 환율은 글로벌 FRED/Yahoo 환율 overlay가 존재할 때 동일한 최신 spot 이력을 사용한다.
- Code.gs의 `APP.VERSION`을 유일한 배포 버전 원천으로 사용하고 Index.html 브라우저 캐시 키는 서버에서 주입받는다.
- 최종 회귀검사: Python pytest 44개 통과, Python compileall 통과, GAS Code.gs 및 Index.html inline JavaScript 구문검사 통과.
