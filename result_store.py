"""FAB review history in the same PostgreSQL-compatible DB as the source adapter."""
from collections import defaultdict
from contextlib import contextmanager
import json
import re
from uuid import uuid4


def relation(name):
    parts = name.strip().lower().split(".")
    if len(parts) > 2 or not all(re.fullmatch("[a-z_][a-z0-9_]{0,62}", p) for p in parts):
        raise ValueError("결과 테이블명과 뷰명은 테이블 또는 스키마.테이블 형식이어야 합니다.")
    return ".".join(f'"{p}"' for p in parts)


class ReviewStore:
    def __init__(self, db, settings):
        self.db = db
        self.db.autocommit = False
        name = settings["result_history_table"].strip()
        self.table = relation(name)
        self.view = relation(settings.get("latest_view", "").strip() or name + "_latest")
        self.work = relation(settings.get("step_work_table", "").strip() or name + "_steps")
        if len({self.work, self.table, self.view}) != 3:
            raise ValueError("결과 테이블과 최신 뷰는 이름이 달라야 합니다.")

    @contextmanager
    def transaction(self):
        try:
            with self.db.cursor() as cur:
                yield cur
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def prepare(self, create):
        with self.transaction() as cur:
            if create:
                cur.execute("SELECT version()")
                distribution = " DISTRIBUTED BY (sopr_no)" if "greenplum" in cur.fetchone()[0].lower() else ""
                cur.execute(f"""CREATE TABLE IF NOT EXISTS {self.table} (
                    sopr_no TEXT NOT NULL, revision TEXT NOT NULL, cre_dttm TIMESTAMP,
                    sopr_nm TEXT NOT NULL, sopr_main_seq NUMERIC NOT NULL, sopr_main_nm TEXT NOT NULL,
                    sopr_sub_seq NUMERIC NOT NULL, sopr_sub_ctnt TEXT NOT NULL,
                    review_id TEXT NOT NULL, reviewed_at TIMESTAMPTZ NOT NULL,
                    content_hash TEXT NOT NULL, index_hash TEXT NOT NULL, prompt_hash TEXT NOT NULL,
                    model TEXT NOT NULL, step INTEGER NOT NULL, finding_no INTEGER NOT NULL,
                    kind TEXT, message TEXT, evidence_id TEXT, source TEXT, page TEXT, quote TEXT,
                    mail_state TEXT, mail_subject TEXT, mail_body TEXT,
                    search_candidates TEXT,
                    PRIMARY KEY(sopr_no, review_id, step, finding_no)){distribution}""")
                has_column = """SELECT 1 FROM pg_attribute WHERE attrelid=to_regclass(%s)
                                AND attname='search_candidates' AND NOT attisdropped"""
                cur.execute(has_column, (self.table,))
                if cur.fetchone() is None:
                    cur.execute(f"LOCK TABLE {self.table} IN ACCESS EXCLUSIVE MODE")
                    cur.execute(has_column, (self.table,))
                    if cur.fetchone() is None:
                        cur.execute(f"ALTER TABLE {self.table} ADD COLUMN search_candidates TEXT")
                cur.execute(f"""CREATE TABLE IF NOT EXISTS {self.work} (
                    sopr_no TEXT NOT NULL, work_key TEXT NOT NULL, step INTEGER NOT NULL,
                    findings TEXT NOT NULL, PRIMARY KEY(sopr_no, work_key, step)){distribution}""")
                cur.execute("SELECT to_regclass(%s)", (self.view,))
                if cur.fetchone()[0] is None:
                    cur.execute(f"""CREATE VIEW {self.view} AS
                        SELECT h.* FROM {self.table} h JOIN (
                            SELECT sopr_no, revision, review_id FROM (
                                SELECT sopr_no, revision, review_id,
                                    ROW_NUMBER() OVER (PARTITION BY sopr_no, revision
                                        ORDER BY reviewed_at DESC, review_id DESC) AS position
                                FROM {self.table} WHERE step=1 AND finding_no=1
                            ) heads WHERE position=1
                        ) latest USING (sopr_no, revision, review_id)""")
            for name in (self.table, self.view):
                cur.execute(f"SELECT review_id, content_hash, mail_state FROM {name} LIMIT 0")
            cur.execute(f"SELECT search_candidates FROM {self.table} LIMIT 0")
            cur.execute(f"SELECT sopr_no, work_key, step, findings FROM {self.work} LIMIT 0")

    def load_steps(self, sop, key):
        with self.transaction() as cur:
            cur.execute(f"SELECT step, findings FROM {self.work} WHERE sopr_no=%s AND work_key=%s",
                        (sop, key))
            return {step: json.loads(data) for step, data in cur.fetchall()}

    def save_step(self, sop, key, step, findings):
        with self.transaction() as cur:
            cur.execute(f"LOCK TABLE {self.work} IN SHARE ROW EXCLUSIVE MODE")
            cur.execute(f"SELECT findings FROM {self.work} WHERE sopr_no=%s AND work_key=%s AND step=%s",
                        (sop, key, step))
            existing = cur.fetchone()
            if existing:
                return json.loads(existing[0])
            cur.execute(f"INSERT INTO {self.work} (sopr_no, work_key, step, findings) VALUES (%s,%s,%s,%s)",
                        (sop, key, step, json.dumps(findings, ensure_ascii=False)))
        return findings

    def _is_current(self, cur, sop, revision, digest):
        cur.execute(f"SELECT content_hash FROM {self.table} WHERE sopr_no=%s AND revision=%s "
                    "ORDER BY reviewed_at DESC, review_id DESC LIMIT 1", (sop, revision))
        row = cur.fetchone()
        return row is not None and row[0] == digest

    def is_current(self, sop, revision, digest):
        with self.transaction() as cur:
            return self._is_current(cur, sop, revision, digest)

    def save(self, sop, revision, digest, rows, findings, index_hash, prompt_hash, model, work_key=None,
             candidates_by_step=None):
        by_step = defaultdict(list)
        lines = []
        for step, kind, message, evidence_id, source, page, quote in findings:
            by_step[step].append((kind, message, evidence_id, source, page, quote))
            r = rows[step - 1]
            lines.append(f"{r['주작업순번']}-{r['하위작업순번']} [{kind}] {message}\n{source} {page or ''} · {quote}")
        mail = ("pending", f"[SOP 검토] {sop} {rows[0]['SOP제목']}", "\n\n".join(lines)) if lines else (None,) * 3
        rid = str(uuid4())
        records = []
        with self.transaction() as cur:
            # Serialize only the short write. Another worker may have finished during the model call.
            cur.execute(f"LOCK TABLE {self.table} IN SHARE ROW EXCLUSIVE MODE")
            if work_key is not None:
                cur.execute(f"DELETE FROM {self.work} WHERE sopr_no=%s AND work_key=%s", (sop, work_key))
            if self._is_current(cur, sop, revision, digest):
                return "재사용"
            cur.execute("SELECT clock_timestamp()")
            reviewed_at = cur.fetchone()[0]
            for step, r in enumerate(rows, 1):
                fields = (sop, revision, r.get("등록일") or None, r["SOP제목"], r["주작업순번"],
                          r["주작업명"], r["하위작업순번"], r["하위작업내용"], rid, reviewed_at,
                          digest, index_hash, prompt_hash, model, step)
                trace = None
                if candidates_by_step is not None:
                    cited_ids = {item[2] for item in by_step[step]}
                    trace = json.dumps([dict(c, cited=c["id"] in cited_ids) for c in candidates_by_step[step]],
                                       ensure_ascii=False, separators=(",", ":"))
                for number, finding in enumerate(by_step[step] or [(None,) * 6], 1):
                    records.append((*fields, number, *finding, *mail, trace if number == 1 else None))
                    mail = (None,) * 3
            cur.executemany(f"""INSERT INTO {self.table}
                (sopr_no,revision,cre_dttm,sopr_nm,sopr_main_seq,sopr_main_nm,sopr_sub_seq,sopr_sub_ctnt,
                 review_id,reviewed_at,content_hash,index_hash,prompt_hash,model,step,finding_no,
                 kind,message,evidence_id,source,page,quote,mail_state,mail_subject,mail_body,search_candidates)
                VALUES ({", ".join(["%s"] * 26)})""", records)
        return "검토 완료"

    def pending_mail(self):
        with self.transaction() as cur:
            cur.execute(f"SELECT review_id,mail_subject,mail_body FROM {self.table} "
                        "WHERE mail_state='pending' ORDER BY reviewed_at,review_id")
            return cur.fetchall()

    def change_mail_state(self, rid, old, new):
        with self.transaction() as cur:
            cur.execute(f"UPDATE {self.table} SET mail_state=%s WHERE review_id=%s AND mail_state=%s",
                        (new, rid, old))
            return cur.rowcount == 1
