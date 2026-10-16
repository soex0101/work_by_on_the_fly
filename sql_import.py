# Import sql files in windows
import argparse
import os
import subprocess
from pathlib import Path

from labeling import db_crud


def run_psql_import(suffix: str) -> None:
    base_dir = Path("download/table")
    jobs = [
        base_dir / f"{suffix}_address.sql",
        base_dir / f"{suffix}_parcel.sql",
        base_dir / f"{suffix}_parcel_street.sql",
        base_dir / f"{suffix}_building.sql",
        base_dir / f"{suffix}_building_labels.sql",
        base_dir / f"{suffix}_building_color.sql",
        base_dir / f"{suffix}_landkreis.sql",
        base_dir / f"{suffix}_labels.sql",
        base_dir / f"{suffix}_lines.sql",
        base_dir / f"{suffix}_symbols.sql",
        base_dir / f"{suffix}_parcel_changed.sql",
        base_dir / f"{suffix}_parcel_check.sql",
        base_dir / f"{suffix}_parcel_confirmed.sql",
        base_dir / f"{suffix}_parcel_sync.sql",
        base_dir / f"{suffix}_vertex.sql",
    ]

    env = os.environ.copy()
    env["PGPASSWORD"] = "mXV5UJBVfP9m83"

    for sql_file in jobs:
        if not sql_file.exists():
            print(f"Skipped (SQL file not found): {sql_file}")
            continue

        cmd = [
            "C:\\Program Files\\PostgreSQL\\16\\bin\\psql.exe",
            "-h", "localhost",
            "-U", "alkis",
            "-d", "by_cadastral",
            "-f", str(sql_file),
        ]

        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )

        if result.returncode != 0:
            raise RuntimeError(
                f"Import failed for {sql_file}:\n{result.stderr}"
            )

        print(f"Imported: {sql_file}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("suffix", help='file prefix, e.g. "002"')
    args = parser.parse_args()

    suffix = args.suffix

    conn = db_crud.create_connection({
        "dbname": "by_cadastral",
        "user": "alkis",
        "password": "mXV5UJBVfP9m83",
        "host": "localhost",
        "port": "5432"
    })
    
    db_crud.drop_table_if_exist(conn, f"by_bbox_address_{suffix}")
    db_crud.drop_table_if_exist(conn, f"by_bbox_parcel_{suffix}")
    db_crud.drop_table_if_exist(conn, f"by_bbox_parcel_street_{suffix}")
    db_crud.drop_table_if_exist(conn, f"by_bbox_building_{suffix}")
    db_crud.drop_table_if_exist(conn, f"by_bbox_building_color_{suffix}")
    db_crud.drop_table_if_exist(conn, f"by_bbox_building_labels_{suffix}")
    db_crud.drop_table_if_exist(conn, f"by_bbox_landkreis_{suffix}")
    db_crud.drop_table_if_exist(conn, f"by_bbox_labels_{suffix}")
    db_crud.drop_table_if_exist(conn, f"by_bbox_lines_{suffix}")
    db_crud.drop_table_if_exist(conn, f"by_bbox_symbols_{suffix}")
    db_crud.drop_table_if_exist(conn, f"by_bbox_sync_parcel_changed_{suffix}")
    # db_crud.drop_table_if_exist(conn, f"by_bbox_sync_parcel_crosswalk_{suffix}")
    # db_crud.drop_table_if_exist(conn, f"by_bbox_sync_parcel_new_{suffix}")
    # db_crud.drop_table_if_exist(conn, f"by_bbox_sync_parcel_not_changed_{suffix}")
    db_crud.drop_table_if_exist(conn, f"by_bbox_vertex_{suffix}")
    db_crud.close_connection(conn)

    run_psql_import(suffix)


if __name__ == "__main__":
    main()