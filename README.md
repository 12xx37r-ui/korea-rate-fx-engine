# Korea Rate & FX Engine

한국 정책금리 방향, 원/달러 환율 경로, 원화 강도 및 향후 원화 방향을 산출하기 위한 GitHub 기반 엔진의 1차 골격입니다.

## 1차 목표

- API 키 및 데이터원 연결 검증
- 미국 정책금리 GitHub JSON 연동
- ECOS, KOSIS, KRX, R-ONE 연결
- 원자료 스냅샷 저장
- 데이터 최신성·결측·형식 오류 검사
- `output/api_health.json` 생성
- `output/us_input.json` 생성

현재 버전은 통계코드와 KRX API ID를 임의로 넣지 않습니다. 실제 승인 화면과 공식 통계코드 검색에서 확인한 값을 `config/*.json`에 입력해야 합니다.

## GitHub Secrets

- `ECOS_API_KEY`
- `KOSIS_API_KEY`
- `KRX_API_KEY`
- `DATA_GO_KR_API_KEY`
- `REB_API_KEY`
- `US_REPO_READ_TOKEN` (미국 저장소가 비공개인 경우)

## GitHub Variables

- `US_POLICY_JSON_URL`
- `MODEL_TIMEZONE=Asia/Seoul`

## 실행

```bash
python -m pip install -r requirements.txt
python -m src.main
```

## 첫 실행 전

- `config/krx_apis.json`: 승인된 실제 KRX endpoint와 API ID 입력
- `config/ecos_series.json`: 실제 ECOS 통계표·항목 코드 입력
- `config/kosis_series.json`: 실제 KOSIS 통계표·분류 코드 입력
- `config/reb_series.json`: 실제 R-ONE 통계코드 입력

## API 키 만료·갱신 알림

워크플로 실행 때 ECOS/KOSIS 인증 오류를 감지하면 다음 파일에 명확히 표시됩니다.

- `output/api_health.json` → `api_credentials.status: "action_required"`
- `output/api_key_status.json` → `status: "API_KEY_RENEWAL_REQUIRED"`
- 해당 source → `status: "credential_error"`
- `metadata.secret_name`과 `metadata.action`에 교체할 GitHub Secret 이름과 조치 방법 표시

API 키를 갱신한 뒤 GitHub의 `Settings → Secrets and variables → Actions`에서 기존 Secret 값만 새 키로 교체하고 workflow를 다시 실행하면 됩니다. 소스코드나 통계코드를 다시 수정할 필요는 없습니다.

## V1.4 최종 검증 강화

- KOSIS 근원 CPI를 공식 전년동월비 우선으로 고정했습니다.
- 공식 전년동월비가 없으면 지수 수준의 12개월 변화율, 그마저 없으면 전월비 12개월 복리 누적으로 안전하게 복구합니다.
- KOSIS 선택 캐시 버전을 3으로 올려 과거 잘못 선택된 CPI 항목을 자동 폐기합니다.
- 한국 기준금리 확률은 과거 월말 기준으로 향후 최대 4개월 내 첫 금리 변경을 평가합니다.
- 다중분류 Brier Score, 역사적 빈도 벤치마크 Brier, Brier Skill Score, 정확도, 로그손실을 출력합니다.
- 백테스트 성능이 약하거나 표본이 부족하면 현재 전망 확률을 역사적 빈도 쪽으로 자동 축소하여 과신을 방지합니다.
