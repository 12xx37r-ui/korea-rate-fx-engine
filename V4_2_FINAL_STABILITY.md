# V4.2 최종 안정화 수정

## 이번 실제 저장소 점검에서 확인된 원인

1. `src/collectors/global_market.py`가 FRED 11개 시계열을 각각 호출하고 각 항목을 재시도해, 장애 시 약 25초씩 연속 대기하는 구버전이었다.
2. FRED graph CSV의 현재 날짜 헤더는 `observation_date`인데 기존 파서는 `DATE`만 읽었다.
3. ECOS는 이미 확정된 통계코드/항목코드가 있어도 매 실행마다 resolver 검색을 수행하도록 되어 있었다.
4. `fx_reserves`는 732Y001 표에서 항목명이 `외환보유액`이 아니라 `합계`여서 기존 키워드 resolver가 실패했다.
5. ECOS 장기 일별 이력을 매번 전구간 재수집해 불필요한 외부 호출이 많았다.

## V4.2 수정

- FRED 11개 지표를 `fredgraph.csv` 1회 배치 요청으로 변경.
- FRED 실패 시 개별 11개 재호출 금지. 직전 정상 이력을 즉시 재사용.
- Yahoo USD/KRW는 별도 1회만 호출. GLOBAL_MARKET 외부요청 예산 최대 2회.
- FRED CSV 날짜열 `observation_date`와 과거 `DATE` 모두 지원.
- ECOS 기존 9개 지표는 검증된 stat_code/item_code를 config에 고정하여 메타데이터 탐색 제거.
- ECOS는 기존 output 이력이 있으면 최근 겹침구간만 증분 수집 후 로컬 병합.
- 외환보유액은 732Y001 전체 소표를 한 번 받아 `합계`를 로컬 필터링.
- `cache/`도 Actions 커밋 대상에 포함해 resolver 결과 재사용.
- 환율 V4 연속예측/OOS 검증 구조는 유지. 외부 보조지표 장애가 있어도 예측은 계속 산출.

## 기대 로그

GLOBAL_MARKET 정상/장애 모두 더 이상 11개 시계열이 25초씩 반복되지 않는다.

예시:
`[GLOBAL_MARKET] FRED batch: 11 series | start=...`
`[GLOBAL_MARKET] FRED batch: fresh_series=.../11 | elapsed=...`

또는 장애 시:
`[GLOBAL_MARKET] FRED batch failed once; no per-series retry | ...`

ECOS는 기존 자료가 있는 지표에서 `mode=incremental`, `fresh=...`가 표시된다.
외환보유액 정상 시 `table=732Y001 item=합계`가 표시된다.
