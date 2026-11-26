# FAB SOP 일검토

Python 3.10 이상, pandas, requests, SQLite FTS5 및 작업중지권의 PostgreSQL 호환 사내 DB 어댑터(sql_adapter.execute_SQL 및 sql_adapter.iqadb1)를 사용합니다. SQLite FTS5는 규정 검색 중 메모리에서만 사용합니다.
최초 설치 시 코드 폴더에서 `config.example.ini`를 `config.ini`로 복사하고 아래 항목을 설정하세요. 기존 설치는 사용 중인 `config.ini`를 그대로 사용합니다. 사내에서 받은 색인 ZIP을 풀어 `knowledge_index.jsonl`을 `run_daily.bat` 옆에 둡니다.

코드를 갱신할 때는 Git으로 받은 폴더에서 `git pull --ff-only`를 실행하거나 새 코드 ZIP을 기존 폴더에 덮어씁니다. 규정 색인을 갱신할 때는 배치가 실행 중이지 않을 때 같은 경로의 JSONL을 교체합니다. 다음 검토 대상부터 새 색인을 사용하며 이미 완료된 동일 버전·본문의 검토 이력은 유지됩니다.

1. 검증된 `knowledge_index.jsonl`을 사내에 두고 `[review] index_path`로 지정합니다.
2. `[fab] query`의 예시 뷰를 실제 FAB SOP 조회문으로 바꿉니다. 각 작업행에는 해당 개정본의 최초등록시각 `CRE_DTTM`이 같은 값으로 들어가야 합니다. 한국 현지 시각을 사용하고 시·분·초와 소수초 정밀도를 유지하세요. 같은 SOP의 서로 다른 버전에 등록시각이 같은 경우에는 실제 개정번호를 SELECT에 추가하고 `revision_column`에 지정하세요.
3. `[data] adapter_dir`에 작업중지권의 sql_adapter.py 폴더를, 필요하면 `driver_dir`에 사내 DB 드라이버 폴더를 지정합니다.
4. `[daily_review] result_history_table`에 SOP 결과 전용 테이블명을 지정합니다. 예를 들어 `ehs.ai_fab_sop_review`를 쓰면 첫 실행 시 그 테이블과 `ehs.ai_fab_sop_review_latest` 뷰가 생성됩니다. 스키마는 이미 존재해야 하고 실행 계정에 생성·읽기·쓰기 권한이 필요합니다. 관리자가 두 객체를 미리 만들었으면 `auto_create_result_table = false`를 사용할 수 있습니다. 기존 SQLite 시험 데이터는 별도로 남습니다.
5. 회사 PC의 `config.ini`에서 `[review] llm_config`를 비우고, 작업중지권의 `[llm]`에서 `base_url`, `model_name`, `x_dep_ticket`, `send_system_name`, `user_id`, `user_type` 값을 같은 이름으로 복사합니다. 기존 설정 파일을 계속 공유하려면 `llm_config`에 그 파일 경로를 지정하세요. 경로가 있으면 해당 파일의 `[llm]` 전체를 사용합니다. 인증값은 회사 PC의 실제 설정에만 입력하세요.

모델 요청은 OpenAI 호환 chat/completions를 사용합니다. `[llm] api_key`는 별도 Bearer 키를 쓰는 경우에 설정합니다. `max_tokens`는 응답 생성 한도이며 비워 두면 서버의 기본값을 사용합니다. `[review] max_input_chars`는 모델 호출 전 입력 **문자 수** 제한입니다.

`python fab_batch.py --show-sop 001`로 자료를 확인하고 `run_daily.bat`로 검토합니다. 과거 날짜 재실행은 `run_daily.bat --as-of 2026-11-26`입니다. 기준일 포함 최근 3일이 기본이며 `lookback_days`로 조정합니다. 시작일 이상·다음날 미만에 최초 등록된 각 개정본의 작업행 전체를 조회합니다.

SOP 번호·개정·본문이 같으면 저장한 판정을 재사용합니다. 소스가 개정번호를 제공하지 않으면 본문 해시로 구별합니다. AI에 근거로 제공하는 자료는 FAB 가이드 시트의 구조화 행 및 단계별 BM25 상위 자료입니다. 실제 사내 규정이 바뀌어 전면 재검토할 경우 `result_history_table`에 새 테이블명을 지정해 이력을 분리할 수 있습니다. 입력 또는 API 오류가 난 문서는 완료로 저장하지 않습니다. 실행일을 넘겨 재시도할 때는 해당 날짜의 `--as-of`를 지정할 수 있습니다.

설정한 결과 테이블에는 검토 이력이 누적됩니다. `<테이블명>_latest` 뷰는 SOP 개정본별 마지막 완료 회차의 모든 작업행을 반환합니다. 작업명과 작업내용 옆에 의견 유형(`kind`), 보완의견(`message`), 근거 문서(`source`), 원문 인용(`quote`)이 붙습니다. 한 단계에 의견이 두 건이면 두 행, 의견이 없으면 의견란이 빈 한 행입니다.

원본과 연결하는 SQL 예시입니다. 결과의 `cre_dttm`은 한국 현지 TIMESTAMP, 작업순번은 NUMERIC, SOP 번호는 TEXT입니다. 별도 개정번호를 사용하면 그 키도 결과의 `revision`과 연결하세요.

```sql
SELECT s.*, r.review_id, r.kind, r.message, r.source, r.quote
FROM your_fab_sop_view s
LEFT JOIN ehs.ai_fab_sop_review_latest r
  ON r.sopr_no = CAST(s.sopr_no AS TEXT)
 AND r.cre_dttm = s.cre_dttm
 AND r.sopr_main_seq = s.sopr_main_seq
 AND r.sopr_sub_seq = s.sopr_sub_seq
```

`review_id`가 비면 연결된 완료 검토가 없고, `review_id`는 있으면서 `message`가 비면 해당 단계의 보완의견이 없습니다.

메일은 기본적으로 결과 테이블의 검토당 첫 행에 발송대기 상태(`mail_state=pending`)를 기록합니다. 담당자 공용 메일함으로 발송하려면 `[mail]`의 서버·발신자·수신자를 설정하고 `enabled = true`로 바꿉니다. 외부 주소로 자동 추정하지 않습니다. 발송 여부가 불명확한 상태(`sending`)는 메일 서버 이력 확인 후 실제 발송됐으면 `sent`, 발송되지 않았으면 `pending`으로 조정합니다. 의견 0건은 메일이 없습니다.

사내 Windows에서 일괄 실행을 등록하려면 PowerShell에서 `.\register_daily.ps1 -At "08:00"`을 실행하세요. 지정 시각은 PC 현지 시각이고 실행 계정의 DB·파일·메일 접근 권한이 적용됩니다. 기본 등록은 로그인 계정 기준이며 무인 서비스 계정이 필요한 환경에서는 사내 작업 스케줄러 설정을 적용하세요.

로컬 시험은 `python -m unittest discover -s . -p "test*.py" -v`로 실행합니다.

DB 통합시험은 psycopg2와 별도 시험 DB의 접속 문자열을 환경변수 `SOP_TEST_PG_DSN`에 설정하면 같은 명령으로 실행됩니다. 이 시험은 임시 스키마를 생성하고 정리합니다.
