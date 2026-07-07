

import csv
import html
import re

INPUT_CSV = "PrincipalReserchersData.csv.csv"
OUTPUT_CSV = "PrincipalReserchersDataClean.csv"

PAPER_SLOTS = 5
TITLE_COL = "paper_{}_title"
ABSTRACT_COL = "paper_{}_abstract"

TAG_RE = re.compile(r"<[^>]{1,80}>")
WHITESPACE_RE = re.compile(r"\s+")


def clean_text(value: str) -> str:
    """Decode entities, strip stray markup tags, and normalize whitespace.
    Never changes the substantive wording of the text."""
    if not value:
        return ""
    text = value
    # Decode twice to handle double-encoded entities like "&amp;quot;"
    text = html.unescape(text)
    text = html.unescape(text)
    # Remove leftover markup fragments (e.g. "<inf>", "<mml:msub>")
    text = TAG_RE.sub("", text)
    # Collapse whitespace introduced by tag removal / present in source
    text = WHITESPACE_RE.sub(" ", text)
    return text.strip()


def dedupe_papers(row: dict) -> dict:
    """Collapse exact duplicate paper slots within one researcher's row
    and shift the remaining distinct papers left. Leaves any leftover
    slots empty rather than fabricating data."""
    seen = set()
    distinct_papers = []
    for i in range(1, PAPER_SLOTS + 1):
        title = row.get(TITLE_COL.format(i), "")
        abstract = row.get(ABSTRACT_COL.format(i), "")
        if not title and not abstract:
            continue
        key = (title, abstract)
        if key in seen:
            continue  # exact duplicate of an earlier slot -- drop it
        seen.add(key)
        distinct_papers.append((title, abstract))

    for i in range(1, PAPER_SLOTS + 1):
        if i <= len(distinct_papers):
            title, abstract = distinct_papers[i - 1]
        else:
            title, abstract = "", ""
        row[TITLE_COL.format(i)] = title
        row[ABSTRACT_COL.format(i)] = abstract
    return row


def main():
    with open(INPUT_CSV, newline="", encoding="utf-8-sig") as f_in:
        reader = csv.DictReader(f_in)
        fieldnames = reader.fieldnames
        rows = list(reader)

    print(f"Loaded {len(rows)} rows from {INPUT_CSV}")

    entity_fixes = 0
    tag_fixes = 0
    whitespace_fixes = 0
    rows_with_dupe_papers = 0

    cleaned_rows = []
    for row in rows:
        new_row = {}
        for key, value in row.items():
            value = value or ""
            cleaned = clean_text(value)
            if cleaned != value:
                if "&" in value:
                    entity_fixes += 1
                if TAG_RE.search(value):
                    tag_fixes += 1
                if value != value.strip() or WHITESPACE_RE.search(value.strip()) and "  " in value:
                    whitespace_fixes += 1
            new_row[key] = cleaned

        titles = [new_row.get(TITLE_COL.format(i), "") for i in range(1, PAPER_SLOTS + 1)]
        nonempty_titles = [t for t in titles if t]
        if len(nonempty_titles) != len(set(nonempty_titles)):
            rows_with_dupe_papers += 1
        new_row = dedupe_papers(new_row)

        cleaned_rows.append(new_row)

    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f_out:
        writer = csv.DictWriter(f_out, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(cleaned_rows)

    print(f"Cells with entity decoding applied:  {entity_fixes}")
    print(f"Cells with markup tags stripped:     {tag_fixes}")
    print(f"Cells with whitespace normalized:    {whitespace_fixes}")
    print(f"Rows with duplicate papers resolved: {rows_with_dupe_papers}")
    print(f"Done. Wrote {len(cleaned_rows)} rows to {OUTPUT_CSV}")


if __name__ == "__main__":
    main()