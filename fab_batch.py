"""FAB SOP 조회 및 개정본별 행 조립. 실행 시 원문 대신 건수만 출력한다."""

import argparse
import configparser
import importlib
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
import sys

import pandas as pd


FAB_COLUMNS = {
    "sopr_no": "SOP번호",
    "sopr_nm": "SOP제목",
    "sopr_main_seq": "주작업순번",
    "sopr_main_nm": "주작업명",
    "sopr_sub_seq": "하위작업순번",
    "sopr_sub_ctnt": "하위작업내용",
}


def sequence_number(value: object) -> int | None:
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    if number.is_finite() and number == number.to_integral_value() and number >= 0:
        return int(number)
    return None


def step_position(row: dict) -> tuple:
    main = sequence_number(row["주작업순번"])
    sub = sequence_number(row["하위작업순번"])
    return (main is None, main or 0, sub is None, sub or 0)


def input_notes(rows: list[dict]) -> list[str]:
    """Report source ambiguities separately from safety findings."""
    notes = set()
    titles = set()
    main_names = {}
    contents = {}
    for row in rows:
        title, name, content = (row[k].strip() for k in ("SOP제목", "주작업명", "하위작업내용"))
        if title:
            titles.add(title)
        main = sequence_number(row["주작업순번"])
        sub = sequence_number(row["하위작업순번"])
        if main is None or sub is None:
            notes.add("작업순번이 비었거나 정수로 판독되지 않는 행이 있습니다. 해당 행의 선후관계를 확인하세요.")
        if not content:
            notes.add(f"주작업 {row['주작업순번']}에 하위작업내용이 빈 행이 있습니다.")
        if main is not None and name:
            main_names.setdefault(main, set()).add(name)
        if main is not None and sub is not None:
            contents.setdefault((main, sub), set()).add(content)
    if len(titles) != 1:
        notes.add("해당 SOP의 제목을 하나로 식별할 수 없습니다.")
    for main, names in main_names.items():
        if len(names) > 1:
            notes.add(f"주작업 {main}의 작업명이 여러 개입니다.")
    for (main, sub), texts in contents.items():
        if len(texts) > 1:
            notes.add(f"{main}-{sub}에 서로 다른 내용이 있습니다. 동일 순번 내 선후관계는 미확정입니다.")
    return sorted(notes)


def read_config(path: Path) -> configparser.ConfigParser:
    config = configparser.ConfigParser(interpolation=None)
    with path.open(encoding="utf-8-sig") as stream:
        config.read_file(stream)
    return config


def group_sop_rows(frame: pd.DataFrame, id_column: str, revision_column: str) -> dict:
    """Return original rows by (SOP ID, revision), preserving the query order."""
    if not frame.columns.is_unique:
        raise ValueError("중복 컬럼명이 있습니다. 조회 SQL에서 고유한 별칭을 지정하세요.")
    keys = [id_column]
    if revision_column:
        if revision_column == id_column:
            raise ValueError("SOP 번호와 개정 컬럼은 서로 다른 컬럼이어야 합니다.")
        keys.append(revision_column)
    for key in keys:
        if key not in frame.columns:
            raise ValueError(f"조회 결과에 필수 컬럼 {key!r}이 없습니다. SQL 및 어댑터 오류를 확인하세요.")
        values = frame[key]
        if values.isna().any() or values.astype(str).str.strip().eq("").any():
            raise ValueError(f"조회 결과의 {key!r}에 빈 값이 있습니다.")

    documents = {}
    for row in frame.to_dict(orient="records"):
        key = (row[id_column], row[revision_column] if revision_column else None)
        documents.setdefault(key, []).append(row)
    return documents


def load_sql_adapter(config_path: Path, config):
    for name in ("driver_dir", "adapter_dir"):
        path = config.get("data", name, fallback="").strip()
        if path or name == "adapter_dir":
            folder = (config_path.parent / path).resolve()
            if not folder.is_dir():
                raise ValueError(f"[data] {name} 폴더를 찾을 수 없습니다: {folder}")
            if str(folder) not in sys.path:
                sys.path.insert(0, str(folder))
    return importlib.import_module(config.get("data", "adapter_module", fallback="sql_adapter"))


def load_fab_sops(config_path: Path, as_of: date | None = None) -> dict:
    config = read_config(config_path)
    fab = config["fab"]
    query = fab["query"].strip()
    if not query:
        raise ValueError("[fab] query에 조회 SQL을 입력하세요.")
    if "lookback_days" in fab or "{window_" in query:
        days = fab.getint("lookback_days", 3)
        if days < 1 or not all(token in query for token in ("{window_start}", "{window_end}")):
            raise ValueError("lookback_days는 양수이고 SQL에는 {window_start}, {window_end}가 모두 필요합니다.")
        today = as_of or datetime.now(timezone(timedelta(hours=9))).date()
        query = query.replace("{window_start}", (today - timedelta(days=days - 1)).isoformat())
        query = query.replace("{window_end}", (today + timedelta(days=1)).isoformat())
    adapter = load_sql_adapter(config_path, config)
    frame = adapter.execute_SQL(query)
    revision = fab.get("revision_column", "")
    row_format = fab.get("row_format", "raw")
    if row_format == "raw":
        return group_sop_rows(frame, fab["sop_id_column"], revision)
    if row_format != "fab_steps":
        raise ValueError(f"알 수 없는 row_format: {row_format}")
    frame = frame.rename(columns=lambda col: str(col).lower())
    missing = set(FAB_COLUMNS).difference(frame.columns)
    if missing:
        raise ValueError(f"FAB 조회 결과에 필요한 컬럼이 없습니다: {', '.join(sorted(missing))}")
    documents = group_sop_rows(frame, fab["sop_id_column"].lower(), revision.lower())
    for key, source_rows in documents.items():
        rows = [
            {label: "" if pd.isna(row[col]) else str(row[col]) for col, label in FAB_COLUMNS.items()}
            for row in source_rows
        ]
        for row, original in zip(rows, source_rows):
            if "cre_dttm" in original:
                row["등록일"] = str(original["cre_dttm"]) if pd.notna(original["cre_dttm"]) else ""
        documents[key] = sorted(rows, key=step_position)
    return documents


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("config.ini"))
    parser.add_argument("--show-sop", help="조회 결과 중 해당 번호의 조립된 원문을 표시")
    args = parser.parse_args()
    documents = load_fab_sops(args.config)
    sop_count = len({sop_id for sop_id, _ in documents})
    row_count = sum(len(rows) for rows in documents.values())
    print(f"FAB 조회 완료 | SOP {sop_count}건 | 검토 단위 {len(documents)}건 | 원본 행 {row_count}개")
    if args.show_sop is not None:
        matches = [(rev, rows) for (sop_id, rev), rows in documents.items() if str(sop_id) == args.show_sop]
        if not matches:
            parser.exit(1, "조회 결과에 해당 SOP 번호가 없습니다.\n")
        for rev, rows in matches:
            result = {"개정": rev, "작업행": rows}
            if "하위작업순번" in rows[0]:
                result["입력자료 확인사항"] = input_notes(rows)
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
