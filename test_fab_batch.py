import shutil
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import pandas as pd
from datetime import datetime, timezone, timedelta

from fab_batch import FAB_COLUMNS, group_sop_rows, input_notes


class FabBatchTests(unittest.TestCase):
    def test_groups_interleaved_sops_and_revisions_without_losing_steps(self):
        frame = pd.DataFrame(
            [("A", 1, 2), ("B", 1, 1), ("A", 2, 1), ("A", 1, 10)],
            columns=["번호", "개정", "순서"],
        )
        docs = group_sop_rows(frame, "번호", "개정")
        self.assertEqual(len(docs), 3)
        self.assertEqual([row["순서"] for row in docs["A", 1]], [2, 10])
        latest = frame[frame["개정"] == 1]
        self.assertEqual(len(group_sop_rows(latest, "번호", "")["A", None]), 2)

    def test_empty_result_is_distinct_from_broken_result(self):
        self.assertEqual(group_sop_rows(pd.DataFrame(columns=["id"]), "id", ""), {})
        with self.assertRaises(ValueError):
            group_sop_rows(pd.DataFrame(), "id", "")
        for missing in [" ", None, pd.NA]:
            with self.subTest(missing=missing), self.assertRaises(ValueError):
                group_sop_rows(pd.DataFrame({"id": ["A"], "rev": [missing]}), "id", "rev")

    def test_cli_executes_multiline_config_query(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            with closing(sqlite3.connect(folder / "sample.db")) as conn, conn:
                conn.execute("CREATE TABLE steps (id TEXT, rev INTEGER, step INTEGER)")
                conn.executemany("INSERT INTO steps VALUES (?, ?, ?)", [
                    ("A1", 1, 2), ("A1", 1, 1), ("A1", 2, 1), ("B", 1, 1),
                ])
            shutil.copy(Path(__file__).with_name("fab_batch.py"), folder)
            (folder / "sql_adapter.py").write_text(
                "from pathlib import Path\nfrom contextlib import closing\nimport sqlite3\nimport pandas as pd\n"
                "def execute_SQL(query):\n"
                "    with closing(sqlite3.connect(Path(__file__).with_name('sample.db'))) as conn:\n"
                "        return pd.read_sql_query(query, conn)\n", encoding="utf-8",
            )
            (folder / "config.ini").write_text(
                "[fab]\nquery =\n    SELECT * FROM steps WHERE id LIKE 'A%'\n"
                "    ORDER BY step\nsop_id_column = id\nrevision_column = rev\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                [sys.executable, "-X", "utf8", str(folder / "fab_batch.py")],
                capture_output=True, encoding="utf-8", check=True,
            )
            self.assertIn("SOP 1건 | 검토 단위 2건 | 원본 행 3개", result.stdout)

            with closing(sqlite3.connect(folder / "sample.db")) as conn, conn:
                conn.execute("CREATE TABLE your_fab_sop_view (" + ", ".join(
                    f"{column.upper()} TEXT" for column in FAB_COLUMNS
                ) + ", CRE_DTTM TEXT)")
                now = datetime.now(timezone(timedelta(hours=9))).date()
                start = (now - timedelta(days=2)).isoformat()
                end = (now + timedelta(days=1)).isoformat()
                conn.executemany("INSERT INTO your_fab_sop_view VALUES (?, ?, ?, ?, ?, ?, ?)", [
                    ("001", "밸브 교체", "10", "정리", "1", "종료 기록", start),
                    ("001", "밸브 교체", "2", "점검", "10", "후속 기록", start),
                    ("001", "밸브 교체", "2", "점검", "10", "구버전 기록", "2000-01-01"),
                    ("001", "밸브 교체", "2", "점검", "2", "점검 A", start),
                    ("002", "다른 작업", "1", "준비", "1", "별도 SOP", now.isoformat()),
                    ("001", "밸브 교체", "2", "점검", "2", "점검 B", start),
                    ("001", "밸브 교체", "2", "점검", None, None, start),
                    ("003", "오래된 문서", "1", "준비", "1", "미대상", "2000-01-01"),
                    ("004", "미래 문서", "1", "준비", "1", "미대상", end),
                ])
            shutil.copy(Path(__file__).with_name("config.example.ini"), folder / "config.ini")
            result = subprocess.run(
                [sys.executable, "-X", "utf8", str(folder / "fab_batch.py"), "--show-sop", "001"],
                capture_output=True, encoding="utf-8", check=True,
            )
            summary, payload = result.stdout.split("\n", 1)
            self.assertIn("SOP 2건 | 검토 단위 2건 | 원본 행 6개", summary)
            doc = json.loads(payload)
            rows = doc["작업행"]
            self.assertEqual([row["SOP번호"] for row in rows], ["001"] * 5)
            self.assertEqual([row["하위작업순번"] for row in rows], ["2", "2", "10", "", "1"])
            self.assertEqual({row["하위작업내용"] for row in rows[:2]}, {"점검 A", "점검 B"})
            self.assertEqual(rows[-1]["주작업순번"], "10")
            self.assertTrue(any("2-2" in note for note in doc["입력자료 확인사항"]))
            self.assertTrue(any("빈 행" in note for note in doc["입력자료 확인사항"]))
            self.assertEqual(input_notes([rows[0], rows[0]]), [])


if __name__ == "__main__":
    unittest.main()
