import csv
import sys
import time
import random
import requests

# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------

API_BASE = "https://api.openalex.org"
AUTHORS_ENDPOINT = f"{API_BASE}/authors"
CONCEPTS_ENDPOINT = f"{API_BASE}/concepts"

MAX_RESEARCHERS = 2000
PER_PAGE = 200                 # OpenAlex max page size
REQUEST_DELAY_SECONDS = 0.5    # polite delay between requests
MAX_RETRIES = 5                # retries per request on failure/429
BACKOFF_BASE_SECONDS = 2       # exponential backoff base

# OpenAlex "polite pool" - set your email to get faster/more reliable
# rate limits. Leave as empty string to skip (still works, just slower/
# less reliable rate limiting from OpenAlex's side).
POLITE_POOL_EMAIL = ""  # e.g. "your_email@example.com"

# Country filter for Moroccan institutions (ISO 3166-1 alpha-2)
COUNTRY_CODE = "MA"

# Research areas to prioritize (used to look up OpenAlex Concept IDs
# dynamically via the Concepts API, so we don't hardcode possibly-stale IDs)
AI_RELATED_TOPIC_NAMES = [
    "Artificial intelligence",
    "Machine learning",
    "Deep learning",
    "Natural language processing",
    "Computer vision",
    "Data mining",
    "Data science",
    "Big data",
    "Business intelligence",
    "Decision support system",
    "Predictive analytics",
    "Explainable artificial intelligence",
    "Generative artificial intelligence",
    "Large language model",
    "Reinforcement learning",
    "Robotics",
    "Knowledge representation and reasoning",
    "Computer security",
    "Cloud computing",
    "Internet of things",
    "Software engineering",
]

OUTPUT_CSV = "researchers_raw.csv"

CSV_FIELDS = [
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
]


# ----------------------------------------------------------------------
# HTTP helper with retry + exponential backoff
# ----------------------------------------------------------------------

def request_with_retry(url, params=None, max_retries=MAX_RETRIES):
    """
    Perform a GET request with:
      - automatic retry on failure
      - exponential backoff on HTTP 429 (rate limit)
      - exponential backoff on transient network / 5xx errors

    Returns the parsed JSON body on success, or None if all retries
    are exhausted (caller should handle None by skipping/continuing).
    """
    params = dict(params or {})
    if POLITE_POOL_EMAIL:
        params["mailto"] = POLITE_POOL_EMAIL

    for attempt in range(1, max_retries + 1):
        try:
            response = requests.get(url, params=params, timeout=30)

            if response.status_code == 200:
                return response.json()

            if response.status_code == 429:
                wait = BACKOFF_BASE_SECONDS ** attempt + random.uniform(0, 1)
                print(f"  [rate limited - 429] backing off {wait:.1f}s "
                      f"(attempt {attempt}/{max_retries})")
                time.sleep(wait)
                continue

            if 500 <= response.status_code < 600:
                wait = BACKOFF_BASE_SECONDS ** attempt + random.uniform(0, 1)
                print(f"  [server error {response.status_code}] "
                      f"retrying in {wait:.1f}s "
                      f"(attempt {attempt}/{max_retries})")
                time.sleep(wait)
                continue

            # Other client errors (400, 404, etc.) - not worth retrying
            print(f"  [HTTP error {response.status_code}] {response.text[:200]}")
            return None

        except requests.exceptions.RequestException as exc:
            wait = BACKOFF_BASE_SECONDS ** attempt + random.uniform(0, 1)
            print(f"  [network error] {exc} - retrying in {wait:.1f}s "
                  f"(attempt {attempt}/{max_retries})")
            time.sleep(wait)
            continue

    print(f"  [FAILED] giving up on {url} after {max_retries} attempts")
    return None


# ----------------------------------------------------------------------
# Step 1: Resolve OpenAlex Concept IDs for the AI-related research areas
# ----------------------------------------------------------------------

def resolve_concept_ids(topic_names):
    """
    Look up each topic name via the OpenAlex Concepts API to get its
    real OpenAlex concept ID. This avoids hardcoding potentially
    outdated/incorrect concept IDs.

    Returns a list of concept IDs (short form, e.g. 'C154945302').
    Topics that fail to resolve are skipped (printed as a warning).
    """
    concept_ids = []
    print("Resolving OpenAlex concept IDs for prioritized research areas...")

    for name in topic_names:
        data = request_with_retry(CONCEPTS_ENDPOINT, params={
            "search": name,
            "per-page": 1,
        })
        time.sleep(REQUEST_DELAY_SECONDS)

        if not data or not data.get("results"):
            print(f"  [warn] could not resolve concept for: '{name}'")
            continue

        top_result = data["results"][0]
        full_id = top_result.get("id", "")  # e.g. "https://openalex.org/C154945302"
        short_id = full_id.rsplit("/", 1)[-1] if full_id else None

        if short_id:
            concept_ids.append(short_id)
            print(f"  resolved '{name}' -> {short_id} "
                  f"({top_result.get('display_name')})")

    return concept_ids


# ----------------------------------------------------------------------
# Step 2: Extract a single author record's raw fields
# ----------------------------------------------------------------------

def extract_author_record(author):
    """
    Pull the raw fields we care about out of a single OpenAlex 'author'
    object. No transformation/inference - if a field is missing, leave
    it as an empty string.
    """
    openalex_id = author.get("id", "") or ""

    full_name = author.get("display_name", "") or ""

    # Institution + country come from last_known_institutions (list)
    institution = ""
    country = ""
    last_known_institutions = author.get("last_known_institutions") or []
    if last_known_institutions:
        first_inst = last_known_institutions[0] or {}
        institution = first_inst.get("display_name", "") or ""
        country = first_inst.get("country_code", "") or ""

    # Research topics - OpenAlex 'topics' field (list of topic objects)
    topics_list = author.get("topics") or []
    research_topics = "; ".join(
        t.get("display_name", "") for t in topics_list if t.get("display_name")
    )

    works_count = author.get("works_count", "")
    if works_count is None:
        works_count = ""

    summary_stats = author.get("summary_stats") or {}
    citation_count = summary_stats.get("cited_by_count", "")
    if citation_count is None:
        citation_count = ""
    # Fallback: OpenAlex also exposes cited_by_count at top level for authors
    if citation_count == "" and author.get("cited_by_count") is not None:
        citation_count = author.get("cited_by_count")

    h_index = summary_stats.get("h_index", "")
    if h_index is None:
        h_index = ""

    profile_url = openalex_id  # OpenAlex author id IS the canonical profile URL

    # OpenAlex author objects do not provide a profile photo field.
    # Leave empty per "if available" instruction - do not fabricate.
    profile_image_url = ""

    return {
        "openalex_id": openalex_id,
        "full_name": full_name,
        "affiliated_institution": institution,
        "country": country,
        "research_topics": research_topics,
        "works_count": works_count,
        "citation_count": citation_count,
        "h_index": h_index,
        "profile_url": profile_url,
        "profile_image_url": profile_image_url,
    }


# ----------------------------------------------------------------------
# Step 3: Cursor-paginated extraction pass
# ----------------------------------------------------------------------

def extract_authors_pass(filter_str, seen_ids, records, max_total, pass_label):
    """
    Paginate through /authors using cursor pagination for the given
    filter string, extracting raw records into `records` (a list),
    skipping any OpenAlex ID already present in `seen_ids`.

    Stops when max_total records have been collected overall, or when
    there is no more data (cursor exhausted), or on repeated failures.
    """
    cursor = "*"
    page_num = 0

    while len(records) < max_total:
        page_num += 1
        print(f"[{pass_label}] fetching page {page_num} "
              f"(collected so far: {len(records)}/{max_total})...")

        data = request_with_retry(AUTHORS_ENDPOINT, params={
            "filter": filter_str,
            "per-page": PER_PAGE,
            "cursor": cursor,
        })

        time.sleep(REQUEST_DELAY_SECONDS)

        if data is None:
            print(f"[{pass_label}] request failed after retries - "
                  f"stopping this pass and continuing with what we have.")
            break

        results = data.get("results", [])
        if not results:
            print(f"[{pass_label}] no more results - pass complete.")
            break

        for author in results:
            if len(records) >= max_total:
                break

            oid = author.get("id", "")
            if not oid or oid in seen_ids:
                continue  # already extracted in an earlier pass

            record = extract_author_record(author)
            records.append(record)
            seen_ids.add(oid)

        meta = data.get("meta", {}) or {}
        next_cursor = meta.get("next_cursor")
        if not next_cursor:
            print(f"[{pass_label}] reached end of results (no next_cursor).")
            break
        cursor = next_cursor

    return records


# ----------------------------------------------------------------------
# Step 4: Write raw CSV
# ----------------------------------------------------------------------

def write_csv(records, output_path):
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for record in records:
            writer.writerow(record)
    print(f"\nSaved {len(records)} researcher records to '{output_path}'")


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------

def main():
    print("=" * 70)
    print("OpenAlex Researcher Profile Extractor (Extract-stage ONLY)")
    print("=" * 70)

    records = []
    seen_ids = set()

    # ---- Pass 1: Moroccan authors matching prioritized AI-related topics
    concept_ids = resolve_concept_ids(AI_RELATED_TOPIC_NAMES)

    if concept_ids:
        concept_filter = "|".join(concept_ids)
        filter_pass1 = (
            f"last_known_institutions.country_code:{COUNTRY_CODE},"
            f"x_concepts.id:{concept_filter}"
        )
        extract_authors_pass(
            filter_str=filter_pass1,
            seen_ids=seen_ids,
            records=records,
            max_total=MAX_RESEARCHERS,
            pass_label="Pass 1: MA + AI-related topics",
        )
    else:
        print("[warn] No AI-related concept IDs resolved - skipping "
              "topic-prioritized pass, proceeding directly to broad "
              "Moroccan-author extraction.")

    # ---- Pass 2: If under quota, top up with any Moroccan-affiliated
    #      researchers (no topic restriction). This ensures we still
    #      reach up to 2000 total researchers if the AI-topic pool
    #      is smaller than that.
    if len(records) < MAX_RESEARCHERS:
        print(f"\nOnly {len(records)} records from Pass 1. "
              f"Topping up with broader Moroccan-affiliated researchers...")
        filter_pass2 = f"last_known_institutions.country_code:{COUNTRY_CODE}"
        extract_authors_pass(
            filter_str=filter_pass2,
            seen_ids=seen_ids,
            records=records,
            max_total=MAX_RESEARCHERS,
            pass_label="Pass 2: MA (broad)",
        )

    if not records:
        print("\nNo researchers were extracted. OpenAlex may be unreachable "
              "or returned no matches. Saving an empty CSV with headers only.")

    write_csv(records, OUTPUT_CSV)

    print("=" * 70)
    print(f"Done. Total researchers extracted: {len(records)}")
    print("=" * 70)


if __name__ == "__main__":
    main()