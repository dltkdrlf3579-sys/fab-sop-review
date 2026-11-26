"""FAB SOP daily review. Write completed reviews to the configured internal result table."""
import argparse
from contextlib import closing
from datetime import date
import hashlib
import json
from pathlib import Path
import sys
from uuid import uuid4

import requests

from fab_batch import input_notes, load_fab_sops, load_sql_adapter, read_config
from regulation_search import RegulationSearch
from notifications import deliver_pending
from result_store import ReviewStore

HERE = Path(__file__).resolve().parent


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def content_hash(rows):
    # A SQL join can reorder tied records. Preserve duplicates, ignore query-return order.
    fields = ("SOP제목", "주작업순번", "주작업명", "하위작업순번", "하위작업내용")
    values = sorted(tuple(row[key] for key in fields) for row in rows)
    return hashlib.sha256(encode(values).encode()).hexdigest()


def request_review(settings, prompt, payload):
    section = settings["llm"]
    url = section["base_url"].strip().rstrip("/")
    if not url or not section.get("model_name", section.get("model", "")).strip():
        raise ValueError("사내 AI base_url 및 model_name 설정 필요")
    headers = {"Content-Type": "application/json", "Prompt-Msg-Id": str(uuid4()),
               "Completion-Msg-Id": str(uuid4())}
    for key, header in (("x_dep_ticket", "x-dep-ticket"), ("send_system_name", "Send-System-Name"),
                        ("user_id", "User-Id"), ("user_type", "User-Type")):
        if section.get(key):
            headers[header] = section[key]
    headers["Authorization"] = "Bearer " + section.get("api_key", "api_key")
    if not url.endswith("/chat/completions"):
        url += "/chat/completions"
    model = section.get("model_name") or section["model"]
    response = requests.post(url, headers=headers, timeout=section.getfloat("timeout_seconds", 180),
                             allow_redirects=False, json={"model": model, "temperature": 0,
                             "messages": [{"role": "system", "content": prompt},
                                          {"role": "user", "content": payload}]})
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


def review_one(store, sop, revision, digest, rows, search, prompt, llm_config, limit):
    problems = input_notes(rows)
    if problems:
        raise ValueError("입력 확인: " + " ".join(problems))
    evidence = search.evidence(rows)
    payload = encode({"rows": [dict(row, step=i) for i, row in enumerate(rows, 1)],
                      "evidence": evidence})
    if len(prompt) + len(payload) > limit:
        raise ValueError("입력 길이 설정 초과. 원문을 생략하지 않고 보류합니다.")
    findings = validate(request_review(llm_config, prompt, payload), rows, evidence)
    model = llm_config["llm"].get("model_name") or llm_config["llm"]["model"]
    return store.save(sop, revision, digest, rows, findings, search.index_hash,
                      hashlib.sha256(prompt.encode()).hexdigest(), model)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=HERE / "config.ini")
    parser.add_argument("--as-of", type=date.fromisoformat, help="한국 기준 조회 마지막 날짜 YYYY-MM-DD")
    args = parser.parse_args()
    config = read_config(args.config)
    documents = load_fab_sops(args.config, args.as_of)
    if not documents:
        print("조회 대상 0건")
    settings = config["review"]
    base = args.config.resolve().parent
    if not config.has_section("daily_review"):
        parser.error("config.ini의 [daily_review]에 result_history_table을 지정하세요.")
    output = config["daily_review"]
    adapter = load_sql_adapter(args.config, config)
    errors = 0
    search = None
    with closing(adapter.iqadb1()) as db:
        store = ReviewStore(db, output)
        store.prepare(output.getboolean("auto_create_result_table", True))
        try:
            for key, rows in documents.items():
                try:
                    sop = str(key[0])
                    revision = "" if key[1] is None else str(key[1])
                    digest = content_hash(rows)
                    if store.is_current(sop, revision, digest):
                        print(key[0], "재사용")
                        continue
                    if search is None:
                        llm = read_config(base / settings["llm_config"])
                        prompt = (HERE / "review_prompt.txt").read_text(encoding="utf-8")
                        search = RegulationSearch(base / settings["index_path"], settings["guide_source"], settings["guide_sheet"])
                    print(key[0], review_one(store, sop, revision, digest, rows, search, prompt, llm,
                                             settings.getint("max_input_chars")))
                except (ValueError, KeyError, TypeError, IndexError, requests.RequestException) as exc:
                    errors += 1
                    print(key[0], "검토 미완료", str(exc), file=sys.stderr)
            deliver_pending(store, config)
        finally:
            if search is not None:
                search.db.close()
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
