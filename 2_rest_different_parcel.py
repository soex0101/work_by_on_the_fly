#!/usr/bin/env python3
"""
2_fetch_different_parcel.py

Same job as 2_fetch_by_sug.py, but simple and synchronous:
  - Build by_bbox_parcel_check_<run_id> from by_parcel within cfg.bbox.
  - For EVERY parcel in that table, look it up by its FULL name
    (Flurstuecksnummer, e.g. "199/4") on the Bayern cadastral REST API. Querying
    the exact name keeps the result set small, so the API's 10-row cap no longer
    hides siblings the way a numerator-only query (e.g. "199") did.
  - Among the results, take the same-name candidate(s) (flstnr == our name) and
    compare against our row:
      * a same-name candidate fully matches  -> copy our parcel into "confirmed".
      * otherwise (changed geometry, or the name is gone) -> the parcel is
        "different": store ITS OWN representative point + id in the "temp" table,
        so a later coordinate-based pass can re-fetch the current geometry of
        that footprint.

No asyncio, no threads, no connection/cookie pools, no boundary-job queue.

Proxy parsing and cookie minting (Playwright) live in common.py
(requests_proxy_url, get_cookie, mint_cookie).

Usage:
    python 2_fetch_different_parcel.py <run_id>
    e.g. python 2_fetch_different_parcel.py by_101125
"""

import os
import sys
import time
from decimal import Decimal
from urllib.parse import quote

import requests
import psycopg2

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import common
from read_settings import load_config

from labeling import db_crud

# ----------------------------
# CONSTANTS
# ----------------------------
REQUEST_HEADERS = {
    "User-Agent": common.USER_AGENT,
    "Accept": "application/json, text/plain, */*",
    "X-Requested-With": "XMLHttpRequest",
    "Origin": "https://geodatenonline.bayern.de",
}

REQUEST_TIMEOUT = 15          # seconds per HTTP request
REQUEST_RETRIES = 3           # attempts per proxy before moving to the next one
COMMIT_EVERY = 100            # commit to DB every N processed parcels


# ----------------------------
# COMPARISON HELPERS
# ----------------------------
def is_float_same(a, b, precision="0.01"):
    if a is None or b is None:
        return False
    try:
        q = Decimal(precision)
        return Decimal(str(a)).quantize(q) == Decimal(str(b)).quantize(q)
    except (ArithmeticError, ValueError, TypeError):
        return False


def is_bbox_same(bbox_list, bbox_box2d_text):
    """bbox_list = [xmin, ymin, xmax, ymax]; bbox_box2d_text = 'BOX(xmin ymin,xmax ymax)'."""
    text = bbox_box2d_text.strip()
    if text.startswith("BOX(") and text.endswith(")"):
        text = text[4:-1]
    parts = text.replace(",", " ").split()
    if not isinstance(bbox_list, list) or len(bbox_list) != 4 or len(parts) != 4:
        return False
    return all(is_float_same(bbox_list[i], parts[i]) for i in range(4))


def build_url(boundary_id, name, api_url):
    safe_name = name.replace("/", "-")
    left = quote(boundary_id, safe="")
    right = quote(safe_name, safe="-")
    return f"{api_url}/{left}%20{right}?&srid=25832"


# ----------------------------
# DB SETUP / QUERIES
# ----------------------------
def ensure_tables(conn, parcel_table, confirmed_table, temp_table, run_id):
    """Create the confirmed and temp tables (idempotent). Source table must exist."""
    confirmed_pkey = f"by_bbox_parcel_confirmed_{run_id}_pkey"
    temp_unique = f"by_bbox_parcel_changed_{run_id}_unique"

    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s)", (parcel_table,))
        if cur.fetchone()[0] is None:
            raise RuntimeError(f"Source table {parcel_table} does not exist. Aborting.")

        # confirmed table (same columns as source, PK on id)
        cur.execute("SELECT to_regclass(%s)", (confirmed_table,))
        if cur.fetchone()[0] is None:
            cur.execute(f"""
                CREATE TABLE {confirmed_table}
                (LIKE {parcel_table} INCLUDING DEFAULTS INCLUDING GENERATED
                                     INCLUDING IDENTITY INCLUDING COMMENTS)
            """)
            cur.execute(f"ALTER TABLE {confirmed_table} "
                        f"ADD CONSTRAINT {confirmed_pkey} PRIMARY KEY (id)")
        else:
            # start fresh
            cur.execute(f"TRUNCATE TABLE {confirmed_table}")

        # temp table (coordinates of parcels that did not match)
        cur.execute("SELECT to_regclass(%s)", (temp_table,))
        if cur.fetchone()[0] is None:
            cur.execute(f"""
                CREATE TABLE {temp_table} (
                    rw double precision,
                    hw double precision,
                    id text NOT NULL
                )
            """)
            cur.execute(f"ALTER TABLE {temp_table} "
                        f"ADD CONSTRAINT {temp_unique} UNIQUE (rw, hw, id)")
        else:
            # start fresh
            cur.execute(f"TRUNCATE TABLE {temp_table}")
    conn.commit()


def fetch_pending_parcels(conn, parcel_table, confirmed_table):
    """Return parcels not yet in the confirmed table, with the fields we compare on."""
    stmt = f"""
        SELECT
            p.id,
            p.gml_id,
            p.boundary_id,
            p.name,
            p.numerator,
            BOX2D(p.geom)::text AS bbox_text,
            ST_X(ST_PointOnSurface(p.geom)) AS rw,
            ST_Y(ST_PointOnSurface(p.geom)) AS hw
        FROM {parcel_table} p
        WHERE NOT EXISTS (
            SELECT 1 FROM {confirmed_table} c WHERE c.id = p.id
        )
        ORDER BY p.id
    """
    with conn.cursor() as cur:
        cur.execute(stmt)
        return cur.fetchall()


def insert_confirmed(conn, parcel_table, confirmed_table, parcel_id):
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {confirmed_table} SELECT * FROM {parcel_table} "
            f"WHERE id = %s ON CONFLICT (id) DO NOTHING",
            (parcel_id,),
        )


def insert_temp(conn, temp_table, run_id, rw, hw, parcel_id):
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {temp_table} (rw, hw, id) VALUES (%s, %s, %s) "
            f"ON CONFLICT ON CONSTRAINT by_bbox_parcel_changed_{run_id}_unique DO NOTHING",
            (rw, hw, parcel_id),
        )


def candidate_matches(ele, row):
    """True if a returned candidate is the exact same parcel as our row."""
    (parcel_id, exp_gml_id, exp_boundary_id, exp_name,
     _exp_numerator, exp_bbox, exp_rw, exp_hw) = row
    same = (
        ele.get("gmkgnr") == exp_boundary_id
        and ele.get("flstnr") == exp_name
        and ele.get("gmlid") == exp_gml_id
        and is_float_same(ele.get("rw"), exp_rw, "0.1")
        and is_float_same(ele.get("hw"), exp_hw, "0.1")
    )
    try:
        return same and is_bbox_same(ele.get("bbox"), exp_bbox)
    except Exception:
        return False


# ----------------------------
# PROCESS ONE PARCEL
# ----------------------------
def process_parcel(conn, session, row, proxies, proxy_start_index, cookie,
                   chromium_path, api_url, launcher_url, 
                   parcel_table, confirmed_table, temp_table,
                   run_id):
    """
    Look up ONE parcel by its full name (e.g. "199/4") and reconcile it. Only
    same-name candidates (flstnr == our name, same Gemarkung) are compared, so
    the API's 10-row cap is irrelevant. Returns
    (cookie, proxy_start_index, confirmed, different) with confirmed/different
    each 0 or 1; the cookie may be re-minted on 403.

    Proxies are tried round-robin starting from `proxy_start_index` (the proxy
    that succeeded last time), REQUEST_RETRIES attempts per proxy, wrapping
    around the whole list before giving up. The index of the proxy that
    succeeded is returned so the next call starts there.

      - a same-name candidate fully matches our row -> our parcel -> confirmed
      - otherwise (changed geometry, or the name is gone) -> "different": our
        own representative point + id -> temp (for the coordinate-based pass)
    """
    (parcel_id, exp_gml_id, exp_boundary_id, exp_name,
     _exp_numerator, exp_bbox, exp_rw, exp_hw) = row

    url = build_url(exp_boundary_id, exp_name, api_url)
    label = f"{exp_boundary_id} {exp_name}"

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
                time.sleep(1)
                continue

            if resp.status_code == 403:
                print(f"403 {label} -> re-minting cookie")
                cookie = common.mint_cookie(proxies, chromium_path, launcher_url)
                continue
            if resp.status_code != 200:
                print(f"HTTP {resp.status_code} {label} (proxy {proxy_index + 1}/{len(proxies)}, "
                      f"attempt {attempt}/{REQUEST_RETRIES})")
                continue

            results = resp.json().get("results") or []

            # keep only the candidate(s) carrying OUR name in OUR Gemarkung AND
            # sitting at OUR location (rw/hw within 0.1 m)
            same_name = [
                ele for ele in results
                if ele.get("flstnr") == exp_name
            ]

            if any(candidate_matches(ele, row) for ele in same_name):
                insert_confirmed(conn, parcel_table, confirmed_table, parcel_id)
                return cookie, proxy_index, 1, 0

            # not confirmed -> the parcel changed (or its name is gone). Remember our
            # own representative point + id so the coordinate pass re-fetches the
            # current geometry of this footprint, and so the later by_parcel
            # reconcile can delete this stale parcel by its own id.
            insert_temp(conn, temp_table, run_id, exp_rw, exp_hw, parcel_id)

            for ele in same_name:
                insert_temp(conn, temp_table, run_id, ele.get("rw"), ele.get("hw"), parcel_id)

            return cookie, proxy_index, 0, 1

    print(f"giving up {label} after trying all {len(proxies)} proxies")
    return cookie, proxy_start_index, 0, 0


# ----------------------------
# MAIN
# ----------------------------
def main(run_id):
    cfg = load_config(run_id)

    parcel_table = f"public.by_bbox_parcel_check_{run_id}"
    confirmed_table = f"public.by_bbox_parcel_confirmed_{run_id}"
    temp_table = f"public.by_bbox_parcel_changed_{run_id}"
    
    # Copy by_bbox_parcel_check_{run_id} from by_parcel with cfg.bbox
    db_cadastral_param = {
        "dbname": cfg.dbname,
        "user": cfg.user,
        "password": cfg.password,
        "host": cfg.host,
        "port": cfg.port
    }
    connection_cadastral = db_crud.create_connection(db_cadastral_param)
    reg = db_crud.run_query(connection_cadastral, "SELECT to_regclass(%s)", (parcel_table,))
    if reg and reg[0][0] is not None:
        # already exists (e.g. a re-run) -> truncate and refill from current by_parcel
        db_crud.run_query(connection_cadastral, f"TRUNCATE TABLE {parcel_table}")
        db_crud.run_query(
            connection_cadastral,
            f"INSERT INTO {parcel_table} "
            f"SELECT * FROM by_parcel WHERE geom && ST_MakeEnvelope(%s, %s, %s, %s, 25832)",
            cfg.bbox,
        )
    else:
        db_crud.copy_table_with_bbox(connection_cadastral, f"by_parcel", parcel_table, cfg.bbox)

    # Load proxy
    proxies = common.load_proxies(cfg)
    if not proxies:
        raise RuntimeError("No proxies loaded (check settings proxy_file).")

    conn = psycopg2.connect(
        user=cfg.user, password=cfg.password,
        host=cfg.host, port=cfg.port, database=cfg.dbname,
    )

    try:
        # Create/Seed tables
        ensure_tables(conn, parcel_table, confirmed_table, temp_table, run_id)

        rows = fetch_pending_parcels(conn, parcel_table, confirmed_table)
        print(f"{len(rows)} parcels to process")
        if not rows:
            print("Nothing to do")
            return 0

        cookie = common.mint_cookie(proxies, cfg.chrome_path, cfg.launcher_url)
        session = requests.Session()

        confirmed = different = 0
        proxy_start_index = 0
        total = len(rows)
        for i, row in enumerate(rows, 1):
            cookie, proxy_start_index, n_confirmed, n_diff = process_parcel(
                conn, session, row,
                proxies, proxy_start_index, cookie, cfg.chrome_path,
                cfg.api_url, cfg.launcher_url,
                parcel_table, confirmed_table, temp_table, run_id,
            )
            confirmed += n_confirmed
            different += n_diff

            if i % COMMIT_EVERY == 0:
                conn.commit()
                print(f"progress: {i}/{total} parcels "
                      f"(confirmed={confirmed}, different={different})")

        conn.commit()
        print(f"Done. confirmed={confirmed}, different={different}")
    finally:
        conn.close()

    return different

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python 2_rest_different_parcel.py <run_id>")
        print('Example: "python 2_rest_different_parcel.py by_101125"')
        sys.exit(1)

    start = time.time()
    different_count = main(sys.argv[1])
    print(f"Elapsed: {time.time() - start:.1f}s")

    sys.exit(1 if different_count > 0 else 0)