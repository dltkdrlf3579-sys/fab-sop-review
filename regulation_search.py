"""Local BM25 search over reviewed text from the portable JSONL index."""
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import sqlite3


def terms(text):
    for word in re.findall(r"[a-z0-9]+|[가-힣]+", text.lower()):
        yield word
        if len(word) > 2 and "가" <= word[0] <= "힣":
            yield from (word[i:i+2] for i in range(len(word)-1))


class RegulationSearch:
    def __init__(self, path: Path, guide_source: str, guide_sheet: str):
        self.db = sqlite3.connect(":memory:")
        self.db.execute("CREATE VIRTUAL TABLE search USING fts5(tokens)")
        self.records = {}
        self.guide = []
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for line in stream:
                digest.update(line)
                item = json.loads(line)
                if (item.get("domain") != "safety_training" or item.get("source_status") != "active"
                        or item.get("registry_hash_status") != "verified" or item.get("reference_only") is not False
                        or item.get("document_type") not in {"guide", "rule", "regulation", "sop", "standard", "procedure"}):
                    continue
                body = item["text"].strip()
                if not body:
                    continue
                num = len(self.records) + 1
                self.records[num] = {"ref": f"E{num}", "id": item["id"], "source": item["source"],
                                     "page": item.get("page"), "text": body}
                if item["source"] == guide_source and item.get("structured_record") and (
                        f"자료 시트: {guide_sheet}" in body.splitlines()):
                    self.guide.append(num)
                self.db.execute("INSERT INTO search(rowid,tokens) VALUES (?,?)",
                                (num, " ".join(terms(body))))
        self.index_hash = digest.hexdigest()
        if not self.guide:
            raise ValueError("색인에서 설정한 FAB 가이드 시트의 구조화 행을 찾지 못했습니다.")

    def evidence(self, rows):
        selected = dict.fromkeys(self.guide)
        for row in rows:
            query = " ".join(row[key] for key in ("SOP제목", "주작업명", "하위작업내용"))
            keywords = [word for word, _ in Counter(terms(query)).most_common(100)]
            if not keywords:
                continue
            expression = " OR ".join(f'"{word}"' for word in keywords)
            hits = self.db.execute("SELECT rowid FROM search WHERE search MATCH ? ORDER BY rank, rowid LIMIT 8",
                                   (expression,))
            for (num,) in hits:
                selected[num] = None
        return [self.records[num] for num in selected]
