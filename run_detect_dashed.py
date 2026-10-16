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

    db_cadastral_params = {
        "host": cfg.host,
        "port": cfg.port,
        "dbname": cfg.dbname,
        "user": cfg.user,
        "password": cfg.password
    }
    conn = db_crud.create_connection(db_cadastral_params)
    # Reset dashed info
    query = f"""
        UPDATE by_bbox_building_{run_id} SET is_dashed = false
    """
    db_crud.run_query(conn, query)
    query = f"""
        UPDATE by_bbox_parcel_{run_id} SET is_dashed = false
    """
    db_crud.run_query(conn, query)
    db_crud.close_connection(conn)


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

    # --- Step 3: Run another Python script with id parameter ---
    scripts = [
        "dashed_detection/download_wms.py",
        "dashed_detection/detect_lines.py",
        "nas/polygon_parcel.py",
        "nas/polygon_building.py",
        "nas/line_boundary.py",
        "nas/line_dashed.py",
        "nas/line_arrow.py",
        "nas/label_building.py",
        "nas/label_parcel.py",
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

        #############################################
        # Update alkis db
        #############################################

        db_alkis_dbname = f"by_alkis_{run_id}"
        db_alkis_params = {
            "host": cfg.host,
            "port": cfg.port,
            "dbname": db_alkis_dbname,
            "user": cfg.user,
            "password": cfg.password
        }
        conn = db_crud.create_connection(db_alkis_params)
        # Remove polygon data which have dashed line
        query = f"""
            DELETE FROM po_polygons p
            WHERE EXISTS (
                SELECT 1
                FROM AX_BesondereFlurstuecksgrenze a
                WHERE a.gml_id = p.gml_id
            )
        """
        db_crud.run_query(conn, query)

        # Update solid line signaturnummer as solid - Not recommend
        # Update parcel solid
        query = f"""
            UPDATE po_lines
            SET signaturnummer = '2001'
            WHERE
                signaturnummer = '2006'
                AND layer = 'ax_besondereflurstuecksgrenze'
        """
        db_crud.run_query(conn, query)

        # Update building solid
        query = f"""
            UPDATE po_lines
            SET signaturnummer = '2002'
            WHERE
                signaturnummer = '2302'
                AND layer = 'ax_besonderegebaeudelinie'
        """
        db_crud.run_query(conn, query)

        db_crud.close_connection(conn)

    logfile.close()

    now = time.time()
    print(f"🎉 All scripts finished successfully! ({now - start:.2f} sec)")

if __name__ == "__main__":
    main()
