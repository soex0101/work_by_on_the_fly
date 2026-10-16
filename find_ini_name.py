import argparse
import configparser
from pathlib import Path
from typing import List, Tuple

SETTINGS_BACKUP_DIR = Path(__file__).resolve().parent / "settings_backup"


def parse_bbox(value: str) -> Tuple[float, float, float, float]:
    parts = [p.strip() for p in value.split(",")]
    x_min, y_min, x_max, y_max = (float(p) for p in parts)
    return x_min, y_min, x_max, y_max


def parse_point(value: str) -> Tuple[float, float]:
    parts = [p.strip() for p in value.split(",")]
    x, y = (float(p) for p in parts)
    return x, y


def find_ini_name(x: float, y: float, settings_backup_dir: Path) -> List[str]:
    matches = []
    for ini_path in sorted(settings_backup_dir.glob("*.ini")):
        cfg = configparser.ConfigParser()
        cfg.read(ini_path)
        if not cfg.has_option("area", "input_bbox"):
            continue

        x_min, y_min, x_max, y_max = parse_bbox(cfg.get("area", "input_bbox"))
        if x_min - 5 <= x <= x_max + 5 and y_min - 5 <= y <= y_max + 5:
            matches.append(ini_path.name)

    return matches


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("point", type=parse_point, help='point as "x,y"')
    args = parser.parse_args()

    x, y = args.point
    matches = find_ini_name(x, y, SETTINGS_BACKUP_DIR)

    if not matches:
        print("No matching ini file found")
        return

    for name in matches:
        print(name)


if __name__ == "__main__":
    main()
