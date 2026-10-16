#########################
# ALTER TABLE building
#     ADD COLUMN color character varying(50),
#     ADD COLUMN known character varying(50),
#     ADD COLUMN area float

import os
import sys
import time
import rasterio
import numpy as np
import psycopg2
from shapely import wkb
from shapely.geometry import box, mapping
from rasterio.features import geometry_mask
from collections import Counter
from scipy.ndimage import binary_erosion
from multiprocessing import Pool, cpu_count
import argparse

from read_settings import load_config
import common
from labeling import db_crud

cfg = load_config()

# Known RGB colors
KNOWN_COLORS = [
#    (178, 178, 178)    # 1301 - (153, 153, 153)
#    , (229, 229, 229)  # 1304 - (204, 204, 204)
#    , (245, 133, 127)  # 1309 - (255, 127, 127) or (244, 132, 127)
#    , (247, 165, 160)  # Red1
#    , (252, 224, 224)  # Boundary only over Pink
#    , (244, 244, 244)  # Boundary only over Light gray
#    , (255, 255, 255)  # Boundary only over White
#    , (242, 244, 203)  # Boundary only over Light green
]

TILE_SIZE = 600  # meters
dpi = 100
scale = cfg.scale
SCHEMA_TABLE = "building"

# Global flag to control whether TIFFs are read from disk
TILE_FOLDER = ""

# --- Database config (used in every process) ---
DB_CONFIG = None

def classify_color(pixels):
    filtered_pixels = [tuple(pixel) for pixel in pixels if not np.array_equal(pixel, (0, 0, 0))]
    if not filtered_pixels:
        return "0,0,0", False

    color_counts = Counter(filtered_pixels)
    most_common_color = color_counts.most_common(1)[0][0]
    known = any(np.allclose(most_common_color, known_rgb, atol=10) for known_rgb in KNOWN_COLORS)
    return ",".join(map(str, most_common_color)), known

def fetch_extent(conn, table):
    with conn.cursor() as cur:
        cur.execute(f"SELECT ST_Extent(geom) FROM {table}")
        extent_str = cur.fetchone()[0]
        coords = extent_str.replace("BOX(", "").replace(")", "").split(",")
        xmin, ymin = map(float, coords[0].split())
        xmax, ymax = map(float, coords[1].split())
        return xmin, ymin, xmax, ymax

def fetch_buildings(conn, table, bbox):
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT id, ST_AsBinary(geom), area
            FROM {table}
            WHERE geom && ST_MakeEnvelope(%s, %s, %s, %s, 25832)
        """, bbox)
        data = cur.fetchall()
        return [(row[0], wkb.loads(bytes(row[1]), hex=False), row[2]) for row in data]

def process_tile_worker(args):
    table, bbox = args
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        process_tile(conn, table, bbox)
    except Exception as e:
        print(f"Error in worker for tile {bbox}: {e}")
    finally:
        conn.close()

def process_tile(conn, table, bbox):
    try:
        print(f"  Reading WMS tile {bbox} ...")
        filename = TILE_FOLDER + f"wms_tile_{bbox[0]}_{bbox[1]}_{bbox[2]}_{bbox[3]}.tif"
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

def detect_all(table, extent_bbox=None, num_workers=None):
    if extent_bbox:
        xmin, ymin, xmax, ymax = extent_bbox
    else:
        conn = psycopg2.connect(**DB_CONFIG)
        xmin, ymin, xmax, ymax = fetch_extent(conn, table)
        conn.close()
    print(f"Processing area: ({xmin}, {ymin}) to ({xmax}, {ymax})")

    # Calculate all tile bboxes
    tile_bboxes = []
    for x in range(int(xmin), int(xmax), TILE_SIZE):
        for y in range(int(ymin), int(ymax), TILE_SIZE):
            tile = (x, y, x + TILE_SIZE, y + TILE_SIZE)

            # check intersection
            if not (tile[2] <= cfg.by_whole_bbox[0] or tile[0] >= cfg.by_whole_bbox[2] or
                    tile[3] <= cfg.by_whole_bbox[1] or tile[1] >= cfg.by_whole_bbox[3]):
                tile_bboxes.append(tile)

    total_tiles = len(tile_bboxes)
    print(f"Total tiles to process: {total_tiles} using {num_workers or cpu_count()} workers")

    args_list = [(table, bbox) for bbox in tile_bboxes]

    with Pool(processes=num_workers or cpu_count()) as pool:
        for i, _ in enumerate(pool.imap_unordered(process_tile_worker, args_list), 1):
            print(f"Processed tile {i} of {total_tiles}")

def main(bbox):

    connection = db_crud.create_connection(DB_CONFIG)

    # Copy building table 
    db_crud.drop_table_if_exist(connection, SCHEMA_TABLE)
    query = f"""
    CREATE TABLE IF NOT EXISTS public.{SCHEMA_TABLE} (
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
    INSERT INTO public.{SCHEMA_TABLE} (id, geom)
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

    total_cnt = db_crud.get_table_row_count(connection, SCHEMA_TABLE)
    print(f"{SCHEMA_TABLE} ({total_cnt} Rows)")

    start_time = time.time()
    detect_all(SCHEMA_TABLE, bbox, cfg.cpu_cores)
    end_time = time.time()

    print(f"Total elapsed time: {end_time - start_time:.2f} seconds")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python 6_detect_building_color.py <id>")
        print('Example command format: "python 6_detect_building_color.py 0424 --bbox=648316,5477946,652714,5480870"')
        sys.exit(1)

    parser = argparse.ArgumentParser()
    parser.add_argument("id", help="Run ID")
    parser.add_argument("--bbox", type=str, required=False, default="", help="Bounding box")

    args = parser.parse_args()

    TILE_FOLDER = f"{cfg.wms_path}_{args.id}/{TILE_SIZE}_{dpi}_{scale}/"

    dbname = f"{cfg.dbname_building}_{args.id}"
    common.create_db(cfg, dbname)

    DB_CONFIG = {
        "dbname": dbname,
        "user": cfg.user,
        "password": cfg.password,
        "host": cfg.host,
        "port": cfg.port
    }

    ## Create postgis, dblink extension
    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = True
    cur = conn.cursor()
    
    cur.execute("CREATE EXTENSION IF NOT EXISTS postgis;")
    cur.execute("CREATE EXTENSION IF NOT EXISTS dblink;")

    cur.close()
    conn.close()

    # get bbox
    bbox = cfg.by_whole_bbox
    if args.bbox != "":
        bbox_arg = args.bbox
        print("BBOX: ", bbox_arg)
        parts = [p.strip() for p in bbox_arg.split(",")]
        if len(parts) != 4:
            raise ValueError("BBox must have exactly 4 values")

        minx, miny, maxx, maxy = map(float, parts)
        bbox = (minx, miny, maxx, maxy)

    main(bbox)

# Usage:
# python 6_detect_building_color.py 0424 --bbox=648316,5477946,652714,5480870
