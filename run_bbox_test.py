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
    if len(sys.argv) < 4:
        print("Usage: python run.py <id> <bbox> <scale>")
        print('Example command format: "python run.py by_101125 776802,5362130,777032,5362360 1000"')
        sys.exit(1)
    #print("Comming soon")
    #sys.exit(1)

    parser = argparse.ArgumentParser()
    parser.add_argument("id", help="Run ID")
    parser.add_argument("bbox", help="Bounding box")
    parser.add_argument("scale", type=int, help="Scale")
    parser.add_argument(
        "--merge-building-labels",
        action="store_true",
        help="Merge building labels (default: false)"
    )
    parser.add_argument(
        "--db-type",
        choices=["DXF", "PDF"],
        default="PDF",
        help="Database type (DXF or PDF, default: PDF)"
    )
    parser.add_argument(
        "--start-script",
        choices=["", "building", "parcel", "street", "nas"],
        default="",
        help="Start Script"
    )
    parser.add_argument(
        "--end-script",
        default="",
        help="Stop after running the script whose filename matches this value (e.g. 'labeling/parcel.py')"
    )
    parser.add_argument(
        "--sync-parcel",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Run 2_rest_different_parcel.py + 2_rest_sync_parcel.py (use --no-sync-parcel to skip, default: true)"
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Debug mode: skip running 8_drop_tables.py"
    )

    args = parser.parse_args()

    run_id = args.id
    bbox_arg = args.bbox
    scale = args.scale
    merge_building_labels = args.merge_building_labels
    db_type = args.db_type
    start_script = args.start_script
    end_script = args.end_script
    sync_parcel = args.sync_parcel
    debug = args.debug

    new_filename = f"settings_{run_id}.ini"
    os.makedirs(cfg.backup_ini_path, exist_ok=True)
    save_path = os.path.join(cfg.backup_ini_path, new_filename)
    # if os.path.exists(save_path):
    #     print(f"[ERROR] settings file for ID '{run_id}' already exists.")
    #     print(f"   Path: {save_path}")
    #     print("   Please choose a different ID or delete the existing file.")
    #     return
    if os.path.exists(save_path):
        os.remove(save_path)
    
    # Parse bbox string "minx,miny,maxx,maxy"
    try:
        parts = [p.strip() for p in bbox_arg.split(",")]
        if len(parts) != 4:
            raise ValueError("BBox must have exactly 4 values")

        minx, miny, maxx, maxy = map(float, parts)
        # Optional sanity check
        if minx >= maxx or miny >= maxy:
            raise ValueError("BBox must satisfy minx < maxx and miny < maxy")
        input_bbox = box(minx, miny, maxx, maxy)
        minx -= 100
        miny -= 100
        maxx += 100
        maxy += 100
        bbox = box(minx, miny, maxx, maxy)
    except Exception as e:
        print(f"Invalid bbox format: {bbox_arg}")
        print("Expected: minx,miny,maxx,maxy")
        print(f"Reason: {e}")
        sys.exit(1)

    # --- Step 2: Update settings.ini ---
    base_file = Path("settings.ini")
    if not base_file.exists():
        print("[ERROR] Base settings.ini not found.")
        sys.exit(1)

    config = configparser.ConfigParser()
    config.read(base_file)

    # --- Update bbox and scale in [area] section ---
    if "area" not in config:
        config["area"] = {}

    config["area"]["input_bbox"] = ",".join(map(str, input_bbox.bounds))
    config["area"]["bbox"] = ",".join(map(str, bbox.bounds))
    config["area"]["scale"] = str(scale)

    # Update merge building labels setting
    config["labels"]["merge_building_labels"] = str(merge_building_labels).lower()

    # Update db type setting
    config["db_type"]["db_type"] = db_type

    # -- Update from bbox setting ---
    cron_setting = Path(cfg.setting_path)
    if not cron_setting.exists():
        print("There is no settings.ini file. Default parameters should set.")
    else:
        cron_config = configparser.ConfigParser()
        cron_config.read(cron_setting)
        
        dbname_building = cron_config.get("latest_update", "dbname_building", fallback=None)
        dbname_tn = cron_config.get("latest_update", "dbname_tn", fallback=None)
        dbname_vertex = cron_config.get("latest_update", "dbname_vertex", fallback=None)

        if cfg.update_building_db and dbname_building is not None and dbname_building != "":
            config["db_connection"]["dbname_building"] = dbname_building
            
        if cfg.update_tn_db and dbname_tn is not None and dbname_tn != "":
            config["db_connection"]["dbname_tn"] = dbname_tn
            
        if cfg.update_vertex_db and dbname_vertex is not None and dbname_vertex != "":
            config["db_connection"]["dbname_vertex"] = dbname_vertex
        
        # print(f"BBOX setting ini is set. dbname_building={dbname_building}, dbname_tn={dbname_tn}, dbname_vertex={dbname_vertex}")
        print(f"BBOX setting ini is set.")
        if cfg.update_building_db:
            print(f"  -> dbname_building={dbname_building} has been updated.")
        if cfg.update_tn_db:
            print(f"  -> dbname_tn={dbname_tn} has been updated.")
        if cfg.update_vertex_db:
            print(f"  -> dbname_vertex={dbname_vertex} has been updated.")

    # --- Save as settings_<id>.ini ---
    with open(save_path, "w") as f:
        config.write(f)

    print(f"[OK] Saved settings_{run_id}.ini with bbox={bbox.bounds} and scale={scale}")

    log_path = os.path.join(cfg.log_path, f"bbox_{run_id}.log")

    # Python executable to use for subprocesses. Set environment variable VENV_PYTHON
    # to the full path of the venv python (e.g. C:\\path\\to\\venv\\Scripts\\python.exe)
    PROJECT = Path(__file__).resolve().parent
    
    system = platform.system().lower()  # 'windows', 'linux', 'darwin'
    if system == "windows":
        python_exec = PROJECT / "venv" / "Scripts" / "python.exe"
    else:
        python_exec = PROJECT / "venv" / "bin" / "python"

    
    ############################################################
    # Pre-check that large difference with building geoms with lod2d vs cadastral
    ############################################################
    is_building_difference = False
    if cfg.fail_script:
        print(bbox.bounds)
        print("[RUN] Running building difference checking ...")
        # Get total building area
        connection_address = db_crud.create_connection({
            "dbname": cfg.dbname_address,
            "user": cfg.user,
            "password": cfg.password,
            "host": cfg.host,
            "port": cfg.port
        })
        query = f"""SELECT COALESCE(SUM(ST_Area(geom)), 0) FROM hausumringe WHERE ST_Intersects(ST_MakeEnvelope(%s, %s, %s, %s,25832), geom)"""
        building_area = db_crud.run_query(connection_address, query, bbox.bounds)
        building_area = building_area[0][0]
        db_crud.close_connection(connection_address)

        # Get lod2d total building area
        connection_lod = db_crud.create_connection({
            "dbname": cfg.lod_dbname,
            "user": cfg.lod_user,
            "password": cfg.lod_password,
            "host": cfg.lod_host,
            "port": cfg.lod_port
        })

        query = f"""
            SELECT COALESCE(ST_Area(ST_Union(ST_Force2D(geometry))), 0)
            FROM citydb.surface_geometry 
            WHERE geometry IS NOT NULL AND ST_Intersects(ST_MakeEnvelope(%s, %s, %s, %s,25832), geometry) 
        """
        lod_area = db_crud.run_query(connection_lod, query, bbox.bounds)
        lod_area = lod_area[0][0]
        db_crud.close_connection(connection_lod)

        print(f"Area Difference:  building: {building_area}, lod: {lod_area}")
        if abs(lod_area - building_area) >= min(lod_area, building_area) * 0.2 and abs(lod_area - building_area) > cfg.building_difference_area:
            print("\n\nThere are large difference on building data.")
            is_building_difference = True
        else:
            print("Acceptable difference on building data")

    # --- Step 3: Run another Python script with id parameter ---
    scripts = [
        "1_initialize.py",
        "2_rest_different_parcel.py",
        "2_rest_sync_parcel.py",
        "building_color/runtime.py",
        "vertex/runtime.py",
        "labeling/prepare.py",
        "labeling/prepare_parcel_street.py",
        "labeling/prepare_building.py",
        "labeling/parcel_symbols.py",
        "labeling/building.py",
        "labeling/parcel.py",
        "labeling/street.py",
        "dashed_detection/download_wms.py",
        "dashed_detection/detect_lines.py",
        "road_classification.py",
        "nas/polygon_parcel.py",
        "nas/polygon_calculated_label.py",
        "nas/polygon_road_classification.py",
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
    ]

    # Map each --start-script keyword to the script it should start from.
    start_script_map = {
        "building": "labeling/building.py",
        "parcel": "labeling/parcel.py",
        "street": "labeling/street.py",
        "nas": "nas/polygon_parcel.py",
    }
    start_index = scripts.index(start_script_map[start_script]) if start_script in start_script_map else 0

    stopped_early = False
    for idx, script in enumerate(scripts):
        if not RUN_ALKIS_IMPORT and script == "7_create_alkis_db.py":
            continue
        if (not sync_parcel or not cfg.alkis_parcel_sync_wms_available) and script in ("2_rest_different_parcel.py", "2_rest_sync_parcel.py"):
            continue
        if not cfg.atlas_dashed_wms_available and script in ("dashed_detection/download_wms.py", "dashed_detection/detect_lines.py"):
            continue
        if cfg.building_source != 'runtime' and script == "building/runtime.py":
            continue
        if cfg.vertex_source != 'runtime' and script == "vertex/runtime.py":
            continue

        if idx < start_index:
            continue
        if start_script != "" and (script.startswith("dashed_detection") or script.startswith("road_classification")):
            continue

        print(f"[RUN] Running {script} with id={run_id} ...")
        try:
            # Run script synchronously and stop if it fails
            # subprocess.run(
            #     [python_exec, script, run_id],
            #     check=True,
            #     stdout=logfile,
            #     stderr=logfile
            # )
            process = subprocess.Popen(
                [python_exec, script, run_id],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, # Merge stderr into stdout
                text=True                 # Return strings instead of bytes
            )
            with open(log_path, "a") as logfile:
                # Read output line by line as it happens
                for line in process.stdout:
                    sys.stdout.write(line) # Print to console
                    logfile.write(line)    # Save to file
            
            return_code = process.wait()
            if return_code != 0:
                print(f"Error: {script} failed with exit code {return_code}")
                if return_code == 1 and script == "2_rest_different_parcel.py":
                    print("\n\nDiffering parcel detected. It will update parcel geoms automatically.")
                    # print("\n\nProcess failed: Parcel mismatch detected.")

                # sys.exit(return_code)

            print(f"[OK] {script} completed successfully.\n")
        except subprocess.CalledProcessError as e:
            print(f"[ERROR] {script} failed with error code {e.returncode}")
            sys.exit(e.returncode)

        if end_script and script == end_script:
            print(f"[STOP] Reached --end-script={end_script}. Stopping.")
            stopped_early = True
            break

    if RUN_ALKIS_IMPORT and not stopped_early:
        # --- Step 4: Run alkis commands ---
        cmd = [
            "bash",
            cfg.sh_path,
            os.path.join(cfg.lst_path, f"by_{run_id}.lst")
        ]

        subprocess.run(cmd, cwd=os.path.dirname(cfg.sh_path), check=True)

        if debug:
            print("[SKIP] --debug set, skipping copy temp tables and clean up by_cadastral")
        else:
            # --- Step 5: Copy alkis-import-generated tables into by_alkis_{run_id}, then clean up by_cadastral ---
            copy_script = "9_copy_alkis_tables.py"
            print(f"[RUN] Running {copy_script} with id={run_id} ...")
            subprocess.run([python_exec, copy_script, run_id], check=True)
            print(f"[OK] {copy_script} completed successfully.\n")

            drop_script = "8_drop_tables.py"
            print(f"[RUN] Running {drop_script} with id={run_id} ...")
            subprocess.run([python_exec, drop_script, run_id], check=True)
            print(f"[OK] {drop_script} completed successfully.\n")

    logfile.close()

    now = time.time()
    if is_building_difference:
        print(f"\n\nThere are large difference on building data. You must check manually.")
    print(f"\n\n[DONE] All scripts finished successfully! ({now - start:.2f} sec)")

if __name__ == "__main__":
    main()
