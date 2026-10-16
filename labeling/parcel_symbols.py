# Parcel symbols (Parcel part only in now)

import math
from multiprocessing import Pool, Manager
import numpy as np
from shapely import wkt
from shapely.geometry import Polygon, MultiPolygon, LineString, Point, MultiLineString
from shapely.ops import nearest_points, substring
from typing import List, Any
from shapely.affinity import rotate, translate
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

def polygon_edges(polygon):
    """Return a list of edges (LineStrings) for a polygon exterior."""
    coords = list(polygon.exterior.coords)
    edges = [LineString([coords[i], coords[i+1]]) for i in range(len(coords)-1)]
    return edges

def get_angle_from_vertex(segment: LineString, vertex_index: int):
    coords = list(segment.coords)
    if vertex_index == 0:
        x1, y1 = coords[0]
        x2, y2 = coords[1]
    else:
        x1, y1 = coords[1]
        x2, y2 = coords[0]

    dx = x2 - x1
    dy = y2 - y1
    
    angle_rad = math.atan2(dy, dx)
    angle_deg = math.degrees(angle_rad)
    
    return angle_deg

def insert_symbol(conn, parcel_id, symbol_name, position, size, angle):
    query = f"INSERT INTO by_bbox_symbols_{run_id} (parcel_id, symbol_name, position, size, angle, alkis_angle, geom) VALUES " \
        "(%s, %s, ST_GeomFromText(%s, 25832), %s, %s, %s, ST_GeomFromText(%s, 25832))"
    
    line = symbol_line_from_point_angle(position, angle + 90)

    sld_rotation = util.convert_to_geoserver_rotation(angle)
    angle = (angle + 360) % 360
    db_crud.run_query(conn, query, (parcel_id, symbol_name, position.wkt, size, sld_rotation, angle, line.wkt))

def geoms_to_unique_neighbor_dict(polygon_geoms):
    """
    polygon_geoms should be multipolygon.geoms.

    Uses 0-based indexes.

    Returns:
        unique_neighbor_dict, ordered_ids
    """

    polygons = list(polygon_geoms)

    if len(polygons) == 0:
        return {}, []

    if len(polygons) == 1:
        return {0: []}, [0]

    # Collect undirected pairs as a unique set so reverse duplicates like
    # (2, 1) / (1, 2) are not stored twice. We combine two strategies below.
    pairs = set()

    # Strategy 1 (original): order the polygons along their main axis (SVD of the
    # centroids) and pair each consecutive polygon.
    centroids = np.array([[poly.centroid.x, poly.centroid.y] for poly in polygons])
    mean = centroids.mean(axis=0)
    _, _, vh = np.linalg.svd(centroids - mean)
    main_axis = vh[0]

    projections = (centroids - mean) @ main_axis
    ordered_ids = sorted(range(len(polygons)), key=lambda idx: projections[idx])

    for i in range(len(ordered_ids) - 1):
        pairs.add(tuple(sorted((ordered_ids[i], ordered_ids[i + 1]))))

    # Strategy 2 (nearest): pair every polygon with its nearest polygon
    # (edge-to-edge distance).
    for i, poly in enumerate(polygons):  # 0-based
        nearest_j = None
        nearest_dist = float('inf')
        for j, other in enumerate(polygons):
            if i == j:
                continue
            dist = poly.distance(other)
            if dist < nearest_dist:
                nearest_dist = dist
                nearest_j = j

        if nearest_j is not None:
            pairs.add(tuple(sorted((i, nearest_j))))

    # Build unique neighbor dictionary keyed by the smaller index.
    neighbor_dict = {}
    for a, b in sorted(pairs):
        neighbor_dict.setdefault(a, []).append(b)

    return neighbor_dict, ordered_ids

def is_duplicate(ext, extended_edges):
    for existing in extended_edges:
        if ext.equals(existing):
            return True
    return False

def get_extended_edges(polygon):
    coords = list(polygon.exterior.coords)
    edges = [
        LineString([coords[i], coords[i + 1]])
        for i in range(len(coords) - 1)  # ignore duplicate closing point
    ]

    # Step 2: Extend each edge using the full set
    extended_edges = []
    for edge in edges:
        ext = extend_edge(edge, edges)
        if not is_duplicate(ext, extended_edges):
            extended_edges.append(ext)

    return extended_edges
    
def angle_between_three_points(p0: Point, p1: Point, p2: Point) -> float:
    v1 = (p0.x - p1.x, p0.y - p1.y)
    v2 = (p2.x - p1.x, p2.y - p1.y)
    len1 = math.hypot(*v1); len2 = math.hypot(*v2)
    if len1 == 0 or len2 == 0:
        return 0.0
    dot = v1[0]*v2[0] + v1[1]*v2[1]
    cos_ = max(-1.0, min(1.0, dot / (len1 * len2)))
    return math.degrees(math.acos(cos_))

def _endpoint_equal(seg, pt: Point) -> bool:
    c = seg.coords
    return Point(c[0]).equals(pt) or Point(c[-1]).equals(pt)

def _other_endpoint(seg, shared: Point) -> Point:
    c0, c1 = seg.coords[0], seg.coords[-1]
    return Point(c1) if Point(c0).equals(shared) else Point(c0)

def _pt_key(p, ndigits=8):
    return (round(p.x, ndigits), round(p.y, ndigits))

def _seg_key(p1, p2, ndigits=8):
    a = _pt_key(p1, ndigits)
    b = _pt_key(p2, ndigits)
    return tuple(sorted([a, b]))

def _extend_one_direction(ext_edge: LineString, edges, at_start: bool,
                          angle_min=150, angle_max=210) -> LineString:
    coords = list(ext_edge.coords)
    # define the oriented segment we compare against
    if at_start:
        start, end = Point(coords[0]), Point(coords[1])
    else:
        start, end = Point(coords[-1]), Point(coords[-2])

    visited_edges = set()

    # mark original ext_edge segments as already used
    for i in range(len(coords) - 1):
        p1 = Point(coords[i])
        p2 = Point(coords[i + 1])
        visited_edges.add(_seg_key(p1, p2))

    while True:
        progressed = False
        for seg in edges:
            # must share the 'start' endpoint, and not be the same 2-pt segment
            if not _endpoint_equal(seg, start):
                continue

            p0 = _other_endpoint(seg, start)   # candidate next point
            candidate_key = _seg_key(start, p0)

            # prevent reusing same edge
            if candidate_key in visited_edges:
                continue

            # prevent closing back into existing coordinate loop
            if _pt_key(p0) in {_pt_key(Point(c)) for c in coords}:
                continue
            
            p1 = start
            p2 = end
            ang = angle_between_three_points(p0, p1, p2)

            if angle_min <= ang <= angle_max:
                if at_start:
                    coords.insert(0, (p0.x, p0.y))
                    # update oriented pair
                    end = start
                    start = p0
                else:
                    coords.append((p0.x, p0.y))
                    # update oriented pair
                    end = start
                    start = p0
                progressed = True
                break
        if not progressed:
            break

    return LineString(coords)

def extend_edge(edge: LineString, edges, angle_min=150, angle_max=210) -> LineString:
    ext = LineString(edge.coords)  # copy
    # grow at the start
    ext = _extend_one_direction(ext, edges, at_start=True,
                                angle_min=angle_min, angle_max=angle_max)
    # grow at the end
    ext = _extend_one_direction(ext, edges, at_start=False,
                                angle_min=angle_min, angle_max=angle_max)
    return ext


def sort_edge_pairs_by_distance(edgesA, edgesB):
    edge_pairs = []
    for i, edgeA in enumerate(edgesA):
        for j, edgeB in enumerate(edgesB):
            dist = edgeA.interpolate(0.5, normalized=True).distance(edgeB.interpolate(0.5, normalized=True))
            edge_pairs.append(((i, j), dist))

    # Sort by distance
    edge_pairs.sort(key=lambda x: x[1])
    return edge_pairs

# Simulate symbol polygon
def symbol_polygon(line: LineString, dist: float):
    start = Point(line.coords[0])
    end = Point(line.coords[-1])
    # Direction vector of original line
    dx = end.x - start.x
    dy = end.y - start.y

    # Normalize and rotate 90 degrees (perpendicular)
    length = np.hypot(dx, dy)
    if length == 0:
        raise ValueError("Zero-length segment")

    # Perpendicular unit vector
    perp_dx = -dy / length
    perp_dy = dx / length

    # Half distance on each side
    half = dist / 2.0

    # Compute two points along the perpendicular
    p1 = Point(start.x + perp_dx * half, start.y + perp_dy * half)
    p2 = Point(start.x - perp_dx * half, start.y - perp_dy * half)

    return Polygon([end, p1, p2, end])

def get_symbol_info(poly1: Polygon, poly2: Polygon, obstacles=None) -> tuple[Point, int, Point, int]:
    symbol_size = util.get_symbol_size("parcel_part", cfg.scale) / 3
    obstacles = obstacles or []
    edges1 = get_extended_edges(poly1)
    edges2 = get_extended_edges(poly2)
    edge_pair = sort_edge_pairs_by_distance(edges1, edges2)

    min_len = float('inf')
    max_area = 0
    pt1 = None
    ang1 = 0
    pt2 = None
    ang2 = 0
    # Best candidate ignoring the "symbol must stay inside polygon inner" rule,
    # used only when force is set and no strict candidate is found.
    force_max_area = 0
    for pair_info in edge_pair:
        pair, _ = pair_info
        i1, i2 = pair

        # Get common area lines
        edge1 = edges1[i1]
        edge2 = edges2[i2]
        start_1 = Point(edge1.coords[0])
        end_1 = Point(edge1.coords[-1])
        start_2 = Point(edge2.coords[0])
        end_2 = Point(edge2.coords[-1])

        dist_list = [] # (distance, pt1, pt2)
        _, start_1_on_edge2 = nearest_points(start_1, edge2)
        dist_list.append((start_1.distance(edge2), start_1, start_1_on_edge2))

        _, end_1_on_edge2 = nearest_points(end_1, edge2)
        dist_list.append((end_1.distance(edge2), end_1, end_1_on_edge2))

        start_2_on_edge1, _ = nearest_points(edge1, start_2)
        dist_list.append((start_2.distance(edge1), start_2_on_edge1, start_2))

        end_2_on_edge1, _ = nearest_points(edge1, end_2)
        dist_list.append((end_2.distance(edge1), end_2_on_edge1, end_2))

        dist_list.sort(key=lambda x: x[0])

        # Get common lines
        d1 = edge1.project(dist_list[0][1])
        d2 = edge1.project(dist_list[1][1])
        if abs(d1 - d2) > 1:
            edge1_lines = substring(edge1, d1, d2)
        else:
            edge1_lines = edge1

        d1 = edge2.project(dist_list[0][2])
        d2 = edge2.project(dist_list[1][2])
        if abs(d1 - d2) > 1:
            edge2_lines = substring(edge2, d1, d2)
        else:
            edge2_lines = edge2

        # Get more nearest positions
        mid1 = edge1_lines.interpolate(0.5, normalized=True)
        mid2 = edge2_lines.interpolate(0.5, normalized=True)
        _, near_2 = nearest_points(mid1, edge2)
        near_1, _ = nearest_points(edge1, mid2)
        if not Point(edge2_lines.coords[0]).equals(near_2) and \
            not Point(edge2_lines.coords[-1]).equals(near_2) and \
            not Point(edge1_lines.coords[0]).equals(near_1) and \
            not Point(edge1_lines.coords[-1]).equals(near_1):
            if mid1.distance(near_2) > mid2.distance(near_1):
                mid1 = near_1
            elif mid1.distance(near_2) < mid2.distance(near_1):
                mid2 = near_2

        mid_line = LineString([mid1, mid2])
        if mid_line.length > cfg.link_parcel_symbol_distance:
            continue

        # poly1 and poly2 must be directly adjacent: reject when the link between
        # them crosses another parcel part (a polygon sitting in the gap).
        if any(obstacle.intersection(mid_line).length > 0.01 for obstacle in obstacles):
            continue

        (x1, y1), (x2, y2) = mid_line.coords
        dx = x2 - x1
        dy = y2 - y1
        line_length = math.hypot(dx, dy)

        # Normalize direction vector
        ux = dx / line_length
        uy = dy / line_length
        
        # New points extended at both ends
        new_start = (x1 - ux * symbol_size, y1 - uy * symbol_size)
        new_end = (x2 + ux * symbol_size, y2 + uy * symbol_size)

        start_segment = LineString([new_start, (x1, y1)])
        end_segment = LineString([new_end, (x2, y2)])
        start_poly = symbol_polygon(start_segment, symbol_size)
        end_poly = symbol_polygon(end_segment, symbol_size)

        # if not poly1.buffer(symbol_size * 0.8).covers(start_poly) or not poly2.buffer(symbol_size * 0.8).covers(end_poly):
        #     continue

        inter_area = poly1.intersection(start_poly).area + poly2.intersection(end_poly).area
        # Get angles
        angle1 = get_angle_from_vertex(start_segment, 1) - 90
        angle2 = get_angle_from_vertex(end_segment, 0) + 90

        candidate_pt1 = Point(x1, y1)
        candidate_pt2 = Point(x2, y2)

        # Remember the best candidate even if it fails the polygon-inner rule,
        # so a forced placement can still fall back to it.
        if inter_area > force_max_area:
            force_max_area = inter_area
            force_vals = (candidate_pt1, angle1, candidate_pt2, angle2)

        # New condition:
        # symbol 1 must fully exist inside poly1
        # symbol 2 must fully exist inside poly2
        if not poly1.buffer(0.1).contains(start_poly):
            continue
        if not poly2.buffer(0.1).contains(end_poly):
            continue

        if inter_area > max_area:
            max_area = inter_area
            pt1 = candidate_pt1
            ang1 = angle1
            pt2 = candidate_pt2
            ang2 = angle2

    return pt1, ang1, pt2, ang2

def symbol_line_from_point_angle(
    point: Point,
    alkis_angle,
    height=2.0,
    side_x=1.05,
    side_y=0.95
):
    """
    Create real parcel-part symbol line using ALKIS angle.

    Original full-size local symbol was:
        LINESTRING (0 0, 0 10, 5.25 4.75)

    Now reduced by half:
        LINESTRING (0 0, 0 5, 2.625 2.375)
    """

    symbol = LineString([
        (0, 0),
        (0, height),
        (side_x, side_y)
    ])

    # Local symbol points upward, so base direction is 90 degrees.
    symbol = rotate(
        symbol,
        alkis_angle - 90,
        origin=(0, 0),
        use_radians=False
    )

    symbol = translate(
        symbol,
        xoff=point.x,
        yoff=point.y
    )

    return symbol

def _db_symbol_intersects(conn, symbol_line) -> bool:
    """Return True if the buffered symbol line overlaps any existing label."""
    query = f"""
        SELECT COUNT(*)
        FROM by_bbox_symbols_{run_id}
        WHERE ST_Intersects(geom, ST_Buffer(ST_GeomFromText(%s, 25832), 0.5))
    """
    result = db_crud.run_query(conn, query, (symbol_line.buffer(0.5).wkt, ))
    return result[0][0] != 0

def _snap_to_boundary(poly, point) -> Point:
    """Snap a point onto the polygon boundary edge."""
    nearest, _ = nearest_points(poly.exterior, point)
    return nearest

def check_overlap_and_update_bias(conn, poly1, poly2, pt1, angle1, pt2, angle2,
                                  max_bias=2.0, step=0.25):
    """
    The two symbols are a pair linked by a long connecting line (pt1 -> pt2).
    When the symbols overlap an existing label, slide the whole pair sideways by
    the same offset so the connecting line stays a single straight line, then
    re-check.

    Constraints kept on every candidate:
        - both points stay on their polygon boundary edge (poly1 / poly2)
        - both symbol lines stay inside their polygon inner area
        - the pair moves together, so pt1 and pt2 remain on one line

    Returns the (possibly biased) pt1, angle1, pt2, angle2, or all None.
    """
    # Slide direction: perpendicular to the connecting line, so that shifting both
    # points by the same vector keeps pt1 and pt2 on one straight line.
    dx = pt2.x - pt1.x
    dy = pt2.y - pt1.y
    length = math.hypot(dx, dy)
    if length == 0:
        return None, None, None, None

    tx = -dy / length
    ty = dx / length

    # Try the original position first, then alternate outward in small steps so
    # the smallest possible bias is preferred.
    offsets = [0.0]
    d = step
    while d <= max_bias:
        offsets.append(d)
        offsets.append(-d)
        d += step

    for off in offsets:
        # Shift the whole pair by the same vector to keep the link straight.
        cand1 = _snap_to_boundary(poly1, Point(pt1.x + tx * off, pt1.y + ty * off))
        cand2 = _snap_to_boundary(poly2, Point(pt2.x + tx * off, pt2.y + ty * off))

        line1 = symbol_line_from_point_angle(cand1, angle1 + 90)
        line2 = symbol_line_from_point_angle(cand2, angle2 + 90)

        # Both symbol lines must stay inside their polygon inner area.
        if not poly1.buffer(0.1).contains(line1):
            continue
        if not poly2.buffer(0.1).contains(line2):
            continue

        # Neither may overlap any existing label.
        if _db_symbol_intersects(conn, line1):
            continue
        if _db_symbol_intersects(conn, line2):
            continue

        return cand1, angle1, cand2, angle2

    return None, None, None, None



def process_parcel_part_range(data: List[Any], counter, run_id, db_cadastral_param) -> None:
    cfg = load_config(run_id)
    
    conn = db_crud.create_connection(db_cadastral_param)
    render_symbol_size = 10

    for item in data:
        id, geom = item
        ######################### testing
        # if id != '09728600005402000000':
        #     continue
        ######################### testing

        query = f"""
            SELECT ST_AsText(p.geom)
            FROM by_bbox_parcel_{run_id} p
            WHERE p.id = %s
        """
        free_wkt = db_crud.run_query(conn, query, (id, ))
        
        if free_wkt is None or len(free_wkt) < 1 or free_wkt[0][0] is None:
            continue # Empty space

        geom = wkt.loads(geom)
        free_geom = wkt.loads(free_wkt[0][0])
        free_geom = util.make_multipolygon(free_geom)

        neighbor_dict, ordered_ids = geoms_to_unique_neighbor_dict(free_geom.geoms)

        part_count = len(free_geom.geoms)
        # Parts (by index) that received at least one symbol.
        drawn = set()

        def draw_pair(idx1, idx2):
            poly1 = free_geom.geoms[idx1]
            poly2 = free_geom.geoms[idx2]
            # Other parcel parts that must not sit in the gap between the pair.
            obstacles = [free_geom.geoms[k] for k in range(part_count) if k != idx1 and k != idx2]
            pt1, angle1, pt2, angle2 = get_symbol_info(poly1, poly2, obstacles)
            if pt1 is None or pt2 is None:
                return False

            # check any overlapping with other labels
            b1, ba1, b2, ba2 = check_overlap_and_update_bias(conn, poly1, poly2, pt1, angle1, pt2, angle2)
            if b1 is not None and b2 is not None:
                pt1, angle1, pt2, angle2 = b1, ba1, b2, ba2
            else:
                # Could not avoid overlap and we are not forcing: skip.
                return False

            insert_symbol(conn, id, cfg.symbol_name_parcel_part, pt1, render_symbol_size, angle1)
            insert_symbol(conn, id, cfg.symbol_name_parcel_part, pt2, render_symbol_size, angle2)
            drawn.add(idx1)
            drawn.add(idx2)
            return True

        def draw_pair_with_nearest(idx1, idx2):
            poly1 = free_geom.geoms[idx1]
            poly2 = free_geom.geoms[idx2]

            dist = poly1.distance(poly2)
            pt = poly1.buffer(dist + 1).intersection(poly2).centroid
            pt2, _ = nearest_points(poly2.boundary, pt)
            pt1, _ = nearest_points(poly1.boundary, pt2)
            
            mid_line = LineString([pt1, pt2])
            if mid_line.length > cfg.link_parcel_symbol_distance:
                return False

            angle1 = get_angle_from_vertex(mid_line, 0) + 90
            angle2 = get_angle_from_vertex(mid_line, 1) + 90
            
            b1, ba1, b2, ba2 = check_overlap_and_update_bias(conn, poly1, poly2, pt1, angle1, pt2, angle2)
            if b1 is not None and b2 is not None:
                pt1, angle1, pt2, angle2 = b1, ba1, b2, ba2
            else:
                # Could not avoid overlap and we are not forcing: skip.
                return False

            insert_symbol(conn, id, cfg.symbol_name_parcel_part, pt1, render_symbol_size, angle1)
            insert_symbol(conn, id, cfg.symbol_name_parcel_part, pt2, render_symbol_size, angle2)
            
        # Record all neighbor pairs and draw their symbols.
        for i, near_list in neighbor_dict.items():
            for j in near_list:
                is_add = draw_pair(i, j)
                if not is_add:
                    # Find nearest point using buffer
                    draw_pair_with_nearest(i, j)


    db_crud.close_connection(conn)

def parcel_part_symbols(is_production):
    print("==============Parcel part symbols")
    start_time = time.time()
    
    conn = db_crud.create_connection(db_cadastral_param)
    
    # Empty labels table with parcel labeling
    query = f"DELETE FROM by_bbox_symbols_{run_id} WHERE symbol_name = 'parcel_part_top.png'"
    db_crud.run_query(conn, query)

    # Get parcel info including free space geometry
    query = f"""
        SELECT 
            id, ST_AsText(geom)
        FROM by_bbox_parcel_{run_id} p
        WHERE ST_GeometryType(geom) = 'ST_MultiPolygon'
            AND ST_NumGeometries(geom) > 1
    """
    data = db_crud.run_query(conn, query)
    print(f"{len(data)} records Processing started")
    
    manager = Manager()
    processed_counter = manager.dict()
    processed_counter['processed'] = 0
    processed_counter['missed'] = 0
    processed_counter['start_time'] = start_time

    # Disable multi processing, because it needs one by one - it checks free space for every polygon
    if is_production and cfg.cpu_cores > 1:
        chunk_list = util.split_into_chunks(data, cfg.cpu_cores)
        
        # close()+join() instead of the context manager: Pool.__exit__ calls terminate(),
        # whose _help_stuff_finish() grabs inqueue._rlock while an idle worker still holds
        # it inside SimpleQueue.get(), which can block the shutdown forever.
        pool = Pool(min(cfg.cpu_cores, len(chunk_list)))
        try:
            pool.starmap(process_parcel_part_range, [(chunk, processed_counter, run_id, db_cadastral_param) for chunk in chunk_list])
            pool.close()
        except BaseException:
            pool.terminate()
            raise
        finally:
            pool.join()
    else:
        process_parcel_part_range(data, processed_counter, run_id, db_cadastral_param)

    print(f"Completed Parcel part symbols, Processed: {processed_counter['processed']}, Total Elapsed: {time.time() - start_time:.2f} sec")
    
    db_crud.close_connection(conn)
    

def main(is_production):
    print("==============Parcel symbols")

    start_time = time.time()

    parcel_part_symbols(is_production)
    
    print(f"Completed, Total Elapsed: {time.time() - start_time:.2f} sec")

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: python parcel_symbols.py <id>")
        print('Example command format: "python parcel_symbols.py by_101125"')
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