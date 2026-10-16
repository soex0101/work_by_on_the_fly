# Find candidate edges and set score with 
#   - edge length
#   - distance to street
#   - distance between the position where the label is drawn and the center point of the line drawn on the convex polygon from that position
import sys
import math
from multiprocessing import Pool, Manager
from shapely import wkt
from shapely.affinity import translate
from shapely.geometry import Polygon, MultiPolygon, LineString, Point, MultiLineString, GeometryCollection
from shapely.ops import nearest_points
from typing import List, Any
import os
from collections import Counter, defaultdict

import db_crud
import time
import util

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from read_settings import load_config

cfg = None
run_id = ""
db_cadastral_param = None

# Add label rendering info to ming_labels table
def insert_label_render(conn, id, pos, angle, label, font_size, label_rect, source = 'building'):
    query = f"INSERT INTO by_bbox_labels_{run_id} (source_id, source, name, label_position, font_size, label_angle, alkis_angle, calculated_label_area, is_street) VALUES " \
        "(%s, %s, %s, ST_GeomFromText(%s, 25832), %s, %s, %s, ST_GeomFromText(%s, 25832), %s)"
    
    sld_rotation = util.convert_to_geoserver_rotation(angle)
    db_crud.run_query(conn, query, (id, source, label, pos.wkt, font_size, sld_rotation, angle, label_rect.wkt, False))

def _unit(vx, vy):
    n = math.hypot(vx, vy)
    if n == 0:
        return 0.0, 0.0
    return vx / n, vy / n


def _signed_area(coords):
    """Positive => CCW, negative => CW"""
    area = 0.0
    for i in range(len(coords) - 1):
        x1, y1 = coords[i]
        x2, y2 = coords[i + 1]
        area += x1 * y2 - x2 * y1
    return area / 2.0


def _outward_normal(p1, p2, ring_is_ccw):
    """
    For an edge p1->p2, return outward unit normal.
    If ring is CCW, interior is left side, so outward is right normal.
    If ring is CW, interior is right side, so outward is left normal.
    """
    dx = p2[0] - p1[0]
    dy = p2[1] - p1[1]
    ux, uy = _unit(dx, dy)

    if ring_is_ccw:
        # right normal
        nx, ny = uy, -ux
    else:
        # left normal
        nx, ny = -uy, ux

    return nx, ny


def _edge_midpoint(p1, p2):
    return Point((p1[0] + p2[0]) / 2.0, (p1[1] + p2[1]) / 2.0)


def _score_edge(edge, midpoint, outward_normal, street_geom, max_edge_len):
    """
    Higher score means more likely to be the facade toward the street.

    score = facing_strength / distance

    facing_strength = dot(outward_normal, direction_to_street)
    only positive values count
    """
    nearest_on_street = nearest_points(midpoint, street_geom)[1]

    vx = nearest_on_street.x - midpoint.x
    vy = nearest_on_street.y - midpoint.y
    dist = math.hypot(vx, vy)

    if dist == 0:
        # midpoint touches street; perfect candidate
        return float("inf"), 1.0, 0.0

    dirx, diry = vx / dist, vy / dist
    nx, ny = outward_normal

    facing = nx * dirx + ny * diry  # cosine-like alignment
    if facing < 0:
        facing = 0.0

    score = facing / dist
    return score, facing, dist


def get_building_edge_toward_street(building_geom, street_geom):
    if street_geom is None or street_geom.is_empty:
        return []

    if isinstance(building_geom, Polygon):
        polygons = (building_geom,)
    elif isinstance(building_geom, MultiPolygon):
        polygons = building_geom.geoms
    else:
        return []

    rings = []
    max_edge_len = 0.0
    for poly in polygons:
        coords = list(poly.exterior.coords)
        ring_is_ccw = _signed_area(coords) > 0
        rings.append((coords, ring_is_ccw))
        for i in range(len(coords) - 1):
            p1 = coords[i]
            p2 = coords[i + 1]
            edge_len = math.hypot(p2[0] - p1[0], p2[1] - p1[1])
            if edge_len > max_edge_len:
                max_edge_len = edge_len

    candidates = []
    for coords, ring_is_ccw in rings:
        for i in range(len(coords) - 1):
            p1 = coords[i]
            p2 = coords[i + 1]
            if p1 == p2:
                continue

            edge = LineString([p1, p2])
            midpoint = _edge_midpoint(p1, p2)
            outward_normal = _outward_normal(p1, p2, ring_is_ccw)
            score, facing, dist = _score_edge(edge, midpoint, outward_normal, street_geom, max_edge_len)
            if facing <= 0.4:
                continue

            candidates.append({
                "edge": edge,
                "score": score,
                "facing": facing,
                "distance": dist
            })

    return sorted(candidates, key=lambda x: x["score"], reverse=True)

def get_street_center_geom(conn, street_id):
    query = f"""
        SELECT 
            ST_AsText(ST_Union(s.street_center_wms)), ST_AsText(ST_Union(p.geom)) 
        FROM by_bbox_parcel_{run_id} p
        LEFT JOIN by_bbox_parcel_street_{run_id} s ON p.id = s.parcel_id
        WHERE p.id = %s
    """
    data = db_crud.run_query(conn, query, (street_id, ))

    return (data[0][0], data[0][1]) if len(data) > 0 else (None, None)

def get_nearest_street_center_geom(conn, geom):
    query = f"""
        WITH nearest AS (
            SELECT id, geom, street_center
            FROM by_bbox_parcel_{run_id}
            WHERE is_street = true
            ORDER BY geom <-> ST_GeomFromText(%s, 25832)
            LIMIT 1
        )
        SELECT
            ST_AsText(ST_Union(COALESCE(s.street_center_wms, n.street_center))),
            ST_AsText(n.geom)
        FROM nearest n
        LEFT JOIN by_bbox_parcel_street_{run_id} s ON n.id = s.parcel_id
        GROUP BY n.id, n.geom
    """
    data = db_crud.run_query(conn, query, (geom.wkt, ))

    return wkt.loads(data[0][0]) if len(data) > 0 else None

def nearest_edge(multiline: MultiLineString, pt: Point):
    """
    Return the LineString edge/segment in MultiLineString nearest to pt.
    """

    best_edge = None
    best_dist = float("inf")

    for line in multiline.geoms:
        coords = list(line.coords)

        # split LineString into individual edges
        for i in range(len(coords) - 1):
            edge = LineString([coords[i], coords[i + 1]])
            dist = edge.distance(pt)

            if dist < best_dist:
                best_dist = dist
                best_edge = edge

    return best_edge, best_dist

_STRAIGHT_ANGLE_COS_SQ = math.cos(math.radians(5.0)) ** 2

def _simplify_ring_near_straight(coords):
    points = list(coords[:-1])
    if len(points) < 4:
        return coords

    simplified = []
    changed = False
    n = len(points)
    for i, p2 in enumerate(points):
        p1 = points[i - 1]
        p3 = points[(i + 1) % n]
        v1x = p1[0] - p2[0]
        v1y = p1[1] - p2[1]
        v2x = p3[0] - p2[0]
        v2y = p3[1] - p2[1]
        len1_sq = v1x * v1x + v1y * v1y
        len2_sq = v2x * v2x + v2y * v2y
        if len1_sq == 0.0 or len2_sq == 0.0:
            changed = True
            continue

        dot = v1x * v2x + v1y * v2y
        if dot < 0.0 and dot * dot >= _STRAIGHT_ANGLE_COS_SQ * len1_sq * len2_sq:
            changed = True
            continue

        simplified.append(p2)

    if not changed or len(simplified) < 3:
        return coords
    simplified.append(simplified[0])
    return simplified

def _simplify_polygon_near_straight(poly):
    exterior = _simplify_ring_near_straight(poly.exterior.coords)
    interiors = [_simplify_ring_near_straight(ring.coords) for ring in poly.interiors]
    simplified = Polygon(exterior, interiors)
    return simplified if simplified.is_valid and not simplified.is_empty else poly

def simplify_building_geom_near_straight(geom):
    if isinstance(geom, Polygon):
        return _simplify_polygon_near_straight(geom)
    if isinstance(geom, MultiPolygon):
        return MultiPolygon([_simplify_polygon_near_straight(poly) for poly in geom.geoms])
    return geom

def _nearest_and_far_point(line: LineString, pt: Point, far_distance: float):
    """
    Nearest point on `line` to `pt`, then a "far" point `far_distance` away from
    it heading toward the line's farther endpoint (on the extended line if it
    overruns). Returns (nearest_point, far_point, far_point_distance_to_pt).
    """
    along = line.project(pt)
    nearest = line.interpolate(along)

    start = Point(line.coords[0])
    end = Point(line.coords[-1])
    target_end = end if nearest.distance(end) >= nearest.distance(start) else start

    dx = target_end.x - nearest.x
    dy = target_end.y - nearest.y
    seg_len = math.hypot(dx, dy)
    if seg_len == 0:
        return nearest, nearest, nearest.distance(pt)
    ux, uy = dx / seg_len, dy / seg_len
    far = Point(nearest.x + ux * far_distance, nearest.y + uy * far_distance)
    return nearest, far, far.distance(pt)


def select_nearest_edge(pt: Point, edges, far_distance=5.0):
    """
    Pick the edge nearest to `pt` using the far-point rule: for each edge take a
    point `far_distance` along it (toward its farther endpoint) from the point
    nearest `pt`, and choose the edge whose far point is closest to `pt`.
    """
    best_edge = None
    best_dist = None
    for edge in edges:
        _, _, dist = _nearest_and_far_point(edge, pt, far_distance)
        if best_dist is None or dist < best_dist:
            best_dist = dist
            best_edge = edge
    return best_edge


def get_boundary_edges(mpoly: MultiPolygon, include_holes=True):
    """
    Return all boundary edges from a Polygon/MultiPolygon.
    Each edge is a LineString between two consecutive boundary points.
    """
    if mpoly is None:
        return []
    
    edges = []

    polygons = mpoly.geoms if mpoly.geom_type == "MultiPolygon" else [mpoly]

    for poly in polygons:
        # exterior boundary
        coords = list(poly.exterior.coords)
        for i in range(len(coords) - 1):
            edges.append(LineString([coords[i], coords[i + 1]]))

        # interior holes
        if include_holes:
            for ring in poly.interiors:
                coords = list(ring.coords)
                for i in range(len(coords) - 1):
                    edges.append(LineString([coords[i], coords[i + 1]]))

    return edges


def get_longest_shared_edge(building_geom, street_geom, label_position=None, tolerance=0.05):
    """
    Return the building boundary edge that shares a segment with the street
    polygon's boundary and lies nearest to `label_position`. When
    `label_position` is None, fall back to the edge with the longest overlap.
    `tolerance` (meters) is the buffer applied to the street boundary to absorb
    minor coordinate drift between the building and street cadastral layers.

    Returns (edge, overlap_length); (None, 0.0) if street_geom is empty or no
    edge shares any segment with the street boundary.
    """
    if street_geom is None or street_geom.is_empty or label_position is None:
        return None, 0

    # if label position buffer 5 contains several edges
    street_boundary_buf = street_geom.boundary.buffer(tolerance + 1)
    best_edge = None
    best_score = -1
    for edge in get_boundary_edges(building_geom, include_holes=False):
        clipped = edge.intersection(street_boundary_buf)
        if clipped.is_empty:
            continue
        overlap_len = clipped.length

        # Pick the shared edge nearest to the label, breaking ties by the
        # longer overlap.
        distance = edge.distance(label_position)
        score = overlap_len * 0.4 + (- distance * 0.2)
        if best_edge is None or score > best_score:
            best_score = score
            best_edge = edge
    return best_edge, best_score

def get_max_font_size_form_parcel(conn, building_id):
    query = f"""
        SELECT p.id
        FROM by_bbox_building_{run_id} b
        JOIN by_bbox_parcel_{run_id} p ON ST_Contains(ST_Buffer(p.geom, 1), b.geom) OR ST_Contains(p.geom, ST_Centroid(b.geom))
        WHERE b.id = %s
            AND ST_Area(b.geom) > ST_Area(p.geom) * 0.8
            AND ST_Area(p.geom) < 30
    """
    data = db_crud.run_query(conn, query, (building_id, ))
    if len(data) > 0:
        return cfg.font_size_building[2]
    return cfg.font_size_building[0]

def move_polygon_away_from_line(
    polygon: Polygon,
    line: LineString,
    step: float
) -> Polygon:
    centroid = polygon.centroid

    # Nearest point on the line to the polygon centroid
    nearest_on_line = line.interpolate(line.project(centroid))

    # Direction from the line toward the polygon centroid
    dx = centroid.x - nearest_on_line.x
    dy = centroid.y - nearest_on_line.y

    length = math.hypot(dx, dy)

    if length == 0:
        raise ValueError("Polygon centroid is on the line; direction is undefined.")

    return translate(
        polygon,
        xoff=step * dx / length,
        yoff=step * dy / length
    )

def process_building_range(data: List[Any], counter, run_id, db_cadastral_param) -> None:
    cfg = load_config(run_id)

    conn = db_crud.create_connection(db_cadastral_param)
    font_size = cfg.font_size_building[0]

    for item in data:
        building_id, building_geom_wkt = item
        building_geom = wkt.loads(building_geom_wkt)
        
        ################## testing
        # if building_id != 460380:
        #    continue
        ################## testing

        max_font_size = get_max_font_size_form_parcel(conn, building_id)
        building_geom = simplify_building_geom_near_straight(building_geom)
        boundary_edges = get_boundary_edges(building_geom, include_holes=False)
        # Get building labels group by street_bbox_geom
        query = f"""
            SELECT 
                house_number, ST_AsText(position), street_parcel_id, ST_AsText(street_bbox_geom)
            FROM by_bbox_building_labels_{run_id} WHERE building_id = %s
            ORDER BY street_bbox_geom
        """
        label_rows = db_crud.run_query(conn, query, (building_id, ))

        label_list = {}
        skip_add = False # define this variable because there are building label which haven't clear edge
        for idx, label in enumerate(label_rows):
            house_number, position_wkt, street_parcel_id, street_bbox_geom_wkt = label
            
            label_width, _ = util.get_label_width_height(label[0], font_size, 'building')
            label_position = wkt.loads(position_wkt)

            # check label_row have only one angle
            candidate_angle_list = []
            for edge in boundary_edges:
                if edge.distance(label_position) <= 5 and edge.length >= label_width * 0.7:
                    angle = int(util.pca_angle_geom(edge))
                    if not any(angle - 5 <= c <= angle + 5 for c in candidate_angle_list):
                        candidate_angle_list.append(angle)

            if len(candidate_angle_list) == 1:
                if skip_add:
                    label_list[str(idx)] = [
                        {
                            "house_number": house_number,
                            "position": label_position,
                            "street_parcel_id": street_parcel_id
                        }
                    ]
                    continue

                skip_add = True

            if street_bbox_geom_wkt not in label_list:
                label_list[street_bbox_geom_wkt] = []
            label_list[street_bbox_geom_wkt].append({
                "house_number": house_number,
                "position": wkt.loads(position_wkt),
                "street_parcel_id": street_parcel_id
            })

        # render label_rows with same angle
        for _, label_rows in label_list.items():
            street_parcel_id = label_rows[0]["street_parcel_id"]  # Get the street_parcel_id from the first label row
            street_center_wkt, street_geom_wkt = get_street_center_geom(conn, street_parcel_id)
            street_center = wkt.loads(street_center_wkt) if street_center_wkt else None
            street_geom = wkt.loads(street_geom_wkt) if street_geom_wkt else None

            label_width, _ = util.get_label_width_height(label_rows[0]["house_number"], font_size, 'building')
        
            best_edge = None
            label_position = label_rows[0]["position"]

            # find all edges that are within 5 units from the label position and have length longer than the label width
            filtered_edges = []

            for label_row in label_rows:
                label_position = label_row["position"]

                for edge in boundary_edges:
                    if edge.distance(label_position) <= 5 and edge.length >= label_width * 0.7:
                        filtered_edges.append({
                            "edge": edge,
                            "distance": label_position.distance(edge)
                        })
            
            # edges that satisfy the condition for every label position of this group
            edge_distances = defaultdict(list)
            for item in filtered_edges:
                edge_distances[item["edge"]].append(item["distance"])

            fit_all_edges = [
                (edge, distances) for edge, distances in edge_distances.items()
                if len(distances) == len(label_rows)
            ]

            if len(label_rows) > 1 and fit_all_edges:
                # when more than one edge fits every label position,
                # keep the one nearest to the label positions
                edge, _ = min(fit_all_edges, key=lambda pair: sum(pair[1]))
                best_edge = {"edge": edge}
            elif len(filtered_edges) == 1:
                best_edge = filtered_edges[0]
            elif (
                street_geom is not None
                and not street_geom.is_empty
                and building_geom.distance(street_geom) < 20
            ):
                # Building shares (or sits right next to) the street parcel.
                # Anchor the label to the longest shared boundary segment.
                tolerance = building_geom.distance(street_geom)
                shared_edge, _ = get_longest_shared_edge(building_geom, street_geom, label_position, tolerance)
                if shared_edge is not None:
                    # Check if nearest edge exist than shared_edge
                    near_edge_count = 0
                    # shared_edge_angle = int(util.pca_angle_geom(shared_edge))
                    for edge in boundary_edges:
                        dist = edge.distance(label_position) + 1 # 1 is a offset value that more near than label_position
                        if dist < shared_edge.distance(label_position) and dist < 5:
                            near_edge_count += 1

                            # if near edge have similar angle with shared_edge, set it as best edge
                            # edge_angle = int(util.pca_angle_geom(edge))
                            # if abs(shared_edge_angle - edge_angle) < 5 or abs(shared_edge_angle - edge_angle - 180) < 5:
                            #     if best_edge is not None and best_edge['edge'].distance(label_position) < edge.distance(label_position):
                            #         continue
                            #     best_edge = {'edge': edge}
                    
                    if near_edge_count < 1:
                        # Try to locate label in that position with angle
                        angle = get_label_angle(building_geom, shared_edge, label_position)
                        label_rect = util.get_label_polygon_from_center(
                            label_position.x,
                            label_position.y,
                            house_number,
                            font_size,
                            'building',
                            angle,
                            True,
                            False,
                            cfg.building_label_padding
                        )
                        if building_geom.contains(label_rect):
                            best_edge = {"edge": shared_edge}
            
            if best_edge is None:
                edge_counts = Counter(item["edge"] for item in filtered_edges)

                filtered_edges = sorted(
                    filtered_edges,
                    key=lambda item: edge_counts[item["edge"]],
                    reverse=True
                )

                if len(filtered_edges) > 0:
                    # take the edges tied for nearest plain distance to the label,
                    # then disambiguate with the far-point rule (above logic)
                    min_distance = min(item["distance"] for item in filtered_edges)
                    nearest_edges = [
                        item["edge"] for item in filtered_edges
                        if item["distance"] <= min_distance + 1e-6
                    ]
                    best_edge = {"edge": select_nearest_edge(label_position, nearest_edges)}
                else:
                    edge_candidates = get_building_edge_toward_street(building_geom, street_center)
                    edge_candidates.sort(key=lambda edge: (edge["facing"], edge["score"]), reverse=True)
                    for edge in edge_candidates:
                        if edge["edge"].length >= label_width:
                            best_edge = edge
                            break

            if best_edge is None:
                continue

            angle = get_label_angle(building_geom, best_edge["edge"], label_position)
            font_size_list = cfg.font_size_building[cfg.font_size_building.index(max_font_size):]
            for label_row in label_rows:
                is_render = False
                for font_size in font_size_list:
                    label_position = label_row["position"]
                    house_number = label_row["house_number"]

                    label_rect = util.get_label_polygon_from_center(
                        label_position.x,
                        label_position.y,
                        house_number,
                        font_size,
                        'building',
                        angle,
                        True,
                        False,
                        cfg.building_label_padding
                    )
                    # if label_rect not contains in building_geom, try to move some offset
                    if not building_geom.contains(label_rect):
                        for step in [0.5, 0.8, 1]:
                            moved_label_rect = move_polygon_away_from_line(label_rect, best_edge["edge"], step)
                            if building_geom.contains(moved_label_rect):
                                label_rect = moved_label_rect
                                label_position = label_rect.centroid
                                break

                    if building_geom.contains(label_rect) or \
                        (not is_render and font_size == cfg.font_size_building[-1]):

                        insert_label_render(conn, building_id, label_position, angle, house_number, font_size, label_rect)
                        counter['processed'] += 1
                        is_render = True
                        break

        if counter['processed'] % 50 == 0:
            now = time.time()
            print(f"Processed: {counter['processed']}, Elapsed: {now - counter['start_time']:.2f} sec")

    db_crud.close_connection(conn)

def get_label_angle(polygon: Polygon, nearest_edge: LineString, label_pos: Point) -> float:
    """
    Return the label rotation angle in degrees so that:
    - text runs parallel to the nearest edge
    - the TOP side of the text faces label_pos (i.e. away from the edge)
    - the BOTTOM side of the text faces the edge

    Parameters
    ----------
    polygon : shapely.geometry.Polygon
    nearest_edge : shapely.geometry.LineString
        The edge segment nearest to the label
    label_pos : shapely.geometry.Point
        Desired label position (used to project onto the edge)

    Returns
    -------
    float
        Rotation angle in degrees
    """

    coords = list(nearest_edge.coords)
    if len(coords) < 2:
        raise ValueError("nearest_edge must have at least 2 coordinates")

    (x1, y1), (x2, y2) = coords[0], coords[-1]

    dx = x2 - x1
    dy = y2 - y1

    if dx == 0 and dy == 0:
        raise ValueError("nearest_edge has zero length")

    # Project label position onto the edge
    projected_dist = nearest_edge.project(label_pos)
    anchor = nearest_edge.interpolate(projected_dist)

    # Which side of the edge label_pos actually sits on (left-normal test).
    # label_pos is the real point the text will be drawn at, so its side
    # IS the side that must face "up" - this is more reliable than probing
    # the polygon (breaks down near reflex vertices) or the polygon
    # centroid (can fall outside the polygon for concave footprints).
    left_normal_x = -dy
    left_normal_y = dx
    vx = label_pos.x - anchor.x
    vy = label_pos.y - anchor.y
    dot = left_normal_x * vx + left_normal_y * vy

    # Point the edge-direction vector toward the label's side before
    # reading off the angle, instead of computing a base angle and
    # conditionally flipping it by 180.
    sign = -1.0 if dot < 0 else 1.0
    angle = math.degrees(math.atan2(sign * dy, sign * dx))

    # Keep angle in [0, 360)
    angle %= 360

    return angle


#############################################################
# Utilties for no geom building labels
#############################################################
def check_valid_no_geom_building_label(conn, label_poly):
    query = f"""
        SELECT COUNT(*) cnt
        FROM by_bbox_labels_{run_id}
        WHERE ST_Intersects(calculated_label_area, ST_GeomFromText(%s, 25832))
    """
    data = db_crud.run_query(conn, query, (label_poly.wkt, ))

    return data[0][0] == 0


def iter_polygon_edges(geom):
    """
    Yield every edge (as LineString) from a Polygon or MultiPolygon.
    Includes exterior edges and hole edges.
    """
    if isinstance(geom, Polygon):
        polygons = [geom]
    elif isinstance(geom, MultiPolygon):
        polygons = list(geom.geoms)
    else:
        raise TypeError("geom must be Polygon or MultiPolygon")

    for poly in polygons:
        # exterior ring
        ext = list(poly.exterior.coords)
        for i in range(len(ext) - 1):
            yield LineString([ext[i], ext[i + 1]])

        # interior rings
        for ring in poly.interiors:
            coords = list(ring.coords)
            for i in range(len(coords) - 1):
                yield LineString([coords[i], coords[i + 1]])

def edge_angle_deg(edge: LineString):
    """
    Return angle of edge in degrees.
    Range: -180 to 180
    """
    coords = list(edge.coords)
    if len(coords) < 2:
        raise ValueError("Edge must have at least 2 points")

    x1, y1 = coords[0]
    x2, y2 = coords[-1]

    angle = math.degrees(math.atan2(y2 - y1, x2 - x1))
    return angle

def nearest_edge_from_multipolygon(multipolygon, point):
    """
    Find the nearest edge in a Polygon/MultiPolygon to a Point.

    Returns:
        {
            "edge": LineString,
            "distance": float,
            "nearest_point_on_edge": Point
        }
    """
    if not isinstance(point, Point):
        raise TypeError("point must be shapely.geometry.Point")

    nearest_edge = None
    nearest_dist = float("inf")
    nearest_point_on_edge = None

    for edge in iter_polygon_edges(multipolygon):
        dist = edge.distance(point)
        if dist < nearest_dist:
            nearest_dist = dist
            nearest_edge = edge
            nearest_point_on_edge = nearest_points(point, edge)[1]

    return {
        "edge": nearest_edge,
        "distance": nearest_dist,
        "nearest_point_on_edge": nearest_point_on_edge,
        "angle_deg": edge_angle_deg(nearest_edge)
    }

# Get drawable position and angle on edge in polygon inner
def get_text_position_and_angle(edge: LineString, polygon: Polygon, offset_distance=5.0) -> tuple[Point, float]:
    # Get midpoint of the edge
    midpoint = edge.interpolate(0.5, normalized=True)

    # Get edge direction vector
    x1, y1 = edge.coords[0]
    x2, y2 = edge.coords[1]
    dx = x2 - x1
    dy = y2 - y1
    edge_angle_rad = math.atan2(dy, dx)
    edge_angle_deg = math.degrees(edge_angle_rad)

    # Get perpendicular (normal) vector, rotate 90┬░ CCW
    nx = -dy
    ny = dx
    norm_len = math.hypot(nx, ny)
    nx /= norm_len
    ny /= norm_len

    # Try inward offset
    offset_point = Point(midpoint.x + nx * offset_distance, midpoint.y + ny * offset_distance)

    if not polygon.contains(offset_point):
        # Try opposite side (flip normal)
        offset_point = Point(midpoint.x - nx * offset_distance, midpoint.y - ny * offset_distance)
 
        if not polygon.contains(offset_point):
            return None, None  # Can't place inside
        else:
            edge_angle_deg = (edge_angle_deg + 180) % 360

    return offset_point, edge_angle_deg

def get_rotated_building_no_geom_label(conn, center_x, center_y, text, font_size, padding):
    query = f"""
        SELECT ST_AsText(geom)
        FROM by_bbox_parcel_{run_id}
        WHERE ST_Contains(geom, ST_SetSRID(ST_MakePoint(%s, %s), 25832))
    """
    data = db_crud.run_query(conn, query, (center_x, center_y))
    geom = data[0][0] if len(data) > 0 else None
    if geom is None:
        return 0
    
    geom = wkt.loads(geom)
    geom = util.make_multipolygon(geom)

    point = Point(center_x, center_y)
    nearest_edge = nearest_edge_from_multipolygon(geom, point)

    _, angle = get_text_position_and_angle(nearest_edge["edge"], geom, cfg.building_label_padding)

    return angle

def get_free_building_geom(conn, center_point):
    query = f"""
        SELECT 
            ST_AsText(ST_Difference(p.geom, o.obstacles_geom)) free_geom
        FROM (
            SELECT geom FROM by_bbox_parcel_{run_id}
            WHERE ST_Contains(geom, ST_GeomFromText(%s, 25832))
        ) p
        LEFT JOIN LATERAL (
            SELECT ST_Union(calculated_label_area) AS obstacles_geom
            FROM by_bbox_labels_{run_id}
        ) o ON true
    """
    data = db_crud.run_query(conn, query, (center_point.wkt, ))
    if len(data) > 0:
        free_geom = wkt.loads(data[0][0])
        return free_geom
        
    return None

def label_rotation_from_two_points(first_point, second_point):
    """
    first_point:  (x1, y1)
    second_point: (x2, y2)

    Returns text baseline rotation angle in degrees.
    The label top side faces second_point.
    The label bottom side faces first_point.
    """

    x1 = first_point.x
    y1 = first_point.y
    x2 = second_point.x
    y2 = second_point.y

    dx = x2 - x1
    dy = y2 - y1

    # Direction angle from first point to second point
    direction_angle = math.degrees(math.atan2(dy, dx))

    # Text top side should face second point,
    # so text baseline is perpendicular to that direction.
    text_angle = direction_angle - 90

    return text_angle

def get_segments_from_multilinestring(mls):
    segments = []

    for line in mls.geoms:
        coords = list(line.coords)

        for i in range(len(coords) - 1):
            segment = LineString([coords[i], coords[i + 1]])
            segments.append(segment)

    return segments

def process_multiple_no_geom_building_labels(conn, address_list):
    cfg = load_config(run_id)

    # find active/passive building labels
    active_info = {}
    label_position_dict = {
        address[0]: wkt.loads(address[2])
        for address in address_list
    }
    house_number_dict = {
        address[0]: address[1]
        for address in address_list
    }
    processed_position_dict = {}

    for address in address_list:
        address_id, house_number, house_center_point_wkt, street_geom_wkt, parcel_geom_wkt, parcel_id = address
        label_pos = label_position_dict[address_id]
        parcel_boundary = wkt.loads(parcel_geom_wkt).boundary

        buffered = label_pos.buffer(label_pos.distance(parcel_boundary))

        inside_positions = [
            pos_id
            for pos_id, pos in processed_position_dict.items()
            if buffered.covers(pos)
        ]

        if label_pos.distance(parcel_boundary) > 5 or len(inside_positions) > 0:
            # it is passive, finding nearest active id
            active_ids = list(active_info.keys())

            if active_ids:
                nearest_active_id = min(
                    active_ids,
                    key=lambda active_id: label_position_dict[active_id].distance(label_pos)
                )
                if nearest_active_id:
                    active_info[nearest_active_id].append(address_id)
                    processed_position_dict[address_id] = label_pos
        else:
            active_info[address_id] = []
            processed_position_dict[address_id] = label_pos

    rendered_id_list = []
    # get angle for active edges
    for active_id, passive_id_list in active_info.items():
        if len(passive_id_list) < 1:
            continue

        # get addresses
        query = f"""
            SELECT
                'HsNr. ' || house_number,
                a.street_name,
                ST_AsText(a.house_center_point_geom),
                ST_AsText(p.geom) parcel_geom,
                ST_AsText(s.geom) street_geom
            FROM by_bbox_address_{run_id} a
            LEFT JOIN by_bbox_parcel_{run_id} p ON ST_Contains(p.geom, a.house_center_point_geom)
            LEFT JOIN (
                SELECT ST_Union(p.geom) geom, s.street_name
                FROM by_bbox_parcel_street_{run_id} s
                LEFT JOIN by_bbox_parcel_{run_id} p ON s.parcel_id = p.id
                GROUP BY street_name
            ) s ON a.street_name = s.street_name
            WHERE a.id = %s
        """
        address_list = db_crud.run_query(conn, query, (active_id, ))
        address_info = address_list[0]
        
        parcel_geom = wkt.loads(address_info[3])
        angle, font_size, label_rect = get_no_geom_building_label(
            conn, address_info[0], address_info[1], 
            wkt.loads(address_info[2]), wkt.loads(address_info[3]), wkt.loads(address_info[3]), wkt.loads(address_info[4]),
            True
        )
        angle_list = [angle]
        for i in range(1, 40):
            angle_list.append(angle + i * 2)
            angle_list.append(angle - i * 2)
        
        font_size_list = cfg.font_size_building[cfg.font_size_building.index(font_size):]
        is_all_rendered = False
        for cand_angle in angle_list:
            for cand_font_size in font_size_list:
                active_pos = label_position_dict[active_id]
                label_rect = util.get_label_polygon_from_center(
                    active_pos.x,
                    active_pos.y,
                    "HsNr. " + house_number_dict[active_id],
                    cand_font_size,
                    'building',
                    cand_angle,
                    True,
                    False,
                    cfg.building_label_padding if font_size == cfg.font_size_building[0] else 0
                )
                rendered_count = 0
                is_all_rendered = False
                
                for passive_id in passive_id_list:
                    passive_pos = label_position_dict[passive_id]
                    passive_label_rect = util.get_label_polygon_from_center(
                        passive_pos.x,
                        passive_pos.y,
                        "HsNr. " + house_number_dict[passive_id],
                        cand_font_size,
                        'building',
                        cand_angle,
                        True,
                        False,
                        cfg.building_label_padding if font_size == cfg.font_size_building[0] else 0
                    )
                
                    if not parcel_geom.contains(passive_label_rect) or label_rect.intersects(passive_label_rect):
                        continue
                    
                    label_rect = util.make_multipolygon(label_rect)
                    label_rect = MultiPolygon(list(label_rect.geoms) + [passive_label_rect])
                    angle = cand_angle
                    font_size = cand_font_size

                    rendered_count += 1
                
                if rendered_count == len(passive_id_list):
                    is_all_rendered = True
                    break
            if is_all_rendered:
                break
    
        if is_all_rendered:
            id_list = [active_id] + passive_id_list
            for address_id in id_list:
                label_position = label_position_dict[address_id]
                label = "HsNr. " + house_number_dict[address_id]
                label_rect = util.get_label_polygon_from_center(
                    label_position.x,
                    label_position.y,
                    label,
                    font_size,
                    'building',
                    angle,
                    True,
                    False,
                    cfg.building_label_padding if font_size == cfg.font_size_building[0] else 0
                )
                insert_label_render(conn, address_id, label_position, angle, label, font_size, label_rect, 'building_no_geom')

            rendered_id_list.append(active_id)
            rendered_id_list = rendered_id_list + passive_id_list

    return rendered_id_list

def get_line_parts(geom):
    if geom.is_empty:
        return []

    if isinstance(geom, LineString):
        return [geom]

    if isinstance(geom, MultiLineString):
        return list(geom.geoms)

    if isinstance(geom, GeometryCollection):
        parts = []
        for g in geom.geoms:
            parts.extend(get_line_parts(g))
        return parts

    return []


def angle_deg(p1, p2):
    dx = p2[0] - p1[0]
    dy = p2[1] - p1[1]
    return math.degrees(math.atan2(dy, dx)) % 360


def get_label_angle_from_intersection(label_position, intersection):
    _, nearest_pt = nearest_points(label_position, intersection)
    normal_angle = angle_deg(
        (label_position.x, label_position.y),
        (nearest_pt.x, nearest_pt.y)
    )

    # label baseline should be perpendicular to this direction
    return (normal_angle + 90) % 360


def get_no_geom_building_label(conn, label, street_name, label_position, parcel_geom, parcel_obs_geom, street_geom, is_multiple):
    cfg = load_config(run_id)

    best_edge = None
    angle = -1
    boundary_org_edges = get_boundary_edges(parcel_geom, include_holes=False)
    boundary_obs_edges = get_boundary_edges(parcel_obs_geom, include_holes=False)
    boundary_edges_list = [boundary_org_edges, boundary_obs_edges]
    filtered_edges = []

    label_width, _ = util.get_label_width_height(label, cfg.font_size_building[-1], 'building')

    for boundary_edges in boundary_edges_list:
        # find street touch edge on parcel
        if street_geom is not None and not street_geom.is_empty:
            for edge in boundary_edges:
                if edge.distance(label_position) < 6:
                    touch_part = edge.intersection(street_geom.boundary)
                    if not touch_part.is_empty and touch_part.length > 0:
                        filtered_edges.append({
                            "edge": edge,
                            "distance": label_position.distance(edge)
                        })

            if len(filtered_edges) > 0:
                filtered_edges = sorted(
                    filtered_edges,
                    key=lambda item: item["distance"]
                )
                filtered_edges = filtered_edges[:1]

        # if no touch edge, find near edge
        if len(filtered_edges) < 1:
            for edge in boundary_edges:
                if edge.distance(label_position) <= 5 and edge.length >= label_width:
                    filtered_edges.append({
                        "edge": edge,
                        "distance": label_position.distance(edge)
                    })

        if len(filtered_edges) == 1:
            best_edge = filtered_edges[0]
            if best_edge["edge"].distance(street_geom) == 0:
                # get parcel-center to street-side direction
                parcel_center = parcel_geom.centroid
                street_side_center = best_edge["edge"].interpolate(best_edge["edge"].length / 2)

                normal_angle = angle_deg(
                    (parcel_center.x, parcel_center.y),
                    (street_side_center.x, street_side_center.y)
                )

                # label baseline should be perpendicular to this direction
                label_angle = (normal_angle + 90) % 360

                intersection = best_edge["edge"].intersection(street_geom)
                if intersection.geom_type == "LineString" or intersection.geom_type == "MultiLineString":
                    label_width, _ = util.get_label_width_height(label, cfg.font_size_building[0], 'building', True, False)

                    # check label can draw enough in parcel inner if is_multiple = False
                    if not is_multiple:
                        # check 
                        temp_angle = get_label_angle_from_intersection(label_position, intersection)
                        label_rect = util.get_label_polygon_from_center(
                            label_position.x,
                            label_position.y,
                            label,
                            cfg.font_size_building[1],
                            'building',
                            temp_angle,
                            True,
                            False,
                            0
                        )
                        if parcel_geom.contains(label_rect):
                            label_angle = temp_angle

                    # check touch street length is enough to render label if is_multiple = True
                    if is_multiple and intersection.length > label_width:
                        label_angle = get_label_angle_from_intersection(label_position, intersection)

                # forcely set the angle
                angle = label_angle
        elif (
            street_geom is not None
            and not street_geom.is_empty
            and parcel_geom.distance(street_geom) < 10
        ):
            # Building shares (or sits right next to) the street parcel.
            # Anchor the label to the longest shared boundary segment.
            tolerance = parcel_geom.distance(street_geom)
            shared_edge, _ = get_longest_shared_edge(parcel_geom, street_geom, label_position, tolerance)
            if shared_edge is not None:
                # Check if nearest edge exist than shared_edge
                near_edge_count = 0
                close_street_edge_count = 0
                for edge in boundary_edges:
                    dist = edge.distance(label_position)
                    if dist <= shared_edge.distance(label_position) and dist < 5:
                        near_edge_count += 1
                        if edge.interpolate(edge.length / 2).distance(street_geom) < 0.1:
                            close_street_edge_count += 1
                
                if close_street_edge_count == 1 or near_edge_count < 1:
                    label_angle = get_label_angle(parcel_geom, shared_edge, label_position)
                    label_rect = util.get_label_polygon_from_center(
                        label_position.x,
                        label_position.y,
                        label,
                        cfg.font_size_building[-1],
                        'building',
                        label_angle,
                        True,
                        False,
                        0
                    )
                    if parcel_geom.contains(label_rect):
                        best_edge = {"edge": shared_edge}
                        angle = label_angle
                elif close_street_edge_count > 1:
                    # get nearest street center edge
                    # query = f"""
                    #     SELECT 
                    #         ST_AsText(ST_Union(street_center_wms))
                    #     FROM by_bbox_parcel_street_{run_id} 
                    #     WHERE street_name = %s
                    # """
                    # street_center = db_crud.run_query(conn, query, (street_name, ))
                    # segments = get_segments_from_multilinestring(wkt.loads(street_center[0][0]))
                    # nearest_edge = min(segments, key=lambda edge: edge.distance(label_position))
                    segments = get_segments_from_multilinestring(util.make_multilinestring(parcel_geom.boundary))
                    nearest_edge = min(segments, key=lambda edge: edge.distance(label_position))

                    best_edge = {"edge": nearest_edge}
        
        if best_edge is None:
            edge_counts = Counter(item["edge"] for item in filtered_edges)

            filtered_edges = sorted(
                filtered_edges,
                key=lambda item: edge_counts[item["edge"]],
                reverse=True
            )

            if len(filtered_edges) > 0:
                # take the edges tied for nearest plain distance to the label,
                # then disambiguate with the far-point rule (above logic)
                min_distance = min(item["distance"] for item in filtered_edges)
                nearest_edges = [
                    item["edge"] for item in filtered_edges
                    if item["distance"] <= min_distance + 1e-6
                ]
                best_edge = {"edge": select_nearest_edge(label_position, nearest_edges)}
            else:
                # find enough width edge that toward to street
                edge_candidates = get_building_edge_toward_street(parcel_geom, street_geom)
                edge_candidates.sort(key=lambda edge: (edge["facing"], edge["score"]), reverse=True)
                for edge in edge_candidates:
                    if edge["edge"].length >= label_width:
                        best_edge = edge
                        break

        if best_edge is None:
            # Get nearest edge that near by street, or label position
            min_dist = -1
            for edge in boundary_edges:
                if min_dist == -1 or (
                    (street_geom is not None and edge.distance(street_geom) < min_dist) or
                    (street_geom is None and edge.distance(label_position) < min_dist)
                ):
                    best_edge = {"edge": edge}

        angle1 = get_label_angle(parcel_geom, best_edge["edge"], label_position)
        angle = int(angle)
        angle1 = int(angle1)

        angle_list = [angle, angle1]
        step = 2
        offsets = [0, step, -step, step * 2, -step * 2, step * 3, -step * 3]
        if angle == -1:
            angle_list = [angle1]
        
        for label_angle in angle_list:
            for font_size in cfg.font_size_building:
                for offset in offsets:
                    angle = label_angle + offset
                    label_rect = util.get_label_polygon_from_center(
                        label_position.x,
                        label_position.y,
                        label,
                        font_size,
                        'building',
                        angle,
                        True,
                        False,
                        cfg.building_label_padding if font_size == cfg.font_size_building[0] else 0
                    )

                    if (font_size >= cfg.font_size_building[1] and parcel_geom.contains(label_rect.buffer(0.5))) or \
                        (font_size <= cfg.font_size_building[2] and parcel_geom.buffer(0.5).contains(label_rect)):

                        if check_valid_no_geom_building_label(conn, label_rect) and not any(edge.intersects(label_rect) for edge in boundary_obs_edges):
                            return angle, font_size, label_rect
        
    return None, None, None

def process_no_geom_building_label(conn, id, label, street_name, label_position, parcel_id, parcel_geom, parcel_obs_geom, street_geom):
    angle, font_size, label_rect = get_no_geom_building_label(conn, label, street_name, label_position, parcel_geom, parcel_obs_geom, street_geom, False)
    if angle is not None:
        insert_label_render(conn, id, label_position, angle, label, font_size, label_rect, 'building_no_geom')
        return True
    
    return False

def main(is_production):
    print("==============Building labeling")

    conn = db_crud.create_connection(db_cadastral_param)

    # Empty labels table with building
    query = f"""
        DELETE FROM by_bbox_labels_{run_id}
        WHERE source IN ('building', 'building_no_geom');
    """
    db_crud.run_query(conn, query)

    start_time = time.time()
    
    # Get building data which have house number and label position
    query = f"""
        SELECT b.id, ST_AsText(b.geom)
        FROM (
            SELECT
                DISTINCT b.id
            FROM by_bbox_building_{run_id} b, by_bbox_building_labels_{run_id} l
            WHERE b.id = l.building_id AND l.house_number IS NOT NULL
        ) t
        LEFT JOIN by_bbox_building_{run_id} b ON t.id = b.id
        WHERE ST_Intersects(b.geom, ST_MakeEnvelope(%s, %s, %s, %s, 25832))
    """
    data = db_crud.run_query(conn, query, (*cfg.input_bbox, ))
    print(f"{len(data)} records processing started")
    
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
            pool.starmap(process_building_range, [(chunk, processed_counter, run_id, db_cadastral_param) for chunk in chunk_list])
            pool.close()
        except BaseException:
            pool.terminate()
            raise
        finally:
            pool.join()
    else:
        process_building_range(data, processed_counter, run_id, db_cadastral_param)

    print(f"{len(data)} records (with geometry) completed")
    
    #############################################################
    # Adding no building geometry labels
    #############################################################
    processed_address_id = []

    #### process building labels first for multiple labels in one parcel
    # get parcel list
    query = f"""
        SELECT
            p.id
        FROM by_bbox_address_{run_id} a
        JOIN by_bbox_parcel_{run_id} p
            ON ST_Contains(p.geom, a.house_center_point_geom)
        WHERE 
            NOT EXISTS (
                SELECT 1
                FROM by_bbox_building_{run_id} b
                WHERE b.geom IS NOT NULL
                AND ST_Contains(b.geom, a.house_center_point_geom)
            )
            AND NOT ST_IsEmpty(p.geom)
        GROUP BY p.id
        HAVING COUNT(a.id) > 1
    """
    parcel_list = db_crud.run_query(conn, query)
    for parcel in parcel_list:
        parcel_id = parcel[0]
        ###############testing
        # if parcel_id != '09921700003168001400':
        #     continue
        ###############testing

        # get street name
        query = f"""
            SELECT street_name
            FROM by_bbox_address_{run_id}
            WHERE ST_Contains(
                (SELECT geom FROM by_bbox_parcel_{run_id} WHERE id = %s), 
                house_center_point_geom
            )
            GROUP BY street_name
            HAVING COUNT(id) > 1
        """
        street_list = db_crud.run_query(conn, query, (parcel_id, ))

        for street in street_list:
            street_name = street[0]
            
            # get addresses
            query = f"""
                SELECT
                    a.id,
                    house_number,
                    ST_AsText(a.house_center_point_geom),
                    ST_AsText(s.geom) street_geom,
                    ST_AsText(p.geom) parcel_geom,
                    p.id
                FROM by_bbox_address_{run_id} a
                LEFT JOIN (
                    SELECT ST_Union(p.geom) geom, s.street_name
                    FROM by_bbox_parcel_street_{run_id} s
                    LEFT JOIN by_bbox_parcel_{run_id} p ON s.parcel_id = p.id
                    GROUP BY street_name
                ) s ON a.street_name = s.street_name
                LEFT JOIN (
                    SELECT 
                        p.id, 
                        ST_Difference(p.geom, 
                            COALESCE(o.obs_geom, ST_GeomFromText('GEOMETRYCOLLECTION EMPTY', 25832))
                        ) geom
                    FROM by_bbox_parcel_{run_id} p
                    LEFT JOIN LATERAL (
                        SELECT ST_Union(geom) obs_geom
                        FROM by_bbox_building_{run_id} b
                        WHERE ST_Intersects(b.geom, p.geom)
                    ) o ON true
                ) p ON ST_Contains(p.geom, a.house_center_point_geom)
                WHERE a.street_name = %s AND p.id = %s AND a.house_center_point_geom IS NOT NULL
                    AND NOT EXISTS (
                        SELECT 1
                        FROM by_bbox_building_{run_id} b
                        WHERE b.geom IS NOT NULL
                        AND ST_Contains(b.geom, a.house_center_point_geom)
                    ) AND NOT ST_IsEmpty(p.geom)
                ORDER BY ST_Distance(ST_Boundary(p.geom), a.house_center_point_geom)
            """
            address_list = db_crud.run_query(conn, query, (street_name, parcel_id))

            if len(address_list) > 0:
                rendered_id_list = process_multiple_no_geom_building_labels(conn, address_list)
                processed_address_id = processed_address_id + rendered_id_list

    # process remains
    query = f"""
        SELECT
            a.id,
            a.house_number,
            a.street_name,
            ST_AsText(a.house_center_point_geom),
            ST_AsText(s.geom) street_geom,
            ST_AsText(p.geom) parcel_geom,
            ST_AsText(p.obs_geom) parcel_obs_geom,
            p.id
        FROM by_bbox_address_{run_id} a
        LEFT JOIN (
            SELECT ST_Union(p.geom) geom, s.street_name
            FROM by_bbox_parcel_street_{run_id} s
            LEFT JOIN by_bbox_parcel_{run_id} p ON s.parcel_id = p.id
            GROUP BY street_name
        ) s ON a.street_name = s.street_name
        LEFT JOIN (
            SELECT p.id, p.geom geom, ST_Difference(p.geom, o.obs_geom) obs_geom
            FROM by_bbox_parcel_{run_id} p
            LEFT JOIN LATERAL (
                SELECT ST_Union(geom) obs_geom
                FROM by_bbox_building_{run_id} b
                WHERE ST_Intersects(b.geom, p.geom)
            ) o ON true
        ) p ON ST_Contains(p.geom, a.house_center_point_geom)
        WHERE a.house_center_point_geom IS NOT NULL
            AND NOT EXISTS (
                SELECT 1
                FROM by_bbox_building_{run_id} b
                WHERE b.geom IS NOT NULL
                AND ST_Contains(b.geom, a.house_center_point_geom)
            ) AND NOT ST_IsEmpty(p.geom)
    """
    data_hsnr = db_crud.run_query(conn, query)
    print(f"{len(data_hsnr)} records (with no geometry) Processing started")

    for item in data_hsnr:
        id, house_number, street_name, label_position_wkt, street_wkt, parcel_wkt, parcel_obs_wkt, parcel_id = item
        ###############testing
        # if id != 'DEBYvAAAAACA57EV':
        #     continue
        ###############testing
        
        # Skip already processed in above stage
        if id in processed_address_id:
            continue
        
        label_position = wkt.loads(label_position_wkt)
        street_geom = wkt.loads(street_wkt)
        parcel_geom = wkt.loads(parcel_wkt)
        parcel_obs_geom = wkt.loads(parcel_obs_wkt)
        text = f'HsNr. {house_number}'
        
        process_no_geom_building_label(conn, id, text, street_name, label_position, parcel_id, parcel_geom, parcel_obs_geom, street_geom)

    print(f"Completed, Total Elapsed: {time.time() - start_time:.2f} sec, Building count - {len(data)}, NonBuilding count - {len(data_hsnr)}")
    
    db_crud.close_connection(conn)
    

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: python building.py <id>")
        print('Example command format: "python building.py by_101125"')
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
