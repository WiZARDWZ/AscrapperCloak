
import argparse
import csv
import re
from pathlib import Path

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db_layer import connect


PERSIAN_DIGITS = str.maketrans("????????????????????", "01234567890123456789")


def normalize_digits(value: str) -> str:
    return str(value or "").translate(PERSIAN_DIGITS)


def clean_postcode(value) -> str:
    text = normalize_digits(str(value or "")).strip()
    if text.endswith(".0"):
        text = text[:-2]
    match = re.search(r"\b(\d{4})\b", text)
    return match.group(1) if match else text


def clean_suburb_name(value) -> str:
    text = str(value or "").strip()
    text = re.sub(r"\s+", " ", text)
    return text.title()


def normalize_name(value) -> str:
    text = normalize_digits(str(value or "")).lower()
    text = re.sub(r"\b(nsw|n\.s\.w|australia)\b", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def compact_name(value) -> str:
    return re.sub(r"[^a-z0-9]+", "", normalize_name(value))


def ensure_table(conn):
    cur = conn.cursor()

    cur.execute("""
    IF OBJECT_ID(N'dbo.NSWSuburbDirectory', N'U') IS NULL
    BEGIN
        CREATE TABLE dbo.NSWSuburbDirectory (
            SuburbID INT IDENTITY(1,1) NOT NULL PRIMARY KEY,
            SuburbName NVARCHAR(200) NOT NULL,
            Postcode NVARCHAR(10) NOT NULL,
            StateCode NVARCHAR(10) NOT NULL CONSTRAINT DF_NSWSuburbDirectory_StateCode DEFAULT 'NSW',
            NormalizedName NVARCHAR(200) NOT NULL,
            CompactName NVARCHAR(200) NOT NULL,
            SearchLabel NVARCHAR(260) NOT NULL,
            IsActive BIT NOT NULL CONSTRAINT DF_NSWSuburbDirectory_IsActive DEFAULT 1,
            CreatedAt DATETIME2 NOT NULL CONSTRAINT DF_NSWSuburbDirectory_CreatedAt DEFAULT SYSDATETIME(),
            UpdatedAt DATETIME2 NOT NULL CONSTRAINT DF_NSWSuburbDirectory_UpdatedAt DEFAULT SYSDATETIME()
        );

        CREATE UNIQUE INDEX UX_NSWSuburbDirectory_Suburb_Postcode_State
        ON dbo.NSWSuburbDirectory(SuburbName, Postcode, StateCode);
    END
    """)

    conn.commit()


def import_csv(csv_path: str):
    path = Path(csv_path)
    if not path.exists():
        raise FileNotFoundError(f"CSV not found: {path.resolve()}")

    conn = connect()
    ensure_table(conn)
    cur = conn.cursor()

    inserted = 0
    updated = 0
    skipped = 0

    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)

        if not reader.fieldnames:
            raise RuntimeError("CSV has no header row")

        field_map = {name.lower().strip(): name for name in reader.fieldnames}

        suburb_col = field_map.get("suburbname") or field_map.get("suburb") or field_map.get("name")
        postcode_col = field_map.get("postcode") or field_map.get("post_code")

        if not suburb_col or not postcode_col:
            raise RuntimeError(f"CSV must contain suburbname and postcode columns. Found: {reader.fieldnames}")

        for row in reader:
            suburb = clean_suburb_name(row.get(suburb_col))
            postcode = clean_postcode(row.get(postcode_col))

            if not suburb or not postcode:
                skipped += 1
                continue

            normalized = normalize_name(suburb)
            compact = compact_name(suburb)
            label = f"{suburb}, NSW {postcode}"

            cur.execute("""
            SELECT SuburbID
            FROM dbo.NSWSuburbDirectory
            WHERE SuburbName = ? AND Postcode = ? AND StateCode = 'NSW'
            """, suburb, postcode)

            existing = cur.fetchone()

            if existing:
                cur.execute("""
                UPDATE dbo.NSWSuburbDirectory
                SET NormalizedName = ?,
                    CompactName = ?,
                    SearchLabel = ?,
                    IsActive = 1,
                    UpdatedAt = SYSDATETIME()
                WHERE SuburbID = ?
                """, normalized, compact, label, existing[0])
                updated += 1
            else:
                cur.execute("""
                INSERT INTO dbo.NSWSuburbDirectory
                    (SuburbName, Postcode, StateCode, NormalizedName, CompactName, SearchLabel, IsActive)
                VALUES
                    (?, ?, 'NSW', ?, ?, ?, 1)
                """, suburb, postcode, normalized, compact, label)
                inserted += 1

    conn.commit()
    conn.close()

    print({
        "status": "ok",
        "inserted": inserted,
        "updated": updated,
        "skipped": skipped,
        "csv": str(path)
    })


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", required=True)
    args = parser.parse_args()
    import_csv(args.csv)


if __name__ == "__main__":
    main()
