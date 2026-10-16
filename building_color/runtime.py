import os
import sys
import time
import itertools
from io import BytesIO

import requests
import rasterio
import numpy as np
import psycopg2
from shapely import wkb
from shapely.geometry import box, mapping
from rasterio.features import geometry_mask
from collections import Counter
from scipy.ndimage import binary_erosion
import argparse

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import common
from read_settings import load_config
from labeling import db_crud

# --- Constants ---
TILE_SIZE = 600  # meters
dpi = 100

# Known RGB colors (kept identical to 2_detect_building_color.py)
KNOWN_COLORS = []

WMS_PARAMS_BASE = {
    "service": "WMS",
    "version": "1.1.1",
    "request": "GetMap",
    "format": "image/tiff",
    "transparent": "false",
    "layers": f"by_alkis_parzellarkarte_farbe",
    "srs": "EPSG:25832",
    "styles": "",
    "DPI": str(dpi),
    "MAP_RESOLUTION": str(dpi),
    "FORMAT_OPTIONS": f"dpi:{dpi}"
}

OVERWRITE_EXISTING = False  # Set to True to always overwrite existing tiles


# ----------------------------------------------------------------------------
# WMS download (same logic as 1_download_wms.py, single process)
# ----------------------------------------------------------------------------
def parse_proxy(proxy_line):
    parts = proxy_line.split(":")
    host = parts[0]
    port = parts[1]
    username = parts[2] if len(parts) > 2 else None
    password = parts[3] if len(parts) > 3 else None
    return f"{username}:{password}@{host}:{port}"


def proxy_dict(proxy_str):
    proxy_line = parse_proxy(proxy_str)
    return {
        'http': f'http://{proxy_line}',
        'https': f'http://{proxy_line}'
    }


def calculate_wms_pixel_size_from_bbox(bbox, dpi, scale):
    xmin, ymin, xmax, ymax = bbox
    width_m = xmax - xmin
    height_m = ymax - ymin
    width_px = int(width_m * 100 * dpi / 2.54 / scale)
    height_px = int(height_m * 100 * dpi / 2.54 / scale)
    return width_px, height_px


def download_wms_tiff_to_memory(bbox, scale, proxy_cycle, max_retries, failed_requests):
    width_px, height_px = calculate_wms_pixel_size_from_bbox(bbox, dpi, scale)
    params = WMS_PARAMS_BASE.copy()
    params.update({
        "bbox": ",".join(map(str, bbox)),
        "WIDTH": str(width_px),
        "HEIGHT": str(height_px)
    })

    last_error = None
    for attempt in range(1, max_retries + 1):
        proxy = next(proxy_cycle)
        proxies = proxy_dict(proxy) if proxy else None
        try:
            response = requests.get(cfg.building_url, params=params, timeout=(5, 60), proxies=proxies)
            response.raise_for_status()
            return BytesIO(response.content)
        except Exception as e:
            last_error = e
            print(f"Download failed using proxy {proxy} (attempt {attempt}/{max_retries}): {e}. Retrying in 5 seconds...")
            if attempt < max_retries:
                time.sleep(5)

    url = requests.Request("GET", cfg.building_url, params=params).prepare().url
    failed_requests.append(common.request_error(last_error, url))
    return None


def download_tiles(extent_bbox, scale, file_folder):
    xmin, ymin, xmax, ymax = extent_bbox
    print(f"Downloading WMS tiles for area: ({xmin}, {ymin}) to ({xmax}, {ymax})")

    proxies = common.load_proxies()
    if not proxies:
        proxies = [None]  # Fallback to no proxy
    proxy_cycle = itertools.cycle(proxies)
    # 3 attempts per proxy, like the other service requests of the run
    max_retries = 3 * len(proxies)
    failed_requests = []

    tiles = []
    for x in range(int(xmin), int(xmax), TILE_SIZE):
        for y in range(int(ymin), int(ymax), TILE_SIZE):
            tiles.append((x, y, x + TILE_SIZE, y + TILE_SIZE))

    print(f"Total tiles to download: {len(tiles)}")
    for tile_bbox in tiles:
        filename = file_folder + f"wms_tile_{tile_bbox[0]}_{tile_bbox[1]}_{tile_bbox[2]}_{tile_bbox[3]}.tif"
        if not OVERWRITE_EXISTING and os.path.exists(filename):
            print(f"  Skipping existing tile: {filename}")
            continue

        try:
            print(f"  Downloading WMS tile {tile_bbox} ...")
            tiff_stream = download_wms_tiff_to_memory(tile_bbox, scale, proxy_cycle, max_retries, failed_requests)
            if tiff_stream is None:
                continue
            with open(filename, "wb") as f:
                f.write(tiff_stream.getbuffer())
            print(f"  Saved TIFF to {filename}")
        except Exception as e:
            print(f"  Error processing tile {tile_bbox}: {e}")

    return failed_requests


# ----------------------------------------------------------------------------
# Building color detection (same logic as 2_detect_building_color.py, single process)
# ----------------------------------------------------------------------------
def classify_color(pixels):
    filtered_pixels = [tuple(pixel) for pixel in pixels if not np.array_equal(pixel, (0, 0, 0))]
    if not filtered_pixels:
        return "0,0,0", False

    color_counts = Counter(filtered_pixels)
    most_common_color = color_counts.most_common(1)[0][0]
    known = any(np.allclose(most_common_color, known_rgb, atol=10) for known_rgb in KNOWN_COLORS)
    return ",".join(map(str, most_common_color)), known


def fetch_buildings(conn, table, bbox):
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT id, ST_AsBinary(geom), area
            FROM {table}
            WHERE geom && ST_MakeEnvelope(%s, %s, %s, %s, 25832)
        """, bbox)
        data = cur.fetchall()
    # End the read transaction right away: process_tile() does the image work before its next
    # commit (and not at all if every building is skipped), so the connection would otherwise
    # stay "idle in transaction" meanwhile, even across tiles.
    conn.commit()
    return [(row[0], wkb.loads(bytes(row[1]), hex=False), row[2]) for row in data]


def process_tile(conn, table, bbox, file_folder):
    try:
        print(f"  Reading WMS tile {bbox} ...")
        filename = file_folder + f"wms_tile_{bbox[0]}_{bbox[1]}_{bbox[2]}_{bbox[3]}.tif"
        if not os.path.exists(filename):
            print(f"  TIFF file not found: {filename}")
            return

        buildings = fetch_buildings(conn, table, bbox)
        if not buildings:
            return

        with rasterio.open(filename) as src:
            img = np.dstack([src.read(1), src.read(2), src.read(3)])
            tile_geom = box(*bbox)

            for bid, geom, old_area in buildings:
                # Create a full mask for the polygon
                mask = geometry_mask([mapping(geom)], transform=src.transform,
                                     invert=True, out_shape=(src.height, src.width))
                if not np.any(mask):
                    continue

                # Apply erosion to remove 1-pixel boundary
                eroded_mask = binary_erosion(mask, structure=np.ones((3, 3)))
                if not np.any(eroded_mask):
                    continue

                pixels = img[eroded_mask]
                if pixels.size == 0:
                    continue

                # Calculate intersection area
                intersection_area = geom.intersection(tile_geom).area
                if intersection_area <= 0:
                    continue

                # Update only if new area > old area (or old is NULL)
                if old_area is None or intersection_area >= old_area:
                    color_str, known = classify_color(pixels)
                    with conn.cursor() as cur:
                        cur.execute(f"""
                            UPDATE {table}
                            SET color = %s, known = %s, area = %s
                            WHERE id = %s
                        """, (color_str, known, intersection_area, bid))
                conn.commit()
        print("Processing image completed.")
    except Exception as e:
        print(f"Error processing tile {bbox}: {e}")
        # Without this a failed UPDATE leaves the connection "idle in transaction (aborted)"
        # and every following tile fails with "current transaction is aborted".
        conn.rollback()


def detect_all(conn, table, extent_bbox, file_folder):
    xmin, ymin, xmax, ymax = extent_bbox
    print(f"Detecting building color for area: ({xmin}, {ymin}) to ({xmax}, {ymax})")

    tile_bboxes = []
    for x in range(int(xmin), int(xmax), TILE_SIZE):
        for y in range(int(ymin), int(ymax), TILE_SIZE):
            tile_bboxes.append((x, y, x + TILE_SIZE, y + TILE_SIZE))

    total_tiles = len(tile_bboxes)
    print(f"Total tiles to process: {total_tiles}")
    for i, tile_bbox in enumerate(tile_bboxes, 1):
        process_tile(conn, table, tile_bbox, file_folder)
        print(f"Processed tile {i} of {total_tiles}")


# ----------------------------------------------------------------------------
def main(run_id, bbox, scale, skip_download=False, skip_table_copy=False):
    file_folder = f"{cfg.building_path}/{run_id}/"
    os.makedirs(file_folder, exist_ok=True)

    table = f"by_bbox_building_color_{run_id}"

    db_config = {
        "dbname": cfg.dbname,
        "user": cfg.user,
        "password": cfg.password,
        "host": cfg.host,
        "port": cfg.port
    }

    # --- Stage 1: download WMS tiles for the run bbox ---
    if skip_download:
        print("Skipping WMS download (--skip-download).")
    else:
        start_time = time.time()
        failed_requests = download_tiles(bbox, scale, file_folder)
        print(f"WMS download elapsed: {time.time() - start_time:.2f} seconds")
        common.exit_if_failed_requests("building_color/runtime.py", failed_requests, run_id)

    connection = db_crud.create_connection(db_config)

    # Build the building-color table from the address DB (hausumringe) within the run bbox.
    if skip_table_copy:
        print("Skipping building data copy from address DB (--skip-table-copy). Resetting color column to NULL.")
        db_crud.run_query(connection, f"UPDATE public.{table} SET color = NULL")
    else:
        db_crud.drop_table_if_exist(connection, table)
        query = f"""
        CREATE TABLE IF NOT EXISTS public.{table} (
            id integer PRIMARY KEY,
            geom geometry(MultiPolygon, 25832),
            color VARCHAR,
            area DOUBLE PRECISION,
            known VARCHAR
        );
        """
        db_crud.run_query(connection, query)

        connection_address_string = "host={} dbname={} user={} password={}".format(
            cfg.host,
            cfg.dbname_address,
            cfg.user,
            cfg.password
        )

        query = f"""
        INSERT INTO public.{table} (id, geom)
        SELECT
            src.ogc_fid,
            src.geom
        FROM dblink(%s, 'SELECT ogc_fid, geom FROM public.hausumringe')
            AS src(
                ogc_fid integer,
                geom geometry(MultiPolygon, 25832))
            WHERE ST_Intersects(src.geom, ST_MakeEnvelope(%s, %s, %s, %s, 25832));
        """
        db_crud.run_query(connection, query, (
            connection_address_string,
            *bbox
        ))

        total_cnt = db_crud.get_table_row_count(connection, table)
        print(f"{table} ({total_cnt} Rows)")

    # --- Stage 2: detect building color for the run bbox ---
    start_time = time.time()
    detect_all(connection, table, bbox, file_folder)
    print(f"Building color detection elapsed: {time.time() - start_time:.2f} seconds")

    db_crud.close_connection(connection)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python runtime.py <id>")
        print('Example command format: "python runtime.py by_101125"')
        sys.exit(1)

    parser = argparse.ArgumentParser()
    parser.add_argument("id", help="Run ID")
    parser.add_argument("--skip-download", action="store_true",
                        help="Skip WMS download and only run building color detection.")
    parser.add_argument("--skip-table-copy", action="store_true",
                        help="Skip copying building data from the address DB (use the existing table).")
    args = parser.parse_args()

    run_id = args.id

    # Read settings_<run_id>.ini (bbox/scale for this run)
    cfg = load_config(run_id)
    bbox = cfg.bbox
    scale = cfg.scale

    main(run_id, bbox, scale, args.skip_download, args.skip_table_copy)
