import argparse
import os
import sys
import time

import requests
from pyproj import Transformer
from psycopg2.extras import execute_values

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from read_settings import load_config
import common
from utils import divide_bound, divide_bound_x, get_header_proxy
import epsg_transformer
from by_get_cookie import get_cookie
from circle import CircleDetector
from labeling import db_crud

COOKIE_TTL = 900  # 15 minutes
EMPTY_TILE_BYTES = 71625  # responses this size or smaller are treated as empty/unauthorized

BASEURL = (
    'https://services.atlas.bayern.de/proxy/wms/map/a29d0fce-6901-43c0-90ed-ac73b7780479?REQUEST=GetMap&SERVICE=WMS'
    '&VERSION=1.1.1&FORMAT=image/png&STYLES=&TRANSPARENT=true&LAYERS=by_alkis_flurkarte_umr_gelb&SRS=EPSG:3857&&WIDTH=4010&HEIGHT=4010'
)


# ----------------------------------------------------------------------------
# WMS download (same logic as scrap.py, single process / single proxy+cookie)
# ----------------------------------------------------------------------------
class Scraper:
    def __init__(self):
        self.proxy_list = common.load_proxies()
        if not self.proxy_list:
            raise Exception("No proxies configured")
        self.idx = 0
        self.cookie = None
        self.cookie_created_at = 0.0
        self.max_retries = 100
        self.failed_requests = []

    def parse_proxy(self, proxy_line):
        parts = proxy_line.split(":")
        host = parts[0]
        port = parts[1]
        username = parts[2] if len(parts) > 2 else None
        password = parts[3] if len(parts) > 3 else None
        return f"{username}:{password}@{host}:{port}"

    def current_proxy(self):
        return self.parse_proxy(self.proxy_list[self.idx])

    def switch_proxy(self):
        self.idx = (self.idx + 1) % len(self.proxy_list)
        self.cookie = None
        self.cookie_created_at = 0.0
        print(f"========[PROXY] Switch to proxy index {self.idx}")

    def ensure_cookie(self):
        if self.cookie and (time.time() - self.cookie_created_at) < COOKIE_TTL:
            return self.cookie
        proxy = self.current_proxy()
        print(f"========[COOKIE] Creating new cookie for proxy index {self.idx} - {proxy}")
        self.cookie = get_cookie(proxy)
        self.cookie_created_at = time.time()
        print(f"========[COOKIE] Success: {self.cookie}")
        return self.cookie

    def scraping(self, uri):
        retries = 0
        last_error = None
        while retries < self.max_retries:
            try:
                cookie = self.ensure_cookie()
                headers = get_header_proxy('services.atlas.bayern.de', cookie, 'services.atlas.bayern.de')
                proxy = self.current_proxy()
                proxies = {'http': f'http://{proxy}', 'https': f'http://{proxy}'}
                response = requests.get(uri, headers=headers, verify=False, proxies=proxies, stream=True)
                response.raise_for_status()
                return response
            except Exception as e:
                retries += 1
                last_error = e
                print(f"request failed. switch to next proxy and create a new cookie: {e}")
                self.switch_proxy()
                continue
        self.failed_requests.append(common.request_error(last_error, uri))
        raise Exception(f"Failed to scrap {uri} after {self.max_retries} retries")


def download_tiles(scraper, square_bbox, path):
    if not os.path.exists(path):
        os.makedirs(path, exist_ok=True)

    step = 1000
    # Tile exactly as scrap.py: 1000-unit rows (y), then 1000-unit tiles (x).
    bound1s = divide_bound(list(square_bbox), step)
    tiles = []
    for bound1 in bound1s:
        tiles.extend(divide_bound_x(bound1, step))

    print(f"Total tiles to download: {len(tiles)}")
    for tile in tiles:
        expanded_bbox = [tile[0], tile[1], tile[2] + 10, tile[3] + 10]
        bbox_str = ','.join(str(v) for v in expanded_bbox)
        filename = '_'.join(str(v) for v in tile)
        out_path = f'{path}/{filename}.png'

        if os.path.exists(out_path):
            print(f'skip--- file exists: {filename}.png')
            continue

        try:
            pl = scraper.scraping(BASEURL + '&BBOX=' + bbox_str)
            # responses at/below the empty-tile size carry no flurkarte data
            if len(pl.content) <= EMPTY_TILE_BYTES:
                continue
            with open(out_path, 'wb') as f:
                for chunk in pl.iter_content(chunk_size=8192):
                    f.write(chunk)
            print(f'  Saved {filename}.png')
        except Exception as e:
            print(f'  Error downloading {filename}: {e}')
            continue


# ----------------------------------------------------------------------------
# Vertex detection (same logic as main.py, single process)
# ----------------------------------------------------------------------------
def detect_circles(image_path, start_point, resolution):
    detector = CircleDetector(
        image_path=image_path,
        kernel_size=7,
        threshold=1,
        start_point=start_point,
        resolution=resolution
    )
    return detector.detect_circles()


def detect_vertices(path):
    """Run circle detection over every downloaded tile and return distinct
    (x, y) vertex points transformed from EPSG:3857 to EPSG:25832."""
    transformer = Transformer.from_crs("EPSG:3857", "EPSG:25832", always_xy=True)

    files = [f for f in os.listdir(path) if os.path.splitext(f)[1] == ".png"]
    print(f"Total tiles to process: {len(files)}")

    points = set()
    for i, filename in enumerate(files, 1):
        name, _ = os.path.splitext(filename)
        try:
            parts = name.split("_")
            xmin = float(parts[0])
            xmax = float(parts[2]) + 10
            ymax = float(parts[3]) + 10
            resolution = (xmax - xmin) / 4010

            image_path = os.path.join(path, filename)
            circles = detect_circles(image_path, (xmin, ymax), resolution)
            if circles:
                xs, ys = zip(*circles)
                xs_t, ys_t = transformer.transform(xs, ys)
                points.update(zip(xs_t, ys_t))
        except Exception as e:
            print(f"Failed {filename}: {e}")
        print(f"Processed tile {i} of {len(files)}")

    return points


def store_vertices(conn, table, points):
    db_crud.drop_table_if_exist(conn, table)
    query = f"""
    CREATE TABLE IF NOT EXISTS public.{table} (
        id character varying PRIMARY KEY,
        geom geometry
    );
    """
    db_crud.run_query(conn, query)

    if not points:
        print(f"{table} (0 Rows)")
        return

    rows = [(str(i), x, y) for i, (x, y) in enumerate(points)]
    with conn.cursor() as cur:
        execute_values(
            cur,
            f"INSERT INTO public.{table} (id, geom) VALUES %s",
            rows,
            template="(%s, ST_SetSRID(ST_MakePoint(%s, %s), 25832))"
        )
    conn.commit()

    total_cnt = db_crud.get_table_row_count(conn, table)
    print(f"{table} ({total_cnt} Rows)")


# ----------------------------------------------------------------------------
def main(run_id, bbox, skip_download=False):
    path = os.path.join(cfg.vertex_path, run_id)
    table = f"by_bbox_vertex_{run_id}"

    db_config = {
        "dbname": cfg.dbname,
        "user": cfg.user,
        "password": cfg.password,
        "host": cfg.host,
        "port": cfg.port
    }

    # --- Stage 1: download flurkarte tiles for the run bbox (EPSG:3857 grid) ---
    if skip_download:
        print("Skipping flurkarte download (--skip-download).")
    else:
        square_bbox = epsg_transformer.convert_bbox_epsg("25832", "3857", bbox)
        start_time = time.time()
        scraper = Scraper()
        download_tiles(scraper, list(square_bbox), path)
        print(f"WMS download elapsed: {time.time() - start_time:.2f} seconds")
        common.exit_if_failed_requests("vertex/runtime.py", scraper.failed_requests, run_id)

    # --- Stage 2: detect vertex positions and store them ---
    start_time = time.time()
    points = detect_vertices(path)
    connection = db_crud.create_connection(db_config)
    store_vertices(connection, table, points)
    db_crud.close_connection(connection)
    print(f"Vertex detection elapsed: {time.time() - start_time:.2f} seconds")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Download flurkarte tiles and detect vertices for a single run bbox.")
    parser.add_argument("run_id", help='Run id, e.g. "by_101125"')
    parser.add_argument("--skip-download", action="store_true",
                        help="Skip flurkarte download and only run vertex detection.")
    args = parser.parse_args()

    run_id = args.run_id

    # Read settings_<run_id>.ini (bbox for this run)
    cfg = load_config(run_id)
    bbox = cfg.bbox

    main(run_id, bbox, args.skip_download)
