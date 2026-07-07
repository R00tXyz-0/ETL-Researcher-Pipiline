import csv
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

INPUT_CSV = "researchers.csv"
OUTPUT_CSV = "researcher_enrichment.csv"

MAX_PAPERS = 5

OPENALEX_BASE = "https://api.openalex.org"
ORCID_BASE = "https://pub.orcid.org/v3.0"
ARXIV_BASE = "http://export.arxiv.org/api/query"

# Being a good API citizen: OpenAlex requests a contact email for the
# "polite pool" (higher, more stable rate limits). Purely optional.
OPENALEX_MAILTO = os.environ.get("OPENALEX_MAILTO", "")

# Left OFF on purpose -- see module docstring. Flip to True only if you
# understand and accept the reliability / ToS risks of scraping.
ENABLE_EXPERIMENTAL_SCHOLAR_SCRAPE = False

REQUEST_TIMEOUT = 20  # seconds
MAX_RETRIES = 4
BACKOFF_FACTOR = 1.5
POLITE_DELAY = 0.15  # seconds between requests, on top of retry backoff

OUTPUT_FIELDS = [
    "researcher_id",
    "full_name",
    "job_position",
    "profile_image_url",
    "paper_1_title",
    "paper_1_abstract",
    "paper_2_title",
    "paper_2_abstract",
    "paper_3_title",
    "paper_3_abstract",
    "paper_4_title",
    "paper_4_abstract",
    "paper_5_title",
    "paper_5_abstract",
]


# --------------------------------------------------------------------------
# HTTP session with retries / backoff / rate-limit handling
# --------------------------------------------------------------------------

def build_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=MAX_RETRIES,
        backoff_factor=BACKOFF_FACTOR,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["GET"]),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update(
        {"User-Agent": "researcher-enrichment-script/1.0 (mailto:%s)" % (OPENALEX_MAILTO or "unspecified")}
    )
    return session


SESSION = build_session()


def safe_get(url: str, params: dict = None):
    """GET a URL, handling HTTP errors / rate limits gracefully.

    Returns the parsed JSON body on success, or None on any failure. Never
    raises -- callers must be able to continue processing other
    researchers even if one lookup fails.
    """
    try:
        resp = SESSION.get(url, params=params, timeout=REQUEST_TIMEOUT)
        time.sleep(POLITE_DELAY)
        if resp.status_code == 429:
            # Retry adapter already tried its budget; give up cleanly.
            print(f"    [rate-limited] giving up on {url}")
            return None
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.RequestException as exc:
        print(f"    [http-error] {url} -> {exc}")
        return None
    except ValueError as exc:  # JSON decode error
        print(f"    [parse-error] {url} -> {exc}")
        return None


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def normalize_openalex_id(raw_id: str) -> str:
    """Accept either a bare ID (A5037658956) or a full OpenAlex URL and
    return the bare ID."""
    if not raw_id:
        return ""
    raw_id = raw_id.strip()
    match = re.search(r"(A\d+)$", raw_id)
    return match.group(1) if match else raw_id


def reconstruct_abstract(inverted_index: dict) -> str:
    """OpenAlex stores abstracts as an inverted index (word -> [positions])
    for copyright reasons. Reconstruct the plain-text abstract from it."""
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


# --------------------------------------------------------------------------
# Data source lookups
# --------------------------------------------------------------------------

def fetch_openalex_author(openalex_id: str) -> dict:
    url = f"{OPENALEX_BASE}/authors/{openalex_id}"
    params = {}
    if OPENALEX_MAILTO:
        params["mailto"] = OPENALEX_MAILTO
    data = safe_get(url, params=params)
    return data or {}


def fetch_job_position_via_orcid(orcid_id: str) -> str:
    """Query the public ORCID API for the author's current employment
    role title. Returns '' if unavailable."""
    if not orcid_id:
        return ""
    orcid_id_clean = orcid_id.rstrip("/").split("/")[-1]
    url = f"{ORCID_BASE}/{orcid_id_clean}/employments"
    headers = {"Accept": "application/json"}
    try:
        resp = SESSION.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        time.sleep(POLITE_DELAY)
        if resp.status_code != 200:
            return ""
        data = resp.json()
    except (requests.exceptions.RequestException, ValueError) as exc:
        print(f"    [orcid-error] {url} -> {exc}")
        return ""

    try:
        groups = data.get("affiliation-group", [])
        for group in groups:
            summaries = group.get("summaries", [])
            for summary_wrapper in summaries:
                summary = summary_wrapper.get("employment-summary", {})
                role_title = summary.get("role-title")
                end_date = summary.get("end-date")
                if role_title and end_date is None:
                    # Prefer a current (no end date) position
                    return role_title
        # Fall back to the first available role title even if it has ended
        for group in groups:
            summaries = group.get("summaries", [])
            for summary_wrapper in summaries:
                summary = summary_wrapper.get("employment-summary", {})
                role_title = summary.get("role-title")
                if role_title:
                    return role_title
    except (AttributeError, TypeError):
        return ""
    return ""


def fetch_recent_works(openalex_id: str, limit: int = MAX_PAPERS) -> list:
    """Return up to `limit` most recent works for this author as a list of
    dicts: {"title": str, "abstract": str}."""
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
        if not abstract and title:
            abstract = fetch_abstract_from_arxiv(title)
        works.append({"title": title, "abstract": abstract})
    return works


def fetch_abstract_from_arxiv(title: str) -> str:
    """Best-effort fallback: look up a paper on arXiv by exact-ish title
    match and return its abstract if OpenAlex didn't have one."""
    if not title:
        return ""
    query = f'ti:"{title}"'
    params = {"search_query": query, "start": 0, "max_results": 1}
    try:
        resp = SESSION.get(ARXIV_BASE, params=params, timeout=REQUEST_TIMEOUT)
        time.sleep(POLITE_DELAY)
        if resp.status_code != 200:
            return ""
        root = ET.fromstring(resp.content)
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        entry = root.find("atom:entry", ns)
        if entry is None:
            return ""
        entry_title = entry.findtext("atom:title", default="", namespaces=ns)
        if entry_title and entry_title.strip().lower() != title.strip().lower():
            # Title mismatch -- don't guess, don't attach the wrong abstract.
            return ""
        summary = entry.findtext("atom:summary", default="", namespaces=ns)
        return " ".join(summary.split()) if summary else ""
    except (requests.exceptions.RequestException, ET.ParseError) as exc:
        print(f"    [arxiv-error] {title[:60]!r} -> {exc}")
        return ""


def fetch_profile_image(openalex_author: dict) -> str:
    """Attempt to find a profile image URL. OpenAlex itself does not host
    author photos. This function is a documented no-op unless the
    experimental scraping hook is explicitly enabled (off by default --
    see module docstring)."""
    if not ENABLE_EXPERIMENTAL_SCHOLAR_SCRAPE:
        return ""
    # Intentionally not implemented: reliably and safely scraping Google
    # Scholar / ResearchGate photos at scale is not feasible without
    # violating their terms of service or producing unreliable results.
    return ""


# --------------------------------------------------------------------------
# Main enrichment logic
# --------------------------------------------------------------------------

def enrich_researcher(researcher_id: str, full_name: str, institution: str) -> dict:
    row = {field: "" for field in OUTPUT_FIELDS}
    row["researcher_id"] = researcher_id
    row["full_name"] = full_name

    openalex_id = normalize_openalex_id(researcher_id)
    if not openalex_id:
        print("    [skip] no usable OpenAlex ID")
        return row

    author = fetch_openalex_author(openalex_id)

    # Job position, via ORCID if OpenAlex links to one.
    orcid_id = ((author.get("ids") or {}).get("orcid")) if author else None
    if orcid_id:
        row["job_position"] = fetch_job_position_via_orcid(orcid_id)

    # Profile image (currently always empty by design -- see docstring).
    row["profile_image_url"] = fetch_profile_image(author)

    # Recent papers + abstracts.
    works = fetch_recent_works(openalex_id, limit=MAX_PAPERS)
    for i, work in enumerate(works, start=1):
        row[f"paper_{i}_title"] = work.get("title", "")
        row[f"paper_{i}_abstract"] = work.get("abstract", "")

    return row


def find_column(fieldnames: list, *candidates: str) -> str:
    """Case/format-insensitive lookup of a source column name."""
    normalized = {re.sub(r"[\s_]+", "", fn).lower(): fn for fn in fieldnames}
    for candidate in candidates:
        key = re.sub(r"[\s_]+", "", candidate).lower()
        if key in normalized:
            return normalized[key]
    return ""


def main():
    if not os.path.exists(INPUT_CSV):
        print(f"ERROR: input file not found: {INPUT_CSV}")
        sys.exit(1)

    with open(INPUT_CSV, newline="", encoding="utf-8-sig") as f_in:
        reader = csv.DictReader(f_in)
        fieldnames = reader.fieldnames or []

        id_col = find_column(fieldnames, "OpenAlex ID", "openalex_id")
        name_col = find_column(fieldnames, "Full Name", "full_name")
        inst_col = find_column(fieldnames, "Affiliated Institution", "affiliated_institution")

        if not id_col or not name_col:
            print("ERROR: required columns (OpenAlex ID, Full Name) not found in input CSV.")
            print(f"Columns found: {fieldnames}")
            sys.exit(1)

        rows = list(reader)

    total = len(rows)
    print(f"Loaded {total} researchers from {INPUT_CSV}")

    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f_out:
        writer = csv.DictWriter(f_out, fieldnames=OUTPUT_FIELDS)
        writer.writeheader()

        for idx, source_row in enumerate(rows, start=1):
            researcher_id = (source_row.get(id_col) or "").strip()
            full_name = (source_row.get(name_col) or "").strip()
            institution = (source_row.get(inst_col) or "").strip() if inst_col else ""

            print(f"[{idx}/{total}] Enriching: {full_name or researcher_id}")

            try:
                enriched_row = enrich_researcher(researcher_id, full_name, institution)
            except Exception as exc:  # noqa: BLE001 - never let one failure stop the run
                print(f"    [fatal-for-this-row] {full_name or researcher_id} -> {exc}")
                enriched_row = {field: "" for field in OUTPUT_FIELDS}
                enriched_row["researcher_id"] = researcher_id
                enriched_row["full_name"] = full_name

            writer.writerow(enriched_row)
            f_out.flush()

    print(f"Done. Wrote {total} rows to {OUTPUT_CSV}")


if __name__ == "__main__":
    main()