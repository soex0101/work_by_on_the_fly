#!/usr/bin/env python3
"""
2_fetch_sync_parcel.py

Synchronous companion to 2_fetch_different_parcel.py.

Driven by the coordinates collected in by_bbox_parcel_changed by
2_fetch_different_parcel.py:
  - Read (rw, hw, id) from by_bbox_parcel_changed_<run_id>.
  - Find the by_bbox_parcel_<run_id> geometry that CONTAINS (rw, hw) and sample
    it on a regular grid (sample_step_m meters apart, from settings.ini), keeping only the grid
    points that fall inside the polygon.
  - Request the cadastral REST API *by coordinate* (.../koor/<x>/<y>) for every
    sample point, so an old parcel that has since been split into several new
    parcels is discovered in full (not just the one parcel under (rw, hw)).
  - Parse each returned parcel's name + geometry (ewkt) and save the UNIQUE
    parcels into by_bbox_parcel_sync_<run_id> (same shape as by_bbox_parcel_<run_id>).

A sample point is skipped when its parcel was already fetched (an in-memory geom
set for the current run, plus a committed point-in-polygon check against the
sync table), so once a parcel is found the remaining sample points inside it
cost no request.

No asyncio, no threads, no pools. Proxy parsing and cookie minting
(Playwright) live in common.py (requests_proxy_url, get_cookie, mint_cookie).

Usage:
    python 2_fetch_sync_parcel.py <run_id>
    e.g. python 2_fetch_sync_parcel.py by_101125
"""

import os
import subprocess
import sys
import tarfile
import time
from datetime import datetime

import requests
import psycopg2

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import common
from read_settings import load_config



def snapshot_master_parcel(cfg, run_id, master_table):
    """
    Dump master_table (public.by_parcel) to a timestamped .dump file (pg_dump
    custom format, compressed natively) under cfg.parcel_snapshots_path before
    reconcile_master_parcel overwrites the changed rows, so history isn't
    lost on each sync run.
    """
    os.makedirs(cfg.parcel_snapshots_path, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = os.path.join(cfg.parcel_snapshots_path, f"by_parcel_{run_id}_{timestamp}.dump")

    env = os.environ.copy()
    env["PGPASSWORD"] = cfg.password

    cmd = [
        "pg_dump",
        "-U", cfg.user,
        "-h", cfg.host,
        "-p", str(cfg.port),
        "-d", cfg.dbname,
        "-t", master_table,
        "-Fc",
    ]

    with open(out_path, "wb") as f:
        result = subprocess.run(cmd, env=env, stdout=f, stderr=subprocess.PIPE)

    if result.returncode != 0:
        raise RuntimeError(f"pg_dump failed for {master_table} snapshot:\n{result.stderr.decode(errors='replace')}")

    print(f"Snapshot saved: {out_path}")


def reconcile_master_parcel(conn, master_table, changed_table, sync_table):
    """
    Final reconciliation against the master parcel table, in ONE transaction:
      1. delete the changed (stale) parcels, identified by the ids recorded in
         by_bbox_parcel_changed_<run_id>;
      2. delete any master rows the synced set will replace (so re-runs / id
         collisions never leave duplicates), then insert the freshly
         synchronized parcels from by_bbox_parcel_sync_<run_id>.

    Uses delete-then-insert rather than ON CONFLICT, because by_parcel is not
    guaranteed to carry a unique constraint on id. Returns (deleted, inserted);
    caller commits.
    """
    cols = ("id, boundary_id, corridor, numerator, denominator, "
            "name, official_area, gml_id, geom")
    with conn.cursor() as cur:
        # 1) drop the changed (stale) parcels
        cur.execute(f"""
            DELETE FROM {master_table} p
            USING {changed_table} ch
            WHERE p.id = ch.id
        """)
        deleted = cur.rowcount

        # 2) drop any master rows the synced parcels will replace, then add them
        cur.execute(f"""
            DELETE FROM {master_table} p
            USING {sync_table} s
            WHERE p.id = s.id
        """)
        cur.execute(f"""
            INSERT INTO {master_table} ({cols})
            SELECT {cols} FROM {sync_table}
        """)
        inserted = cur.rowcount

    return deleted, inserted


# ----------------------------
# MAIN
# ----------------------------
def main(run_id):
    cfg = load_config(run_id)

    parcel_table = f"public.by_bbox_parcel_check_{run_id}"
    sync_table = f"public.by_bbox_parcel_sync_{run_id}"
    temp_table = f"public.by_bbox_parcel_changed_{run_id}"
    master_table = "public.by_parcel"

    # Snapshot the master by_parcel table before it gets overwritten below.
    snapshot_master_parcel(cfg, run_id, master_table)



if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python 2_rest_sync_parcel.py <run_id>")
        print('Example: "python 2_rest_sync_parcel.py by_101125"')
        sys.exit(1)

    start = time.time()
    main(sys.argv[1])
    print(f"Elapsed: {time.time() - start:.1f}s")
