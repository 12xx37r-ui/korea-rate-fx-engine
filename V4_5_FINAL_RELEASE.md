# Korea Engine V4.5 Final Resilient Release

## 목표
- 환율·원화강도·원화유동성·한국 정책금리 모두 연속 예측
- 외부자료 장애가 예측 중단이나 현재값 복사형 중립으로 이어지지 않게 함
- 기능 정확성을 유지하면서 외부 호출을 최소화하고 캐시·증분수집·로컬 계산을 우선함

## GLOBAL_MARKET
- FRED 13개를 3개 소형 그룹(currency / rates-risk / commodities)으로 나눠 병렬 배치 수집
- 개별 series 재시도 금지, 그룹당 1회만 요청
- 최초 최근 900일 bootstrap, 이후 120일 겹침 증분수집
- FRED 실패 시 기존 last-good 병합
- KRW NEER/REER가 비면 공식 BIS EER bulk를 조건부 1회 fallback
- Yahoo USD/KRW는 1회만 사용
- 정상 최대 4회, BIS fallback 포함 최악 최대 5회 외부요청

## FX V4.5
- 기술 6모형 + macro_public_factors를 동일 walk-forward OOS 경쟁에 투입
- 글로벌 요인이 끊겨도 ECOS 한국 2년물-기준금리 gap, 경상수지 percentile, 외환보유액 추세로 macro 후보 유지
- 독립 거시축 2개 이상이면 후보를 만들고 OOS 성적이 나쁘면 자동 저가중
- 랜덤워크는 평가 benchmark일 뿐 실전 중심값 복사에 사용하지 않음
- 약한 검증은 예측 중단이 아니라 shrinkage와 낮은 등급으로 반영

## KRW Strength
- USD/KRW 수준·모멘텀, NEER/REER, 글로벌통화, 금리차, 대외건전성 그룹
- US 2Y가 없으면 미국 유효정책금리-한국은행 기준금리 gap을 저가중 proxy로 사용
- 단순 그룹 개수뿐 아니라 weighted_group_coverage 출력
- 별도 원화강도 OOS 적중률로 과장하지 않고 FX OOS + 요인 커버리지로 품질 정의

## KRW Liquidity
- 미래 M2 YoY를 3/6/12개월 고정규칙 expanding-origin OOS로 별도 검증
- persistence benchmark, RMSE, skill, direction accuracy, samples 출력
- forecast_quality_score와 input_data_quality_score 완전 분리
- 약한 검증은 예측 중단이 아니라 shrinkage로 반영

## Korea Policy Rate
- 후보등급과 엄격검증 통과 여부를 분리
- forecast_quality_score는 확률이 아니라 walk-forward 품질지표
- probability-weighted expected_rate_pct와 가장 가능성 높은 25bp modal_rate_pct를 동시에 출력
- 화면 방향표시는 modal 경로, 확률가중 평균은 불확실성 요약값으로 사용

## 검증
- pytest 61개 통과
- Python source/tests compile 통과
- GitHub Actions 중복 실행 자동취소(concurrency)
- workflow 최대 실행시간 12분
