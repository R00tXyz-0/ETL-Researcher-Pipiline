#!/usr/bin/env python3
import csv

INPUT_CSV = "PrincipalReserchersDataClean.csv"
OUTPUT_CSV = "FinalData.csv"
COUNTRY_CODE = "MA"
DROP_COLUMN = "profile_image_url"


def main():
    with open(INPUT_CSV, newline="", encoding="utf-8-sig") as f_in:
        reader = csv.DictReader(f_in)
        fieldnames = [c for c in reader.fieldnames if c != DROP_COLUMN]
        rows = [row for row in reader if row.get("country") == COUNTRY_CODE]

    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f_out:
        writer = csv.DictWriter(f_out, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: v for k, v in row.items() if k != DROP_COLUMN})

    print(f"Wrote {len(rows)} rows, {len(fieldnames)} columns to {OUTPUT_CSV}")


if __name__ == "__main__":
    main()