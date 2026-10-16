import subprocess, shutil
import os
import sys
from read_settings import load_config
import common

def main(run_id):
    cfg = load_config()

    download_name = os.path.join(cfg.gpkg_out_dir, f"nutzung_kreis_{run_id}.gpkg")

    # Find the ogr2ogr executable (works on macOS/Linux/Windows)
    ogr2ogr = shutil.which("ogr2ogr") or shutil.which("ogr2ogr.exe")
    if not ogr2ogr:
        raise RuntimeError("ogr2ogr not found on PATH. Install GDAL and ensure 'ogr2ogr' is on PATH.")

    dbname = f"{cfg.dbname_tn}_{run_id}"
    # Create
    common.create_db(cfg, dbname)

    cmd = [
        ogr2ogr,
        "-f", "PostgreSQL",
        # import special characters in table name
        "-lco", "LAUNDER=YES",
        "-lco", "SCHEMA=public",
        "-lco", "OVERWRITE=YES",
        "-lco", "GEOMETRY_NAME=geom",
        "-nlt", "PROMOTE_TO_MULTI",
        # NOTE: as a single list element so spaces don't get split
        f"PG:host={cfg.host} dbname={dbname} user={cfg.user} password={cfg.password}",
        download_name
    ]

    if not os.path.exists(cfg.log_path):
        os.makedirs(cfg.log_path)

    org_log_path = os.path.join(cfg.log_path, f"tn_import_{run_id}.log")
    with open(org_log_path, "w", encoding="utf-8") as log:
        result = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT, text=True)

    print("ogr2ogr done with exit code:", result.returncode, f"— see {org_log_path}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python 3_ogr.py <id>")
        print('Example command format: "python 3_ogr.py 20251113_050000"')
        sys.exit(1)

    run_id = sys.argv[1]

    main(run_id)