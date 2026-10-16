import argparse
import importlib.util
import math
import os
import sys

import requests

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Reuse the exact tile size, DPI/scale, WMS params, pixel-size math and config
# from 1_download_wms.py so the generated URL and filename always match what the
# downloader actually produces. The module name starts with a digit, so it can
# only be loaded by path via importlib.
_dl_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "1_download_wms.py")
_spec = importlib.util.spec_from_file_location("_download_wms", _dl_path)
_dl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_dl)

cfg = _dl.cfg
TILE_SIZE = _dl.TILE_SIZE
dpi = _dl.dpi
scale = _dl.scale


def get_filename_from_point(x, y, run_id=None, extent_bbox=None):
    """Return (wms_url, filename) for the tile that contains point (x, y).

    The point must be in EPSG:25832 (the WMS srs the downloader uses). The tile
    grid matches 1_download_wms.py: an integer TILE_SIZE-meter grid anchored at
    the extent's min corner (default extent: cfg.by_whole_bbox).

    If run_id is given, filename includes the same output folder the downloader
    writes to; otherwise just the bare tile filename is returned.
    """
    if extent_bbox is None:
        extent_bbox = cfg.by_whole_bbox

    # Grid origin: the downloader uses range(int(xmin), ...), so the tile grid is
    # anchored at the integer-truncated min corner.
    origin_x = int(extent_bbox[0])
    origin_y = int(extent_bbox[1])

    kx = math.floor((x - origin_x) / TILE_SIZE)
    ky = math.floor((y - origin_y) / TILE_SIZE)

    tile_x = origin_x + kx * TILE_SIZE
    tile_y = origin_y + ky * TILE_SIZE
    tile_bbox = (tile_x, tile_y, tile_x + TILE_SIZE, tile_y + TILE_SIZE)

    base = f"wms_tile_{tile_x}_{tile_y}_{tile_x + TILE_SIZE}_{tile_y + TILE_SIZE}.tif"
    if run_id:
        folder = f"{cfg.building_path}_{run_id}/{TILE_SIZE}_{dpi}_{scale}/"
        filename = folder + base
    else:
        filename = base

    # Build the WMS GetMap URL exactly as download_wms_tiff_to_memory does.
    width_px, height_px = _dl.calculate_wms_pixel_size_from_bbox(tile_bbox, dpi, scale)
    params = _dl.WMS_PARAMS_BASE.copy()
    params.update({
        "bbox": ",".join(map(str, tile_bbox)),
        "WIDTH": str(width_px),
        "HEIGHT": str(height_px),
    })
    wms_url = requests.Request("GET", cfg.building_url, params=params).prepare().url

    return wms_url, filename


if __name__ == "__main__":
    #########################################
    # python get_filename_from_point.py 498452.13 5236257.43
    # python get_filename_from_point.py 498452.13 5236257.43 --id 20251113_050000
    #########################################
    parser = argparse.ArgumentParser(
        description="Get the WMS download URL and tile filename for a point (EPSG:25832)."
    )
    parser.add_argument("x", type=float, help="Easting in EPSG:25832.")
    parser.add_argument("y", type=float, help="Northing in EPSG:25832.")
    parser.add_argument("--id", default=None,
                        help="Run ID; if given, the filename includes the output folder.")
    args = parser.parse_args()

    bbox = cfg.by_whole_bbox
    if not (bbox[0] <= args.x < bbox[2] and bbox[1] <= args.y < bbox[3]):
        print(f"warning: point ({args.x}, {args.y}) is outside by_whole_bbox {bbox}",
              file=sys.stderr)

    url, filename = get_filename_from_point(args.x, args.y, args.id)
    print(f"filename: {filename}")
    print(f"url: {url}")
