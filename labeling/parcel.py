# Parcel labeling (Except street)

import math
from multiprocessing import Pool, Manager
import numpy as np
from psycopg2.extensions import connection
from scipy.ndimage import distance_transform_edt
from shapely import wkt
from shapely.algorithms.polylabel import polylabel
from shapely.geometry import Polygon, MultiPolygon, LineString, MultiLineString, Point, box
from shapely.prepared import prep
from shapely.affinity import rotate, translate
from shapely.ops import unary_union
from typing import List, Any
import sys
import os

import db_crud
import time
import util

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from read_settings import load_config

cfg = None
db_cadastral_param = None
run_id = ""

# Add label rendering info to labels table
def insert_label_render(conn: connection, id: str, zaehler: str, nenner: str, pos: Point, angle_deg: int,  font_size: int, 
                        label_rect: Polygon, leader_line: LineString, splitter: str, is_nenner_only: bool, is_overlap: bool):

    if splitter == '/':
        label_text = f"{zaehler}/{nenner}"
        if zaehler is None or zaehler == '' or is_nenner_only:
            label_text = f"/{nenner}"
        if nenner is None or nenner == '':
            label_text = f"{zaehler}"

        pos_offset = [0, 1, -1, 2, -2]
        
        minx, miny, maxx, maxy = label_rect.bounds
        width = maxx - minx
        height = maxy - miny

        for x_off in pos_offset:
            for y_off in pos_offset:
                placed_label = util.translate_rotate_polygon(label_rect, width / 2 + x_off, height / 2 + y_off, 0)
                is_add_label = insert_label_render_one(conn, id, pos, angle_deg, label_text, font_size, placed_label, is_overlap)

                if is_add_label:
                    if leader_line is not None:
                        insert_line_render(conn, id, leader_line)
                    return True

    return False

# Insert label drawing info
def insert_label_render_one(conn, id, pos, angle, label, font_size, label_rect, is_overlap):
    # Check label overlapping
    query = f"""
        SELECT SUM(cnt) AS total_intersection_count
        FROM (
            SELECT COUNT(*) AS cnt
            FROM by_bbox_labels_{run_id}
            WHERE ST_Intersects(calculated_label_area, ST_GeomFromText(%s, 25832))

            UNION ALL

            SELECT COUNT(*) AS cnt
            FROM by_bbox_lines_{run_id}
            WHERE ST_Intersects(geom, ST_GeomFromText(%s, 25832))

            UNION ALL

            SELECT COUNT(*) AS cnt
            FROM by_bbox_symbols_{run_id}
            WHERE ST_Intersects(ST_Buffer(geom, 0.2), ST_GeomFromText(%s, 25832))
        ) t
    """
    result = db_crud.run_query(conn, query, (label_rect.wkt, label_rect.wkt, label_rect.wkt))

    if result[0][0] > 0:
        return False

    query = f"INSERT INTO by_bbox_labels_{run_id} (source_id, source, name, label_position, font_size, label_angle, alkis_angle, calculated_label_area, is_street, is_overlap) VALUES " \
        "(%s, %s, %s, ST_GeomFromText(%s, 25832), %s, %s, %s, ST_GeomFromText(%s, 25832), %s, %s)"
    
    # Update angle as draw text from LEFT to RIGHT, BOTTOM to TOP
    # bottom to top has higher weight
    # bottom to top wins if two competing race conditions exists
    angle = angle % 360
    if angle >= 120 and angle <= 300:
        angle = (angle + 180) % 360

    sld_rotation = util.convert_to_geoserver_rotation(angle)
    db_crud.run_query(conn, query, (id, 'parcel', label, pos.wkt, font_size, sld_rotation, angle, label_rect.wkt, False, is_overlap))

    return True

# Insert line drawing info
def insert_line_render(conn, id, geom):
    query = f"INSERT INTO by_bbox_lines_{run_id} (source_id, source, geom, is_street) VALUES " \
        "(%s, %s, ST_GeomFromText(%s, 25832), %s)"
    
    db_crud.run_query(conn, query, (id, 'parcel', geom.wkt, False))

# Get distance by point from street polygon
def get_distance_by_point_from_street(conn, point, run_id):
    query = f"""
    SELECT MIN(ST_Distance(geom, ST_SetSRID(ST_MakePoint(%s, %s), 25832)))
    FROM by_bbox_parcel_{run_id}
    WHERE is_street = true
    """

    distance_data = db_crud.run_query(conn, query, (float(point[0]), float(point[1])))
    return distance_data[0][0]

# Find label position with raster method
def get_label_position_candidates(conn, polygon, text, font_size, rotation, run_id):
    center_point = polygon.representative_point()
    label_x = center_point.x
    label_y = center_point.y
    
    width_m, height_m = util.get_label_width_height(text, font_size, 'parcel', True, False)

    # Raster resolution
    resolution = 100
    minx, miny, maxx, maxy = polygon.bounds
    width = maxx - minx
    height = maxy - miny

    x = np.linspace(minx, maxx, resolution)
    y = np.linspace(miny, maxy, resolution)
    xx, yy = np.meshgrid(x, y)
    coords = np.column_stack((xx.ravel(), yy.ravel()))

    # Inside check, respecting holes(Fast prepared geometry)
    prepared_geom = prep(polygon)
    mask = np.array([prepared_geom.contains(Point(p)) for p in coords]).reshape((resolution, resolution))

    # Distance transform
    dist = distance_transform_edt(mask)

    # Convert label size to pixels
    bounding_width = width_m
    bounding_height = height_m
    if rotation > 0:
        angle_rad = math.radians(rotation)
        # Compute bounding box dimensions
        bounding_width = abs(width_m * math.cos(angle_rad)) + abs(height_m * math.sin(angle_rad))
        bounding_height = abs(width_m * math.sin(angle_rad)) + abs(height_m * math.cos(angle_rad))
    
    px_label_width = int((bounding_width / width) * resolution)
    px_label_height = int((bounding_height / height) * resolution)
    label_radius = min(px_label_width, px_label_height) / 2

    # Find best position
    fits_mask = dist > label_radius
    if np.any(fits_mask):
        # Step 1: Get the indices where fits_mask is True
        yx = np.argwhere(fits_mask)

        # Step 2: Get the filtered dist values corresponding to the valid indices
        filtered_dist_values = dist[fits_mask]

        # Step 3: Get the indices that would sort dist values in descending order
        sorted_indices = np.argsort(filtered_dist_values)[::-1]  # Sort in descending order

        # Step 4: Get the top 2 indices
        top_indices = sorted_indices[:10]

        # Step 5: Get the corresponding yx (coordinates) of the top 2 values
        top_yx = yx[top_indices]

        # Step 6: Get points that nearest by street
        min_dist_street = -1
        min_label_x = -1
        min_label_y = -1
        for (top_y, top_x) in top_yx:
            label_x = x[top_x]
            label_y = y[top_y]
            distance_street = get_distance_by_point_from_street(conn, (label_x, label_y), run_id)
            if distance_street == None:
                label_x = center_point.x
                label_y = center_point.y
                break
            
            if min_dist_street < 0 or min_dist_street > distance_street:
                min_dist_street = distance_street
                min_label_x = label_x
                min_label_y = label_y
        
        if min_label_x >= 0 and min_label_y >= 0:
            label_x = min_label_x
            label_y = min_label_y
        else:
            label_x = center_point.x
            label_y = center_point.y    
    else:
        label_x = center_point.x
        label_y = center_point.y
        
    return Point(label_x, label_y)


def _candidate_angles(max_angle=10, step=2):
    """Prefer small rotations: 0, +step, -step, +2*step, -2*step, ..."""
    angles = [0]
    for a in range(step, max_angle + 1, step):
        angles.extend([a, -a])
    return angles

def _fits_axis_aligned(prepared_poly, cx, cy, w, h):
    """Fast check for axis-aligned rectangle inside polygon."""
    rect = box(cx - w / 2.0, cy - h / 2.0, cx + w / 2.0, cy + h / 2.0)
    return prepared_poly.contains(rect), rect


def _score(poly, rect, angle, angle_weight):
    """Higher clearance is better, smaller rotation is better."""
    clearance = poly.boundary.distance(rect.boundary)
    return clearance - angle_weight * abs(angle), clearance


def find_label_pose(
    target_wkt,
    label_wkt,
    max_angle=85,
    angle_step=8,
    coarse_step=1.0,
    refine_steps=(0.5, 0.2, 0.1),
    angle_weight=0.15,
    stop_if_zero_angle_found=True,
):
    """
    Find a fast, good-enough placement for a rotatable label rectangle inside
    a polygon-with-holes. The label is not allowed inside interior rings.

    Returns:
        {
            "x": center_x,
            "y": center_y,
            "angle": angle_deg,
            "clearance": ...,
            "score": ...
        }
        or None if no fit is found.
    """
    target_geom = wkt.loads(target_wkt)
    label_geom = wkt.loads(label_wkt)

    poly = util._get_main_polygon(target_geom)

    # Label dimensions from its bounds. Assumes label polygon is a rectangle.
    lminx, lminy, lmaxx, lmaxy = label_geom.bounds
    label_w = lmaxx - lminx
    label_h = lmaxy - lminy

    best = None

    for angle in _candidate_angles(max_angle=max_angle, step=angle_step):
        # Rotate polygon by -angle so the label can stay axis-aligned.
        rotated_poly = rotate(poly, -angle, origin="centroid", use_radians=False)
        prepared = prep(rotated_poly)

        minx, miny, maxx, maxy = rotated_poly.bounds

        # Search region for rectangle center
        x0 = minx + label_w / 2.0
        x1 = maxx - label_w / 2.0
        y0 = miny + label_h / 2.0
        y1 = maxy - label_h / 2.0

        if x0 > x1 or y0 > y1:
            continue

        local_best = None

        # ---------- coarse search ----------
        y = y0
        while y <= y1:
            x = x0
            while x <= x1:
                ok, rect = _fits_axis_aligned(prepared, x, y, label_w, label_h)
                if ok:
                    score, clearance = _score(rotated_poly, rect, angle, angle_weight)
                    if local_best is None or score > local_best["score"]:
                        local_best = {
                            "x": x,
                            "y": y,
                            "angle": angle,
                            "score": score,
                            "clearance": clearance,
                        }
                x += coarse_step
            y += coarse_step

        if local_best is None:
            continue

        # ---------- local refinement ----------
        for step in refine_steps:
            improved = True
            while improved:
                improved = False
                candidates = [
                    (local_best["x"], local_best["y"]),
                    (local_best["x"] + step, local_best["y"]),
                    (local_best["x"] - step, local_best["y"]),
                    (local_best["x"], local_best["y"] + step),
                    (local_best["x"], local_best["y"] - step),
                    (local_best["x"] + step, local_best["y"] + step),
                    (local_best["x"] + step, local_best["y"] - step),
                    (local_best["x"] - step, local_best["y"] + step),
                    (local_best["x"] - step, local_best["y"] - step),
                ]

                for cx, cy in candidates:
                    if not (x0 <= cx <= x1 and y0 <= cy <= y1):
                        continue
                    ok, rect = _fits_axis_aligned(prepared, cx, cy, label_w, label_h)
                    if not ok:
                        continue
                    score, clearance = _score(rotated_poly, rect, angle, angle_weight)
                    if score > local_best["score"]:
                        local_best = {
                            "x": cx,
                            "y": cy,
                            "angle": angle,
                            "score": score,
                            "clearance": clearance,
                        }
                        improved = True

        # Transform center back to original coordinates
        center_pt_rot = Point(local_best["x"], local_best["y"])
        center_pt_orig = rotate(center_pt_rot, angle, origin=poly.centroid, use_radians=False)

        candidate = {
            "x": center_pt_orig.x,
            "y": center_pt_orig.y,
            "angle": angle,
            "score": local_best["score"],
            "clearance": local_best["clearance"],
        }

        if best is None or candidate["score"] > best["score"]:
            best = candidate

        # Early exit if user prefers minimal rotation and 0° already works
        if stop_if_zero_angle_found and angle == 0 and best is not None:
            return best

    return best

def check_parcel_similar_building_label(conn, placed_label, angle_deg, font_size):
    if font_size == cfg.font_size_parcel[0]:
        return False
    
    # check near by building label exist
    query = f"""
        SELECT COUNT(id) FROM by_bbox_labels_{run_id} WHERE ST_Intersects(
            calculated_label_area, 
            ST_Buffer(
                ST_GeomFromText(%s, 25832),
                2
            )
        ) AND source = 'building'
    """
    result = db_crud.run_query(conn, query, (placed_label.wkt, ))
    if result[0][0] > 0:
        return True
    
    # check it on building boundary with similar angle
    query = f"""
        SELECT ST_AsText(geom) FROM by_bbox_building_{run_id} WHERE ST_Intersects(geom, 
            ST_Buffer(
                st_geomfromtext(%s, 25832),
                2
            )
        )
    """
    result = db_crud.run_query(conn, query, (placed_label.wkt, ))
    if len(result) == 0:
        return False
    
    # check angle
    for row in result:
        geom = row[0]
        mpoly = util.make_multipolygon(wkt.loads(geom))
        # check placed label almost exist in geom inner
        if mpoly.intersection(placed_label).area < placed_label.area * 0.4:
            continue

        mls = mpoly.boundary
        for ls in mls.geoms:
            coordinates = list(ls.coords)
            for index in range(len(list(coordinates)) - 1):
                start = coordinates[index]
                end = coordinates[index + 1]
                dx = end[0] - start[0]
                dy = end[1] - start[1]

                # Counterclockwise angle from the positive X axis.
                ls_angle = math.degrees(math.atan2(dy, dx)) % 360

                angle_offset = 10
                target_angle = angle_deg % 360
                target_angle_opposite = (angle_deg + 180) % 360

                diff = min(abs(ls_angle - target_angle), 360 - abs(ls_angle - target_angle))
                diff_opposite = min(abs(ls_angle - target_angle_opposite), 360 - abs(ls_angle - target_angle_opposite))

                if diff <= angle_offset or diff_opposite <= angle_offset:
                    return True

    return False

# check_method -> 1: check with large font(rotatable), 2: check with small fonts(rotatable), 3: check with small(overlappable)
def place_slashed_label_with_out_arrow(conn, id, original_geom, geom_parcel, label_text, padding, check_method = 1):
    factor_padding = 0.2

    if geom_parcel.geom_type == "Polygon":
        largest = geom_parcel
    elif geom_parcel.geom_type == "MultiPolygon" and len(geom_parcel.geoms) > 0:
        largest = max(geom_parcel.geoms, key=lambda p: p.area)
    else:
        return None, None, 0, 0, '/', False
    
    original_largest = max(original_geom.geoms, key=lambda p: p.area)

    # if largest intersects with input_bbox, then label should draw in input_bbox inner
    minx, miny, maxx, maxy = cfg.input_bbox
    input_bbox = box(minx, miny, maxx, maxy)
    if input_bbox.intersects(largest) and input_bbox.intersection(largest).area > 20:
        clipped = input_bbox.intersection(largest)
        # intersection may yield MultiPolygon/GeometryCollection; polylabel needs a Polygon
        if clipped.geom_type != "Polygon":
            clipped = max((g for g in getattr(clipped, "geoms", []) if g.geom_type == "Polygon"), key=lambda p: p.area)
        largest = clipped

    angle_list = [int(util.pca_angle_geom(original_largest)), int(util.pca_angle_geom(largest))]
    label_point_list = [polylabel(largest), largest.representative_point()]

    for font_size in cfg.font_size_parcel:
        if check_method != 1 and font_size == cfg.font_size_parcel[0]:
            continue

        inline_poly = util.text_to_polygon(label_text, font_size, 'parcel', padding)

        if inline_poly.area < largest.area * factor_padding:
            inline_poly = util.text_to_polygon(label_text, font_size, 'parcel', padding + 0.5)
            
        position = find_label_pose(largest.wkt, inline_poly.wkt)
        if position is not None:
            label_point_list.append(Point(position["x"], position["y"]))

        label_point_list.append(get_label_position_candidates(conn, largest, label_text, font_size, 0, run_id))
        for angle_deg in angle_list:
            label_point_list.append(get_label_position_candidates(conn, largest, label_text, font_size, angle_deg, run_id))
        label_point_list.append(largest.centroid)
        label_point_list.append(largest.representative_point())
        
        # Check with non-rotation
        for label_point in label_point_list:
            placed_label = util.translate_rotate_polygon(inline_poly, label_point.x, label_point.y, 0)
            if largest.contains(placed_label) and not check_parcel_similar_building_label(conn, placed_label, 0, font_size):
                return label_point, placed_label, 0, font_size, '/', False
        
        # Check with rotatable
        result_candidates = []
        cand_angle_list = []
        step = 2
        offsets = [0, step, -step, step * 2, -step * 2]
        for angle_deg in angle_list:
            for off in offsets:
                ang = off + angle_deg
                if ang not in cand_angle_list:
                    cand_angle_list.append(ang)
        
        for angle_deg in cand_angle_list:
            for label_point in label_point_list:
                placed_label = util.translate_rotate_polygon(inline_poly, label_point.x, label_point.y, angle_deg)
                
                if largest.contains(placed_label) and not check_parcel_similar_building_label(conn, placed_label, angle_deg, font_size):
                    result_candidates.append({
                        "label_point": label_point,
                        "placed_label": placed_label,
                        "angle_deg": angle_deg,
                        "font_size": font_size,
                        "dist": largest.boundary.distance(placed_label.boundary)
                    })
        
        if len(result_candidates) > 0:
            result_candidates.sort(key=lambda x: x["dist"], reverse=True)
            return result_candidates[0]["label_point"], result_candidates[0]["placed_label"], result_candidates[0]["angle_deg"], result_candidates[0]["font_size"], '/', False
    
        if check_method == 1:
            return None, None, 0, 0, '/', False

    if check_method == 3:
        # Check with overlap
        font_size = cfg.font_size_parcel[-1]
        inline_poly = util.text_to_polygon(label_text, font_size, 'parcel', padding)
        candidate_list = []
        # find largest intersection label position & angle
        for angle_deg in angle_list:
            for label_point in label_point_list:
                placed_label = util.translate_rotate_polygon(inline_poly, label_point.x, label_point.y, angle_deg)

                if geom_parcel.intersection(placed_label).area < placed_label.area * 0.7:
                    continue

                candidate_list.append({
                    "position": label_point,
                    "angle": angle_deg,
                    "placed_label": placed_label,
                    "intersection_area": geom_parcel.intersection(placed_label).area
                })

        if len(candidate_list) > 0:
            candidate_list.sort(key=lambda g: g["intersection_area"], reverse=True)
            return candidate_list[0]["position"], candidate_list[0]["placed_label"], candidate_list[0]["angle"], font_size, '/', False
            
    return None, None, 0, 0, '/', False
        
def check_valid_leader_line(conn, leader_line, is_building_cover):
    obs_building = ""
    if not is_building_cover:
        obs_building = f"""
            SELECT ST_Buffer(geom, 0.1) AS obstacles_geom
            FROM by_bbox_building_{run_id}
            WHERE ST_Intersects(geom, ST_Buffer(ST_GeomFromText(%s, 25832), 0.5))

            UNION ALL
        """
    query = f"""
        SELECT ST_Area(ST_Union(obstacles_geom)) AS obstacles_geom_area
        FROM (
            SELECT ST_Buffer(calculated_label_area, 0.5) AS obstacles_geom
            FROM by_bbox_labels_{run_id}
            WHERE ST_Intersects(calculated_label_area, ST_GeomFromText(%s, 25832))
    
            UNION ALL
    
            SELECT ST_Buffer(geom, 0.1) AS obstacles_geom
            FROM by_bbox_lines_{run_id}
            WHERE ST_Intersects(geom, ST_Buffer(ST_GeomFromText(%s, 25832), 0.5))
            
            UNION ALL
    
            {obs_building}
    
            SELECT ST_Buffer(geom, 0.2) AS obstacles_geom
            FROM by_bbox_symbols_{run_id}
            WHERE ST_Contains(ST_Buffer(ST_GeomFromText(%s, 25832), 0.5), position)
        ) o
    """
    if is_building_cover:
        overlap_area = db_crud.run_query(conn, query, (leader_line.wkt, leader_line.wkt, leader_line.wkt))
    else:
        overlap_area = db_crud.run_query(conn, query, (leader_line.wkt, leader_line.wkt, leader_line.wkt, leader_line.wkt))
    return overlap_area[0][0] == 0 or overlap_area[0][0] is None


# Draw label with arrow in geom_parcel
# Return label_point, placed_label, angle_deg, font_size, splitter, is_nenner_only
def place_label_with_arrow(conn, gml_id, geom_parcel, zaehler, nenner, check_parcel_only, can_label_simple, is_building_cover):
    font_size = cfg.font_size_parcel[1]

    is_nenner_only = False
    # Calc buffer size
    label_text = f"{zaehler}/{nenner}"
    if zaehler is None or zaehler == '' or (can_label_simple and nenner is not None and nenner != ''):
        label_text = f"/{nenner}"
        is_nenner_only = True
    elif nenner is None or nenner == '':
        label_text = f"{zaehler}"
        is_nenner_only = False
    
    padding = get_label_padding(geom_parcel)
    inline_poly = util.text_to_polygon(label_text, font_size, 'parcel', padding)
    minx, miny, maxx, maxy = inline_poly.envelope.bounds
    diagonal = LineString([(minx, miny), (maxx, maxy)])
    max_buffer = 8 if geom_parcel.area else 10
    parcel_buffer_length = max(max_buffer, diagonal.length * 1.3)

    where_clause_parcel_only = "AND is_street = false" if check_parcel_only else ""
    # Get free geom in parcel geometry and get free geom near parcel
    building_obstacle = ""
    free_building_obstacle = ""
    if not is_building_cover:
        building_obstacle = f"""
            SELECT geom AS obstacles_geom
            FROM by_bbox_building_{run_id}
        
            UNION ALL
        """
    free_building_obstacle = f"""
        SELECT geom AS obstacles_geom
        FROM by_bbox_building_{run_id}

    
        UNION ALL
    """

    query = f"""
        SELECT
            ST_AsText(
                ST_Difference(  
                    o.obstacles_geom, 
                    ST_GeomFromText(%s, 25832)
                )
            ) free_geom,
            ST_AsText(p.free_org_geom) free_org_geom
        FROM (
            SELECT 
                ST_Buffer(free.free_geom, %s) geom,
                free.free_geom free_org_geom
            FROM (
                SELECT ST_GeomFromText(%s, 25832) AS geom
            ) parcel
            LEFT JOIN LATERAL (
                SELECT ST_Union(obstacles_geom) AS obstacles_geom
                FROM (
                    {building_obstacle}

                    SELECT calculated_label_area AS obstacles_geom
                    FROM by_bbox_labels_{run_id}

                    UNION ALL

                    SELECT ST_Buffer(geom, 0.1) AS obstacles_geom
                    FROM by_bbox_lines_{run_id}

                    UNION ALL

                    SELECT ST_Buffer(geom, 0.8) AS obstacles_geom
                    FROM by_bbox_vertex_{run_id}

                    UNION ALL

                    SELECT ST_Buffer(ST_Boundary(geom), 0.5) obstacles_geom 
                    FROM by_bbox_landkreis_{run_id}
                ) obstacle_items
            ) obstacle_union ON true

            -- Calculate ST_Difference only once
            CROSS JOIN LATERAL (
                SELECT ST_Difference(
                    parcel.geom,
                    COALESCE(
                        obstacle_union.obstacles_geom,
                        ST_GeomFromText(
                            'GEOMETRYCOLLECTION EMPTY',
                            25832
                        )
                    )
                ) AS diff_geom
            ) difference_result

            -- Use original parcel geometry if difference is NULL or empty
            CROSS JOIN LATERAL (
                SELECT
                    CASE
                        WHEN difference_result.diff_geom IS NULL
                        OR ST_IsEmpty(difference_result.diff_geom)
                        THEN parcel.geom
                        ELSE difference_result.diff_geom
                    END AS free_geom
            ) free
        ) p
        LEFT JOIN LATERAL (
            SELECT ST_Intersection(ST_Difference(np.geom, COALESCE(
                    o.obstacles_geom, 
                    ST_GeomFromText('GEOMETRYCOLLECTION EMPTY', 25832)
                )), p.geom) AS obstacles_geom, is_street,
                np.geom original_geom
            FROM (
                SELECT geom, is_street
                FROM by_bbox_parcel_{run_id}
                WHERE ST_Intersects(geom, p.geom) AND id != %s
            ) np
            LEFT JOIN LATERAL (
                SELECT ST_Union(obstacles_geom) AS obstacles_geom
                FROM (
                    {free_building_obstacle}
                
                    SELECT calculated_label_area AS obstacles_geom
                    FROM by_bbox_labels_{run_id}
            
                    UNION ALL
            
                    SELECT ST_Buffer(geom, 0.1) AS obstacles_geom
                    FROM by_bbox_lines_{run_id}
        
                    UNION ALL
            
                    SELECT ST_Buffer(geom, 0.8) AS obstacles_geom
                    FROM by_bbox_vertex_{run_id}

                    UNION ALL
                    
                    SELECT ST_Buffer(ST_Boundary(geom), 0.5) obstacles_geom 
                    FROM by_bbox_landkreis_{run_id}
                ) o
            ) o ON true
        ) o ON true
        WHERE obstacles_geom is not null AND not ST_IsEmpty(obstacles_geom) {where_clause_parcel_only}
        ORDER BY is_street DESC, ST_Distance(ST_Centroid(geom), obstacles_geom) ASC, ST_Area(o.original_geom) DESC
    """
    free_geom_list = db_crud.run_query(conn, query, (geom_parcel.wkt, parcel_buffer_length, geom_parcel.wkt, gml_id))
    
    if free_geom_list is None:
        return None, None, 0, 0, '', False, None
    
    if len(free_geom_list) < 1:
        # Get boundary free parcel from parcel geometry
        query = f"""
            SELECT
                ST_AsText(o.obstacles_geom) free_geom,
                ST_AsText(free_org_geom) free_org_geom
            FROM (
                SELECT ST_Buffer(ST_Difference(p.geom, COALESCE(
                        o.obstacles_geom, 
                        ST_GeomFromText('GEOMETRYCOLLECTION EMPTY', 25832)
                    )), %s) geom,
                    ST_Difference(p.geom, o.obstacles_geom) free_org_geom
                FROM (
                    SELECT geom
                    FROM by_bbox_parcel_{run_id}
                    WHERE id = %s
                ) p
                LEFT JOIN LATERAL (
                    SELECT COALESCE(ST_Union(obstacles_geom), ST_GeomFromText('GEOMETRYCOLLECTION EMPTY', 25832)) AS obstacles_geom
                    FROM (
                        SELECT calculated_label_area AS obstacles_geom
                        FROM by_bbox_labels_{run_id}

                        UNION ALL

                        SELECT ST_Buffer(geom, 0.1) AS obstacles_geom
                        FROM by_bbox_lines_{run_id}

                        UNION ALL
                        
                        SELECT ST_Buffer(ST_Boundary(geom), 0.5) obstacles_geom 
                        FROM by_bbox_landkreis_{run_id}
                    ) o
                ) o ON true
            ) p
            LEFT JOIN LATERAL (
                SELECT ST_Intersection(ST_Difference(np.geom, COALESCE(
                        o.obstacles_geom, 
                        ST_GeomFromText('GEOMETRYCOLLECTION EMPTY', 25832)
                    )), p.geom) AS obstacles_geom, is_street
                FROM (
                    SELECT geom, is_street
                    FROM by_bbox_parcel_{run_id}
                    WHERE ST_Intersects(geom, p.geom) AND id != %s
                ) np
                LEFT JOIN LATERAL (
                    SELECT ST_Union(obstacles_geom) AS obstacles_geom
                    FROM (
                        {free_building_obstacle}
                    
                        SELECT calculated_label_area AS obstacles_geom
                        FROM by_bbox_labels_{run_id}

                        UNION ALL

                        SELECT ST_Buffer(geom, 0.1) AS obstacles_geom
                        FROM by_bbox_lines_{run_id}

                        UNION ALL

                        SELECT ST_Buffer(geom, 0.8) AS obstacles_geom
                        FROM by_bbox_vertex_{run_id}

                        UNION ALL

                        SELECT ST_Buffer(ST_Boundary(geom), 0.5) obstacles_geom 
                        FROM by_bbox_landkreis_{run_id}
                    ) o
                ) o ON true
            ) o ON true
            WHERE obstacles_geom is not null AND not ST_IsEmpty(obstacles_geom) {where_clause_parcel_only}
            ORDER BY is_street DESC, ST_Distance(geom, obstacles_geom) ASC, ST_Area(obstacles_geom) DESC
        """
        free_geom_list = db_crud.run_query(conn, query, (parcel_buffer_length, gml_id, gml_id))
        
    angle_geom = int(util.pca_angle_geom(geom_parcel))
    step = 2
    offsets = [0, step, -step, step * 2, -step * 2]
    angle_list = list(dict.fromkeys(
        [0 + offset for offset in offsets] +
        [angle_geom + offset for offset in offsets]
    ))
    
    # Get vertexes list to avoid vertex list in arrow line
    query = f"""
        SELECT ST_AsText(ST_Union(ST_Buffer(geom, 0.8)))
        FROM by_bbox_vertex_{run_id}
        WHERE ST_Intersects(geom, ST_Buffer(ST_GeomFromText(%s, 25832), 10))
    """
    vertexes_list = db_crud.run_query(conn, query, (geom_parcel.wkt, ))
    vertex_info = wkt.loads(vertexes_list[0][0])

    for free_geom_wkt in free_geom_list:
        free_geom = wkt.loads(free_geom_wkt[0])
        if isinstance(free_geom, LineString) or isinstance(free_geom, MultiLineString):
            continue

        free_geom_multi = util.make_multipolygon(free_geom)
        free_org_geom = wkt.loads(free_geom_wkt[1])

        for free_geom in free_geom_multi.geoms:
            if free_geom.is_empty or isinstance(free_geom, LineString) or isinstance(free_geom, MultiLineString):
                continue

            container_geom = unary_union([free_geom, geom_parcel])
            container_geom = container_geom.buffer(0.5)
            
            # prepare positions
            try:
                position_candidates = [free_geom.representative_point(), polylabel(free_geom)]
            except:
                print(f"Exception for getting label position candidates: {gml_id}, {zaehler}/{nenner}")
                print(free_geom.wkt)

            inline_poly = util.text_to_polygon(label_text, font_size, 'parcel', padding)
            position = find_label_pose(geom_parcel.wkt, inline_poly.wkt)
            if position is not None:
                position_candidates.append(Point(position["x"], position["y"]))
            position_candidates.append(get_label_position_candidates(conn, free_geom, label_text, font_size, 0, run_id))

            for angle_deg in angle_list:
                for label_point in position_candidates:
                    for x_off in offsets:
                        for y_off in offsets:
                            new_label_point = Point(label_point.x + x_off, label_point.y + y_off)
                            placed_label = util.translate_rotate_polygon(inline_poly, new_label_point.x, new_label_point.y, angle_deg)

                            if free_geom.contains(placed_label):
                                leader_line, new_label_point = util.generate_leader_line(free_geom, free_org_geom, vertex_info, placed_label, cfg.scale, angle_deg)
                                
                                if leader_line is not None and container_geom.contains(leader_line) and check_valid_leader_line(conn, leader_line, is_building_cover):
                                    label_point = new_label_point
                                    placed_label = util.translate_rotate_polygon(inline_poly, new_label_point.x, new_label_point.y, angle_deg)
                                    if not free_geom.contains(placed_label):
                                        continue
                                    # check angle again from leader line
                                    angle_list = list(dict.fromkeys(
                                        [0 + offset for offset in offsets]
                                    ))

                                    for cand_angle_deg in angle_list:
                                        cand_placed_label = util.translate_rotate_polygon(inline_poly, label_point.x, label_point.y, angle_deg + cand_angle_deg)
                                        if free_geom.contains(cand_placed_label):
                                            return label_point, cand_placed_label, angle_deg + cand_angle_deg, font_size, '/', is_nenner_only, leader_line
                                    return label_point, placed_label, angle_deg, font_size, '/', is_nenner_only, leader_line

    # Check with rotation if / splitter
    for free_geom_wkt in free_geom_list:
        free_geom = wkt.loads(free_geom_wkt[0])
        if free_geom.is_empty:
            continue
        label_point = free_geom.representative_point()
        container_geom = unary_union([free_geom, geom_parcel])
        container_geom = container_geom.buffer(0.1)

        for angle_deg in angle_list:
            placed_label = util.translate_rotate_polygon(inline_poly, label_point.x, label_point.y, angle_deg)
            if free_geom.contains(placed_label):
                leader_line, new_label_point = util.generate_leader_line(free_geom, free_org_geom, vertex_info, placed_label, cfg.scale, angle_deg)
                if container_geom.contains(leader_line):
                    label_point = new_label_point
                    placed_label = util.translate_rotate_polygon(inline_poly, label_point.x, label_point.y, angle_deg)
                    return label_point, placed_label, angle_deg, font_size, '/', is_nenner_only, leader_line

    return None, None, 0, 0, '', is_nenner_only, None


# Draw label on overlapping point
# Return label_point, placed_label, angle_deg, font_size, splitter, is_nenner_only
def place_label_with_overlap(geom_parcel, free_geom_parcel, zaehler, nenner, overlap_threshold):
    font_size_normal = cfg.font_size_parcel[1]

    label_text = f"{zaehler}/{nenner}"
    if zaehler is None or zaehler == '':
        label_text = f"/{nenner}"
    if nenner is None or nenner == '':
        label_text = f"{zaehler}"
    inline_poly = util.text_to_polygon(label_text, font_size_normal, 'parcel', 0)

    geom_list = [geom_parcel, free_geom_parcel]
    angle = util.pca_angle_geom(geom_parcel)
    for geom in geom_list:
        polygon_geoms = sorted(geom.geoms, key=lambda p: p.area, reverse=True)
        for polygon in polygon_geoms:
            label_point = polylabel(polygon)

            placed_label = util.translate_rotate_polygon(inline_poly, label_point.x, label_point.y, angle)

            if geom_parcel.buffer(overlap_threshold).contains(placed_label):
                return label_point, placed_label, angle, font_size_normal, '/', False

    representative_point = geom_parcel.representative_point()
    placed_label = util.translate_rotate_polygon(inline_poly, representative_point.x, representative_point.y, angle)
    return representative_point, placed_label, angle, font_size_normal, '/', False

def get_free_geom(conn, id, polygon_wkt, is_include_building):
    if not is_include_building:
        query = f"""
            SELECT ST_AsText(ST_Difference(p.geom, COALESCE(
                    b.obstacles_geom, 
                    ST_GeomFromText('GEOMETRYCOLLECTION EMPTY', 25832)
                ))) free_geom
            FROM (
                SELECT ST_GeomFromText(%s, 25832) geom
            ) p
            LEFT JOIN LATERAL (
                SELECT ST_Union(obstacles_geom) AS obstacles_geom
                FROM (
                    SELECT geom AS obstacles_geom
                    FROM by_bbox_building_{run_id}
					WHERE ST_Intersects(p.geom, geom)
                
                    UNION ALL

                    SELECT calculated_label_area AS obstacles_geom
                    FROM by_bbox_labels_{run_id}
					WHERE ST_Intersects(p.geom, calculated_label_area)
        
                    UNION ALL
            
                    SELECT ST_Buffer(geom, 0.1) AS obstacles_geom
                    FROM by_bbox_lines_{run_id}
					WHERE ST_Intersects(p.geom, geom)
        
                    UNION ALL
            
                    SELECT ST_Buffer(geom, 0.8) AS obstacles_geom
                    FROM by_bbox_vertex_{run_id}
					WHERE ST_Intersects(p.geom, geom)
        
                    UNION ALL
            
                    SELECT ST_Buffer(geom, 0.2) AS obstacles_geom
                    FROM by_bbox_symbols_{run_id}
					WHERE ST_Intersects(p.geom, geom)

                    UNION ALL
                    
                    SELECT ST_Buffer(ST_Boundary(geom), 0.5) obstacles_geom 
                    FROM by_bbox_landkreis_{run_id}
					WHERE ST_Intersects(p.geom, geom)

                    UNION ALL
                    
                    SELECT geom obstacles_geom 
                    FROM by_bbox_landkreis_{run_id}
					WHERE ST_Intersects(p.geom, geom) AND nutzart IN ('Industrie- und Gewerbefläche')
                ) obs
            ) b ON true
        """
        free_geom_data = db_crud.run_query(conn, query, (polygon_wkt, ))
    else:
        query = f"""
            WITH parcel AS (
                SELECT geom
                FROM by_bbox_parcel_{run_id}
                WHERE id = %s
            ),

            building_raw AS (
                SELECT
                    ST_Intersection(b.geom, p.geom) AS geom
                FROM by_bbox_building_{run_id} b
                JOIN parcel p
                    ON ST_Intersects(p.geom, b.geom)
                WHERE ST_Area(ST_Intersection(p.geom, b.geom)) > 1
            ),

            building_intersections AS (
                SELECT
                    ST_UnaryUnion(ST_Collect(geom)) AS geom
                FROM building_raw
                WHERE NOT ST_IsEmpty(geom)
            ),

            landkreis_intersections AS (
                SELECT
                    ST_Intersection(l.geom, p.geom) AS geom
                FROM by_bbox_landkreis_{run_id} l
                JOIN parcel p
                    ON ST_Intersects(p.geom, l.geom)
                WHERE ST_Area(ST_Intersection(p.geom, l.geom)) > 1
                AND l.nutzart IN ('Industrie- und Gewerbefläche')
            ),

            landkreis_minus_buildings AS (
                SELECT
                    ST_Difference(
                        l.geom,
                        COALESCE(
                            b.geom,
                            ST_GeomFromText('GEOMETRYCOLLECTION EMPTY', ST_SRID(l.geom))
                        )
                    ) AS geom
                FROM landkreis_intersections l
                LEFT JOIN building_intersections b
                    ON true
            ),

            candidate_geoms AS (
                SELECT geom, 0 source
                FROM landkreis_minus_buildings
                WHERE NOT ST_IsEmpty(geom)

                UNION ALL

                SELECT geom, 1 source
                FROM building_raw
                WHERE NOT ST_IsEmpty(geom)
            )

            SELECT
                ST_AsText(
                    ST_Difference(
                        c.geom,
                        COALESCE(
                            obs.obstacles_geom,
                            ST_GeomFromText('GEOMETRYCOLLECTION EMPTY', 25832)
                        )
                    )
                ) AS free_geom
            FROM candidate_geoms c
            LEFT JOIN LATERAL (
                SELECT ST_Union(obstacles_geom) AS obstacles_geom
                FROM (
                    SELECT ST_Buffer(calculated_label_area, 0.8) AS obstacles_geom
                    FROM by_bbox_labels_{run_id}

                    UNION ALL

                    SELECT ST_Buffer(geom, 0.1) AS obstacles_geom
                    FROM by_bbox_lines_{run_id}

                    UNION ALL

                    SELECT ST_Buffer(geom, 0.8) AS obstacles_geom
                    FROM by_bbox_vertex_{run_id}

                    UNION ALL

                    SELECT ST_Buffer(geom, 0.2) AS obstacles_geom
                    FROM by_bbox_symbols_{run_id}
                ) obs
            ) obs ON true
            WHERE NOT ST_IsEmpty(
                ST_Difference(
                    c.geom,
                    COALESCE(
                        obs.obstacles_geom,
                        ST_GeomFromText('GEOMETRYCOLLECTION EMPTY', 25832)
                    )
                )
            )
            ORDER BY source ASC, ST_Area(c.geom) DESC
        """
        free_geom_data = db_crud.run_query(conn, query, (id, ))

    if free_geom_data is None or len(free_geom_data) == 0:
        free_geom = MultiPolygon([])
    else:
        free_geom = []
        for geom in free_geom_data:
            geom = wkt.loads(geom[0])
            if not geom.is_empty and (isinstance(geom, Polygon) or isinstance(geom, MultiPolygon)):
                geom = util.make_multipolygon(geom)
                free_geom.extend(geom.geoms)
        
        free_geom = MultiPolygon(free_geom)
    
    return free_geom

def get_clustered_parcel_info(conn):
    """
    For every parcel with a numerator, determine whether its numerator can be
    hidden in the label (rendered as just "/nenner"). True when:
    - every neighbouring parcel (touching within 0.1m) shares the same numerator, or
    - neighbouring parcels length should be larger than 1 which with the same numerator has its centroid within
      5m of this parcel's geometry (a nearby, similarly-placed parcel)
    - or neighbouring parcels length should be larger than 5 which with the same numerator has its centroid within
      10m of this parcel's geometry (a nearby, similarly-placed parcel)
    """
    query = f"""
        WITH parcels AS (
            SELECT id, numerator, geom
            FROM by_bbox_parcel_{run_id}
            WHERE numerator IS NOT NULL AND denominator IS NOT NULL
        ),
        pairs AS (
            SELECT
                s.id,
                s.numerator,
                b.id AS near_id,
                ST_Distance(ST_Centroid(b.geom), ST_Centroid(s.geom)) AS centroid_distance
            FROM parcels s
            LEFT JOIN by_bbox_parcel_{run_id} b
                ON b.id <> s.id AND ST_Intersects(b.geom, ST_Buffer(s.geom, 0.1)) AND b.numerator = s.numerator
        )
        SELECT
            id,
            COUNT(id),
			array_agg(near_id) near_id
        FROM pairs
        WHERE centroid_distance < 5
        GROUP BY id
        HAVING COUNT(id) > 1

        UNION

        SELECT
            id,
            COUNT(id),
			array_agg(near_id) near_id
        FROM pairs
        WHERE centroid_distance < 10
        GROUP BY id
        HAVING COUNT(id) > 5
    """
    result = db_crud.run_query(conn, query)

    simple_list = []
    for row in result:
        id, _, near_ids = row
        if id not in simple_list:
            simple_list.append(id)
        for near_id in near_ids:
            if near_id not in simple_list:
                simple_list.append(near_id)

    return simple_list

def check_label_render_without_zaehler(conn, id, numerator):
    # If all surround parcel have same numerator
    query = f"""
        SELECT COUNT(id) cnt
        FROM by_bbox_parcel_{run_id} b
        JOIN (
            SELECT geom
            FROM by_bbox_parcel_{run_id}
            WHERE id = %s
        ) s
        ON ST_Intersects(b.geom, ST_Buffer(s.geom, 0.1))
        WHERE b.id <> %s AND numerator != %s
    """
    result = db_crud.run_query(conn, query, (id, id, numerator))

    if result[0][0] == 0:
        return True

    return False


def check_enough_free_size(conn, id):
    # Check parcel have enough free size
    query = f"""
        WITH parcel_geom AS (
            SELECT geom
            FROM by_bbox_parcel_{run_id}
            WHERE id = %s
        ),
        obstacle_parts AS (
            SELECT ST_Buffer(b.geom, 0.1) AS geom
            FROM by_bbox_building_{run_id} b
            CROSS JOIN parcel_geom p
            WHERE ST_Intersects(
                b.geom,
                ST_Buffer(p.geom, 0.5)
            )

            UNION ALL

            SELECT ST_Buffer(l.geom, 0.1) AS geom
            FROM by_bbox_landkreis_{run_id} l
            CROSS JOIN parcel_geom p
            WHERE l.nutzart = 'Industrie- und Gewerbefläche'
            AND ST_Intersects(
                l.geom,
                ST_Buffer(p.geom, 0.5)
            )
        ),
        obstacles AS (
            SELECT ST_Union(geom) AS geom
            FROM obstacle_parts
        )
        SELECT
            ST_Area(
                ST_Intersection(
                    ST_Difference(
                        p.geom,
                        COALESCE(
                            o.geom,
                            ST_GeomFromText('POLYGON EMPTY', 25832)
                        )
                    ),
                    ST_MakeEnvelope(%s, %s, %s, %s, 25832)
                )
            ) AS remaining_parcel_area
        FROM parcel_geom p
        CROSS JOIN obstacles o;
    """

    result = db_crud.run_query(conn, query, (id, *cfg.input_bbox))
    area = result[0][0]

    if area == 0:
        # Remove Industrie- und Gewerbefläche in obstacles
        query = f"""
            WITH parcel_geom AS (
                SELECT geom
                FROM by_bbox_parcel_{run_id}
                WHERE id = %s
            ),
            obstacle_parts AS (
                SELECT ST_Buffer(b.geom, 0.1) AS geom
                FROM by_bbox_building_{run_id} b
                CROSS JOIN parcel_geom p
                WHERE ST_Intersects(
                    b.geom,
                    ST_Buffer(p.geom, 0.5)
                )
            ),
            obstacles AS (
                SELECT ST_Union(geom) AS geom
                FROM obstacle_parts
            )
            SELECT
                ST_Area(
                    ST_Intersection(
                        ST_Difference(
                            p.geom,
                            COALESCE(
                                o.geom,
                                ST_GeomFromText('POLYGON EMPTY', 25832)
                            )
                        ),
                        ST_MakeEnvelope(%s, %s, %s, %s, 25832)
                    )
                ) AS remaining_parcel_area
            FROM parcel_geom p
            CROSS JOIN obstacles o;
        """
        result = db_crud.run_query(conn, query, (id, *cfg.input_bbox))
        area = result[0][0]

    return area > 150
        

def get_label_padding(geom):
    padding = cfg.parcel_label_padding
    if geom.area > 260:
        padding = 0.8

    return padding

def check_near_similar_parcel_exist(conn, id):
    query = f"""
        WITH parcel as (
            SELECT id, geom FROM by_bbox_parcel_{run_id} WHERE id = %s
        )
        SELECT COUNT(*) 
        FROM by_bbox_parcel_{run_id} near, parcel
        WHERE 
            ST_Intersects(near.geom, parcel.geom) AND near.id != parcel.id
            AND ST_Area(near.geom) >= ST_Area(parcel.geom) - 1 AND ST_Area(near.geom) < ST_Area(parcel.geom) + 1 
            AND ST_Length(near.geom) >= ST_Length(parcel.geom) - 1 AND ST_Length(near.geom) < ST_Length(parcel.geom) + 1 
    """
    result = db_crud.run_query(conn, query, (id, ))
    return result[0][0] > 0


def check_label_should_overlap(conn, id):
    # check how many parcels exist that same numerator and similar size near by id parcel
    query = f"""
        WITH parcel AS (
            SELECT 
                id, numerator, geom, ST_Buffer(ST_Centroid(geom), 20) buffer 
            FROM by_bbox_parcel_{run_id}
            WHERE id = %s
        )
        SELECT COUNT(p.id)
        FROM by_bbox_parcel_{run_id} p, parcel
        WHERE 
            ST_Intersects(p.geom, parcel.buffer) AND 
            p.numerator = parcel.numerator AND 
            p.id <> parcel.id AND
            ST_Area(p.geom) >= ST_Area(parcel.geom) - 5 AND
            ST_Area(p.geom) <= ST_Area(parcel.geom) + 5
    """
    result = db_crud.run_query(conn, query, (id, ))
    # if near parcels count larger than 3
    if result[0][0] >= 3:
        # check building or another label drawn
        query = f"""
            SELECT COUNT(*)
            FROM by_bbox_labels_{run_id}
            WHERE ST_Contains(
                (SELECT geom FROM by_bbox_parcel_{run_id} WHERE id = %s),
                calculated_label_area
            )
        """
        result = db_crud.run_query(conn, query, (id, ))
        if result[0][0] < 1:
            return True

    return False


# Processing parcel labeling for given data range
def process_parcel_range(data: List[Any], counter, db_cadastral_param, clustered_info) -> None:
    conn = db_crud.create_connection(db_cadastral_param)
    overlap_threshold = [0.3, 0.8, 1.2, 1.6]
    for item in data:
        id, zaehler, nenner, geom, _, _ = item
        ################## testing
        # non_skip_ids = {'09970700000277001700'}
        # if id not in non_skip_ids:
        #     continue
        ################## testing

        geom_parcel = wkt.loads(geom)

        print(f"Working - {id}")
        ########## debug code
        if geom_parcel.area > 1500 and geom_parcel.boundary.length > 2000:
            continue

        # Make geom_parcel as MultiPolygon
        geom_parcel = util.make_multipolygon(geom_parcel)
        if len(geom_parcel.geoms) == 0:
            continue

        can_label_simple = False
        if nenner != '':
            can_label_simple = id in clustered_info or check_label_render_without_zaehler(conn, id, zaehler)
            if can_label_simple and check_enough_free_size(conn, id):
                can_label_simple = False

        # it use when several polygon exist to render label
        is_rendered = False

        sorted_geoms = sorted(geom_parcel.geoms, key=lambda g: g.area, reverse=True)

        for polygon in sorted_geoms:
            # Get Free geom
            free_geom_parcel = get_free_geom(conn, id, polygon.wkt, False)

            is_free_geom_empty = False
            if free_geom_parcel.is_empty:
                free_geom_parcel = util.make_multipolygon(polygon)
                is_free_geom_empty = True

            multi_polygon = util.make_multipolygon(polygon)

            leader_line = None
            label_point = None
            is_overlap = False
            is_building_cover = False

            free_geom_list = []

            # check placed label with small font size can't place in free_geom
            label = f"/{nenner}" if can_label_simple else f"{zaehler}/{nenner}"
            if nenner == "":
                label = f"{zaehler}"
            inline_poly = util.text_to_polygon(label, cfg.font_size_parcel[1], 'parcel', 0)

            if is_free_geom_empty or \
                (
                    free_geom_parcel.area < cfg.min_free_geom_area and 
                    free_geom_parcel.area < polygon.area * 0.35 and
                    polygon.area > 2 and
                    free_geom_parcel.area < inline_poly.area * 2
                ):
                    # it means that almost contain area is covered by building
                    # in this case, add building geom to free geom
                    is_building_cover = True

                    # if similar parcel exist, then try to render label on building inner
                    if is_free_geom_empty or check_near_similar_parcel_exist(conn, id) or \
                        free_geom_parcel.area < 3 or \
                        (free_geom_parcel.area < 8 and free_geom_parcel.area / free_geom_parcel.length < 0.3):

                        free_geom_parcel_include_building = get_free_geom(conn, id, polygon.wkt, True)
                        if not free_geom_parcel_include_building.is_empty:
                            free_geom_list.extend(free_geom_parcel_include_building.geoms)
                    
                    # if similar parcel not exist, draw on free label or draw with arrow
                    if not free_geom_parcel.is_empty and len(free_geom_list) == 0:
                        # if almost parcel area is covered by building
                        free_geom_list.extend(free_geom_parcel.geoms)

            elif not is_free_geom_empty and free_geom_parcel is not None and not free_geom_parcel.is_empty:
                if free_geom_parcel.area > 2:
                    free_geom_list.extend(free_geom_parcel.geoms)
                else:
                    free_geom_list.extend(multi_polygon.geoms)
                    is_building_cover = True
            free_geom_list.sort(key=lambda g: g.area, reverse=True)

            if not can_label_simple and nenner != '':
                # Set /nenner only if parcel area is similar with /nenner label size
                inline_poly = util.text_to_polygon(f"/{nenner}", cfg.font_size_parcel[1], 'parcel', 0)
                if abs(polygon.area - inline_poly.area) < inline_poly.area and polygon.area > inline_poly.area * 0.7:
                    can_label_simple = True

            is_nenner_only = False
            for free_geom in free_geom_list:
                # Check with out arrow
                # Check with full text
                leader_line = None
                label_text = f"{zaehler}/{nenner}" 
                if can_label_simple:
                    label_text = f"/{nenner}"
                    is_nenner_only = True
                if nenner == '':
                    label_text = f"{zaehler}"
                    is_nenner_only = False

                if isinstance(free_geom, MultiPolygon):
                    continue

                parcel_label_padding = get_label_padding(free_geom)

                label_point, placed_label, angle_deg, font_size, splitter, _ = place_slashed_label_with_out_arrow(conn, id, multi_polygon, free_geom, label_text, parcel_label_padding, 1)

                # Check with denominator only
                if label_point is None:
                    label_point, placed_label, angle_deg, font_size, splitter, _ = place_slashed_label_with_out_arrow(conn, id, multi_polygon, free_geom, label_text, parcel_label_padding, 2)

                if label_point is not None:
                    break
    
                if label_point is None and not is_rendered:
                    # check parcel area, boundary length is similar with label rendering
                    inline_poly = util.text_to_polygon(f"/{nenner}", cfg.font_size_parcel[-1], 'parcel', 0)
                    if free_geom.area >= inline_poly.area * 0.3 and free_geom.area / free_geom.boundary.length >= 0.3:
                        # Check this parcel label should overlap or not
                        is_label_overlap = check_label_should_overlap(conn, id)
                        if is_label_overlap:
                            label_point, placed_label, angle_deg, font_size, splitter, _ = place_slashed_label_with_out_arrow(conn, id, multi_polygon, free_geom, label_text, parcel_label_padding, 3)

                if label_point is None and not is_rendered:
                    check_parcel = [True, False]
                    for check in check_parcel:
                        # Check with arrow on parcel first (except street) with padding
                        label_point, placed_label, angle_deg, font_size, splitter, is_nenner_only, leader_line = place_label_with_arrow(conn, id, multi_polygon, zaehler, nenner, check, can_label_simple, is_building_cover)
                        if label_point is not None and leader_line is not None and check_valid_leader_line(conn, leader_line, is_building_cover):
                            break
                        else:
                            label_point = None
                            leader_line = None
            
                if label_point is None and not is_rendered:
                    # Try with draw leader line with full geom
                    check_parcel = [True, False]
                    for check in check_parcel:
                        # Check with arrow on parcel first (except street) with padding
                        label_point, placed_label, angle_deg, font_size, splitter, is_nenner_only, leader_line = place_label_with_arrow(conn, id, multi_polygon, zaehler, nenner, check, can_label_simple, is_building_cover)
                        if label_point is not None and leader_line is not None and check_valid_leader_line(conn, leader_line, is_building_cover):
                            break
                        else:
                            label_point = None
                            leader_line = None

                if label_point is None and not can_label_simple and nenner != "":
                    label_text = f"/{nenner}"
                    is_nenner_only = True
                    # Check with parcel
                    padding = get_label_padding(free_geom_parcel)
                    label_point, placed_label, angle_deg, font_size, splitter, _ = place_slashed_label_with_out_arrow(conn, id, multi_polygon, free_geom_parcel, label_text, padding, 2)
                    if label_point is None:
                        check_parcel = [True, False]
                        for check in check_parcel:
                            # Check with arrow within parcel first (except street) with padding
                            label_point, placed_label, angle_deg, font_size, splitter, is_nenner_only, leader_line = place_label_with_arrow(conn, id, multi_polygon, zaehler, nenner, check, can_label_simple, is_building_cover)
                            if label_point is not None and leader_line is not None and check_valid_leader_line(conn, leader_line, is_building_cover):
                                break
                            else:
                                label_point = None

                if label_point is not None:
                    break

            if label_point is None and not is_rendered:
                # Draw on overlap
                if label_point is None and not is_rendered:
                    for threshold in overlap_threshold:
                        label_point, placed_label, angle_deg, font_size, splitter, is_nenner_only = place_label_with_overlap(multi_polygon, free_geom_parcel, zaehler, nenner, threshold)
                        if label_point is not None:
                            is_overlap = True
                            break

            if label_point is not None:
                # Add label info to table
                is_rendered = insert_label_render(conn, id, zaehler, nenner, label_point, angle_deg, font_size, placed_label, leader_line, splitter, is_nenner_only, is_overlap) or is_rendered

        if is_rendered:
            counter['processed'] += 1
        else:
            counter['missed'] += 1
            if geom_parcel.intersects(box(*cfg.bbox).boundary.buffer(0.1)):
                print(f"Missed {id} - {label_text} - It touches bbox boundary, you can ignore it.")
                counter['missed_ignorable'] += 1
            else:
                print(f"Missed {id} - {label_text}")

        if counter['processed'] % 50 == 0:
            now = time.time()
            print(f"Processed: {counter['processed']}, Missed: {counter['missed']}, Ignorable: {counter['missed_ignorable']}, Elapsed: {now - counter['start_time']:.2f} sec")

    db_crud.close_connection(conn)

def main(is_production):
    print("==============Parcel labeling (Except street name)")

    conn = db_crud.create_connection(db_cadastral_param)

    # Empty labels table with parcel labeling
    query = F"DELETE FROM by_bbox_labels_{run_id} WHERE source = 'parcel'"
    db_crud.run_query(conn, query)
    query = F"DELETE FROM by_bbox_lines_{run_id} WHERE is_street = false"
    db_crud.run_query(conn, query)

    start_time = time.time()

    # Precompute, for every parcel, whether its numerator can be hidden in
    # the label - done once here instead of per-parcel inside each worker.
    clustered_info = get_clustered_parcel_info(conn)

    # Divide parcels into spatial zones: one zone per CPU core in production, a single zone in debug mode
    is_debug = not (is_production and cfg.cpu_cores > 1)
    num_zones = 1 if is_debug else cfg.cpu_cores

    # Get parcel info including free space geometry.
    # ST_ClusterKMeans groups neighbouring parcels into the same zone (by centroid),
    # so each worker handles a contiguous area; within a zone parcels are sorted by area.
    query = f"""
        WITH base AS (
            SELECT 
                p.id,
                p.numerator,
                p.denominator,
                p.geom,
                ST_Difference(
                    p.geom,
                    COALESCE(
                        b.obstacles_geom,
                        ST_GeomFromText('GEOMETRYCOLLECTION EMPTY', 25832)
                    )
                ) AS diff_geom
            FROM by_bbox_parcel_{run_id} p
            LEFT JOIN LATERAL (
                SELECT ST_Union(bld.geom) AS obstacles_geom
                FROM by_bbox_building_{run_id} bld
                WHERE ST_Intersects(bld.geom, p.geom)
            ) b ON true
            WHERE 
                (p.numerator IS NOT NULL OR p.denominator IS NOT NULL)
                AND (
                    (
                        p.is_street = false
                        AND GeometryType(p.geom) IN ('POLYGON', 'MULTIPOLYGON')
                    )
                    OR
                    (
                        p.is_street = true
                        AND NOT EXISTS (
                            SELECT 1
                            FROM by_bbox_parcel_street_{run_id} ps
                            WHERE ps.parcel_id = p.id
                        )
                    )
                )

            UNION ALL

            SELECT
                p.id,
                p.numerator,
                p.denominator,
                p.geom,
                p.geom AS diff_geom
            FROM by_bbox_parcel_{run_id} p
            WHERE p.id IN (
                SELECT parcel_id
                FROM by_bbox_parcel_street_{run_id}
                WHERE is_street_name_suppressed = true
            )
        ),

        valid AS (
            SELECT
                *,
                ST_Centroid(geom) AS centroid
            FROM base
            WHERE geom IS NOT NULL
            AND NOT ST_IsEmpty(geom)
        ),

        stats AS (
            SELECT COUNT(*) AS valid_count
            FROM valid
        ),

        clustered AS (
            SELECT
                v.id,
                ST_ClusterKMeans(
                    v.centroid,
                    LEAST(%s, s.valid_count - 1)::integer
                ) OVER () AS zone
            FROM valid v
            CROSS JOIN stats s
            WHERE s.valid_count > 1

            UNION ALL

            SELECT
                v.id,
                0 AS zone
            FROM valid v
            CROSS JOIN stats s
            WHERE s.valid_count <= 1
        )

        SELECT
            b.id,
            b.numerator,
            COALESCE(b.denominator, '') denominator,
            ST_AsText(b.geom) AS geom,
            b.diff_geom,
            COALESCE(c.zone, -1) AS zone
        FROM base b
        LEFT JOIN clustered c ON c.id = b.id
        WHERE ST_Intersects(b.geom, ST_MakeEnvelope(%s, %s, %s, %s, 25832))
        ORDER BY COALESCE(c.zone, -1), ST_Area(b.geom) ASC
    """
    data = db_crud.run_query(conn, query, (num_zones, *cfg.input_bbox))
    print(f"{len(data)} records Processing started in {num_zones} zone(s)")
    
    manager = Manager()
    processed_counter = manager.dict()
    processed_counter['processed'] = 0
    processed_counter['missed'] = 0
    processed_counter['missed_ignorable'] = 0
    processed_counter['start_time'] = start_time

    # Group rows into their spatial zones (already area-sorted within each zone),
    # dropping the trailing zone column so each worker row keeps its original shape.
    zones = {}
    for row in data:
        zone = row[-1]
        if zone == -1:  # empty polygon, skip
            continue
        zones.setdefault(zone, []).append(row)
    chunk_list = list(zones.values())

    # One zone per worker. Each zone is a contiguous area, so neighbouring parcels
    # (which check free space against each other) are processed by the same worker.
    if num_zones > 1:
        # close()+join() instead of the context manager: Pool.__exit__ calls terminate(),
        # whose _help_stuff_finish() grabs inqueue._rlock while an idle worker still holds
        # it inside SimpleQueue.get(), which can block the shutdown forever.
        pool = Pool(min(num_zones, cfg.cpu_cores))
        try:
            pool.starmap(process_parcel_range, [(chunk, processed_counter, db_cadastral_param, clustered_info) for chunk in chunk_list])
            pool.close()
        except BaseException:
            pool.terminate()
            raise
        finally:
            pool.join()
    else:
        process_parcel_range(data, processed_counter, db_cadastral_param, clustered_info)

    print(f"Completed, Processed: {processed_counter['processed']}, Missed: {processed_counter['missed']}, Ignorable: {processed_counter['missed_ignorable']}, Total Elapsed: {time.time() - start_time:.2f} sec")
    
    db_crud.close_connection(conn)
    

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: python parcel.py <id>")
        print('Example command format: "python parcel.py by_101125"')
        sys.exit(1)

    run_id = sys.argv[1]
    is_production = util.parse_production_flag(sys.argv)
    
    cfg = load_config(run_id)
    db_cadastral_param = {
        "dbname": cfg.dbname,
        "user": cfg.user,
        "password": cfg.password,
        "host": cfg.host,
        "port": cfg.port
    }

    main(is_production)