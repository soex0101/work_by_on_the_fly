from datetime import datetime
import sys
from multiprocessing import Pool
import subprocess
import common
from read_settings import load_config
import os

cfg = load_config()

USE_MULTIPROCESS = True

# Script groups (each group runs in its own process)
script_groups = [
    # ["3_download_tn.py", "3_ogr.py", "3_tn_to_cadastral.py"], # tn
    # ["4_shp.py"],                                             # building
    ["building_color/1_download_wms.py", "building_color/2_detect_building_color.py"],        # building color
    ["vertex/scrap.py", "vertex/main.py"]                       # vertex
]

def run_script_group(scripts, group_id):
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")

    first_script = ""
    last_script = ""
    for script in scripts:
        print(f"🚀 Running {script} with id={run_id} ...")

        script_name = os.path.splitext(script)[0]
        log_path = os.path.join(cfg.log_path, f"{script_name.replace('/', '_')}_{run_id}.log")
        logfile = open(log_path, "a", encoding="utf-8")

        if first_script == "":
            first_script = script
        last_script = script

        try:
            subprocess.run(
                ["python", script, run_id],
                check=True,
                stdout=logfile,
                stderr=logfile
            )
            print(f"✅ {script} completed successfully.\n")

            if script == "3_ogr.py":
                common.update_cron_setting(cfg.setting_path, "dbname_tn", f"{cfg.dbname_tn}_{run_id}")
            elif script == "6_detect_building_color.py":
                common.update_cron_setting(cfg.setting_path, "dbname_building", f"{cfg.dbname_building}_{run_id}")
            elif script == "vertex/main.py":
                common.update_cron_setting(cfg.setting_path, "dbname_vertex", f"{cfg.dbname_vertex}_{run_id}")

        except subprocess.CalledProcessError as e:
            print(f"❌ {script} failed with error code {e.returncode}")
            sys.exit(e.returncode)

    logfile.close()
    
    print(f"🎉 All scripts in {first_script} - {last_script} group finished successfully!")

if __name__ == "__main__":
    if USE_MULTIPROCESS:
        print("⚡ Running in MULTI-PROCESS mode...")
        group_inputs = [(group, idx + 1) for idx, group in enumerate(script_groups)]

        with Pool(cfg.cpu_cores) as pool:
            pool.starmap(run_script_group, group_inputs)

        print("\n✨ All processes completed!")

    else:
        print("🐢 Running in SINGLE-PROCESS mode...")
        # Flatten list of lists into one list
        all_scripts = [script for group in script_groups for script in group]
        run_script_group(all_scripts)

        print("\n✨ All processes completed!")