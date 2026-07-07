#!/usr/bin/env python3
"""
load_data.py

Final stage of the researchers ETL pipeline: loads the cleaned dataset
into a SQLite database.

Pipeline position
------------------
    extract_data.py  ->  clean_data.py  ->  load_data.py  (this file)

Input
-----
Reads the first file found, in priority order, from INPUT_CANDIDATES:
    1. researchers_cleaned.csv   (expected output of clean_data.py)
    2. merged_researchers_clean.csv
    3. researchers_updated.csv

Schema
------
The wide CSV (one row per researcher, with paper_1..paper_5 columns) is
normalized into two SQLite tables:

    researchers
        openalex_id           TEXT PRIMARY KEY
        full_name             TEXT
        affiliated_institution TEXT
        country               TEXT
        research_topics       TEXT
        works_count           INTEGER
        citation_count        INTEGER
        h_index               INTEGER
        profile_url           TEXT
        profile_image_url     TEXT
        job_position          TEXT
        name_mismatch         TEXT
        updated_at            TEXT   -- ISO timestamp of this load

    papers
        id             INTEGER PRIMARY KEY AUTOINCREMENT
        researcher_id  TEXT  (FK -> researchers.openalex_id)
        position       INTEGER  -- 1..5, matches paper_N_* column order
        title          TEXT
        abstract       TEXT

Only non-empty paper slots are inserted (an empty paper_N_title /
paper_N_abstract pair is skipped rather than stored as a blank row).

Idempotency
-----------
Loading the same (or a refreshed) dataset multiple times is safe:
    - `researchers` rows are upserted on `openalex_id` (INSERT ... ON
      CONFLICT DO UPDATE), so re-running never creates duplicate
      researchers and always reflects the latest CSV values.
    - For each researcher, existing `papers` rows are deleted and
      reinserted from the current CSV row inside the same transaction,
      so re-running never accumulates duplicate or stale papers.

This script does NOT clean, transform, or re-fetch anything -- it only
loads what's already in the CSV.

Usage
-----
    python load_data.py
"""

import csv
import os
import sqlite3
import sys
from datetime import datetime, timezone

INPUT_CANDIDATES = [
    "researchers_cleaned.csv",
    "merged_researchers_clean.csv",
    "researchers_updated.csv",
]
DB_PATH = "researchers.db"
MAX_PAPERS = 5

RESEARCHER_COLUMNS = [
    "openalex_id",
    "full_name",
    "affiliated_institution",
    "country",
    "research_topics",
    "works_count",
    "citation_count",
    "h_index",
    "profile_url",
    "profile_image_url",
    "job_position",
    "name_mismatch",
]

INTEGER_COLUMNS = {"works_count", "citation_count", "h_index"}


def find_input_file() -> str:
    for candidate in INPUT_CANDIDATES:
        if os.path.exists(candidate):
            return candidate
    return ""


def to_int_or_none(value: str):
    value = (value or "").strip()
    if not value:
        return None
    try:
        return int(float(value))
    except ValueError:
        return None


def create_schema(conn: sqlite3.Connection):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS researchers (
            openalex_id            TEXT PRIMARY KEY,
            full_name              TEXT,
            affiliated_institution TEXT,
            country                TEXT,
            research_topics        TEXT,
            works_count            INTEGER,
            citation_count         INTEGER,
            h_index                INTEGER,
            profile_url            TEXT,
            profile_image_url      TEXT,
            job_position           TEXT,
            name_mismatch           TEXT,
            updated_at              TEXT
        );

        CREATE TABLE IF NOT EXISTS papers (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            researcher_id TEXT NOT NULL,
            position      INTEGER NOT NULL,
            title         TEXT,
            abstract      TEXT,
            FOREIGN KEY (researcher_id) REFERENCES researchers (openalex_id)
        );

        CREATE INDEX IF NOT EXISTS idx_papers_researcher_id
            ON papers (researcher_id);
        """
    )
    conn.commit()


def upsert_researcher(conn: sqlite3.Connection, row: dict, timestamp: str):
    values = {}
    for col in RESEARCHER_COLUMNS:
        raw = row.get(col, "")
        if col in INTEGER_COLUMNS:
            values[col] = to_int_or_none(raw)
        else:
            values[col] = (raw or "").strip() or None
    values["updated_at"] = timestamp

    columns = list(values.keys())
    placeholders = ", ".join(f":{c}" for c in columns)
    column_list = ", ".join(columns)
    update_clause = ", ".join(f"{c}=excluded.{c}" for c in columns if c != "openalex_id")

    conn.execute(
        f"""
        INSERT INTO researchers ({column_list})
        VALUES ({placeholders})
        ON CONFLICT(openalex_id) DO UPDATE SET {update_clause}
        """,
        values,
    )


def replace_papers(conn: sqlite3.Connection, researcher_id: str, row: dict):
    conn.execute("DELETE FROM papers WHERE researcher_id = ?", (researcher_id,))
    for i in range(1, MAX_PAPERS + 1):
        title = (row.get(f"paper_{i}_title", "") or "").strip()
        abstract = (row.get(f"paper_{i}_abstract", "") or "").strip()
        if not title and not abstract:
            continue  # nothing for this slot -- don't store an empty row
        conn.execute(
            "INSERT INTO papers (researcher_id, position, title, abstract) VALUES (?, ?, ?, ?)",
            (researcher_id, i, title, abstract),
        )


def main():
    input_csv = find_input_file()
    if not input_csv:
        print(f"ERROR: none of the expected input files found: {INPUT_CANDIDATES}")
        sys.exit(1)

    with open(input_csv, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    total = len(rows)
    print(f"Loaded {total} researchers from {input_csv}")
    print(f"Writing to SQLite database: {DB_PATH}")

    timestamp = datetime.now(timezone.utc).isoformat()

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON;")
    create_schema(conn)

    loaded = 0
    skipped = 0

    for idx, row in enumerate(rows, start=1):
        researcher_id = (row.get("openalex_id", "") or "").strip()
        if not researcher_id:
            skipped += 1
            print(f"Loading researcher {idx} / {total} -- [skip] missing openalex_id")
            continue

        print(f"Loading researcher {idx} / {total}")
        try:
            with conn:  # transaction per researcher: upsert + paper refresh
                upsert_researcher(conn, row, timestamp)
                replace_papers(conn, researcher_id, row)
            loaded += 1
        except sqlite3.Error as exc:
            print(f"    [db-error] {researcher_id} -> {exc}")
            skipped += 1

    conn.close()

    print(f"Loaded:  {loaded}")
    print(f"Skipped: {skipped}")
    print(f"Done. {DB_PATH} is up to date.")


if __name__ == "__main__":
    main()