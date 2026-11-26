import json
import os
from uuid import uuid4
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

from run_daily import HERE, load_llm_settings, main, request_review
from notifications import deliver_pending
from fab_batch import read_config
from result_store import ReviewStore, relation


class DailyReviewTest(unittest.TestCase):
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
