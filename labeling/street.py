# Street labeling
import math
import sys
from multiprocessing import Pool, Manager
from psycopg2.extensions import connection
from scipy.ndimage import distance_transform_edt
from shapely import wkt
from shapely.affinity import translate, rotate
from shapely.algorithms.polylabel import polylabel
from shapely.geometry import Polygon, LineString, Point, MultiPolygon, box
from shapely.ops import substring
from shapely.prepared import prep
from typing import List, Any
import os
import numpy as np

import db_crud
import time
import util

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from read_settings import load_config

cfg = None
db_cadastral_param = None
run_id = ""

def check_street_name_render(conn, id, street_name, geom):
    # Find near streets
    query = f"""
        WITH input_geom AS (
            SELECT ST_GeomFromText(%s, 25832) AS geom
        )
        SELECT COUNT(*)
        FROM (
            SELECT p.geom
            FROM by_bbox_parcel_{run_id} p
            JOIN by_bbox_parcel_street_{run_id} s
                ON s.parcel_id = p.id
            CROSS JOIN input_geom g
            WHERE s.street_name = %s
                AND s.parcel_id <> %s
                AND ST_Intersects(p.geom, g.geom)
        ) t
        CROSS JOIN input_geom g
        WHERE
            ST_Area(ST_Intersection(t.geom, ST_Buffer(g.geom, 0.1))) > 1
            AND ST_Area(t.geom) > ST_Area(g.geom)
    """
    neighbor_list = db_crud.run_query(conn, query, (geom, street_name, id))
    return neighbor_list is None or neighbor_list[0][0] == 0

def check_non_overlap(conn, parcel_id, poly):
    # Check parcel label polygon overlap
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
            WHERE ST_Intersects(ST_Buffer(position, 0.2), ST_GeomFromText(%s, 25832))

            UNION ALL

            SELECT COUNT(*) AS cnt
            FROM by_bbox_vertex_{run_id}
            WHERE ST_Intersects(ST_Buffer(geom, 0.8), ST_GeomFromText(%s, 25832))
        ) t
    """
    overlap_parcel_data = db_crud.run_query(conn, query, (poly.wkt, poly.wkt, poly.wkt, poly.wkt))
    if overlap_parcel_data[0][0] > 0:
        return False

    return True

# Insert street label to DB
def insert_street_label(conn, parcel_id: str, geom_parcel: MultiPolygon, name: str, category: str, poly: Polygon, angle: float, is_street: bool, font_size: int, is_overlap: bool = False):
    if not is_overlap:
        if not check_non_overlap(conn, parcel_id, poly):
            off_range = [0, 1, -1, 2, -2, 3, -3, 4, -4, 6, -6, 8, -8]
            is_non_overlap = False
            for x in off_range:
                for y in off_range:
                    new_x = poly.centroid.x + x
                    new_y = poly.centroid.y + y
                    new_poly = util.get_label_polygon_from_center(new_x, new_y, name, font_size, category, angle, False, True)
                    if geom_parcel.contains(new_poly) and check_non_overlap(conn, parcel_id, new_poly):
                        is_non_overlap = True
                        poly = new_poly
                        break

                if is_non_overlap:
                    break
        
            if not is_non_overlap:
                return False
    
    # Update angle as draw text from LEFT to RIGHT, BOTTOM to TOP
    # bottom to top has higher weight
    # bottom to top wins if two competing race conditions exists
    angle = angle % 360
    if angle >= 120 and angle <= 300:
        angle = (angle + 180) % 360

    if util.has_descender(name):
        poly = util.move_poly_up(poly, angle, 0.3)

    sld_rotation = util.convert_to_geoserver_rotation(angle)

    query = f"""
    INSERT INTO by_bbox_labels_{run_id} (source_id, source, name, label_position, font_size, label_angle, alkis_angle, calculated_label_area, is_street, is_overlap)
    VALUES (%s, %s, %s, ST_SetSRID(ST_GeomFromText(%s), 25832), %s, %s, %s, ST_SetSRID(ST_GeomFromText(%s), 25832), %s, %s)
    """

    db_crud.run_query(conn, query, (parcel_id, 'street', name, poly.centroid.wkt, font_size, sld_rotation, angle, poly.wkt, is_street, is_overlap))

    return True

def insert_street_line(conn, id, geom):
    query = f"INSERT INTO by_bbox_lines_{run_id} (source_id, source, geom, is_street) VALUES " \
        "(%s, %s, ST_GeomFromText(%s, 25832), %s)"
    
    db_crud.run_query(conn, query, (id, 'parcel', geom.wkt, True))

def get_longest_edge(geom):
    """
    Return the longest edge (LineString) from a Polygon or MultiPolygon.
    """
    polygons = []
    if geom.geom_type == "Polygon":
        polygons = [geom]
    elif geom.geom_type == "MultiPolygon":
        polygons = list(geom.geoms)
    else:
        raise ValueError("Geometry must be Polygon or MultiPolygon")

    all_edges = []
    for poly in polygons:
        coords = list(poly.exterior.coords)
        edges = [LineString([coords[i], coords[i+1]]) for i in range(len(coords)-1)]
        all_edges.extend(edges)

        # if you want to include holes too:
        for ring in poly.interiors:
            icoords = list(ring.coords)
            iedges = [LineString([icoords[i], icoords[i+1]]) for i in range(len(icoords)-1)]
            all_edges.extend(iedges)

    if not all_edges:
        return None

    return max(all_edges, key=lambda e: e.length)

def get_street_angle(poly):
    # mrr = poly.minimum_rotated_rectangle
    # coords = list(mrr.exterior.coords)
    # (x1,y1),(x2,y2) = coords[0], coords[1]
    # label_angle = math.degrees(math.atan2(y2-y1, x2-x1))

    buffer_width = 1
    while True:
        buffered = poly.buffer(-buffer_width)
        if buffered.is_empty:
            buffered = poly.buffer(-buffer_width + 1)
            line = get_longest_edge(buffered)

            (x1, y1), (x2, y2) = list(line.coords)[0], list(line.coords)[-1]
            # Compute angle in radians
            angle_rad = math.atan2(y2 - y1, x2 - x1)

            # Convert to degrees (0° = East, CCW positive)
            label_angle = math.degrees(angle_rad)
            break

        buffer_width += 1

    return label_angle


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
    angle_step=4,
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


# Draw label with arrow in geom_parcel
# Return label_point, placed_label, angle_deg, font_size, splitter, is_nenner_only
def place_label_with_arrow(conn, gml_id, geom_parcel, zaehler, nenner, overlap_threshold, buffer_value, can_label_simple):
    buffered_geom_parcel = geom_parcel.buffer(buffer_value)
    
    font_size = cfg.font_size_street[1]

    is_nenner_only = False
    # Calc buffer size
    label_text = f"{zaehler}/{nenner}"
    if zaehler is None or zaehler == '':
        label_text = f"/{nenner}"
        is_nenner_only = True
    elif can_label_simple and nenner != '':
        label_text = f"/{nenner}"
        is_nenner_only = True
    elif nenner is None or nenner == '':
        label_text = f"{zaehler}"
        is_nenner_only = False

    inline_poly = util.text_to_polygon(label_text, font_size, 'parcel', 0)
    minx, miny, maxx, maxy = inline_poly.envelope.bounds
    diagonal = LineString([(minx, miny), (maxx, maxy)])
    buffer_length = diagonal.length * 1.5

    # Get free geom in parcel geometry and get free geom near parcel
    query = f"""
        SELECT
            ST_AsText(ST_Difference(o.obstacles_geom, ST_GeomFromText(%s, 25832))) free_geom
        FROM (
            SELECT ST_Buffer(ST_Difference(p.geom, COALESCE(
                    o.obstacles_geom, 
                    ST_GeomFromText('GEOMETRYCOLLECTION EMPTY', 25832)
                )), %s) geom
            FROM (
                SELECT geom
                FROM by_bbox_parcel_{run_id}
                WHERE id = %s
			) p
			LEFT JOIN LATERAL (
				SELECT ST_Union(obstacles_geom) AS obstacles_geom
                FROM (
                    SELECT geom AS obstacles_geom
                    FROM by_bbox_building_{run_id}
                    WHERE ST_Intersects(geom, p.geom)
                
                    UNION ALL
                
                    SELECT calculated_label_area AS obstacles_geom
                    FROM by_bbox_labels_{run_id}
                    WHERE ST_Intersects(geom, p.geom)
            
                    UNION ALL
            
                    SELECT ST_Buffer(geom, 0.1) AS obstacles_geom
                    FROM by_bbox_lines_{run_id}
                    WHERE ST_Intersects(geom, p.geom)
            
                    UNION ALL
            
                    SELECT ST_Buffer(position, 0.2) AS obstacles_geom
                    FROM by_bbox_symbols_{run_id}
                    WHERE ST_Contains(p.geom, position)
                
                    UNION ALL
            
                    SELECT ST_Buffer(geom, 0.8) AS obstacles_geom
                    FROM by_bbox_vertex_{run_id}
                    WHERE ST_Contains(p.geom, geom)
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
                    SELECT geom AS obstacles_geom
                    FROM by_bbox_building_{run_id}
                    WHERE ST_Intersects(geom, np.geom)
                
                    UNION ALL
                
                    SELECT calculated_label_area AS obstacles_geom
                    FROM by_bbox_labels_{run_id}
                    WHERE ST_Intersects(geom, np.geom)
            
                    UNION ALL
            
                    SELECT ST_Buffer(geom, 0.1) AS obstacles_geom
                    FROM by_bbox_lines_{run_id}
                    WHERE ST_Intersects(geom, np.geom)
            
                    UNION ALL
            
                    SELECT ST_Buffer(position, 0.2) AS obstacles_geom
                    FROM by_bbox_symbols_{run_id}
                    WHERE ST_Contains(np.geom, position)
            
                    UNION ALL
            
                    SELECT ST_Buffer(geom, 0.8) AS obstacles_geom
                    FROM by_bbox_vertex_{run_id}
                    WHERE ST_Contains(np.geom, geom)
                ) o
            ) o ON true
        ) o ON true
        WHERE obstacles_geom is not null AND not ST_IsEmpty(obstacles_geom)
        ORDER BY is_street DESC, ST_Distance(ST_Centroid(geom), obstacles_geom) ASC
    """
    free_geom_list = db_crud.run_query(conn, query, (buffered_geom_parcel.wkt, buffer_length, gml_id, gml_id))
    
    if free_geom_list is None:
        return None, None, 0, 0, '', False
    
    if len(free_geom_list) < 1:
        # Get boundary free parcel from parcel geometry
        query = f"""
            SELECT
                ST_AsText(o.obstacles_geom) free_geom
            FROM (
                SELECT ST_Buffer(ST_Difference(p.geom, COALESCE(
                        o.obstacles_geom, 
                        ST_GeomFromText('GEOMETRYCOLLECTION EMPTY', 25832)
                    )), %s) geom
                FROM (
                    SELECT geom
                    FROM by_bbox_parcel_{run_id}
                    WHERE id = %s
                ) p
                LEFT JOIN LATERAL (
                    SELECT ST_Union(obstacles_geom) AS obstacles_geom
                    FROM (
                        SELECT calculated_label_area AS obstacles_geom
                        FROM by_bbox_labels_{run_id}
                        WHERE ST_Intersects(geom, p.geom)
                
                        UNION ALL
                
                        SELECT ST_Buffer(geom, 0.1) AS obstacles_geom
                        FROM by_bbox_lines_{run_id}
                        WHERE ST_Intersects(geom, p.geom)
                
                        UNION ALL
                
                        SELECT ST_Buffer(position, 0.2) AS obstacles_geom
                        FROM by_bbox_symbols_{run_id}
                        WHERE ST_Contains(p.geom, position)
                
                        UNION ALL
                
                        SELECT ST_Buffer(geom, 0.8) AS obstacles_geom
                        FROM by_bbox_vertex_{run_id}
                        WHERE ST_Contains(p.geom, geom)
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
                        SELECT geom AS obstacles_geom
                        FROM by_bbox_building_{run_id}
                        WHERE ST_Intersects(geom, np.geom)
                    
                        UNION ALL
                    
                        SELECT calculated_label_area AS obstacles_geom
                        FROM by_bbox_labels_{run_id}
                        WHERE ST_Intersects(geom, np.geom)
                
                        UNION ALL
                
                        SELECT ST_Buffer(geom, 0.1) AS obstacles_geom
                        FROM by_bbox_lines_{run_id}
                        WHERE ST_Intersects(geom, np.geom)
            
                        UNION ALL
                
                        SELECT ST_Buffer(position, 0.2) AS obstacles_geom
                        FROM by_bbox_symbols_{run_id}
                        WHERE ST_Contains(np.geom, position)
            
                        UNION ALL
                
                        SELECT ST_Buffer(geom, 0.8) AS obstacles_geom
                        FROM by_bbox_vertex_{run_id}
                        WHERE ST_Contains(np.geom, geom)
                    ) o
                ) o ON true
            ) o ON true
            WHERE obstacles_geom is not null AND not ST_IsEmpty(obstacles_geom)
            ORDER BY is_street DESC, ST_Distance(geom, obstacles_geom) ASC
        """
        free_geom_list = db_crud.run_query(conn, query, (buffer_length, gml_id, gml_id))
        
    # Get vertexes list to avoid vertex list in arrow line
    query = f"""
        SELECT ST_AsText(ST_Union(ST_Buffer(geom, 0.8)))
        FROM by_bbox_vertex_{run_id}
        WHERE ST_Intersects(geom, ST_Buffer(ST_GeomFromText(%s, 25832), 10))
    """
    vertexes_list = db_crud.run_query(conn, query, (geom_parcel.wkt, ))
    vertex_info = wkt.loads(vertexes_list[0][0])
    
    for free_geom in free_geom_list:
        free_geom = wkt.loads(free_geom[0])
        if free_geom.is_empty:
            continue
        position_candidates = [
            free_geom.representative_point(),
            polylabel(free_geom)
        ]
            
        for label_point in position_candidates:
            placed_label = util.translate_rotate_polygon(inline_poly, label_point.x, label_point.y, 0)

            if free_geom.buffer(overlap_threshold).contains(placed_label):
                leader_line, new_label_point = util.generate_leader_line(free_geom, geom_parcel, vertex_info, placed_label, cfg.scale, 0)
                if leader_line is not None and check_valid_leader_line(conn, leader_line):
                    label_point = new_label_point
                    placed_label = util.translate_rotate_polygon(inline_poly, label_point.x, label_point.y, 0)
                    return label_point, placed_label, 0, font_size, '/', is_nenner_only

    # Check with rotation if / splitter
    for free_geom in free_geom_list:
        position = find_label_pose(geom_parcel.wkt, inline_poly.wkt)
        if position is not None:
            label_point = Point(position["x"], position["y"])
            angle_deg = position["angle"]
            placed_label = util.translate_rotate_polygon(inline_poly, label_point.x, label_point.y, angle_deg)

            if free_geom.buffer(overlap_threshold).contains(placed_label):
                return label_point, placed_label, angle_deg, font_size, '/', is_nenner_only


    for free_geom in free_geom_list:
        free_geom = wkt.loads(free_geom[0])
        if free_geom.is_empty:
            continue
        
        position_candidates = [
            free_geom.representative_point(),
            polylabel(free_geom)
        ]

        for angle_deg in range(0, 180, 10):
            placed_label = util.translate_rotate_polygon(inline_poly, label_point.x, label_point.y, angle_deg)

            if free_geom.buffer(overlap_threshold).contains(placed_label):
                return label_point, placed_label, angle_deg, font_size, '/', is_nenner_only

    return None, None, 0, 0, '', is_nenner_only

# Draw label on overlapping point
# Return label_point, placed_label, angle_deg, font_size, splitter, is_nenner_only
def place_label_with_overlap(geom_parcel, free_geom_parcel, zaehler, nenner, overlap_threshold):
    font_size_normal = cfg.font_size_street[1]

    label_text = f"{zaehler}/{nenner}"
    if zaehler is None or zaehler == '':
        label_text = f"/{nenner}"
    if nenner is None or nenner == '':
        label_text = f"{zaehler}"
    inline_poly = util.text_to_polygon(label_text, font_size_normal, 'parcel', 0)

    geom_list = [geom_parcel, free_geom_parcel]
    for geom in geom_list:
        polygon_geoms = sorted(geom.geoms, key=lambda p: p.area, reverse=True)
        for polygon in polygon_geoms:
            label_point = polylabel(polygon)

            placed_label = util.translate_rotate_polygon(inline_poly, label_point.x, label_point.y, 0)

            if geom_parcel.buffer(overlap_threshold).contains(placed_label):
                return label_point, placed_label, 0, font_size_normal, '/', False

    representative_point = geom_parcel.representative_point()
    placed_label = util.translate_rotate_polygon(inline_poly, representative_point.x, representative_point.y, 0)
    return representative_point, placed_label, 0, font_size_normal, '/', False

def check_valid_leader_line(conn, leader_line):
    query = f"""
        SELECT ST_Area(ST_Union(obstacles_geom)) AS obstacles_geom_area
        FROM (
            SELECT calculated_label_area AS obstacles_geom
            FROM by_bbox_labels_{run_id}
            WHERE ST_Intersects(calculated_label_area, ST_GeomFromText(%s, 25832))
    
            UNION ALL
    
            SELECT ST_Buffer(geom, 0.1) AS obstacles_geom
            FROM by_bbox_lines_{run_id}
            WHERE ST_Intersects(geom, ST_GeomFromText(%s, 25832))

            UNION ALL

            SELECT ST_Buffer(position, 0.2) AS obstacles_geom
            FROM by_bbox_symbols_{run_id}
            WHERE ST_Contains(ST_GeomFromText(%s, 25832), position)

            UNION ALL

            SELECT ST_Buffer(geom, 0.8) AS obstacles_geom
            FROM by_bbox_vertex_{run_id}
            WHERE ST_Contains(ST_GeomFromText(%s, 25832), geom)
        ) o
    """
    overlap_area = db_crud.run_query(conn, query, (leader_line.wkt, leader_line.wkt, leader_line.wkt, leader_line.wkt))
    return overlap_area[0][0] == 0 or overlap_area[0][0] is None

def get_free_geom(conn, id):
    query = f"""
        SELECT ST_AsText(ST_Difference(
            p.geom,
            COALESCE(
                b.obstacles_geom, 
                ST_GeomFromText('GEOMETRYCOLLECTION EMPTY', 25832)
            )
        )) free_geom
        FROM (
            SELECT geom
            FROM by_bbox_parcel_{run_id}
            WHERE id = %s
        ) p
        LEFT JOIN LATERAL (
            SELECT ST_Union(obstacles_geom) AS obstacles_geom
            FROM (
                SELECT geom AS obstacles_geom
                FROM by_bbox_building_{run_id}
                WHERE ST_Intersects(geom, p.geom)
            
                UNION ALL
            
                SELECT calculated_label_area AS obstacles_geom
                FROM by_bbox_labels_{run_id}
                WHERE ST_Intersects(geom, p.geom)
    
                UNION ALL
        
                SELECT ST_Buffer(geom, 0.1) AS obstacles_geom
                FROM by_bbox_lines_{run_id}
                WHERE ST_Intersects(geom, p.geom)
    
                UNION ALL
        
                SELECT ST_Buffer(position, 0.2) AS obstacles_geom
                FROM by_bbox_symbols_{run_id}
                WHERE ST_Contains(p.geom, position)
    
                UNION ALL
        
                SELECT ST_Buffer(geom, 0.8) AS obstacles_geom
                FROM by_bbox_vertex_{run_id}
                WHERE ST_Contains(p.geom, geom)
            ) obs
        ) b ON true
    """
    free_geom_data = db_crud.run_query(conn, query, (id, ))

    if free_geom_data is None or len(free_geom_data) == 0:
        return None
    else:
        free_geom = free_geom_data[0][0]

    return wkt.loads(free_geom)

def split_line_centered(line: LineString, threshold: float):
    """
    Split a LineString into threshold-length parts, centered so that
    the leftover length is divided equally at the beginning and end.

    Returns:
        offset: float
        split_distances: list[float]
        parts: list[LineString]
    """
    if threshold <= 0:
        raise ValueError("threshold must be > 0")

    total_length = line.length

    if total_length <= threshold:
        # no split needed
        return 0.0, [0.0, total_length], [line]

    n = int(total_length // threshold)
    rest = total_length - n * threshold
    offset = rest / 2.0

    # boundaries: 0, offset, offset+T, offset+2T, ..., offset+nT, total_length
    boundaries = [0.0]

    # only add offset if it is meaningful
    if offset > 1e-9:
        boundaries.append(offset)

    for i in range(1, n + 1):
        boundaries.append(offset + i * threshold)

    if boundaries[-1] < total_length - 1e-9:
        boundaries.append(total_length)

    # remove duplicates caused by edge cases
    clean_boundaries = [boundaries[0]]
    for d in boundaries[1:]:
        if abs(d - clean_boundaries[-1]) > 1e-9:
            clean_boundaries.append(d)

    parts = []
    for start_d, end_d in zip(clean_boundaries[:-1], clean_boundaries[1:]):
        part = substring(line, start_d, end_d)
        if part.length > 1e-9:
            parts.append(part)

    return offset, clean_boundaries, parts

def compute_adaptive_gap(
    line_length: float,
    street_width: float,
    parcel_width: float,
    min_gap_abs: float = 4.0,
    max_gap_abs: float = 40.0,
    center_weight: float = 1.4,
    outer_weight: float = 1.0,
):
    """
    Compute a visually balanced adaptive gap between street and parcel labels.

    Returns:
        gap, can_draw_both
    """
    L = line_length
    Ws = street_width
    Wp = parcel_width

    free = L - Ws - Wp
    if free <= 0:
        return 0.0, False

    total_weight = outer_weight + center_weight + outer_weight
    preferred_gap = free * center_weight / total_weight

    min_gap = max(min_gap_abs, 0.10 * min(Ws, Wp))

    gap = max(min_gap, min(preferred_gap, max_gap_abs))

    can_draw_both = (Ws + gap + Wp) <= L
    return gap, can_draw_both

def line_angle_at(line, distance, eps=0.01):
    """
    Returns angle in degrees at distance along a Shapely LineString.
    0° = east/right, 90° = north/up.
    """
    d1 = max(0, distance - eps)
    d2 = min(line.length, distance + eps)

    p1 = line.interpolate(d1)
    p2 = line.interpolate(d2)

    dx = p2.x - p1.x
    dy = p2.y - p1.y

    return math.degrees(math.atan2(dy, dx))

def get_positions_from_gap(
    line,
    street_width,
    parcel_width,
    gap
):
    """
    Assumes both labels fit.
    Returns street and parcel positions.
    """

    L = line.length
    block = street_width + gap + parcel_width
    start = (L - block) / 2.0

    street_d = start + street_width / 2.0
    parcel_d = start + street_width + gap + parcel_width / 2.0

    street_anchor = line.interpolate(street_d)
    parcel_anchor = line.interpolate(parcel_d)

    street_angle = line_angle_at(line, street_d)
    parcel_angle = line_angle_at(line, parcel_d)

    return {
        "street": {
            "pos": Point([street_anchor.x, street_anchor.y]),
            "angle": street_angle
        },
        "parcel": {
            "pos": Point([parcel_anchor.x, parcel_anchor.y]),
            "angle": parcel_angle
        }
    }

def classify_polygon_road_long(polygon: Polygon):
    area = polygon.area
    perimeter = polygon.length

    compactness = 4 * math.pi * area / (perimeter ** 2)

    return compactness < 0.6

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

def check_label_render_without_zaehler(conn, id, numerator):
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
    
    return result[0][0] == 0

def move_bias(geom_parcel, poly, org_angle, drawn_street_label_rect_list, max_dist = 10, max_angle = 15):
    vals = range(-max_dist, max_dist + 1, 2)
    bias_steps = [(x, y) for x in vals for y in vals]
    bias_steps.sort(key=lambda p: (p[0] ** 2 + p[1] ** 2, abs(p[0]) + abs(p[1])))

    angles = [0] + [a for i in range(3, max_angle + 1, 3) for a in (i, -i)]

    for xoff, yoff in bias_steps:
        moved = translate(poly, xoff=xoff, yoff=yoff)

        for angle in angles:
            candidate = moved if angle == 0 else rotate(
                moved,
                angle,
                use_radians=False,
            )

            is_near_drawn_label = any(rect.distance(candidate) < 10 for rect in drawn_street_label_rect_list)
            if geom_parcel.contains(candidate) and not is_near_drawn_label:
                return candidate, org_angle + angle

    return poly, org_angle

def process_street_range(data: List[Any], counter, run_id, db_cadastral_param) -> None:
    cfg = load_config(run_id)

    conn = db_crud.create_connection(db_cadastral_param)
    threshold_segment_length = util.get_split_segment_length(cfg.scale)
    overlap_threshold = [0.3, 0.2, 0.4, 0.5, 0.1]
    font_size_list = cfg.font_size_street
    font_size_parcel_list = cfg.font_size_parcel

    for item in data:
        id, street_name, parcel_name, geom, street_center_geoms_wkt = item
        if not street_name and not parcel_name:
            continue
        if street_name is None:
            street_name = ""
        if parcel_name is None:
            parcel_name = ""
        ################## testing
        # non_skip_ids = {'09036100003576003400'}
        # if id not in non_skip_ids:
        #     continue
        ################## testing
        
        if street_name != "" and not check_street_name_render(conn, id, street_name, geom):
            street_name = ""

        zaehler = parcel_name[:parcel_name.find("/")] if "/" in parcel_name else parcel_name
        can_label_simple = check_label_render_without_zaehler(conn, id, zaehler)
        simple_parcel_name = parcel_name[parcel_name.find("/"):] if "/" in parcel_name and can_label_simple else parcel_name

        geom_parcel = wkt.loads(geom)
        # Make geom_parcel as MultiPolygon
        geom_parcel = util.make_multipolygon(geom_parcel)
        if len(geom_parcel.geoms) == 0:
            continue

        street_center_geoms = wkt.loads(street_center_geoms_wkt)
        if street_center_geoms is None or street_center_geoms.is_empty:
            continue
        street_center_geoms = util.make_multilinestring(street_center_geoms)

        drawn_street_label_rect_list = []
        drawn_parcel_label_rect_list = []

        # if label polygon almost exist in street inner, try to move some offset/angle
        # Step 1: if center line is enough long then split it and try to render parcel/street
        # Step 2: if not render street name, try to render on whole polygon
        # Step 3: if not render parcel name, try to render on whole polygon
        # Step 4: if not render parcel name, try to render with arrow

        # Get street, parcel label width
        street_width, _ = util.get_label_width_height(street_name, font_size_list[0], 'street', False, True)
        parcel_width, _ = util.get_label_width_height(parcel_name, font_size_parcel_list[0], 'parcel', True, False)

        # Make center line in bbox if it is enough long
        intersect_street_center_geoms = box(*cfg.input_bbox).intersection(street_center_geoms)
        if intersect_street_center_geoms.length > street_width + parcel_width + 40:
            street_center_geoms = util.make_multilinestring(intersect_street_center_geoms)

        # Step 1
        free_geom = geom_parcel
        is_render_parcel = False
        is_render_street = False
        if street_center_geoms.length > street_width + parcel_width + parcel_width: # last parcel_width is for offset
            # Longest line first
            sorted_lines = sorted(
                street_center_geoms.geoms,
                key=lambda line: line.length,
                reverse=True
            )
            for street_center_geom in sorted_lines:
                # skip center geom if length is too small and touch boundary
                if geom_parcel.boundary.distance(street_center_geom) < 1 and street_center_geom.length < street_width:
                    continue
                _, _, line_segments = split_line_centered(street_center_geom, threshold_segment_length)

                # Remove first, last segment if line count is eg 3
                if len(line_segments) > 2:
                    line_segments = line_segments[1:-1]

                for line in line_segments:
                    gap, _ = compute_adaptive_gap(line.length, street_width, parcel_width, max_gap_abs=threshold_segment_length / 3)
                    render_info = get_positions_from_gap(line, street_width, parcel_width, gap)

                    street_position = render_info["street"]["pos"]
                    street_angle = render_info["street"]["angle"]

                    parcel_position = render_info["parcel"]["pos"]
                    parcel_angle = render_info["parcel"]["angle"]

                    # Try to render street name
                    for font_size in font_size_list:
                        street_poly = util.get_label_polygon_from_center(street_position.x, street_position.y, street_name, font_size, 'street', street_angle, False, True)
                        if not free_geom.contains(street_poly):
                            # if label polygon almost exist in street inner, try to move some offset/angle
                            street_poly, street_angle = move_bias(free_geom, street_poly, street_angle, drawn_street_label_rect_list)
                    
                        if free_geom.contains(street_poly):
                            break
                    
                    # skip if too close to an already drawn street label
                    is_near_drawn_label = any(rect.distance(street_poly) < 50 for rect in drawn_street_label_rect_list)
                    if free_geom.contains(street_poly) and not is_near_drawn_label:
                        is_render = insert_street_label(conn, id, geom_parcel, street_name, "street", street_poly, street_angle, True, font_size)
                        if is_render:
                            drawn_street_label_rect_list.append(street_poly)
                            is_render_street = True
                            free_geom = free_geom.difference(street_poly)
                            free_geom = util.make_multipolygon(free_geom)
                    
                    if not is_render_street:
                        continue
                    # Try to render parcel name
                    for font_size in font_size_parcel_list:
                        parcel_poly = util.get_label_polygon_from_center(parcel_position.x, parcel_position.y, parcel_name, font_size, 'parcel', parcel_angle, True, False)
                        if not free_geom.contains(parcel_poly):
                            # if label polygon almost exist in street inner, try to move some offset/angle
                            parcel_poly, parcel_angle = move_bias(free_geom, parcel_poly, parcel_angle, drawn_street_label_rect_list)
                        
                        if free_geom.contains(parcel_poly):
                            break
                    
                    # skip if too close to an already drawn parcel/street label
                    is_near_drawn_label = any(rect.distance(parcel_poly) < 50 for rect in drawn_parcel_label_rect_list)
                    is_near_drawn_street = any(rect.distance(parcel_poly) < 10 for rect in drawn_street_label_rect_list)
                    if free_geom.contains(parcel_poly) and not is_near_drawn_label and not is_near_drawn_street:
                        is_render = insert_street_label(conn, id, geom_parcel, parcel_name, "parcel", parcel_poly, parcel_angle, False, font_size)
                        if is_render:
                            drawn_parcel_label_rect_list.append(parcel_poly)
                            free_geom = free_geom.difference(parcel_poly)
                            free_geom = util.make_multipolygon(free_geom)
                            is_render_parcel = True

        max_polygon = max(geom_parcel.geoms, key=lambda p: p.area)
        max_polygon_free = max(free_geom.geoms, key=lambda p: p.area)
        # prepare angles
        angle_deg = util.pca_angle(max_polygon)
        street_center_angle = util.pca_line_angle(max(street_center_geoms.geoms, key=lambda line: line.length))
        base_angles = [
            angle_deg,
            street_center_angle
        ]
        angle_list = [0]
        offsets = [0, -3, 3, -6, 6, -9, 9, -12, 12, -15, 15]
        for base_angle in base_angles:
            for offset in offsets:
                angle_list.append(base_angle + offset)

        # Step 2
        if not is_render_street:
            label = street_name

            for font_size in font_size_list:
                label_point_list = [max_polygon.representative_point(), polylabel(max_polygon)]
                label_point_list.append(get_label_position_candidates(conn, free_geom, label, font_size, 0, run_id))
                label_point_list.append(get_label_position_candidates(conn, free_geom, label, font_size, angle_deg, run_id))
                for angle in angle_list:
                    inline_poly = util.text_to_polygon(label, font_size, 'street', cfg.parcel_label_padding)
                    for label_point in label_point_list:
                        placed_label = util.translate_rotate_polygon(inline_poly, label_point.x, label_point.y, angle)
                        # skip if too close to an already drawn street label
                        if any(rect.distance(placed_label) < 50 for rect in drawn_street_label_rect_list):
                            continue
                        if free_geom.contains(placed_label):
                            is_render = insert_street_label(conn, id, geom_parcel, label, "street", placed_label, angle, True, font_size)
                            if is_render:
                                free_geom = free_geom.difference(placed_label)
                                free_geom = util.make_multipolygon(free_geom)
                                is_render_street = True
                                drawn_street_label_rect_list.append(placed_label)
                                break
                    
                    if is_render_street:
                        break
                if is_render_street:
                    break
        if not is_render_street:
            label = street_name
            # Longest line first
            sorted_lines = sorted(
                street_center_geoms.geoms,
                key=lambda line: line.length,
                reverse=True
            )

            for font_size in font_size_list:
                street_width, _ = util.get_label_width_height(label, font_size, 'street', False, True)
                if street_width <= 0:
                    break

                # get points in split center line splitted by street label width
                label_point_list = []
                for street_center_geom in sorted_lines:
                    _, _, line_segments = split_line_centered(street_center_geom, street_width / 2)
                    for line in line_segments:
                        label_point_list.append(line.interpolate(line.length / 2))

                # set angle from angle_list
                for angle in angle_list:
                    for label_point in label_point_list:
                        placed_label = util.get_label_polygon_from_center(label_point.x, label_point.y, label, font_size, 'street', angle, False, True)
                        # skip if too close to an already drawn street label
                        if any(rect.distance(placed_label) < 50 for rect in drawn_street_label_rect_list):
                            continue
                        if free_geom.contains(placed_label):
                            is_render = insert_street_label(conn, id, geom_parcel, label, "street", placed_label, angle, True, font_size)
                            if is_render:
                                free_geom = free_geom.difference(placed_label)
                                free_geom = util.make_multipolygon(free_geom)
                                is_render_street = True
                                drawn_street_label_rect_list.append(placed_label)
                                break

                    if is_render_street:
                        break
                if is_render_street:
                    break

        # Step 3
        if not is_render_parcel:
            label = parcel_name
            
            for font_size in font_size_parcel_list:
                label_point_list = [max_polygon.representative_point(), polylabel(max_polygon), free_geom.representative_point(), polylabel(max_polygon_free)]
                label_point_list.append(get_label_position_candidates(conn, free_geom, label, font_size, 0, run_id))
                label_point_list.append(get_label_position_candidates(conn, free_geom, label, font_size, angle_deg, run_id))
                for angle in angle_list:
                    inline_poly = util.text_to_polygon(label, font_size, 'parcel', cfg.parcel_label_padding)
                    for label_point in label_point_list:
                        placed_label = util.translate_rotate_polygon(inline_poly, label_point.x, label_point.y, angle)
                        is_near_drawn_street = any(rect.distance(placed_label) < 10 for rect in drawn_street_label_rect_list)
                        if free_geom.contains(placed_label):
                            if is_near_drawn_street:
                                placed_label, angle = move_bias(free_geom, placed_label, angle, drawn_street_label_rect_list)
                            is_render = insert_street_label(conn, id, geom_parcel, label, "parcel", placed_label, angle, True, font_size)
                            if is_render:
                                is_render_parcel = True
                                break
                    
                    if is_render_parcel:
                        break
                if is_render_parcel:
                    break
        
        # Step 4
        vertex_info = wkt.loads("POLYGON EMPTY")
        if not is_render_parcel:
            labels = [parcel_name, simple_parcel_name] if parcel_name != simple_parcel_name else [parcel_name]
            for label in labels:
                padding = [util.get_arrow_length_parcel(cfg.scale) * 0.4, util.get_arrow_length_parcel(cfg.scale) * 0.2]
                for pad in padding:
                    for threshold in overlap_threshold:
                        label_point, placed_label, label_angle, font_size, splitter, is_nenner_only = place_label_with_arrow(conn, id, free_geom, label, '', threshold, pad, can_label_simple)
                        if label_point is not None:
                            leader_line, new_label_point = util.generate_leader_line(free_geom, free_geom, vertex_info, placed_label, cfg.scale, label_angle)
                            if leader_line is not None and check_valid_leader_line(conn, leader_line):
                                label_point = new_label_point
                                placed_label = util.translate_rotate_polygon(inline_poly, label_point.x, label_point.y, angle_deg)
                                is_render = insert_street_label(conn, id, geom_parcel, label, "parcel", placed_label, label_angle, False, font_size)
                                if is_render:
                                    # insert leader line
                                    insert_street_line(conn, id, leader_line)
                                    is_render_parcel = True
                                    break
                    if is_render_parcel:
                        break
                if is_render_parcel:
                    break
        
        if not is_render_parcel:
            label = simple_parcel_name
            label_point, placed_label, label_angle, font_size, splitter, is_nenner_only = place_label_with_overlap(free_geom, geom_parcel, label, '', 0)
            insert_street_label(conn, id, geom_parcel, label, "parcel", placed_label, label_angle, False, font_size, True)

        if is_render_street or is_render_parcel:
            counter['processed'] += 1
        else:
            counter['missed'] += 1

        if counter['processed'] % 20 == 0:
            now = time.time()
            print(f"Processed: {counter['processed']}, Elapsed: {now - counter['start_time']:.2f} sec")

    db_crud.close_connection(conn)

def main(is_production):
    print("==============Street name labeling")

    conn = db_crud.create_connection(db_cadastral_param)

    # Empty labels table with street labeling
    query = f"DELETE FROM by_bbox_labels_{run_id} WHERE source = 'street'"
    db_crud.run_query(conn, query)
    query = f"DELETE FROM by_bbox_lines_{run_id} WHERE is_street = true"
    db_crud.run_query(conn, query)

    start_time = time.time()
    
    # Get parcel info including free space geometry
    query = f"""
        SELECT
            p.id, s.street_name, p.name AS parcel_name,
            ST_AsText(
                ST_Difference(
                    p.geom, 
                    COALESCE(
                        obstacle.obs_geom, 
                        ST_GeomFromText('GEOMETRYCOLLECTION EMPTY', 25832)
                    )
                )
            ) AS geom,
            ST_AsText(
                ST_Difference(
                    COALESCE(s.street_center_wms, p.street_center),
                    COALESCE(
                        obstacle.obs_geom, 
                        ST_GeomFromText('GEOMETRYCOLLECTION EMPTY', 25832)
                    )
                )
            ) AS center_geom
        FROM by_bbox_parcel_{run_id} p
        JOIN by_bbox_parcel_street_{run_id} s ON p.id = s.parcel_id
		LEFT JOIN LATERAL (
			SELECT ST_Union(obs) obs_geom
			FROM (
				SELECT ST_Buffer(geom, 3) obs FROM by_bbox_symbols_{run_id}
				WHERE ST_Intersects(p.geom, geom)

				UNION ALL

				SELECT ST_Buffer(geom, 0.5) obs FROM by_bbox_lines_{run_id}
				WHERE ST_Intersects(p.geom, geom)

				UNION ALL
                
				SELECT ST_Buffer(calculated_label_area, 0.5) obs FROM by_bbox_labels_{run_id}
				WHERE ST_Intersects(p.geom, calculated_label_area)

				UNION ALL
                
				SELECT ST_Buffer(ST_Boundary(geom), 0.5) obs FROM by_bbox_landkreis_{run_id}
				WHERE ST_Intersects(p.geom, geom)
			) o
		) obstacle ON true
        WHERE is_street = true AND GeometryType(p.geom) IN ('POLYGON', 'MULTIPOLYGON')
            AND COALESCE(s.is_street_name_suppressed, false) = false AND 
            ST_Intersects(p.geom, ST_MakeEnvelope(%s, %s, %s, %s, 25832))
        ORDER BY ST_Area(geom) ASC
    """
    data = db_crud.run_query(conn, query, (*cfg.input_bbox, ))
    print(f"{len(data)} records Processing started")
    
    
    manager = Manager()
    processed_counter = manager.dict()
    processed_counter['processed'] = 0
    processed_counter['missed'] = 0
    processed_counter['start_time'] = start_time

    if is_production and cfg.cpu_cores > 1:
        chunk_list = util.split_into_chunks(data, cfg.cpu_cores)
        
        # close()+join() instead of the context manager: Pool.__exit__ calls terminate(),
        # whose _help_stuff_finish() grabs inqueue._rlock while an idle worker still holds
        # it inside SimpleQueue.get(), which can block the shutdown forever.
        pool = Pool(min(cfg.cpu_cores, len(chunk_list)))
        try:
            pool.starmap(process_street_range, [(chunk, processed_counter, run_id, db_cadastral_param) for chunk in chunk_list])
            pool.close()
        except BaseException:
            pool.terminate()
            raise
        finally:
            pool.join()
    else:
        process_street_range(data, processed_counter, run_id, db_cadastral_param)

    print(f"Missed: {processed_counter['missed']}, Processed: {processed_counter['processed']}")
    print(f"Completed, Total Elapsed: {time.time() - start_time:.2f} sec")
    
    db_crud.close_connection(conn)
    

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: python street.py <id>")
        print('Example command format: "python street.py by_101125"')
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