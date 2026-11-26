"""Local BM25 search over reviewed text from the portable JSONL index."""
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import unicodedata


RANKING_VERSION = "fab-row-bm25-v2"
LIST_MARKER = re.compile(r"(?<!\S)\d+[.)](?!\d)")
WORD = re.compile(r"[a-z][a-z0-9]*|\d+(?:[./]\d+)+|\d+[a-z][a-z0-9]*|\d+|[가-힣]+")


def words(text):
    text = unicodedata.normalize("NFKC", text).lower()
    for match in WORD.finditer(LIST_MARKER.sub(" ", text)):
        # Keep decimal/fraction boundaries inside a single FTS token.
        yield match.group().replace(".", "dot").replace("/", "over")


def korean_parts(tokens):
    for word in tokens:
        if len(word) > 1 and "가" <= word[0] <= "힣":
            for size in (2, 3):
                yield from (word[i:i+size] for i in range(len(word)-size+1))


def query_group(column, tokens, limit):
    phrases = [f'"{t}"' for t, _ in Counter(tokens).most_common(limit)]
    return f"{column} : (" + " OR ".join(phrases) + ")" if phrases else ""


class RegulationSearch:
    def __init__(self, path: Path, guide_source: str, guide_sheet: str, max_evidence_items: int = 20,
                 other_area_sheets=()):
        if max_evidence_items < 1:
            raise ValueError("max_evidence_items는 양수여야 합니다.")
        self.max_evidence_items = max_evidence_items
        self.db = sqlite3.connect(":memory:")
        self.db.execute("CREATE VIRTUAL TABLE search USING fts5(action, partial, context)")
        self.records = {}
        has_guide = False
        excluded_headers = {f"자료 시트: {sheet}" for sheet in other_area_sheets if sheet != guide_sheet}
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
                # The configured workbook contains parallel checklists for different
                # operations. Only apply this scope to its explicitly labelled sheets.
                sheet_header = body.splitlines()[0]
                if item["source"] == guide_source and sheet_header in excluded_headers:
                    continue
                num = len(self.records) + 1
                self.records[num] = {"ref": f"E{num}", "id": item["id"], "source": item["source"],
                                     "page": item.get("page"), "text": body}
                if item["source"] == guide_source and item.get("structured_record") and (
                        f"자료 시트: {guide_sheet}" in body.splitlines()):
                    has_guide = True
                tokens = list(words(body))
                exact = " ".join(tokens)
                self.db.execute("INSERT INTO search(rowid, action, partial, context) VALUES (?,?,?,?)",
                                (num, exact, " ".join(korean_parts(tokens)), exact))
        self.index_hash = digest.hexdigest()
        if not has_guide:
            self.db.close()
            raise ValueError("색인에서 설정한 FAB 가이드 시트의 구조화 행을 찾지 못했습니다.")

    def evidence(self, row):
        detail = list(words(row["하위작업내용"]))
        actions = detail + list(words(row["주작업명"]))
        heading = LIST_MARKER.split(row["하위작업내용"], maxsplit=1)
        focus = list(words(row["주작업명"]))
        if len(heading) > 1:
            focus.extend(words(heading[0]))
        pairs = [f"{a} {b}" for a, b in zip(detail, detail[1:])
                 if (a.isascii() or b.isascii()) and min(len(a), len(b)) > 1]
        groups = [query_group("action", actions, 80), query_group("action", pairs, 30),
                  query_group("action", focus, 32), query_group("partial", korean_parts(focus), 32),
                  query_group("partial", korean_parts(actions), 60),
                  query_group("context", words(row["SOP제목"]), 24)]
        repeated = [word for word, count in Counter(actions).items()
                    if count > 1 and word.isascii() and len(word) > 1 and not word.isdigit()]
        # FTS5 counts matching phrase occurrences in the query. Give repeated
        # technical identifiers extra weight without making them compulsory.
        groups.extend([query_group("action", repeated, 24)] * 3)
        expression = " OR ".join(group for group in groups if group)
        if not expression:
            return []
        hits = self.db.execute(
            "SELECT rowid FROM search WHERE search MATCH ? "
            "ORDER BY bm25(search, 1.0, 0.45, 0.06), rowid LIMIT ?",
            (expression, self.max_evidence_items),
        )
        return [self.records[num] for (num,) in hits]
