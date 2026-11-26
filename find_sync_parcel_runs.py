#!/usr/bin/env python3
"""
Run 2_rest_sync_parcel.py for every run in settings_backup and print the run ids
for which it returned 1, i.e. the runs whose parcels still need to be synced.

The run id of settings_backup/settings_<run_id>.ini is <run_id>.

Usage (from the by_on_the_fly directory):
    python find_sync_parcel_runs.py
"""

import subprocess
import sys
from pathlib import Path

from read_settings import load_config

PROJECT = Path(__file__).resolve().parent
SYNC_SCRIPT = "2_rest_sync_parcel.py"


def main():
    backup_dir = PROJECT / load_config().backup_ini_path
    run_ids = [path.stem[len("settings_"):] for path in sorted(backup_dir.glob("settings_*.ini"))]
    print(f"{len(run_ids)} runs in {backup_dir}")

    failed_run_ids = []
    for run_id in run_ids:
        print(f"[RUN] {SYNC_SCRIPT} {run_id}", flush=True)
        return_code = subprocess.run([sys.executable, SYNC_SCRIPT, run_id], cwd=PROJECT).returncode
        print(f"[RUN] {run_id} returned {return_code}", flush=True)
        if return_code == 1:
            failed_run_ids.append(run_id)

    print(f"\n{len(failed_run_ids)} runs returned 1:")
    for run_id in failed_run_ids:
        print(run_id)


if __name__ == "__main__":
    main()
