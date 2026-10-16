#!/usr/bin/env python3

import argparse
import shutil
from pathlib import Path

from read_settings import load_config

def delete_file(file_path: Path) -> None:
    if file_path.is_file() or file_path.is_symlink():
        file_path.unlink()
        print(f"Deleted file: {file_path}")
    else:
        print(f"Not found: {file_path}")


def delete_folder(folder_path: Path) -> None:
    if folder_path.is_dir():
        shutil.rmtree(folder_path)
        print(f"Deleted folder: {folder_path}")
    else:
        print(f"Not found: {folder_path}")


def delete_matching_files(folder: Path, pattern: str) -> None:
    matched_files = list(folder.glob(pattern))

    if not matched_files:
        print(f"No matching files: {folder / pattern}")
        return

    for file_path in matched_files:
        if file_path.is_file() or file_path.is_symlink():
            file_path.unlink()
            print(f"Deleted file: {file_path}")


def validate_run_id(run_id: str) -> str:
    if not run_id:
        raise ValueError("run_id cannot be empty")

    if not all(char.isalnum() or char in "_-" for char in run_id):
        raise ValueError(
            "run_id may contain only letters, numbers, underscores, and hyphens"
        )

    return run_id


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Delete generated files related to a run ID."
    )
    parser.add_argument("run_id", help="Run ID, for example: s0115")
    args = parser.parse_args()

    run_id = validate_run_id(args.run_id)
    cfg = load_config()

    # Script is assumed to be located in the project root.
    base_dir = Path(__file__).resolve().parent

    # logs/bbox_{run_id}.log
    delete_file(
        base_dir / cfg.log_path / f"bbox_{run_id}.log"
    )

    # download/building_color/{run_id}/
    delete_folder(
        base_dir / cfg.building_path / run_id
    )

    # download/dashed_line_detection/{run_id}_*.png
    delete_matching_files(
        base_dir / cfg.dashed_wms_path,
        f"{run_id}_*.png"
    )

    # download/vertex/{run_id}/
    delete_folder(
        base_dir / cfg.vertex_path / run_id
    )

    # alkis_xml/by_{run_id}.lst
    lst_dir = Path(cfg.lst_path)
    delete_file(
        lst_dir / f"by_{run_id}.lst"
    )

    # alkis_xml/xml/{run_id}_*.xml
    xml_dir = Path(cfg.xml_path)
    delete_matching_files(
        xml_dir,
        f"{run_id}_*.xml"
    )

    # settings_backup/settings_{run_id}.ini
    delete_file(
        base_dir / cfg.backup_ini_path / f"settings_{run_id}.ini"
    )


if __name__ == "__main__":
    main()