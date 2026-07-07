#!/usr/bin/env python3
"""
merge_researchers.py

Merges the raw researcher dataset with the enrichment dataset on the
OpenAlex researcher ID and writes a single combined CSV.

Inputs
------
- researchers.csv
    Columns: openalex_id, full_name, affiliated_institution, country,
             research_topics, works_count, citation_count, h_index,
             profile_url, profile_image_url

- researcherMissingData.csv
    Columns: researcher_id, full_name, job_position, profile_image_url,
             paper_1_title, paper_1_abstract, ... paper_5_title,
             paper_5_abstract

Join key
--------
researchers.csv.openalex_id  <->  researcherMissingData.csv.researcher_id
(both are full OpenAlex URLs, e.g. https://openalex.org/A5037658956)

Column collision handling
--------------------------
Both files carry `full_name` and `profile_image_url`. Rather than silently
overwriting one with the other (which could hide real data), this script:
  - keeps `full_name` from researchers.csv (the source-of-truth roster),
  - coalesces `profile_image_url`: keeps the researchers.csv value, and
    only fills it in from the enrichment file if it was empty,
  - flags any row where the two `full_name` values actually disagree, so
    nothing is merged blindly.
No values are invented, cleaned, normalized, or deduplicated -- this
script only combines existing cells side by side.

Output
------
merged_researchers.csv containing every column from both input files
(collisions resolved as above), one row per researcher.

Usage
-----
    python merge_researchers.py
"""

import csv
import sys

RAW_CSV = "researchers.csv"
ENRICHMENT_CSV = "researcherMissingData.csv"
OUTPUT_CSV = "PrincipalReserchersData.csv"

RAW_ID_COL = "openalex_id"
ENRICHMENT_ID_COL = "researcher_id"


def load_rows(path: str) -> list:
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def main():
    try:
        raw_rows = load_rows(RAW_CSV)
    except FileNotFoundError:
        print(f"ERROR: could not find {RAW_CSV}")
        sys.exit(1)

    try:
        enrichment_rows = load_rows(ENRICHMENT_CSV)
    except FileNotFoundError:
        print(f"ERROR: could not find {ENRICHMENT_CSV}")
        sys.exit(1)

    print(f"Loaded {len(raw_rows)} rows from {RAW_CSV}")
    print(f"Loaded {len(enrichment_rows)} rows from {ENRICHMENT_CSV}")

    # Index enrichment rows by id for O(1) lookup.
    enrichment_by_id = {}
    for row in enrichment_rows:
        rid = (row.get(ENRICHMENT_ID_COL) or "").strip()
        if rid:
            enrichment_by_id[rid] = row

    # Build the merged column order: all raw columns, then enrichment-only
    # columns (skip the duplicate id/full_name/profile_image_url, which
    # are reconciled explicitly below).
    enrichment_only_cols = [
        c for c in (enrichment_rows[0].keys() if enrichment_rows else [])
        if c not in (ENRICHMENT_ID_COL, "full_name", "profile_image_url")
    ]
    raw_cols = list(raw_rows[0].keys()) if raw_rows else []
    output_fields = raw_cols + enrichment_only_cols + ["name_mismatch"]

    matched = 0
    missing = 0
    name_mismatches = 0

    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f_out:
        writer = csv.DictWriter(f_out, fieldnames=output_fields)
        writer.writeheader()

        for i, raw_row in enumerate(raw_rows, start=1):
            rid = (raw_row.get(RAW_ID_COL) or "").strip()
            merged_row = dict(raw_row)  # start with all raw columns as-is

            enrichment_row = enrichment_by_id.get(rid)
            name_mismatch = ""

            if enrichment_row:
                matched += 1

                # Coalesce profile_image_url: keep raw value, fall back to
                # enrichment value only if raw is empty.
                raw_img = (raw_row.get("profile_image_url") or "").strip()
                enrich_img = (enrichment_row.get("profile_image_url") or "").strip()
                merged_row["profile_image_url"] = raw_img or enrich_img

                # Flag (don't silently resolve) a full_name disagreement.
                raw_name = (raw_row.get("full_name") or "").strip()
                enrich_name = (enrichment_row.get("full_name") or "").strip()
                if raw_name and enrich_name and raw_name != enrich_name:
                    name_mismatch = f"raw='{raw_name}' vs enrichment='{enrich_name}'"
                    name_mismatches += 1

                for col in enrichment_only_cols:
                    merged_row[col] = enrichment_row.get(col, "")
            else:
                missing += 1
                print(f"    [no-match] {rid or '(empty id)'} — no enrichment row found")
                for col in enrichment_only_cols:
                    merged_row[col] = ""

            merged_row["name_mismatch"] = name_mismatch
            writer.writerow(merged_row)

    print(f"Matched:        {matched}")
    print(f"Unmatched:      {missing}")
    print(f"Name mismatches:{name_mismatches}")
    print(f"Done. Wrote {len(raw_rows)} rows to {OUTPUT_CSV}")


if __name__ == "__main__":
    main()