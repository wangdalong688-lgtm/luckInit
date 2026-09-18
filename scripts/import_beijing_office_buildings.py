import sqlite3
import time
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "cache.sqlite3"
TABLE_NAME = "beijing_office_buildings"
SOURCE_GLOB = "docs/*.xlsx"

NS = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
ALLOWED_LOCATION_TYPES = {"写字楼", "商场"}
OPTIONAL_GEO_COLUMNS = (
    "gcj02_longitude",
    "gcj02_latitude",
    "geo_confidence",
    "geo_method",
    "geo_query",
    "geo_match_address",
    "geo_match_level",
    "geo_score",
)


def first_matching_file(pattern: str) -> Path:
    matches = sorted(
        path for path in PROJECT_ROOT.glob(pattern)
        if not path.name.startswith("~$")
    )
    if not matches:
        raise FileNotFoundError(f"No file matched pattern: {pattern}")
    return matches[0]


def load_shared_strings(zf: zipfile.ZipFile) -> list[str]:
    try:
        root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
    except KeyError:
        return []

    values = []
    for item in root:
        text_parts = [
            node.text or ""
            for node in item.iter("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t")
        ]
        values.append("".join(text_parts))
    return values


def column_letters(cell_ref: str) -> str:
    letters = []
    for ch in cell_ref:
        if ch.isalpha():
            letters.append(ch)
        else:
            break
    return "".join(letters)


def read_first_sheet_rows(xlsx_path: Path) -> tuple[str, list[dict[str, str | None]]]:
    with zipfile.ZipFile(xlsx_path) as zf:
        workbook = ET.fromstring(zf.read("xl/workbook.xml"))
        sheets = workbook.find("main:sheets", NS)
        if sheets is None or not list(sheets):
            raise ValueError("Workbook does not contain any sheets")

        rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
        rel_map = {rel.attrib["Id"]: rel.attrib["Target"] for rel in rels}

        first_sheet = list(sheets)[0]
        sheet_name = first_sheet.attrib["name"]
        target = rel_map[first_sheet.attrib[REL_NS]]
        if not target.startswith("xl/"):
            target = f"xl/{target.lstrip('/')}"

        shared_strings = load_shared_strings(zf)
        sheet_root = ET.fromstring(zf.read(target))
        sheet_data = sheet_root.find("main:sheetData", NS)
        if sheet_data is None:
            return sheet_name, []

        rows = []
        for row in sheet_data:
            item: dict[str, str | None] = {}
            for cell in row:
                ref = cell.attrib.get("r", "")
                key = column_letters(ref)
                cell_type = cell.attrib.get("t")
                value_node = cell.find("main:v", NS)
                value = None if value_node is None else value_node.text
                if cell_type == "s" and value is not None:
                    value = shared_strings[int(value)]
                item[key] = value
            rows.append(item)
        return sheet_name, rows


def normalize_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned if cleaned else None


def normalize_number(value: str | None) -> float | int | None:
    text = normalize_text(value)
    if text in {None, "-"}:
        return None
    number = float(text)
    if number.is_integer():
        return int(number)
    return number


def build_records(sheet_name: str, rows: list[dict[str, str | None]], source_file: str) -> list[tuple]:
    if len(rows) < 3:
        return []

    records = []
    for excel_row_number, row in enumerate(rows[2:], start=3):
        location_type = normalize_text(row.get("D"))
        if location_type not in ALLOWED_LOCATION_TYPES:
            continue

        records.append(
            (
                source_file,
                sheet_name,
                excel_row_number,
                normalize_text(row.get("A")),
                normalize_text(row.get("B")),
                normalize_text(row.get("C")),
                location_type,
                normalize_text(row.get("E")),
                normalize_text(row.get("F")),
                normalize_number(row.get("G")),
                normalize_number(row.get("H")),
                normalize_number(row.get("I")),
                normalize_number(row.get("J")),
                normalize_number(row.get("K")),
                normalize_number(row.get("L")),
                normalize_number(row.get("M")),
                normalize_number(row.get("N")),
                normalize_number(row.get("O")),
                normalize_number(row.get("P")),
                normalize_number(row.get("Q")),
                normalize_number(row.get("R")),
                normalize_number(row.get("S")),
                None,
                None,
                int(time.time()),
            )
        )
    return records


def init_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_file TEXT NOT NULL,
            source_sheet TEXT NOT NULL,
            excel_row_number INTEGER NOT NULL,
            city_name TEXT,
            district TEXT,
            location_name TEXT NOT NULL,
            location_type TEXT,
            location_subtype TEXT,
            recommendation_status TEXT,
            opened_store_count INTEGER,
            work_population REAL,
            work_target_consumption REAL,
            work_target_industry_ratio REAL,
            daily_traffic REAL,
            traffic_target_consumption REAL,
            traffic_high_end_phone_ratio REAL,
            student_population REAL,
            student_target_consumption REAL,
            student_female_ratio REAL,
            throughput REAL,
            target_consumption_traffic REAL,
            daily_delivery_orders REAL,
            longitude REAL,
            latitude REAL,
            imported_at INTEGER NOT NULL,
            UNIQUE(source_file, source_sheet, excel_row_number)
        )
        """
    )
    conn.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_{TABLE_NAME}_district
        ON {TABLE_NAME}(district)
        """
    )
    conn.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_{TABLE_NAME}_location_name
        ON {TABLE_NAME}(location_name)
        """
    )


def get_existing_columns(conn: sqlite3.Connection) -> set[str]:
    return {
        row[1]
        for row in conn.execute(f"PRAGMA table_info({TABLE_NAME})").fetchall()
    }


def load_preserved_geo_values(
    conn: sqlite3.Connection,
    available_columns: set[str],
) -> dict[tuple[str, str, int], tuple]:
    geo_columns = [name for name in OPTIONAL_GEO_COLUMNS if name in available_columns]
    if not geo_columns:
        return {}

    rows = conn.execute(
        f"""
        SELECT source_file, source_sheet, excel_row_number, {", ".join(geo_columns)}
        FROM {TABLE_NAME}
        """
    ).fetchall()
    preserved: dict[tuple[str, str, int], tuple] = {}
    for row in rows:
        key = (row[0], row[1], row[2])
        values_by_name = {
            name: row[index + 3]
            for index, name in enumerate(geo_columns)
        }
        preserved[key] = tuple(values_by_name.get(name) for name in OPTIONAL_GEO_COLUMNS)
    return preserved


def replace_table_data(conn: sqlite3.Connection, records: list[tuple]) -> None:
    available_columns = get_existing_columns(conn)
    preserved_geo = load_preserved_geo_values(conn, available_columns)
    insert_columns = [
        "source_file",
        "source_sheet",
        "excel_row_number",
        "city_name",
        "district",
        "location_name",
        "location_type",
        "location_subtype",
        "recommendation_status",
        "opened_store_count",
        "work_population",
        "work_target_consumption",
        "work_target_industry_ratio",
        "daily_traffic",
        "traffic_target_consumption",
        "traffic_high_end_phone_ratio",
        "student_population",
        "student_target_consumption",
        "student_female_ratio",
        "throughput",
        "target_consumption_traffic",
        "daily_delivery_orders",
        "longitude",
        "latitude",
    ]
    geo_insert_columns = [name for name in OPTIONAL_GEO_COLUMNS if name in available_columns]
    if geo_insert_columns:
        insert_columns.extend(geo_insert_columns)
    insert_columns.append("imported_at")

    enriched_records = []
    for record in records:
        key = (record[0], record[1], record[2])
        geo_values = preserved_geo.get(key, (None,) * len(OPTIONAL_GEO_COLUMNS))
        geo_values_by_name = {
            name: geo_values[index]
            for index, name in enumerate(OPTIONAL_GEO_COLUMNS)
        }
        extra_values = [geo_values_by_name.get(name) for name in geo_insert_columns]
        enriched_records.append(record[:-1] + tuple(extra_values) + (record[-1],))

    conn.execute(f"DELETE FROM {TABLE_NAME}")
    conn.executemany(
        f"""
        INSERT INTO {TABLE_NAME} (
            {", ".join(insert_columns)}
        ) VALUES ({", ".join("?" for _ in insert_columns)})
        """,
        enriched_records,
    )


def main() -> None:
    xlsx_path = first_matching_file(SOURCE_GLOB)
    sheet_name, rows = read_first_sheet_rows(xlsx_path)
    records = build_records(sheet_name, rows, xlsx_path.name)

    with sqlite3.connect(DB_PATH) as conn:
        init_table(conn)
        replace_table_data(conn, records)
        conn.commit()

        total = conn.execute(f"SELECT COUNT(*) FROM {TABLE_NAME}").fetchone()[0]
        with_coords = conn.execute(
            f"SELECT COUNT(*) FROM {TABLE_NAME} WHERE longitude IS NOT NULL AND latitude IS NOT NULL"
        ).fetchone()[0]

    print(f"source_file={xlsx_path.name}")
    print(f"source_sheet={sheet_name}")
    print(f"imported_rows={total}")
    print(f"rows_with_coordinates={with_coords}")


if __name__ == "__main__":
    main()
