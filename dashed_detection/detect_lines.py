import os
import sys
import numpy as np
import cv2
from shapely import LineString, wkt
from shapely.geometry import Polygon
from PIL import Image
import time
from collections import defaultdict
from shapely.ops import unary_union
from shapely.geometry import LineString, MultiLineString, GeometryCollection

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from read_settings import load_config
from labeling import db_crud

run_id = ""
cfg = None
db_cadastral_param = {}

DASHED_LINE_LENGTH_RANGE = (1, 15)  # min and max length of dashed lines in pixels
TOTAL_TOUCH_LENGTH_THRESHOLD = 4  # minimum length of line that must lie on edge to be considered a match
OVERLAP_LENGTH_THRESHOLD = 3.0   # minimum overlap length in pixels to consider shared
BUFFER_TOLERANCE = 1.0           # tolerance for near-collinear / tiny offsets
EDGE_KEY_ROUND = 2               # still used for quick exact-match shortcut

def get_image_size(minx, miny, maxx, maxy):
    img_width = (maxx - minx) * 4010 / 587
    img_height = (maxy - miny) * 4010 / 587

    return int(img_width), int(img_height)

def world_to_pixel(x, y, left, top, px_size_x, px_size_y):
    return (
        (x - left) / px_size_x,# + 0.5,
        (top - y) / px_size_y# + 0.5
    )

def draw_polygon_image(type):
    # --------------------------------------------------
    # DRAW POLYGONS INTO IMAGE
    # --------------------------------------------------

    minx, miny, maxx, maxy = cfg.bbox

    # Prepare Grid
    bbox_width = maxx - minx
    bbox_height = maxy - miny

    img_width, img_height = get_image_size(minx, miny, maxx, maxy)
    print(f"Image Size: {img_width} x {img_height}")

    pixel_size_x = bbox_width / img_width
    pixel_size_y = bbox_height / img_height

    # Get polygons
    conn = db_crud.create_connection(db_cadastral_param)

    bbox_str = f"{minx},{miny},{maxx},{maxy}"
    order_clause = "" if type == "building" else ", is_street ASC"
    where_clause = f"geom && ST_MakeEnvelope({bbox_str}, 25832) AND GeometryType(geom) IN ('POLYGON', 'MULTIPOLYGON')"

    query = f"""
        SELECT id, ST_AsText(geom)
        FROM by_bbox_{type}_{run_id}
        WHERE {where_clause}
        ORDER BY ST_Area(geom) DESC{order_clause}
    """
    rows = db_crud.run_query(conn, query)
    db_crud.close_connection(conn)

    poly_mask = np.full(
        (int(img_height), int(img_width)), 255,
        dtype=np.uint8
    )

    # Draw polygons
    for row in rows:
        geom = wkt.loads(row[1])
        if geom.is_empty:
            continue

        polygons = [geom] if isinstance(geom, Polygon) else geom.geoms

        for poly in polygons:
            pts = []

            for x, y in poly.exterior.coords:
                px, py = world_to_pixel(
                    x, y,
                    minx, maxy,
                    pixel_size_x, pixel_size_y
                )
                pts.append([int(round(px)), int(round(py))])

            if len(pts) >= 2:
                pts = np.array(pts, dtype=np.int32)
                thickness = 1
                cv2.polylines(
                    poly_mask,
                    [pts],
                    isClosed=True,
                    color=0,                 # white mask
                    thickness=thickness
                )

    # Save drawing to file
    db_png_path = f"{cfg.dashed_wms_path}/{run_id}_2_{type}_drawn.png"
    Image.fromarray(poly_mask).save(db_png_path)
    
    return db_png_path, rows

def get_difference(wms_path, geom_path, type):
    # Read images in grayscale
    img_wms = cv2.imread(wms_path, cv2.IMREAD_GRAYSCALE)
    img_geom = cv2.imread(geom_path, cv2.IMREAD_GRAYSCALE)

    # Safety check
    if img_wms is None or img_geom is None:
        raise ValueError("One or both images could not be loaded")

    geom_black = (img_geom == 0)
    wms_nonblack = (img_wms > 0)

    wms_black = (img_wms == 0).astype(np.uint8)

    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    wms_black_dilated = cv2.dilate(wms_black, kernel)

    mask = geom_black & wms_nonblack & (wms_black_dilated == 0)

    mask_img = mask.astype(np.uint8) * 255
    mask_inv = cv2.bitwise_not(mask_img)

    diff_img_path = f"{cfg.dashed_wms_path}/{run_id}_3_{type}_difference.png"
    cv2.imwrite(diff_img_path, mask_inv)

    return diff_img_path


def skeletonize(binary_img):
    skeleton = np.zeros(binary_img.shape, np.uint8)
    element = cv2.getStructuringElement(cv2.MORPH_CROSS, (3, 3))
    img = binary_img.copy()

    while True:
        eroded = cv2.erode(img, element)
        temp = cv2.dilate(eroded, element)
        temp = cv2.subtract(img, temp)
        skeleton = cv2.bitwise_or(skeleton, temp)
        img = eroded

        if cv2.countNonZero(img) == 0:
            break

    return skeleton

def get_neighbors(y, x, img):
    neighbors = []
    for dy in [-1, 0, 1]:
        for dx in [-1, 0, 1]:
            if dy == 0 and dx == 0:
                continue
            ny, nx = y + dy, x + dx
            if 0 <= ny < img.shape[0] and 0 <= nx < img.shape[1]:
                if img[ny, nx] > 0:
                    neighbors.append((ny, nx))
    return neighbors

def extract_all_lines(skel):
    visited = set()
    lines = []

    ys, xs = np.where(skel > 0)
    pixels = list(zip(ys, xs))

    def trace(start):
        line = []
        current = start
        prev = None

        while True:
            line.append((current[1], current[0]))  # (x, y)
            visited.add(current)

            neighbors = get_neighbors(current[0], current[1], skel)
            neighbors = [n for n in neighbors if n != prev]

            # stop if:
            # - no neighbor
            # - branching
            # - dead end
            unvisited = [n for n in neighbors if n not in visited]

            if len(unvisited) != 1:
                break

            prev = current
            current = unvisited[0]

        return line

    for p in pixels:
        if p in visited:
            continue

        neighbors = get_neighbors(p[0], p[1], skel)

        # Isolated pixel
        if len(neighbors) == 0:
            visited.add(p)
            lines.append([(p[1], p[0]), (p[1], p[0])])  # single point line
            continue

        # Start tracing
        line = trace(p)
        if line:
            lines.append(line)

    return lines

def get_lines(diff_img_path, type):
    mask_img = cv2.imread(diff_img_path, cv2.IMREAD_GRAYSCALE)
    _, binary = cv2.threshold(
        mask_img, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU
    )
    skeleton = skeletonize(binary)

    line_img_path = f"{cfg.dashed_wms_path}/{run_id}_4_{type}_skeleton.png"
    cv2.imwrite(line_img_path, skeleton)

    lines = extract_all_lines(skeleton)

    # Filter lines with less than 10 pixels
    filtered_lines = [line for line in lines if DASHED_LINE_LENGTH_RANGE[0] <= len(line) <= DASHED_LINE_LENGTH_RANGE[1]]

    save_lines_png(filtered_lines, skeleton.shape)

    return filtered_lines


def save_lines_png(lines, img_shape, thickness=1, color=(255, 255, 255)):
    """
    lines: list of [(x, y), (x, y), ...]
    img_shape: (height, width)
    """
    out_path = f"{cfg.dashed_wms_path}/{run_id}_5_parcel_lines.png"
    canvas = np.zeros((img_shape[0], img_shape[1], 3), dtype=np.uint8)

    for line in lines:
        for (x1, y1), (x2, y2) in zip(line[:-1], line[1:]):
            cv2.line(
                canvas,
                (int(x1), int(y1)),
                (int(x2), int(y2)),
                color,
                thickness
            )

    cv2.imwrite(out_path, canvas)

    return out_path

def _normalize_edge_key(edge: LineString, ndigits=EDGE_KEY_ROUND):
    """Undirected edge key from endpoints (fast exact match shortcut)."""
    a, b = list(edge.coords)
    a = (round(a[0], ndigits), round(a[1], ndigits))
    b = (round(b[0], ndigits), round(b[1], ndigits))
    return tuple(sorted([a, b]))


def _safe_overlap_length(line1: LineString, line2: LineString, tol=BUFFER_TOLERANCE) -> float:
    """
    Robust overlap length between two edges in pixel space.
    Handles exact overlap and slight offsets using a small buffer tolerance.
    """
    try:
        # Fast path: exact geometric intersection
        inter = line1.intersection(line2)
        if not inter.is_empty and inter.length > 0:
            return float(inter.length)

        # Fallback: tolerance-based overlap
        # Intersect line2 with a thin tube around line1
        inter2 = line2.intersection(line1.buffer(tol, cap_style=2, join_style=2))
        if not inter2.is_empty:
            return float(inter2.length)

        return 0.0
    except Exception:
        return 0.0

def _collect_lines(geom):
    """Normalize geometry result to a list[LineString]."""
    if geom is None or geom.is_empty:
        return []

    if isinstance(geom, LineString):
        return [geom] if geom.length > 0 else []

    if isinstance(geom, MultiLineString):
        return [g for g in geom.geoms if g.length > 0]

    if isinstance(geom, GeometryCollection):
        return [g for g in geom.geoms if isinstance(g, LineString) and g.length > 0]

    return []

def get_processed_polygon_edges(lines, polygon_list):
    """
    lines: detected line list in PIXEL coordinates (each line = [(x1,y1), (x2,y2), ...])
    polygon_list: [(polygon_id, polygon_wkt), ...]
    type: unused currently (kept for compatibility)

    Returns:
    {
      "polygon_id_list": [...],
      "results": [
        {
          "polygon_id": ...,
          "touched_boundary_lines": [{"polygon_index","edge_idx","edge":[(x,y),...]}, ...],     # EPSG coords
          "not_touched_boundary_lines": [{"polygon_index","edge_idx","part_idx","edge":[...]},...], # EPSG coords
          "total_touch_length": ...
        },
        ...
      ]
    }
    """
    minx, miny, maxx, maxy = cfg.bbox

    # Detection lines (pixel coords)
    linestrings = [
        LineString([(int(x), int(y)) for x, y in line])
        if len(line) >= 2
        else LineString([
            (int(line[0][0]), int(line[0][1])),
            (int(line[0][0]), int(line[0][1]))
        ])
        for line in lines
        if len(line) >= 1
    ]

    # Image/world transform
    # World->pixel transform params
    bbox_width = maxx - minx
    bbox_height = maxy - miny
    img_width, img_height = get_image_size(minx, miny, maxx, maxy)

    pixel_size_x = bbox_width / img_width
    pixel_size_y = bbox_height / img_height

    # Convert pixel tolerance to world tolerance (for EPSG geometry subtraction)
    world_tol = max(pixel_size_x, pixel_size_y) * BUFFER_TOLERANCE
    
    # ------------------------------------------------------------------
    # 1) Build all parcel edges with BOTH world and pixel geometries
    # ------------------------------------------------------------------
    edges_by_parcel = defaultdict(list)
    all_edge_records = []

    for polygon in polygon_list:
        polygon_id = polygon[0]
        geom = wkt.loads(polygon[1])

        if geom.is_empty:
            continue

        polygons = [geom] if isinstance(geom, Polygon) else list(geom.geoms)

        for poly_index, poly in enumerate(polygons):
            rings = [poly.exterior] + list(poly.interiors)  # outer + holes
            for ring in rings:
                coords = list(ring.coords)
                for i in range(len(coords) - 1):
                    # World/EPSG edge (OUTPUT geometry)
                    edge_world = LineString([coords[i], coords[i + 1]])

                    # Pixel edge (MATCHING geometry)
                    p1 = world_to_pixel(coords[i][0], coords[i][1], minx, maxy, pixel_size_x, pixel_size_y)
                    p2 = world_to_pixel(coords[i + 1][0], coords[i + 1][1], minx, maxy, pixel_size_x, pixel_size_y)
                    edge_pixel = LineString([p1, p2])

                    rec = {
                        "polygon_id": polygon_id,
                        "polygon_index": poly_index,           # subpolygon index for multipolygon
                        "edge_idx": i,

                        "edge_world": edge_world,
                        "edge_pixel": edge_pixel,
                        "edge_key": _normalize_edge_key(edge_pixel),

                        "detected_touch_score": 0.0,     # matching score from detection lines (pixel-space)
                        "claimed": False,                # final touched ownership after conflict resolution
                        "remaining_not_touched_parts_world": [],  # filled later
                    }

                    edges_by_parcel[polygon_id].append(rec)
                    all_edge_records.append(rec)

    # quick lookup for exact pixel endpoint matches
    edge_records_by_key = defaultdict(list)
    for rec in all_edge_records:
        edge_records_by_key[rec["edge_key"]].append(rec)

    # ------------------------------------------------------------------
    # 2) Detect touched edges in pixel space (using your existing matcher)
    # ------------------------------------------------------------------
    for polygon_id, edge_records in edges_by_parcel.items():
        parcel_edges_pixel = [r["edge_pixel"] for r in edge_records]
        matched_edges_pixel = edges_containing_line(parcel_edges_pixel, linestrings, BUFFER_TOLERANCE)

        for matched_edge in matched_edges_pixel:
            m_key = _normalize_edge_key(matched_edge)

            # Fast exact-match path
            exact_candidates = edge_records_by_key.get(m_key, [])
            if exact_candidates:
                # shared edges can exist on multiple parcels -> all get a candidate score for now
                for rec in exact_candidates:
                    rec["detected_touch_score"] += float(matched_edge.length)
                continue

            # Fallback for rounding mismatch: overlap against this parcel's edges
            for rec in edge_records:
                ov = _safe_overlap_length(matched_edge, rec["edge_pixel"], tol=BUFFER_TOLERANCE)
                if ov >= OVERLAP_LENGTH_THRESHOLD:
                    rec["detected_touch_score"] += ov

    # ------------------------------------------------------------------
    # 3) Conflict resolution across parcels (partial-overlap aware)
    #    Keep touched ownership on only ONE parcel for overlapping/shared boundaries
    # ------------------------------------------------------------------
    touched_candidates = [r for r in all_edge_records if r["detected_touch_score"] > 0]
    candidate_touched_polygon_ids = {r["polygon_id"] for r in touched_candidates}

    # strongest candidates claim first
    touched_candidates.sort(
        key=lambda r: (r["detected_touch_score"], r["edge_pixel"].length),
        reverse=True
    )

    claimed_records = []

    for rec in touched_candidates:
        if rec["claimed"]:
            continue

        cur_edge_px = rec["edge_pixel"]
        cur_parcel = rec["polygon_id"]

        conflicting_claim = None
        max_overlap = 0.0

        for claimed in claimed_records:
            if claimed["polygon_id"] == cur_parcel:
                continue  # same parcel can keep multiple touched edges

            ov = _safe_overlap_length(cur_edge_px, claimed["edge_pixel"], tol=BUFFER_TOLERANCE)
            if ov >= OVERLAP_LENGTH_THRESHOLD and ov > max_overlap:
                max_overlap = ov
                conflicting_claim = claimed

        if conflicting_claim is None:
            rec["claimed"] = True
            claimed_records.append(rec)
        else:
            # choose one owner
            winner = sorted(
                [rec, conflicting_claim],
                key=lambda r: (r["detected_touch_score"], r["edge_pixel"].length, str(r["polygon_id"])),
                reverse=True
            )[0]
            loser = rec if winner is conflicting_claim else conflicting_claim

            winner["claimed"] = True
            loser["claimed"] = False

            if winner not in claimed_records:
                claimed_records.append(winner)

            # keep list clean
            claimed_records = [c for c in claimed_records if c["claimed"]]

    # ------------------------------------------------------------------
    # 4) Remove touched geometry from not-touched edges IN WORLD/EPSG SPACE
    #    so dashed boundaries won't draw on top of solid duplicates
    # ------------------------------------------------------------------
    claimed_touched_world = [r["edge_world"] for r in all_edge_records if r["claimed"]]
    touched_union_world = unary_union(claimed_touched_world) if claimed_touched_world else None

    for rec in all_edge_records:
        rec["remaining_not_touched_parts_world"] = []

        if rec["claimed"]:
            continue  # touched edge is rendered as dashed, not solid

        edge_world = rec["edge_world"]

        if touched_union_world is None or touched_union_world.is_empty:
            rec["remaining_not_touched_parts_world"] = [edge_world]
            continue

        try:
            # small world-space buffer to avoid tiny precision gaps
            diff = edge_world.difference(
                touched_union_world.buffer(world_tol, cap_style=2, join_style=2)
            )
        except Exception:
            diff = edge_world

        rec["remaining_not_touched_parts_world"] = _collect_lines(diff)

    # ------------------------------------------------------------------
    # 5) Build final output (EPSG/world coordinates only)
    # ------------------------------------------------------------------
    results = []
    polygon_id_list = []

    for polygon_id, edge_records in edges_by_parcel.items():
        touched_boundary_lines = []
        not_touched_boundary_lines = []
        total_touch_length = 0.0

        for rec in edge_records:
            if rec["claimed"]:
                touched_boundary_lines.append({
                    "polygon_index": rec["polygon_index"],
                    "edge_idx": rec["edge_idx"],
                    "edge": list(rec["edge_world"].coords),  # EPSG coords
                })
                total_touch_length += rec["edge_world"].length
            else:
                for part_idx, part in enumerate(rec.get("remaining_not_touched_parts_world", [])):
                    not_touched_boundary_lines.append({
                        "polygon_index": rec["polygon_index"],
                        "edge_idx": rec["edge_idx"],
                        "part_idx": part_idx,
                        "edge": list(part.coords),             # EPSG coords
                    })

        if total_touch_length >= TOTAL_TOUCH_LENGTH_THRESHOLD:
            polygon_id_list.append(polygon_id)

        if polygon_id in candidate_touched_polygon_ids:
            results.append({
                "polygon_id": polygon_id,
                "touched_boundary_lines": touched_boundary_lines,
                "not_touched_boundary_lines": not_touched_boundary_lines,
                "total_touch_length": total_touch_length,
            })

    # # Draw geom and edges for visualization
    # fig, ax = plt.subplots(figsize=(6, 6))

    # # Draw all polygon edges in BLUE
    # for one in results:
    #     # Draw touched edges in RED
    #     for edge in one["touched_boundary_lines"]:
    #         plot_linestring(ax, edge["edge"], color='red', linewidth=1)

    #     # Draw untouched edges in GRAY
    #     for edge in one["not_touched_boundary_lines"]:
    #         plot_linestring(ax, edge["edge"], color='gray', linewidth=1)
    
    # ax.set_aspect('equal')
    # ax.invert_yaxis()  # IMPORTANT for pixel coordinates
    # ax.set_title("Gray: Not touched edges | Red: Touched edges")
    # plt.show()

    return {
        "polygon_id_list": polygon_id_list,
        "results": results,
    }
    
def plot_linestring(ax, ls, color, linewidth=2, linestyle='-'):
    line = LineString([ls[0], ls[1]])
    x, y = line.xy
    ax.plot(x, y, color=color, linewidth=linewidth, linestyle=linestyle)
    
def edges_containing_line(
    edges,
    lines,
    tol
):
    matched_edges = []
    buffered = [line.buffer(tol) for line in lines]
    merged = unary_union(buffered)
    
    for edge in edges:
        # Portion of line that lies on the edge
        intersection = edge.intersection(merged)
        if intersection.is_empty or intersection.length < 1:
            continue

        matched_edges.append(edge)

    return matched_edges

def update_boundary_lines(result_list, type):

    conn = db_crud.create_connection(db_cadastral_param)

    query = f"""
        UPDATE by_bbox_{type}_{run_id}
        SET 
            is_dashed = true,
            solid_boundary = ST_GeomFromText(%s, 25832),
            dashed_boundary = ST_GeomFromText(%s, 25832)
        WHERE id=%s
    """

    for result in result_list:
        polygon_id = result["polygon_id"]
        touched_lines = [LineString(line["edge"]) for line in result["touched_boundary_lines"]]
        touched_lines = unary_union(touched_lines)
        not_touched_lines = [LineString(line["edge"]) for line in result["not_touched_boundary_lines"]]
        not_touched_lines = unary_union(not_touched_lines)

        if type == "building" and not_touched_lines.is_empty:
            db_crud.run_query(conn, query, (not_touched_lines.wkt, touched_lines.wkt, polygon_id))
        elif type == "parcel":
            db_crud.run_query(conn, query, (not_touched_lines.wkt, touched_lines.wkt, polygon_id))
    
    db_crud.close_connection(conn)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python detect_lines.py <id>")
        print('Example command format: "python detect_lines.py 20251113_050000"')
        sys.exit(1)

    run_id = sys.argv[1]
    cfg = load_config(run_id)

    db_cadastral_param = {
        "dbname": cfg.dbname,
        "user": cfg.user,
        "password": cfg.password,
        "host": cfg.host,
        "port": cfg.port
    }

    wms_path = os.path.join(cfg.dashed_wms_path, run_id + "_1_wms.png")
    if not os.path.exists(wms_path):
        print(f"WMS image not found at {wms_path}. Please run download_wms.py first to fetch the image.")
        sys.exit(1)

    start = time.time()

    type_list = ["parcel", "building"]
    for type in type_list:
        geom_path, polygon_list = draw_polygon_image(type)
        different_path = get_difference(wms_path, geom_path, type)
        line_list = get_lines(different_path, type)

        result = get_processed_polygon_edges(line_list, polygon_list)
        polygon_id_cnt = len(result["polygon_id_list"])
        print(f"Detected dashed line on {type} count: {polygon_id_cnt}")

        update_boundary_lines(result["results"], type)

    elapsed = time.time() - start
    print(f"{elapsed:.3f} seconds to detect dashed lines.")
