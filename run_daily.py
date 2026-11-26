"""FAB SOP daily review. Write completed reviews to the configured internal result table."""
import argparse
from contextlib import closing, contextmanager
from datetime import date
import hashlib
import json
from pathlib import Path
import sys
from threading import Event, Thread
from time import monotonic
from uuid import uuid4

import requests

from fab_batch import input_notes, load_fab_sops, load_sql_adapter, read_config
from regulation_search import RegulationSearch
from notifications import deliver_pending
from result_store import ReviewStore

HERE = Path(__file__).resolve().parent


@contextmanager
def waiting(label, interval=15):
    print(label, flush=True)
    started = monotonic()
    finished = Event()

    def report_wait():
        while not finished.wait(interval):
            print(f"{label} | 경과 {int(monotonic() - started)}초", flush=True)

    reporter = Thread(target=report_wait, daemon=True)
    reporter.start()
    try:
        yield
    finally:
        finished.set()
        reporter.join()


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def content_hash(rows):
    # A SQL join can reorder tied records. Preserve duplicates, ignore query-return order.
    fields = ("SOP제목", "주작업순번", "주작업명", "하위작업순번", "하위작업내용")
    values = sorted(tuple(row[key] for key in fields) for row in rows)
    return hashlib.sha256(encode(values).encode()).hexdigest()


def load_llm_settings(config, base):
    path = config["review"].get("llm_config", "").strip()
    settings = read_config(base / path) if path else config
    if not settings.has_section("llm"):
        raise ValueError("모델 설정 파일에 [llm] 섹션이 필요합니다.")
    return settings


def request_review(settings, prompt, payload):
    section = settings["llm"]
    url = section.get("base_url", "").strip().rstrip("/")
    model = section.get("model_name") or section.get("model")
    if not url or not model:
        raise ValueError("사내 AI base_url 및 model_name 설정 필요")
    headers = {"Content-Type": "application/json", "Prompt-Msg-Id": str(uuid4()),
               "Completion-Msg-Id": str(uuid4())}
    for key, header in (("x_dep_ticket", "x-dep-ticket"), ("send_system_name", "Send-System-Name"),
                        ("user_id", "User-Id"), ("user_type", "User-Type")):
        if section.get(key):
            headers[header] = section[key]
    headers["Authorization"] = "Bearer " + (section.get("api_key") or "api_key")
    if not url.endswith("/chat/completions"):
        url += "/chat/completions"
    body = {"model": model, "temperature": 0,
            "messages": [{"role": "system", "content": prompt}, {"role": "user", "content": payload}]}
    if section.get("max_tokens", "").strip():
        max_tokens = section.getint("max_tokens")
        if max_tokens < 1:
            raise ValueError("[llm] max_tokens는 양의 정수여야 합니다.")
        body["max_tokens"] = max_tokens
    response = requests.post(url, headers=headers, timeout=section.getfloat("timeout_seconds", 180),
                             allow_redirects=False, json=body)
    if response.status_code != 200:
        raise ValueError(f"사내 AI 응답 HTTP {response.status_code}")
    choice = response.json()["choices"][0]
    if choice.get("finish_reason") not in ("stop", None):
        raise ValueError("사내 AI 응답 미완료")
    return json.loads(choice["message"]["content"])


def validate(result, rows, evidence):
    if not isinstance(result, dict) or result.get("status") not in ("complete", "held"):
        raise ValueError("검토 상태 누락")
    findings = result.get("findings")
    if not isinstance(findings, list):
        raise ValueError("검토 배열 누락")
    if result["status"] == "held":
        raise ValueError("AI 판단 보류: " + str(result.get("reason", ""))[:200])
    sources = {record["ref"]: record for record in evidence}
    checked = []
    for item in findings:
        step = item["step"]
        if type(step) is not int or not 1 <= step <= len(rows):
            raise ValueError("존재하지 않는 작업행")
        if item["kind"] not in ("보완 요청", "확인 요청") or not isinstance(item["message"], str) or not item["message"].strip():
            raise ValueError("지적 형식 오류")
        ref = sources.get(item["ref"])
        quote = item["quote"].strip()
        if not ref or not quote or quote not in ref["text"]:
            raise ValueError("규정 인용 불일치")
        checked.append((step, item["kind"], item["message"], ref["id"], ref["source"], ref["page"], quote))
    return checked


def review_one(store, sop, revision, digest, rows, search, prompt, llm_config, limit, label):
    problems = input_notes(rows)
    if problems:
        raise ValueError("입력 확인: " + " ".join(problems))
    with waiting(f"{label} | 규정 검색 중 (작업 {len(rows)}행)"):
        evidence = search.evidence(rows)
    payload = encode({"rows": [dict(row, step=i) for i, row in enumerate(rows, 1)],
                      "evidence": evidence})
    size = len(prompt) + len(payload)
    if size > limit:
        raise ValueError(f"입력 길이 설정 초과 (입력 {size:,}자 / 설정 {limit:,}자). 원문을 생략하지 않고 보류합니다.")
    with waiting(f"{label} | AI 응답 대기 (입력 {size:,}자)"):
        response = request_review(llm_config, prompt, payload)
    findings = validate(response, rows, evidence)
    model = llm_config["llm"].get("model_name") or llm_config["llm"]["model"]
    with waiting(f"{label} | DB 저장 중"):
        return store.save(sop, revision, digest, rows, findings, search.index_hash,
                          hashlib.sha256(prompt.encode()).hexdigest(), model)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=HERE / "config.ini")
    parser.add_argument("--as-of", type=date.fromisoformat, help="한국 기준 조회 마지막 날짜 YYYY-MM-DD")
    args = parser.parse_args()
    config = read_config(args.config)
    with waiting("FAB SOP 조회 중"):
        documents = load_fab_sops(args.config, args.as_of)
    total = len(documents)
    print(f"조회 완료 | 검토 대상 {total}건", flush=True)
    settings = config["review"]
    base = args.config.resolve().parent
    if not config.has_section("daily_review"):
        parser.error("config.ini의 [daily_review]에 result_history_table을 지정하세요.")
    output = config["daily_review"]
    adapter = load_sql_adapter(args.config, config)
    counts = {"검토 완료": 0, "재사용": 0, "검토 미완료": 0}
    search = None
    with waiting("결과 DB 연결 중"):
        connection = adapter.iqadb1()
    with closing(connection) as db:
        store = ReviewStore(db, output)
        with waiting("결과 테이블 확인 중"):
            store.prepare(output.getboolean("auto_create_result_table", True))
        try:
            for number, (key, rows) in enumerate(documents.items(), 1):
                label = f"[{number}/{total}] SOP {key[0]}"
                detail = ""
                try:
                    sop = str(key[0])
                    revision = "" if key[1] is None else str(key[1])
                    digest = content_hash(rows)
                    with waiting(f"{label} | 기존 검토 조회 중"):
                        current = store.is_current(sop, revision, digest)
                    if current:
                        status = "재사용"
                    else:
                        if search is None:
                            llm = load_llm_settings(config, base)
                            prompt = (HERE / "review_prompt.txt").read_text(encoding="utf-8")
                            with waiting(f"{label} | 규정 색인 로딩 중"):
                                search = RegulationSearch(base / settings["index_path"], settings["guide_source"], settings["guide_sheet"])
                        status = review_one(store, sop, revision, digest, rows, search, prompt, llm,
                                            settings.getint("max_input_chars"), label)
                except (ValueError, KeyError, TypeError, IndexError, requests.RequestException) as exc:
                    status = "검토 미완료"
                    detail = f" | {exc}"
                counts[status] += 1
                print(f"{label} | {status}{detail} | 처리율 {number/total:.1%}",
                      file=sys.stderr if detail else sys.stdout, flush=True)
            if config.getboolean("mail", "enabled", fallback=False):
                with waiting("대기 메일 처리 중"):
                    deliver_pending(store, config)
        finally:
            if search is not None:
                search.db.close()
    summary = " | ".join(f"{name} {count}건" for name, count in counts.items())
    print(f"배치 종료 | 전체 {total}건 | {summary}", flush=True)
    return 1 if counts["검토 미완료"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
