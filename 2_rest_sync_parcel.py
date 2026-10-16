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
import time
from datetime import datetime

import requests
import psycopg2

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import common
from read_settings import load_config


# ----------------------------
# CONSTANTS
# ----------------------------
REST_COORD_TMPL = "https://geodatenonline.bayern.de/gdoflurkarte2/rest/flurstueck/koor/{rw}/{hw}?"

REQUEST_HEADERS = {
    "User-Agent": common.USER_AGENT,
    "Accept": "application/json, text/plain, */*",
    "X-Requested-With": "XMLHttpRequest",
    "Origin": "https://geodatenonline.bayern.de",
}

REQUEST_TIMEOUT = 15          # seconds per HTTP request
REQUEST_RETRIES = 3           # attempts per proxy before moving to the next one
COMMIT_EVERY = 100            # commit to DB every N processed temp points

# Grid spacing (meters) for sampling inside a parcel footprint is configurable
# via [parcel_sync] sample_step_m in settings.ini (cfg.parcel_sample_step_m).
MAX_SAMPLE_POINTS = 100      # safety cap on grid points generated per footprint


# ----------------------------
# PARSING HELPERS
# ----------------------------
def parse_numerator_denominator(name):
    """'199/4' -> ('199', '4'); '199' -> ('199', None)."""
    if not name:
        return "", None
    if "/" in name:
        a, b = name.split("/", 1)
        return a, b
    return name, None


def build_parcel_id(boundary_id, numerator, denominator):
    if denominator is None:
        return boundary_id + '000' + numerator.zfill(5) + '000000'
    return boundary_id + '000' + numerator.zfill(5) + denominator.zfill(4) + '00'


# ----------------------------
# DB SETUP / QUERIES
# ----------------------------
def ensure_sync_table(conn, parcel_table, sync_table, temp_table, run_id):
    """Create the sync table (LIKE the parcel table) + indexes. Sources must exist."""
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s)", (parcel_table,))
        if cur.fetchone()[0] is None:
            raise RuntimeError(f"Source table {parcel_table} does not exist. Aborting.")

        cur.execute("SELECT to_regclass(%s)", (temp_table,))
        if cur.fetchone()[0] is None:
            raise RuntimeError(f"Source table {temp_table} does not exist. Aborting.")

        cur.execute("SELECT to_regclass(%s)", (sync_table,))
        if cur.fetchone()[0] is None:
            cur.execute(f"""
                CREATE TABLE {sync_table}
                (LIKE {parcel_table} INCLUDING DEFAULTS INCLUDING GENERATED
                                     INCLUDING IDENTITY INCLUDING COMMENTS)
            """)
            cur.execute(f"ALTER TABLE {sync_table} "
                        f"ADD CONSTRAINT by_bbox_parcel_sync_{run_id}_pkey PRIMARY KEY (id)")
        else:
            cur.execute(f"TRUNCATE TABLE {sync_table}")

        cur.execute(
            f"CREATE INDEX IF NOT EXISTS by_bbox_parcel_sync_{run_id}_geom_gix "
            f"ON {sync_table} USING GIST (geom)"
        )
    conn.commit()


def fetch_temp_points(conn, temp_table):
    """Return (rw, hw, id) coordinates collected by the different-parcel pass."""
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT t.rw, t.hw, t.id
            FROM {temp_table} t
            WHERE t.rw IS NOT NULL AND t.hw IS NOT NULL
            ORDER BY t.id
        """)
        return cur.fetchall()


def sample_points_in_parcel(conn, parcel_table, rw, hw, step, max_points):
    """
    Find the parcel_table geometry that contains (rw, hw) and return a list of
    (x, y) sample points: a regular grid `step` meters apart, keeping only nodes
    that fall inside the polygon, plus its interior representative point
    (ST_PointOnSurface) so the result is never empty for parcels too small/thin
    for the grid to land inside. Returns [] when no geometry contains (rw, hw).
    """
    with conn.cursor() as cur:
        query = f"""
            WITH target AS (
                SELECT geom,
                    ST_XMin(geom) AS xmin, ST_YMin(geom) AS ymin,
                    ST_XMax(geom) AS xmax, ST_YMax(geom) AS ymax
                FROM {parcel_table}
                WHERE geom && ST_SetSRID(ST_Point(%(rw)s, %(hw)s), 25832)
                AND ST_Contains(geom, ST_SetSRID(ST_Point(%(rw)s, %(hw)s), 25832))
                ORDER BY ST_Area(geom)
                LIMIT 1
            ),
            grid AS (
                SELECT p.gx AS x, p.gy AS y
                FROM target,
                    generate_series(0, ceil((target.xmax - target.xmin) / %(step)s)::int) AS ix(i),
                    generate_series(0, ceil((target.ymax - target.ymin) / %(step)s)::int) AS iy(j),
                    LATERAL (
                        SELECT target.xmin + ix.i * %(step)s AS gx,
                                target.ymin + iy.j * %(step)s AS gy
                    ) AS p
                WHERE ST_Contains(
                    target.geom,
                    ST_SetSRID(ST_Point(p.gx, p.gy), 25832)
                )
                LIMIT %(maxpts)s
            )
            SELECT x, y FROM grid
            UNION
            SELECT %(rw)s, %(hw)s
        """

        params = {
            "rw": rw,
            "hw": hw,
            "step": step,
            "maxpts": max_points
        }

        cur.execute(query, params)
        print(params)
        return [(float(x), float(y)) for (x, y) in cur.fetchall()]


def point_is_covered(conn, sync_table, x, y):
    """True if (x, y) already lies inside a parcel committed to the sync table."""
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT 1
            FROM {sync_table} p
            WHERE p.geom && ST_SetSRID(ST_Point(%s, %s), 25832)
              AND ST_Contains(p.geom, ST_SetSRID(ST_Point(%s, %s), 25832))
            LIMIT 1
        """, (x, y, x, y))
        return cur.fetchone() is not None


def insert_sync(conn, sync_table, payload):
    with conn.cursor() as cur:
        cur.execute(f"""
            INSERT INTO {sync_table}
              (id, boundary_id, corridor, numerator, denominator, name, official_area, gml_id, geom)
            VALUES
              (%s, %s, %s, %s, %s, %s, %s, %s, ST_SetSRID(ST_GeomFromText(%s), 25832))
            ON CONFLICT (id) DO NOTHING
        """, payload)


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
# FETCH / INSERT FOR ONE COORDINATE
# ----------------------------
def fetch_point_results(session, x, y, proxies, proxy_start_index, cookie,
                        chromium_path, launcher_url, label):
    """
    GET the cadastral parcel(s) under one coordinate, with retries. Returns
    (cookie, proxy_start_index, results) where results is the API 'results'
    list, or None when the request hard-failed on every proxy. The cookie may
    be re-minted on 403.

    Proxies are tried round-robin starting from `proxy_start_index` (the proxy
    that succeeded last time), REQUEST_RETRIES attempts per proxy, wrapping
    around the whole list before giving up. The index of the proxy that
    succeeded is returned so the next call starts there.
    """
    url = REST_COORD_TMPL.format(rw=x, hw=y)
    for offset in range(len(proxies)):
        proxy_index = (proxy_start_index + offset) % len(proxies)
        proxy = common.requests_proxy_url(proxies[proxy_index])

        for attempt in range(1, REQUEST_RETRIES + 1):
            headers = dict(REQUEST_HEADERS, Cookie=cookie, Referer=launcher_url)
            try:
                resp = session.get(url, headers=headers,
                                   proxies={"http": proxy, "https": proxy},
                                   timeout=REQUEST_TIMEOUT)
            except Exception as e:
                print(f"request error {label} (proxy {proxy_index + 1}/{len(proxies)}, "
                      f"attempt {attempt}/{REQUEST_RETRIES}): {e}")
                continue

            if resp.status_code == 403:
                print(f"403 {label} -> re-minting cookie")
                cookie = common.mint_cookie(proxies, chromium_path, launcher_url)
                continue
            if resp.status_code != 200:
                print(f"HTTP {resp.status_code} {label} (proxy {proxy_index + 1}/{len(proxies)}, "
                      f"attempt {attempt}/{REQUEST_RETRIES})")
                continue

            return cookie, proxy_index, (resp.json().get("results") or [])

    print(f"giving up {label} after trying all {len(proxies)} proxies")
    return cookie, proxy_start_index, None


def insert_results(conn, sync_table, results):
    """
    Write every usable parcel from one coordinate response to the sync table.
    ON CONFLICT (id) DO NOTHING keeps parcels unique across the whole run.
    Returns the number of parcels inserted.
    """
    n = 0
    for ele in results:
        ewkt = ele.get("ewkt")
        if not ewkt or ";" not in ewkt:
            continue

        gml_id = ele.get("gmlid")
        boundary_id = ele.get("gmkgnr")
        name = ele.get("flstnr")
        geom_wkt = ewkt.split(";", 1)[1]
        numerator, denominator = parse_numerator_denominator(name or "")
        parcel_id = build_parcel_id(boundary_id, numerator, denominator)

        insert_sync(conn, sync_table, (
            parcel_id, boundary_id, None, numerator, denominator,
            name, None, gml_id, str(geom_wkt),
        ))
        n += 1
    return n


# ----------------------------
# PROCESS ONE TEMP POINT (SAMPLE ITS FOOTPRINT)
# ----------------------------
def process_point(conn, session, row, proxies, proxy_start_index, cookie,
                  chromium_path, launcher_url,
                  parcel_table, sync_table, step, max_points):
    """
    Sample the parcel footprint in parcel_table that contains (rw, hw) on a
    regular grid and fetch the parcel under every interior sample point, so an
    old parcel that has since been split is recovered in full. Sample points
    already covered by a parcel written earlier in this transaction (the
    point_is_covered check sees the session's own uncommitted inserts) cost no
    request. Returns (cookie, proxy_start_index, n_inserted, n_failed).
    """
    rw, hw, point_id = row

    sample_points = sample_points_in_parcel(
        conn, parcel_table, rw, hw, step, max_points)
    if not sample_points:
        # (rw, hw) is not inside any parcel footprint -> fall back to the bare point
        sample_points = [(float(rw), float(hw))]

    n_inserted = 0
    n_failed = 0
    for x, y in sample_points:
        # covered by a parcel already written this run (own uncommitted inserts
        # are visible to this connection) -> skip the GET
        try:
            if point_is_covered(conn, sync_table, x, y):
                continue
        except Exception as e:
            print(f"coverage check failed id={point_id} ({x:.2f},{y:.2f}): {e}")

        cookie, proxy_start_index, results = fetch_point_results(
            session, x, y, proxies, proxy_start_index, cookie, chromium_path,
            launcher_url,
            f"id={point_id} ({x:.2f},{y:.2f})")

        if results is None:
            n_failed += 1
            continue

        n_inserted += insert_results(conn, sync_table, results)

    return cookie, proxy_start_index, n_inserted, n_failed


# ----------------------------
# MAIN
# ----------------------------
def main(run_id):
    cfg = load_config(run_id)

    parcel_table = f"public.by_bbox_parcel_check_{run_id}"
    sync_table = f"public.by_bbox_parcel_sync_{run_id}"
    temp_table = f"public.by_bbox_parcel_changed_{run_id}"
    master_table = "public.by_parcel"

    proxies = common.load_proxies(cfg)
    if not proxies:
        raise RuntimeError("No proxies loaded (check settings proxy_file).")

    conn = psycopg2.connect(
        user=cfg.user, password=cfg.password,
        host=cfg.host, port=cfg.port, database=cfg.dbname,
    )

    try:
        ensure_sync_table(conn, parcel_table, sync_table, temp_table, run_id)

        rows = fetch_temp_points(conn, temp_table)
        print(f"{len(rows)} temp points to process")
        if not rows:
            print("Nothing to do")
            return

        cookie = common.mint_cookie(proxies, cfg.chrome_path, cfg.launcher_url)
        session = requests.Session()

        inserted = failed = 0
        points_with_hits = 0
        proxy_start_index = 0
        for i, row in enumerate(rows, 1):
            cookie, proxy_start_index, n_inserted, n_failed = process_point(
                conn, session, row, proxies, proxy_start_index, cookie,
                cfg.chrome_path, cfg.launcher_url,
                parcel_table, sync_table,
                cfg.parcel_sample_step_m, MAX_SAMPLE_POINTS,
            )
            inserted += n_inserted
            failed += n_failed
            if n_inserted:
                points_with_hits += 1

            if i % COMMIT_EVERY == 0:
                conn.commit()
                print(f"progress: {i}/{len(rows)} temp points "
                      f"(parcels inserted={inserted}, failed requests={failed})")

        conn.commit()
        print(f"Done. parcels inserted={inserted}, "
              f"temp points with hits={points_with_hits}/{len(rows)}, "
              f"failed requests={failed}")

        # Snapshot the master by_parcel table before it gets overwritten below.
        snapshot_master_parcel(cfg, run_id, master_table)

        # Final reconciliation of the master by_parcel table: drop the changed
        # (stale) parcels and add the freshly synchronized ones.
        deleted, inserted_master = reconcile_master_parcel(
            conn, master_table, temp_table, sync_table)
        conn.commit()
        print(f"Reconciled {master_table}: deleted={deleted} changed, "
              f"inserted={inserted_master} synced parcels")
    finally:
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python 2_rest_sync_parcel.py <run_id>")
        print('Example: "python 2_rest_sync_parcel.py by_101125"')
        sys.exit(1)

    start = time.time()
    main(sys.argv[1])
    print(f"Elapsed: {time.time() - start:.1f}s")
