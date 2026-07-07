import sys
import pandas as pd

INPUT_CSV = "researchers_raw.csv"
OUTPUT_REPORT_CSV = "missing_data_report.csv"

# Values that should be treated as "missing" in addition to truly empty
# cells / NaN. Comparison is done case-sensitively against the literal
# strings requested; empty-cell and NaN detection is handled separately
# via pandas' own NaN machinery.
MISSING_TOKENS = ["N/A", "Unknown", "-", "NULL", "None"]

# The specific fields the report must cover, in the order requested.
FIELDS_TO_CHECK = [
    "ORCID",
    "Affiliated_Institution",
    "Country",
    "Research_Topics",
    "H_Index",
    "Citation_Count",
    "Profile_URL",
    "Profile_Image_URL",
]

# Maps the internal CSV column names above to the human-readable labels
# used in the printed / saved report.
FIELD_DISPLAY_NAMES = {
    "ORCID": "ORCID",
    "Affiliated_Institution": "Institution",
    "Country": "Country",
    "Research_Topics": "Research Topics",
    "H_Index": "H-index",
    "Citation_Count": "Citation Count",
    "Profile_URL": "Profile URL",
    "Profile_Image_URL": "Profile Image URL",
}


def load_data(csv_path: str) -> pd.DataFrame:
    """
    Load the CSV into a DataFrame without mutating the source file.
    Keeps all columns as strings (dtype=str) so that tokens like "-" or
    "N/A" aren't accidentally reinterpreted, and so numeric-looking
    columns (H_Index, Citation_Count) can still be checked against the
    same missing-token rules requested.
    """
    try:
        df = pd.read_csv(csv_path, dtype=str, keep_default_na=True)
    except FileNotFoundError:
        print(f"ERROR: Could not find '{csv_path}'. Make sure it exists in this directory.")
        sys.exit(1)
    except pd.errors.EmptyDataError:
        print(f"ERROR: '{csv_path}' is empty.")
        sys.exit(1)
    return df


def is_missing(value) -> bool:
    """
    Returns True if a single cell value should be counted as missing:
    - Actual NaN (from pandas, e.g. a truly empty cell)
    - Empty string or whitespace-only string
    - One of the explicit missing tokens: "N/A", "Unknown", "-", "NULL", "None"
    """
    if pd.isna(value):
        return True

    stripped = str(value).strip()

    if stripped == "":
        return True

    if stripped in MISSING_TOKENS:
        return True

    return False


def compute_missing_stats(df: pd.DataFrame, fields: list) -> pd.DataFrame:
    """
    For each field in `fields`, compute the missing count and missing
    percentage (relative to the total number of rows/researchers).
    Returns a DataFrame with columns: Field, Missing_Count, Missing_Percentage.
    """
    total_researchers = len(df)
    rows = []

    for field in fields:
        display_name = FIELD_DISPLAY_NAMES.get(field, field)

        if total_researchers == 0:
            missing_count = 0
        elif field not in df.columns:
            print(f"WARNING: Column '{field}' not found in {INPUT_CSV}. Treating as 100% missing.")
            missing_count = total_researchers
        else:
            missing_count = int(df[field].apply(is_missing).sum())

        missing_percentage = (
            round((missing_count / total_researchers) * 100, 1) if total_researchers > 0 else 0.0
        )

        rows.append(
            {
                "Field": display_name,
                "Missing_Count": int(missing_count),
                "Missing_Percentage": missing_percentage,
            }
        )

    return pd.DataFrame(rows, columns=["Field", "Missing_Count", "Missing_Percentage"])


def print_field_report(stats_df: pd.DataFrame, total_researchers: int) -> None:
    """
    Prints the total researcher count, followed by each field's missing
    count and percentage in the requested format.
    """
    print(f"Total Researchers: {total_researchers}")
    print("-" * 40)

    for _, row in stats_df.iterrows():
        print(row["Field"])
        print(f"Missing: {row['Missing_Count']}")
        print(f"Percentage: {row['Missing_Percentage']}%")
        print()


def print_summary(stats_df: pd.DataFrame) -> None:
    """
    Prints a summary of all fields sorted from highest to lowest missing
    percentage.
    """
    sorted_df = stats_df.sort_values(by="Missing_Percentage", ascending=False)

    print("=" * 40)
    print("SUMMARY (highest missing % to lowest)")
    print("=" * 40)

    for _, row in sorted_df.iterrows():
        print(f"{row['Field']}: {row['Missing_Count']} missing ({row['Missing_Percentage']}%)")


def save_report(stats_df: pd.DataFrame, output_path: str) -> None:
    """
    Saves the missing-data report to a CSV file, sorted from highest to
    lowest missing percentage, with columns: Field, Missing_Count,
    Missing_Percentage.
    """
    sorted_df = stats_df.sort_values(by="Missing_Percentage", ascending=False)
    sorted_df.to_csv(output_path, index=False, encoding="utf-8")
    print(f"\nReport saved to: {output_path}")


def main():
    df = load_data(INPUT_CSV)
    total_researchers = len(df)

    stats_df = compute_missing_stats(df, FIELDS_TO_CHECK)

    print_field_report(stats_df, total_researchers)
    print_summary(stats_df)
    save_report(stats_df, OUTPUT_REPORT_CSV)


if __name__ == "__main__":
    main()
