import sys
import configparser
import subprocess
from shapely.geometry import box
from pathlib import Path
from labeling import db_crud
from read_settings import load_config
import os
import platform
import argparse
import time

RUN_ALKIS_IMPORT = True

def main():
    start = time.time()

    cfg = load_config()

    # --- Step 1: Parse command-line arguments ---
    if len(sys.argv) < 2:
        print("Usage: python run.py <id>")
        print('Example command format: "python run.py by_101125"')
        sys.exit(1)

    parser = argparse.ArgumentParser()
    parser.add_argument("id", help="Run ID")

    args = parser.parse_args()

    run_id = args.id
    cfg = load_config(run_id)


    log_path = os.path.join(cfg.log_path, f"bbox_{run_id}.log")
    logfile = open(log_path, "a", encoding="utf-8")

    # Python executable to use for subprocesses. Set environment variable VENV_PYTHON
    # to the full path of the venv python (e.g. C:\\path\\to\\venv\\Scripts\\python.exe)
    PROJECT = Path(__file__).resolve().parent
    
    system = platform.system().lower()  # 'windows', 'linux', 'darwin'
    if system == "windows":
        python_exec = PROJECT / "venv" / "Scripts" / "python.exe"
    else:
        python_exec = PROJECT / "venv" / "bin" / "python"

    db_cadastral_params = {
        "host": cfg.host,
        "port": cfg.port,
        "dbname": "by_cadastral",
        "user": cfg.user,
        "password": cfg.password
    }
    conn = db_crud.create_connection(db_cadastral_params)
    query = f"""
        DELETE FROM by_bbox_labels_{run_id};
    """
    db_crud.run_query(conn, query)
    query = f"""
        DELETE FROM by_bbox_lines_{run_id};
    """
    db_crud.run_query(conn, query)
    db_crud.close_connection(conn)

    # --- Step 3: Run another Python script with id parameter ---
    scripts = [
        # "1_initialize.py",
        # "labeling/prepare.py",
        "labeling/prepare_parcel_street.py",
        # "labeling/prepare_building.py",
        "labeling/building.py",
        # "labeling/parcel_symbols.py",
        # "labeling/parcel.py",
        "labeling/street.py",
        "nas/polygon_parcel.py",
        # "nas/polygon_building.py",
        "nas/line_arrow.py",
        "nas/label_building.py",
        "nas/label_parcel.py",
        "nas/polygon_calculated_label.py",
        "nas/vertex.py",
        "nas/symbols.py",
        "nas/make_lst.py",
        "7_create_alkis_db.py",
        "8_drop_tables.py"
    ]

    for script in scripts:
        if not RUN_ALKIS_IMPORT and script == "7_create_alkis_db.py":
            continue
        if not cfg.drop_temp_tables and script == "8_drop_tables.py":
            continue

        print(f"🚀 Running {script} with id={run_id} ...")
        try:
            # Run script synchronously and stop if it fails
            subprocess.run(
                [python_exec, script, run_id],
                check=True,
                stdout=logfile,
                stderr=logfile
            )
            print(f"✅ {script} completed successfully.\n")
        except subprocess.CalledProcessError as e:
            print(f"❌ {script} failed with error code {e.returncode}")
            sys.exit(e.returncode)

    if RUN_ALKIS_IMPORT:
        # --- Step 4: Run alkis commands ---
        cmd = [
            "bash",
            cfg.sh_path,
            os.path.join(cfg.lst_path, f"by_{run_id}.lst")
        ]

        subprocess.run(cmd, cwd=os.path.dirname(cfg.sh_path), check=True)


    logfile.close()

    now = time.time()
    print(f"🎉 All scripts finished successfully! ({now - start:.2f} sec)")

if __name__ == "__main__":
    main()
