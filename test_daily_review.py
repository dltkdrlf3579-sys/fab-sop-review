import json
import os
from uuid import uuid4
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
from pathlib import Path
from threading import Event, Lock
from unittest.mock import Mock, patch

from run_daily import HERE, load_llm_settings, main, request_review, run_reviews, waiting
from notifications import deliver_pending
from regulation_search import RegulationSearch
from fab_batch import read_config
from result_store import ReviewStore, relation


class DailyReviewTest(unittest.TestCase):
    def test_guide_and_rules_compete_by_step_without_unconditional_guide_rows(self):
        guide = {"domain": "safety_training", "document_type": "guide", "reference_only": False,
                 "source_status": "active", "registry_hash_status": "verified", "structured_record": True,
                 "source": "fixture", "text": "자료 시트: FAB\n"}
        def item(key, term, rule=False):
            return dict(guide, id=key, text=guide["text"] + term,
                        source="rules" if rule else "fixture", document_type="rule" if rule else "guide")
        def step(term):
            return {"SOP제목": "", "주작업명": "", "하위작업내용": term}
        records = [item("other-guide", "gamma"), item("matched-guide", "alpha"),
                   item("matched-rule", "beta", rule=True)]
        records.extend(item(str(i), "alpha", rule=True) for i in range(32))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fixture.jsonl"
            path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
            search = RegulationSearch(path, "fixture", "FAB", 2)
            self.addCleanup(search.db.close)
            self.assertEqual(len(search.evidence(step("alpha"))), 2)
            self.assertEqual(search.evidence(step("alpha"))[0]["id"], "matched-guide")
            self.assertEqual([r["id"] for r in search.evidence(step("beta"))], ["matched-rule"])
            self.assertEqual(search.evidence(step("absent")), [])
            default_search = RegulationSearch(path, "fixture", "FAB")
            self.addCleanup(default_search.db.close)
            self.assertEqual(len(default_search.evidence(step("alpha"))), 20)

    def test_waiting_reports_until_stage_ends(self):
        reported = Event()
        with patch("builtins.print", side_effect=lambda line, **kw: reported.set() if "경과" in line else None) as log:
            with waiting("시험 단계", interval=0.01):
                self.assertTrue(reported.wait(2))
            self.assertTrue(all(call.kwargs["flush"] for call in log.call_args_list))

    def test_batch_progress_counts_saved_reused_and_held(self):
        rows = [{"SOP제목": "연습", "주작업명": "준비", "주작업순번": "1",
                 "하위작업순번": "1", "하위작업내용": "준비 항목 확인"}]
        docs = {(key, "1"): rows for key in ("A", "B", "C")}
        cfg = read_config(HERE / "config.example.ini")
        cfg["llm"]["model_name"] = "fixture"
        output = StringIO()
        with (patch.object(sys, "argv", ["run_daily"]), patch("run_daily.read_config", return_value=cfg),
              patch("run_daily.load_fab_sops", return_value=docs), patch("run_daily.load_sql_adapter"),
              patch("run_daily.ReviewStore") as store, patch("run_daily.RegulationSearch") as search,
              patch("run_daily.request_review", return_value={"status": "complete", "findings": []}) as api,
              redirect_stdout(output), redirect_stderr(output)):
            store.return_value.is_current.side_effect = [False, True, False]
            store.return_value.load_steps.side_effect = lambda *args: {}
            search.return_value.index_hash = "idx"
            store.return_value.save_step.side_effect = lambda sop, key, step, findings: findings
            store.return_value.save.return_value = "검토 완료"
            search.return_value.evidence.side_effect = [[{"ref": "E1", "text": "근거"}], [{"text": "x" * 160000}]]
            self.assertEqual(main(), 1)
            api.assert_called_once()
        lines = output.getvalue()
        self.assertIn("[1/3] SOP A | AI 응답 대기", lines)
        self.assertIn("[2/3] SOP B | 재사용", lines)
        self.assertIn("[3/3] SOP C | 검토 미완료", lines)
        self.assertIn("설정 160,000자", lines)
        self.assertIn("전체 3건 | 검토 완료 1건 | 재사용 1건 | 검토 미완료 1건", lines)

    def test_parallel_pool_spans_sops_and_only_failed_target_is_retried(self):
        docs = {(sop, "1"): [
            {"SOP제목": sop, "주작업명": "work", "주작업순번": "1",
             "하위작업순번": str(i), "하위작업내용": f"{sop}-{i}"} for i in range(1, 31)
        ] for sop in ("A", "B")}
        cfg = read_config(HERE / "config.example.ini")
        cfg["llm"]["model_name"] = "fixture"
        store = Mock()
        checkpoints = {}
        completed = set()
        def save_step(sop, key, step, findings):
            checkpoints.setdefault((sop, key), {})[step] = findings
            return findings
        def save(sop, *args):
            completed.add(sop)
            return "검토 완료"
        store.save.side_effect = save
        store.is_current.side_effect = lambda sop, *args: sop in completed
        store.load_steps.side_effect = lambda sop, key: dict(checkpoints.get((sop, key), {}))
        store.save_step.side_effect = save_step
        search = Mock(index_hash="idx")
        search.evidence.side_effect = lambda row: [{"ref": "E1", "text": row["하위작업내용"],
                                                    "id": "1", "source": "fixture", "page": 1}]
        hits = []
        active = peak = 0
        lock, full = Lock(), Event()
        def respond(cfg, prompt, payload):
            nonlocal active, peak
            body = json.loads(payload)
            step, rows = body["target_step"], body["rows"]
            sop = rows[0]["SOP제목"]
            self.assertEqual(len(rows), 30)
            self.assertEqual(body["evidence"][0]["text"], rows[step-1]["하위작업내용"])
            with lock:
                active += 1
                peak = max(peak, active)
                hits.append((sop, step))
                if active == 50:
                    full.set()
            try:
                self.assertTrue(full.wait(10))
                if (sop, step) == ("B", 30) and hits.count(("B", 30)) == 1:
                    return {"status": "complete", "findings": [{"step": 1, "kind": "확인 요청",
                            "message": "확인해 주세요.", "ref": "E1", "quote": "B-30"}]}
                return {"status": "complete", "findings": []}
            finally:
                with lock:
                    active -= 1
        def run():
            counts = dict.fromkeys(("검토 완료", "검토 미완료", "재사용"), 0)
            run_reviews(docs, store, cfg, HERE, counts)
            return counts
        with (patch("run_daily.RegulationSearch", return_value=search),
              patch("run_daily.request_review", side_effect=respond),
              redirect_stdout(StringIO()), redirect_stderr(StringIO())):
            self.assertEqual(run(), {"검토 완료": 1, "검토 미완료": 1, "재사용": 0})
            self.assertEqual(completed, {"A"})
            self.assertEqual(len(hits), 60)
            self.assertEqual(peak, 50)
            self.assertEqual(run(), {"검토 완료": 1, "검토 미완료": 0, "재사용": 1})
            self.assertEqual(hits[60:], [("B", 30)])
            cfg["review"]["max_concurrent_requests"] = "51"
            with self.assertRaises(ValueError):
                run()

    def test_inline_and_external_llm_settings(self):
        cfg = read_config(HERE / "config.example.ini")
        cfg["llm"].update({"base_url": "https://inline.invalid/v1", "model_name": "local",
                           "x_dep_ticket": "fixture-ticket", "max_tokens": "1024"})
        reply = Mock(status_code=200)
        reply.json.return_value = {"choices": [{"message": {"content": "{}"}}]}
        with tempfile.TemporaryDirectory() as tmp, patch("run_daily.requests.post", return_value=reply) as post:
            folder = Path(tmp)
            request_review(load_llm_settings(cfg, folder), "prompt", "rows")
            args, options = post.call_args
            self.assertEqual(args, ("https://inline.invalid/v1/chat/completions",))
            self.assertEqual(options["headers"]["x-dep-ticket"], "fixture-ticket")
            self.assertEqual(options["json"]["max_tokens"], 1024)
            cfg["review"]["llm_config"] = "other.ini"
            (folder / "other.ini").write_text("[llm]\nbase_url=https://other.invalid/v1\nmodel_name=other\n")
            request_review(load_llm_settings(cfg, folder), "prompt", "rows")
            args, options = post.call_args
            self.assertEqual(args, ("https://other.invalid/v1/chat/completions",))
            self.assertNotIn("x-dep-ticket", options["headers"])
            self.assertNotIn("max_tokens", options["json"])
            cfg["review"]["llm_config"] = ""
            cfg["llm"]["max_tokens"] = "-1"
            with self.assertRaises(ValueError):
                request_review(load_llm_settings(cfg, folder), "prompt", "rows")
            self.assertEqual(post.call_count, 2)

    def test_result_names(self):
        self.assertEqual(relation("ehs.FAB_reviews"), '"ehs"."fab_reviews"')
        for name in ("a;b", "", "a..b", "a" * 64):
            with self.subTest(name=name), self.assertRaises(ValueError):
                relation(name)

    @unittest.skipUnless(os.getenv("SOP_TEST_PG_DSN"), "set SOP_TEST_PG_DSN to a disposable PostgreSQL DB")
    def test_api_contract_retry_then_reuse_and_changed_version(self):
        calls = []

        def respond(url, **kwargs):
            self.assertEqual(url, "https://fixture.invalid/v1/chat/completions")
            payload = json.loads(kwargs["json"]["messages"][1]["content"])
            calls.append(payload)
            evidence = payload["evidence"][0]
            findings = [{"step": 1, "kind": "보완 요청", "message": "준비 항목 확인",
                         "ref": evidence["ref"], "quote": "틀린 인용" if len(calls) == 1 else "준비 항목"}]
            if len(calls) == 3:
                findings = []
            response = Mock(status_code=200)
            response.json.return_value = {"choices": [{"finish_reason": "stop", "message": {"content":
                json.dumps({"status": "complete", "reason": "", "findings": findings})}}]}
            return response

        with tempfile.TemporaryDirectory() as tmp, patch("run_daily.requests.post", side_effect=respond):
            folder = Path(tmp)
            import psycopg2
            schema = "fab_test_" + uuid4().hex
            db = psycopg2.connect(os.environ["SOP_TEST_PG_DSN"])
            self.addCleanup(db.close)
            def sql(query, args=()):
                with db, db.cursor() as cursor:
                    cursor.execute(query, args)
                    return cursor.fetchall() if cursor.description else []
            sql(f"CREATE SCHEMA {schema}")
            self.addCleanup(sql, f"DROP SCHEMA {schema} CASCADE")
            sql(f"SET search_path TO {schema}")
            (folder / "sql_adapter.py").write_text(
                "from contextlib import closing\nimport os, psycopg2, pandas as pd\n"
                f"def iqadb1():\n    return psycopg2.connect(os.environ['SOP_TEST_PG_DSN'], "
                f"options='-c search_path={schema}')\n"
                "def execute_SQL(query):\n    with closing(iqadb1()) as c, c.cursor() as cur:\n"
                "        cur.execute(query)\n        return pd.DataFrame(cur.fetchall(), "
                "columns=[d[0] for d in cur.description])\n", encoding="utf-8")
            sql("CREATE TABLE your_fab_sop_view (sopr_no text,sopr_nm text,sopr_main_seq int,"
                "sopr_main_nm text,sopr_sub_seq int,sopr_sub_ctnt text,CRE_DTTM timestamp)")
            sql("INSERT INTO your_fab_sop_view VALUES('001','연습',1,'준비',1,'준비한다','2026-11-26')")
            cfg = (HERE/"config.example.ini").read_text(encoding="utf-8").replace("llm_config =\n", "llm_config = model.ini\n")
            cfg = cfg.replace("result_history_table = ai_fab_sop_review", f"result_history_table = {schema}.custom_review")
            (folder/"config.ini").write_text(cfg, encoding="utf-8")
            (folder/"model.ini").write_text("[llm]\nbase_url=https://fixture.invalid/v1\nmodel_name=fixture\n")
            item = {"domain":"safety_training","document_type":"guide","reference_only":False,
                    "source_status":"active","registry_hash_status":"verified","source":"통합 환경안전 SOP 가이드_2026-08-27.xlsx",
                    "id":"guide:1","structured_record":True,"text":"자료 시트: 환경안전가이드(FAB)\n준비 항목"}
            (folder/"knowledge_index.jsonl").write_text(json.dumps(item), encoding="utf-8")
            def run(code):
                with patch.object(sys, "argv", ["run_daily", "--config", str(folder/"config.ini"),
                                                "--as-of", "2026-11-26"]), redirect_stdout(StringIO()) as output, redirect_stderr(StringIO()) as err:
                    self.assertEqual(main(), code, err.getvalue())
                return output.getvalue()
            try:
                run(1)
                self.assertEqual(sql("SELECT count(*) FROM custom_review"), [(0,)])
                run(0)
                self.assertIn("재사용", run(0))
                sql("UPDATE your_fab_sop_view SET sopr_sub_ctnt='준비 항목 확인'")
                run(0)
                self.assertEqual(len(calls), 3)
                self.assertEqual(sql("SELECT kind FROM custom_review ORDER BY reviewed_at"), [("보완 요청",), (None,)])
                cfg = read_config(folder/"config.ini")
                store = ReviewStore(db, cfg["daily_review"])
                cfg["mail"].update({"enabled":"true","host":"smtp.invalid","sender":"test@example.invalid",
                                    "recipient":"review@example.invalid"})
                with patch("notifications.smtplib.SMTP") as mail:
                    deliver_pending(store, cfg)
                    deliver_pending(store, cfg)
                    mail.return_value.__enter__.return_value.send_message.assert_called_once()
                self.assertEqual(sql("SELECT mail_state FROM custom_review WHERE mail_state IS NOT NULL"), [("sent",)])
                sql("INSERT INTO your_fab_sop_view SELECT sopr_no,sopr_nm,sopr_main_seq,"
                    "sopr_main_nm,sopr_sub_seq,sopr_sub_ctnt,'2026-11-26 18:00:00' FROM your_fab_sop_view")
                run(0)
                run(0)
                self.assertEqual(len(calls), 4)
                joined = sql("SELECT s.cre_dttm::time::text, r.kind, r.review_id IS NOT NULL "
                             "FROM your_fab_sop_view s LEFT JOIN custom_review_latest r "
                             "USING (sopr_no,cre_dttm,sopr_main_seq,sopr_sub_seq) ORDER BY s.cre_dttm")
                self.assertEqual(joined, [("00:00:00", None, True), ("18:00:00", "보완 요청", True)])
                self.assertFalse(list(folder.glob("*.sqlite3")))
                # Failure on the second insert rolls back the first row and its pending notification.
                rows = calls[-1]["rows"]
                broken = [*rows, dict(rows[0], 주작업순번="bad")]
                with self.assertRaises(psycopg2.DataError):
                    store.save("bad", "1", "hash", broken, [], "idx", "prompt", "model")
                self.assertFalse(sql("SELECT 1 FROM custom_review WHERE sopr_no='bad'"))
            finally:
                sys.modules.pop("sql_adapter", None)
