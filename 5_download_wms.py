import os
import sys
import time
import requests
import multiprocessing
from io import BytesIO
import argparse

import common
from read_settings import load_config

cfg = load_config()
run_id = ""

# --- Constants ---
TILE_SIZE = 600  # meters
dpi = 100
scale = cfg.scale
FILE_FOLDER = ""

# Global flags
SAVE_TIFF_TO_DISK = True  # Set to False to skip saving
OVERWRITE_EXISTING = False  # Set to True to always overwrite existing tiles

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

# Format proxy string into requests proxy dict
def proxy_dict(proxy_str):
    return {
        'http': f'http://{proxy_str}',
        'https': f'http://{proxy_str}'
    }

def calculate_wms_pixel_size_from_bbox(bbox, dpi, scale):
    xmin, ymin, xmax, ymax = bbox
    width_m = xmax - xmin
    height_m = ymax - ymin
    width_px = int(width_m * 100 * dpi / 2.54 / scale)
    height_px = int(height_m * 100 * dpi / 2.54 / scale)
    return width_px, height_px

def download_wms_tiff_to_memory(bbox, proxy_queue):
    width_px, height_px = calculate_wms_pixel_size_from_bbox(bbox, dpi, scale)
    params = WMS_PARAMS_BASE.copy()
    params.update({
        "bbox": ",".join(map(str, bbox)),
        "WIDTH": str(width_px),
        "HEIGHT": str(height_px)
    })

    while True:
        proxy = proxy_queue.get()  # Get next proxy
        proxy_queue.put(proxy)    # Rotate back

        proxies = proxy_dict(proxy) if proxy else None
        try:
            print("Before downloading: " + proxy)
            response = requests.get(cfg.wms_url, params=params, timeout=(5, 60), proxies=proxies)
            print("After downloading: " + proxy + " : " + response.url)
            response.raise_for_status()
            return BytesIO(response.content)
        except Exception as e:
            print(f"Download failed using proxy {proxy}: {e}. Retrying in 5 seconds...")
            time.sleep(5)

# Worker function for multiprocessing
def process_tile(task_and_proxy_queue):
    task, proxy_queue = task_and_proxy_queue
    x, y, TILE_SIZE = task
    tile_bbox = (x, y, x + TILE_SIZE, y + TILE_SIZE)
    filename = FILE_FOLDER + f"wms_tile_{x}_{y}_{x + TILE_SIZE}_{y + TILE_SIZE}.tif"

    if not OVERWRITE_EXISTING and os.path.exists(filename):
        print(f"  Skipping existing tile: {filename}")
        return

    try:
        print(f"  Downloading WMS tile {tile_bbox} ...")
        tiff_stream = download_wms_tiff_to_memory(tile_bbox, proxy_queue)

        if SAVE_TIFF_TO_DISK:
            with open(filename, "wb") as f:
                f.write(tiff_stream.getbuffer())
            print(f"  Saved TIFF to {filename}")
    except Exception as e:
        print(f"  Error processing tile {tile_bbox}: {e}")

def download_tiles(extent_bbox, num_workers=4):
    xmin, ymin, xmax, ymax = extent_bbox
    print(f"Processing area: ({xmin}, {ymin}) to ({xmax}, {ymax})")

    proxies = common.load_proxies()
    if not proxies:
        proxies = [None]  # Fallback to no proxy

    manager = multiprocessing.Manager()
    proxy_queue = manager.Queue()
    for proxy in proxies:
        proxy_queue.put(proxy)

    # Generate tile tasks
    tasks = []
    for x in range(int(xmin), int(xmax), TILE_SIZE):
        for y in range(int(ymin), int(ymax), TILE_SIZE):
            tasks.append(((x, y, TILE_SIZE), proxy_queue))

    total_tiles = len(tasks)
    print(f"Total tiles to process: {total_tiles}")

    with multiprocessing.Pool(processes=num_workers) as pool:
        pool.map(process_tile, tasks)

def main(bbox):
    start_time = time.time()
    download_tiles(bbox, num_workers=cfg.cpu_cores)
    end_time = time.time()
    elapsed = end_time - start_time
    print(f"Total elapsed time: {elapsed:.2f} seconds")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python 5_download_wms.py <id>")
        print('Example command format: "python 5_download_wms.py 20251113_050000"')
        sys.exit(1)

    parser = argparse.ArgumentParser()
    parser.add_argument("id", help="Run ID")
    parser.add_argument("--bbox", type=str, required=False, default="", help="Bounding box")

    args = parser.parse_args()

    FILE_FOLDER = f"{cfg.wms_path}_{args.id}/{TILE_SIZE}_{dpi}_{scale}/"

    if not os.path.exists(FILE_FOLDER):
        os.makedirs(FILE_FOLDER)
    
    # get bbox
    bbox = cfg.by_whole_bbox
    if args.bbox != "":
        bbox_arg = args.bbox
        parts = [p.strip() for p in bbox_arg.split(",")]
        if len(parts) != 4:
            raise ValueError("BBox must have exactly 4 values")

        minx, miny, maxx, maxy = map(float, parts)
        bbox = (minx, miny, maxx, maxy)

    main(bbox)

# Usage:
# python download_wms.py 498452.13 5236257.43 855762.27 5600434.59 --workers 8 --overwrite
# proxy file format (proxies.csv): user:pwd@ip:port (one per line)
