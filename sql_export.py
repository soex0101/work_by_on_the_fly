import argparse
import os
import subprocess
from pathlib import Path


def table_exists(table_name: str, db_name: str, env: dict) -> bool:
    cmd = [
        "psql",
        "-U", "alkis",
        "-d", db_name,
        "-tAc", f"SELECT to_regclass('public.{table_name}') IS NOT NULL",
    ]

    result = subprocess.run(
        cmd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"Failed to check existence of table {table_name}:\n{result.stderr}"
        )

    return result.stdout.strip() == "t"


def run_pg_dump(suffix: str, password: str, debug: bool = False) -> None:
    out_dir = Path("download/table")
    out_dir.mkdir(parents=True, exist_ok=True)

    jobs = [
        (
            f"by_bbox_address_{suffix}",
            out_dir / f"{suffix}_address.sql",
        ),
        (
            f"by_bbox_parcel_{suffix}",
            out_dir / f"{suffix}_parcel.sql",
        ),
        (
            f"by_bbox_parcel_street_{suffix}",
            out_dir / f"{suffix}_parcel_street.sql",
        ),
        (
            f"by_bbox_building_{suffix}",
            out_dir / f"{suffix}_building.sql",
        ),
        (
            f"by_bbox_building_labels_{suffix}",
            out_dir / f"{suffix}_building_labels.sql",
        ),
        (
            f"by_bbox_building_color_{suffix}",
            out_dir / f"{suffix}_building_color.sql",
        ),
        (
            f"by_bbox_landkreis_{suffix}",
            out_dir / f"{suffix}_landkreis.sql",
        ),
        (
            f"by_bbox_labels_{suffix}",
            out_dir / f"{suffix}_labels.sql",
        ),
        (
            f"by_bbox_lines_{suffix}",
            out_dir / f"{suffix}_lines.sql",
        ),
        (
            f"by_bbox_symbols_{suffix}",
            out_dir / f"{suffix}_symbols.sql",
        ),
        (
            f"by_bbox_parcel_changed_{suffix}",
            out_dir / f"{suffix}_parcel_changed.sql",
        ),
        (
            f"by_bbox_parcel_check_{suffix}",
            out_dir / f"{suffix}_parcel_check.sql",
        ),
        (
            f"by_bbox_parcel_confirmed_{suffix}",
            out_dir / f"{suffix}_parcel_confirmed.sql",
        ),
        (
            f"by_bbox_parcel_sync_{suffix}",
            out_dir / f"{suffix}_parcel_sync.sql",
        ),
        (
            f"by_bbox_vertex_{suffix}",
            out_dir / f"{suffix}_vertex.sql",
        ),
    ]

    db_name = "by_cadastral" if debug else f"by_alkis_{suffix}"

    env = os.environ.copy()
    env["PGPASSWORD"] = password

    for table_name, output_file in jobs:
        if not table_exists(table_name, db_name, env):
            print(f"Skipped (table not found): {table_name}")
            continue

        cmd = [
            "pg_dump",
            "-U", "alkis",
            "-d", db_name,
            "-t", table_name,
        ]

        with open(output_file, "w", encoding="utf-8") as f:
            result = subprocess.run(
                cmd,
                env=env,
                stdout=f,
                stderr=subprocess.PIPE,
                text=True,
            )

        if result.returncode != 0:
            raise RuntimeError(
                f"pg_dump failed for table {table_name}:\n{result.stderr}"
            )

        print(f"Created: {output_file}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("suffix", help='table suffix, e.g. "002"')
    parser.add_argument("--debug", action="store_true", help="use by_cadastral db instead of by_alkis_{suffix}")
    args = parser.parse_args()

    run_pg_dump(args.suffix, "mXV5UJBVfP9m83", args.debug)


if __name__ == "__main__":
    main()
