import argparse
import math
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from read_settings import load_config
import epsg_transformer

cfg = load_config()

STEP = 1000


def get_filename(x, y, src_epsg="25832", step=STEP):
    """Return the tile filename (without extension) that contains point (x, y).

    The tile grid matches scrap.py: the whole bbox (cfg.by_whole_bbox, given in
    EPSG:25832) is converted to EPSG:3857 and divided into `step`-unit tiles
    anchored at the bbox min corner. The filename is the tile bounds joined by
    '_', i.e. "x0_y0_x1_y1".
    """
    # Origin of the grid: bbox min corner in EPSG:3857.
    x0_origin, y0_origin, _, _ = epsg_transformer.convert_bbox_epsg(
        "25832", "3857", cfg.by_whole_bbox
    )

    # Point in EPSG:3857 (same CRS the filenames are in).
    px, py, _, _ = epsg_transformer.convert_bbox_epsg(
        src_epsg, "3857", (x, y, x, y)
    )

    kx = math.floor((px - x0_origin) / step)
    ky = math.floor((py - y0_origin) / step)

    x0 = x0_origin + kx * step
    y0 = y0_origin + ky * step
    x1 = x0_origin + (kx + 1) * step
    y1 = y0_origin + (ky + 1) * step

    return '_'.join(str(v) for v in [x0, y0, x1, y1])


if __name__ == "__main__":
    #########################################
    # python get_filename.py 491906.3 5217361.5
    # python get_filename.py 1234567.8 6543210.9 --epsg 3857
    #########################################
    parser = argparse.ArgumentParser(
        description="Get the tile filename that contains a point coordinate."
    )
    parser.add_argument("x", type=float, help="X / easting (or longitude with --epsg 4326).")
    parser.add_argument("y", type=float, help="Y / northing (or latitude with --epsg 4326).")
    parser.add_argument("--epsg", default="25832",
                        help="EPSG code of the input point (default: 25832).")
    args = parser.parse_args()

    filename = get_filename(args.x, args.y, args.epsg)
    print(filename + ".png")
