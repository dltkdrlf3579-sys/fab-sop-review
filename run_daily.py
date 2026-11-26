"""FAB SOP daily review. Write completed reviews to the configured internal result table."""
import argparse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
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
from regulation_search import RANKING_VERSION, RegulationSearch
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
    if not isinstance(choice, dict):
        raise ValueError("사내 AI 응답 형식 오류")
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
        if not isinstance(item["quote"], str):
            raise ValueError("근거 원문 인용 누락")
        quote = item["quote"].strip()
        if not ref or not quote or quote not in ref["text"]:
            raise ValueError("규정 인용 불일치")
        checked.append((step, item["kind"], item["message"], ref["id"], ref["source"], ref["page"], quote))
    return checked


@dataclass
class DocumentReview:
    sop: str
    revision: str
    digest: str
    rows: list
    label: str
    key: str
    results: dict
    errors: dict = field(default_factory=dict)


ROW_ERRORS = (ValueError, KeyError, TypeError, IndexError, requests.RequestException)


def review_step(config, prompt, payload, rows, step, evidence):
    result = request_review(config, prompt, payload)
    findings = validate(result, rows, evidence)
    if any(finding[0] != step for finding in findings):
        raise ValueError("AI가 대상과 다른 작업행을 지적했습니다.")
    return findings


def run_reviews(documents, store, config, base, counts):
    settings = config["review"]
    declared_sheets = settings.get("other_area_sheets")
    if declared_sheets is None:
        defaults = read_config(HERE / "config.example.ini")["review"]
        same_guide = all(settings[k] == defaults[k] for k in ("guide_source", "guide_sheet"))
        declared_sheets = defaults.get("other_area_sheets", "") if same_guide else ""
    other_area_sheets = sorted({name.strip() for name in declared_sheets.split(",") if name.strip()})
    workers = settings.getint("max_concurrent_requests", fallback=50)
    if not 1 <= workers <= 50:
        raise ValueError("max_concurrent_requests는 1~50 사이여야 합니다.")
    evidence_limit = min(20, settings.getint("max_evidence_items", fallback=20))
    char_limit = settings.getint("max_input_chars")
    total = len(documents)
    search = None
    pending = {}
    print(f"행별 검토 | 배치 동시 호출 최대 {workers}개 | 행당 근거 최대 {evidence_limit}개", flush=True)

    def report(label, status, detail=""):
        counts[status] += 1
        print(f"{label} | {status}{detail} | 처리율 {sum(counts.values())/total:.1%}",
              file=sys.stderr if detail else sys.stdout, flush=True)

    def check_progress(doc):
        done = len(doc.results) + len(doc.errors)
        print(f"{doc.label} | 작업행 처리 {done}/{len(doc.rows)} | 보류 {len(doc.errors)}행", flush=True)
        if done != len(doc.rows):
            return
        if doc.errors:
            report(doc.label, "검토 미완료", f" | 보류 {len(doc.errors)}행")
            return
        findings = [item for n in sorted(doc.results) for item in doc.results[n]["findings"]]
        candidates = {n: data["candidates"] for n, data in doc.results.items()}
        with waiting(f"{doc.label} | DB 저장 중"):
            status = store.save(doc.sop, doc.revision, doc.digest, doc.rows, findings,
                                search.index_hash, prompt_hash, model, doc.key, candidates)
        report(doc.label, status)

    def hold_row(doc, step, error):
        doc.errors[step] = str(error)
        print(f"{doc.label} | 작업행 {step} 보류 | {error}", file=sys.stderr, flush=True)
        check_progress(doc)

    def collect():
        while True:
            finished, _ = wait(pending, timeout=15, return_when=FIRST_COMPLETED)
            if finished:
                break
            elapsed = int(monotonic() - min(start for _, _, start, _ in pending.values()))
            print(f"AI 응답 대기 | 진행 중 {len(pending)}행 | 가장 오래된 요청 경과 {elapsed}초", flush=True)
        for future in finished:
            doc, step, _, candidates = pending.pop(future)
            try:
                findings = future.result()
            except ROW_ERRORS as exc:
                hold_row(doc, step, exc)
            else:
                result = {"findings": findings, "candidates": candidates}
                doc.results[step] = store.save_step(doc.sop, doc.key, step, result)
                check_progress(doc)

    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for number, ((sop_id, rev), rows) in enumerate(documents.items(), 1):
                sop = str(sop_id)
                revision = "" if rev is None else str(rev)
                label = f"[{number}/{total}] SOP {sop}"
                digest = content_hash(rows)
                if store.is_current(sop, revision, digest):
                    report(label, "재사용")
                    continue
                problems = input_notes(rows)
                if problems:
                    report(label, "검토 미완료", " | 입력 확인: " + " ".join(problems))
                    continue
                if search is None:
                    llm = load_llm_settings(config, base)
                    prompt = (HERE / "review_prompt.txt").read_text(encoding="utf-8")
                    prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()
                    model = llm["llm"].get("model_name") or llm["llm"].get("model", "")
                    with waiting("규정 색인 로딩 중"):
                        search = RegulationSearch(base / settings["index_path"], settings["guide_source"],
                                                  settings["guide_sheet"], evidence_limit, other_area_sheets)
                numbered = [dict(row, step=i) for i, row in enumerate(rows, 1)]
                key_data = [sop, revision, numbered, search.index_hash, RANKING_VERSION, prompt_hash, model,
                            settings["guide_source"], settings["guide_sheet"], other_area_sheets,
                            llm["llm"].get("base_url"), llm["llm"].get("max_tokens"), evidence_limit]
                key = hashlib.sha256(encode(key_data).encode()).hexdigest()
                resumed = store.load_steps(sop, key)
                doc = DocumentReview(sop, revision, digest, rows, label, key, resumed)
                print(f"{label} | 작업 {len(rows)}행 | 이전 성공 {len(resumed)}행", flush=True)
                if len(resumed) == len(rows):
                    check_progress(doc)
                    continue
                for step, row in enumerate(rows, 1):
                    if step in resumed:
                        continue
                    if len(pending) >= workers:
                        collect()
                    evidence = search.evidence(row)
                    if not evidence:
                        hold_row(doc, step, "작업 내용과 일치하는 규정 근거를 찾지 못했습니다.")
                        continue
                    payload = encode({"rows": numbered, "target_step": step, "evidence": evidence})
                    size = len(prompt) + len(payload)
                    if size > char_limit:
                        hold_row(doc, step, f"입력 길이 설정 초과 (입력 {size:,}자 / 설정 {char_limit:,}자)")
                        continue
                    future = pool.submit(review_step, llm, prompt, payload, rows, step, evidence)
                    candidates = [{"rank": rank, "id": entry["id"], "source": entry["source"],
                                   "page": entry.get("page")} for rank, entry in enumerate(evidence, 1)]
                    pending[future] = (doc, step, monotonic(), candidates)
                    print(f"{label} | AI 응답 대기 | 작업행 {step}/{len(rows)} "
                          f"(근거 {len(evidence)}개, 입력 {size:,}자)", flush=True)
            while pending:
                collect()
    finally:
        if search is not None:
            search.db.close()


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
    with waiting("결과 DB 연결 중"):
        connection = adapter.iqadb1()
    with closing(connection) as db:
        store = ReviewStore(db, output)
        with waiting("결과 테이블 확인 중"):
            store.prepare(output.getboolean("auto_create_result_table", True))
        run_reviews(documents, store, config, base, counts)
        if config.getboolean("mail", "enabled", fallback=False):
            with waiting("대기 메일 처리 중"):
                deliver_pending(store, config)
    summary = " | ".join(f"{name} {count}건" for name, count in counts.items())
    print(f"배치 종료 | 전체 {total}건 | {summary}", flush=True)
    return 1 if counts["검토 미완료"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
