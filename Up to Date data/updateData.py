#!/usr/bin/env python3
"""
extract_data.py

Automatic dataset refresh for the researchers ETL pipeline.

This script re-queries OpenAlex for every researcher already present in
the dataset and refreshes ONLY the dynamic fields listed below, using
the OpenAlex ID as the primary key. It never touches clean_data.py or
load_data.py, and it never cleans or loads data itself -- it only
refreshes the extracted dataset.

Fields updated (only if OpenAlex returns a non-empty value)
-------------------------------------------------------------
  - affiliated_institution
  - country
  - research_topics
  - works_count
  - citation_count
  - h_index
  - profile_url
  - paper_1_title / paper_1_abstract ... paper_5_title / paper_5_abstract

Rule 6 (never overwrite valid data with empty values)
-------------------------------------------------------
For every field above, the new value from OpenAlex is only written if
it is non-empty. If OpenAlex has nothing for a field (or the API call
fails for that researcher), the existing value in the dataset is kept
as-is.

All other columns (e.g. openalex_id, full_name, job_position,
name_mismatch, ...) are copied through unchanged -- the CSV structure
(column set and order) is preserved exactly.

Idempotency (rule 11)
-----------------------
Running this script multiple times in a row is safe and expected:
  - It reads from `researchers_updated.csv` if that file already exists
    (i.e. refreshing an already-refreshed dataset), otherwise it falls
    back to the original dataset file.
  - Each run only ever pulls the current state from OpenAlex and
    reapplies the same "keep existing value if new one is empty" rule,
    so repeated runs converge on the newest available OpenAlex data
    without corrupting or duplicating anything.

Output
------
researchers_updated.csv (same columns as the input, values refreshed)

Usage
-----
    python extract_data.py

Optional environment variable:
    OPENALEX_MAILTO   Your email, used for OpenAlex's "polite pool"
                       (faster, more stable rate limits).
"""

import csv
import os
import re
import sys
import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

# Prefer refreshing an already-refreshed dataset if present (idempotency);
# otherwise fall back to the base dataset.
INPUT_CANDIDATES = ["researchers_updated.csv", "merged_researchers_clean.csv"]
OUTPUT_CSV = "researchers_updated.csv"

OPENALEX_BASE = "https://api.openalex.org"
MAX_PAPERS = 5

OPENALEX_MAILTO = os.environ.get("OPENALEX_MAILTO", "")

REQUEST_TIMEOUT = 20
MAX_RETRIES = 5
BACKOFF_FACTOR = 2.0  # exponential backoff: 2s, 4s, 8s, 16s, 32s...
POLITE_DELAY = 0.15

DYNAMIC_SCALAR_FIELDS = [
    "affiliated_institution",
    "country",
    "research_topics",
    "works_count",
    "citation_count",
    "h_index",
    "profile_url",
]


# --------------------------------------------------------------------------
# HTTP session: retries, exponential backoff, 429 handling
# --------------------------------------------------------------------------

def build_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=MAX_RETRIES,
        backoff_factor=BACKOFF_FACTOR,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["GET"]),
        respect_retry_after_header=True,  # honor Retry-After on 429
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update(
        {"User-Agent": f"researcher-refresh-script/1.0 (mailto:{OPENALEX_MAILTO or 'unspecified'})"}
    )
    return session


SESSION = build_session()


def safe_get(url: str, params: dict = None):
    """GET with retry/backoff already handled by the session's Retry
    adapter. Returns parsed JSON on success, or None on any failure --
    callers must be able to continue with the next researcher."""
    try:
        resp = SESSION.get(url, params=params, timeout=REQUEST_TIMEOUT)
        time.sleep(POLITE_DELAY)
        if resp.status_code == 429:
            print(f"    [rate-limited] giving up on {url} after retries")
            return None
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.RequestException as exc:
        print(f"    [http-error] {url} -> {exc}")
        return None
    except ValueError as exc:
        print(f"    [parse-error] {url} -> {exc}")
        return None


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def normalize_openalex_id(raw_id: str) -> str:
    if not raw_id:
        return ""
    match = re.search(r"(A\d+)$", raw_id.strip())
    return match.group(1) if match else raw_id.strip()


def reconstruct_abstract(inverted_index: dict) -> str:
    if not inverted_index:
        return ""
    position_word = {}
    max_pos = 0
    for word, positions in inverted_index.items():
        for pos in positions:
            position_word[pos] = word
            if pos > max_pos:
                max_pos = pos
    words = [position_word.get(i, "") for i in range(max_pos + 1)]
    return " ".join(w for w in words if w)


def set_if_present(row: dict, field: str, new_value):
    """Rule 6: only overwrite `field` in `row` if new_value is non-empty.
    Otherwise the existing value is left untouched."""
    if new_value is None:
        return
    if isinstance(new_value, str):
        new_value = new_value.strip()
        if new_value == "":
            return
    else:
        # numeric fields (works_count, citation_count, h_index)
        new_value = str(new_value)
    row[field] = new_value


# --------------------------------------------------------------------------
# OpenAlex lookups
# --------------------------------------------------------------------------

def fetch_author(openalex_id: str) -> dict:
    url = f"{OPENALEX_BASE}/authors/{openalex_id}"
    params = {}
    if OPENALEX_MAILTO:
        params["mailto"] = OPENALEX_MAILTO
    return safe_get(url, params=params) or {}


def fetch_recent_works(openalex_id: str, limit: int = MAX_PAPERS) -> list:
    url = f"{OPENALEX_BASE}/works"
    params = {
        "filter": f"author.id:{openalex_id}",
        "sort": "publication_date:desc",
        "per-page": limit,
    }
    if OPENALEX_MAILTO:
        params["mailto"] = OPENALEX_MAILTO
    data = safe_get(url, params=params)
    if not data:
        return []

    works = []
    for item in data.get("results", [])[:limit]:
        title = item.get("title") or item.get("display_name") or ""
        abstract = reconstruct_abstract(item.get("abstract_inverted_index"))
        works.append({"title": title, "abstract": abstract})
    return works


def extract_dynamic_fields(author: dict) -> dict:
    """Map a raw OpenAlex author object to our dynamic column names.
    Missing pieces are simply absent from the returned dict (never
    fabricated as empty-string placeholders here -- set_if_present
    handles the "keep existing" logic downstream)."""
    result = {}
    if not author:
        return result

    inst = None
    last_known = author.get("last_known_institutions") or []
    if last_known:
        inst = last_known[0]
    elif author.get("last_known_institution"):
        inst = author["last_known_institution"]

    if inst:
        if inst.get("display_name"):
            result["affiliated_institution"] = inst["display_name"]
        if inst.get("country_code"):
            result["country"] = inst["country_code"]

    topics = author.get("topics") or []
    if topics:
        names = [t.get("display_name") for t in topics if t.get("display_name")]
        if names:
            result["research_topics"] = "; ".join(names)

    if author.get("works_count") is not None:
        result["works_count"] = author["works_count"]

    summary = author.get("summary_stats") or {}
    if summary.get("cited_by_count") is not None:
        result["citation_count"] = summary["cited_by_count"]
    elif author.get("cited_by_count") is not None:
        result["citation_count"] = author["cited_by_count"]

    if summary.get("h_index") is not None:
        result["h_index"] = summary["h_index"]

    if author.get("id"):
        result["profile_url"] = author["id"]

    return result


# --------------------------------------------------------------------------
# Main refresh logic
# --------------------------------------------------------------------------

def refresh_researcher(row: dict) -> dict:
    openalex_id = normalize_openalex_id(row.get("openalex_id", ""))
    if not openalex_id:
        return row  # nothing to key on -- leave the row untouched

    author = fetch_author(openalex_id)
    fresh = extract_dynamic_fields(author)
    for field in DYNAMIC_SCALAR_FIELDS:
        if field in fresh:
            set_if_present(row, field, fresh[field])

    works = fetch_recent_works(openalex_id, limit=MAX_PAPERS)
    for i, work in enumerate(works, start=1):
        set_if_present(row, f"paper_{i}_title", work.get("title", ""))
        set_if_present(row, f"paper_{i}_abstract", work.get("abstract", ""))

    return row


def find_input_file() -> str:
    for candidate in INPUT_CANDIDATES:
        if os.path.exists(candidate):
            return candidate
    return ""


def main():
    input_csv = find_input_file()
    if not input_csv:
        print(f"ERROR: none of the expected input files found: {INPUT_CANDIDATES}")
        sys.exit(1)

    with open(input_csv, newline="", encoding="utf-8-sig") as f_in:
        reader = csv.DictReader(f_in)
        fieldnames = reader.fieldnames
        rows = list(reader)

    total = len(rows)
    print(f"Loaded {total} researchers from {input_csv}")

    refreshed_rows = []
    for idx, row in enumerate(rows, start=1):
        print(f"Updating researcher {idx} / {total}")
        try:
            refreshed_row = refresh_researcher(row)
        except Exception as exc:  # noqa: BLE001 - one failure must not stop the run
            print(f"    [fatal-for-this-row] {row.get('full_name', row.get('openalex_id', ''))} -> {exc}")
            refreshed_row = row  # keep the existing row untouched on failure
        refreshed_rows.append(refreshed_row)

    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f_out:
        writer = csv.DictWriter(f_out, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(refreshed_rows)

    print(f"Done. Wrote {total} rows to {OUTPUT_CSV}")


if __name__ == "__main__":
    main()