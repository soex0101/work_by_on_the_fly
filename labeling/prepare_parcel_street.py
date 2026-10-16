################################################
# Update parcel type_of_use and street name, centerline
################################################

from centerline.geometry import Centerline
from centerline import exceptions
from datetime import datetime
from shapely import wkt
from shapely.geometry import MultiPolygon, Polygon, MultiLineString, LineString, Point, box, shape, GeometryCollection
from shapely.ops import linemerge, unary_union, substring, nearest_points
from collections import defaultdict, deque, Counter
import time
import math
from math import hypot
import sys
import os
import requests
from typing import Optional
import networkx as nx

import db_crud
import util
import common

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from read_settings import load_config

cfg = None
db_cadastral_param = None
parcel_data = []
run_id = ""
# Street center WMS requests that failed on every proxy
failed_requests = []


# ============================================================
# Basic geometry helpers
# ============================================================

def force_multilinestring(geom):
    """
    Always return MultiLineString.
    Ignore non-line geometries.
    """
    if geom.is_empty:
        return MultiLineString([])

    if isinstance(geom, LineString):
        return MultiLineString([geom])

    if isinstance(geom, MultiLineString):
        return geom

    if isinstance(geom, GeometryCollection):
        lines = []
        for part in geom.geoms:
            if isinstance(part, LineString):
                lines.append(part)
            elif isinstance(part, MultiLineString):
                lines.extend(list(part.geoms))
        return MultiLineString(lines)

    return MultiLineString([])


def get_line_parts(geom):
    """
    Extract LineString parts from LineString / MultiLineString / GeometryCollection.
    """
    if geom.is_empty:
        return []

    if isinstance(geom, LineString):
        return [geom]

    if isinstance(geom, MultiLineString):
        return list(geom.geoms)

    if isinstance(geom, GeometryCollection):
        out = []
        for part in geom.geoms:
            out.extend(get_line_parts(part))
        return out

    return []


def safe_linemerge(geom):
    """
    linemerge cannot be called on a single LineString.
    """
    if geom.is_empty:
        return MultiLineString([])

    if isinstance(geom, LineString):
        return geom

    ml = force_multilinestring(geom)

    if len(ml.geoms) == 0:
        return ml

    if len(ml.geoms) == 1:
        return ml.geoms[0]

    return linemerge(ml)


def dist_xy(a, b):
    return hypot(a[0] - b[0], a[1] - b[1])


def sample_points(line, n=11):
    """
    Sample points along a line.
    """
    if line.length == 0:
        return [Point(line.coords[0])]

    return [
        line.interpolate(line.length * i / (n - 1))
        for i in range(n)
    ]


# ============================================================
# New requirement: keep only lines inside polygon
# ============================================================

def keep_lines_inside_polygon(lines, polygon, min_length=0.5):
    """
    Keep street center lines only inside geom polygon.

    If a line is fully inside/touching polygon:
        keep it.

    If a line partly crosses outside:
        clip it by polygon and keep only inside part.

    If a line is fully outside:
        remove it.

    polygon.covers(line) is used instead of polygon.contains(line),
    because valid skeleton lines may touch the polygon boundary.
    """
    inside_lines = []

    for line in lines:
        if line.is_empty or line.length < min_length:
            continue

        if polygon.covers(line):
            inside_lines.append(line)
            continue

        clipped = line.intersection(polygon)

        for part in get_line_parts(clipped):
            if not part.is_empty and part.length >= min_length:
                inside_lines.append(part)

    return inside_lines


# ============================================================
# Scoring rule: choose better street skeleton branch
# ============================================================

def center_score(line, polygon):
    """
    Higher score = better street skeleton.

    Prefer:
    1. Line points inside/touching polygon.
    2. Line farther from polygon boundary.
       This usually means more central inside the street polygon.
    """
    pts = sample_points(line, n=11)

    inside_ratio = sum(polygon.covers(p) for p in pts) / len(pts)
    avg_boundary_dist = sum(p.distance(polygon.boundary) for p in pts) / len(pts)

    return inside_ratio * 100000.0 + avg_boundary_dist


# ============================================================
# Endpoint clustering
# ============================================================

def cluster_endpoints(lines, tolerance=1.0):
    """
    Cluster endpoints that are almost the same coordinate.
    """
    centers = []
    coord_to_cluster = {}

    for line in lines:
        if line.is_empty or len(line.coords) < 2:
            continue

        for coord in (line.coords[0], line.coords[-1]):
            xy = (float(coord[0]), float(coord[1]))

            found = None

            for idx, center in enumerate(centers):
                if dist_xy(xy, center) <= tolerance:
                    found = idx
                    break

            if found is None:
                found = len(centers)
                centers.append(xy)
            else:
                cx, cy = centers[found]
                centers[found] = ((cx + xy[0]) / 2.0, (cy + xy[1]) / 2.0)

            coord_to_cluster[xy] = found

    return coord_to_cluster, centers


def endpoint_pair_key(line, coord_to_cluster):
    """
    Key for a line using clustered start/end points.
    Direction is ignored.
    """
    a = (float(line.coords[0][0]), float(line.coords[0][1]))
    b = (float(line.coords[-1][0]), float(line.coords[-1][1]))

    ca = coord_to_cluster[a]
    cb = coord_to_cluster[b]

    return tuple(sorted((ca, cb)))


# ============================================================
# Duplicate / bad-branch filters
# ============================================================

def remove_same_endpoint_duplicate_branches(
    lines,
    polygon,
    endpoint_tolerance=1.0,
):
    """
    Remove branches that connect the same snapped endpoint pair.

    Example:
    A -> B has two possible branches.
    Keep the branch more central inside the polygon.
    """
    if not lines:
        return []

    coord_to_cluster, _ = cluster_endpoints(lines, endpoint_tolerance)

    by_pair = defaultdict(list)

    for line in lines:
        if line.is_empty or len(line.coords) < 2:
            continue

        key = endpoint_pair_key(line, coord_to_cluster)
        by_pair[key].append(line)

    kept = []

    for group in by_pair.values():
        if len(group) == 1:
            kept.append(group[0])
        else:
            best = max(group, key=lambda ln: center_score(ln, polygon))
            kept.append(best)

    return kept


def is_near_parallel_duplicate(
    line_a,
    line_b,
    distance_tol=11.0,
    coverage_ratio=0.72,
):
    """
    True when one line mostly runs very close to another line.

    This catches near-duplicate branches even when their endpoints
    are not exactly the same.
    """
    if line_a.length == 0 or line_b.length == 0:
        return False

    # Compare shorter line against longer line.
    short, long = (
        (line_a, line_b)
        if line_a.length <= line_b.length
        else (line_b, line_a)
    )

    pts = sample_points(short, n=13)

    close_count = sum(
        p.distance(long) <= distance_tol
        for p in pts
    )

    close_ratio = close_count / len(pts)

    return close_ratio >= coverage_ratio


def remove_nearby_parallel_branches(
    lines,
    polygon,
    distance_tol=11.0,
):
    """
    Remove nearby/parallel duplicate branches.

    Important for case 2:
    branches can be near-duplicate even if endpoints are different.
    """
    kept = []

    # Longer first helps preserve main branches.
    for line in sorted(lines, key=lambda ln: ln.length, reverse=True):
        duplicate_idx = None

        for idx, existing in enumerate(kept):
            if is_near_parallel_duplicate(
                line,
                existing,
                distance_tol=distance_tol,
                coverage_ratio=0.72,
            ):
                duplicate_idx = idx
                break

        if duplicate_idx is None:
            kept.append(line)
        else:
            existing = kept[duplicate_idx]

            line_score = center_score(line, polygon)
            existing_score = center_score(existing, polygon)

            # Slightly prefer longer line if center score is close.
            line_score += line.length * 0.02
            existing_score += existing.length * 0.02

            if line_score > existing_score:
                kept[duplicate_idx] = line

    return kept


def remove_tiny_spurs(lines, min_length=3.0):
    """
    Remove tiny leftover segments.
    """
    return [
        line
        for line in lines
        if not line.is_empty and line.length >= min_length
    ]


def split_suspicious_long_jump(line):
    """
    Conservative fix for case 3.

    Sometimes one LineString contains two real branches connected by
    one abnormal long segment. This removes only that abnormal bridge.
    """
    coords = list(line.coords)

    if len(coords) < 4:
        return [line]

    seg_lengths = [
        dist_xy(coords[i], coords[i + 1])
        for i in range(len(coords) - 1)
    ]

    if len(seg_lengths) < 2:
        return [line]

    sorted_lengths = sorted(seg_lengths, reverse=True)
    longest = sorted_lengths[0]
    second = sorted_lengths[1]

    # Only split if one segment is clearly abnormal.
    if longest < 100 or longest < second * 1.6:
        return [line]

    jump_idx = seg_lengths.index(longest)

    left_coords = coords[: jump_idx + 1]
    right_coords = coords[jump_idx + 1 :]

    result = []

    if len(left_coords) >= 2:
        result.append(LineString(left_coords))

    if len(right_coords) >= 2:
        result.append(LineString(right_coords))

    return result if result else [line]


# ============================================================
# Main cleanup function for one street name
# ============================================================

def clean_one_street(
    lines,
    polygon,
    endpoint_tolerance=1.0,
    nearby_distance_tol=11.0,
    min_spur_length=3.0,
    inside_min_length=0.5,
):
    """
    Create one cleaned MultiLineString for one street name.

    Rules:
    1. Street line must exist inside geom polygon.
    2. Remove duplicate branches with same/similar endpoints.
    3. Remove nearby parallel duplicated branches.
    4. Keep the branch more central inside polygon.
    """
    raw_lines = []

    for line in lines:
        line = line.intersection(polygon)
        for part in get_line_parts(line):
            raw_lines.extend(split_suspicious_long_jump(part))

    if not raw_lines:
        return MultiLineString([])

    # Requirement:
    # Street center lines must exist inside geom inner area.
    raw_lines = keep_lines_inside_polygon(
        raw_lines,
        polygon,
        min_length=inside_min_length,
    )

    if not raw_lines:
        return MultiLineString([])

    # 1. Remove branches that connect the same snapped endpoint pair.
    lines = remove_same_endpoint_duplicate_branches(
        raw_lines,
        polygon,
        endpoint_tolerance=endpoint_tolerance,
    )

    # 2. Clip/check inside again after first filtering.
    lines = keep_lines_inside_polygon(
        lines,
        polygon,
        min_length=inside_min_length,
    )

    if not lines:
        return MultiLineString([])

    # 3. Remove nearby/parallel duplicates with different endpoints.
    lines = remove_nearby_parallel_branches(
        lines,
        polygon,
        distance_tol=nearby_distance_tol,
    )

    # 4. Clip/check inside again after duplicate filtering.
    lines = keep_lines_inside_polygon(
        lines,
        polygon,
        min_length=inside_min_length,
    )

    if not lines:
        return MultiLineString([])

    # 5. Remove tiny leftovers.
    lines = remove_tiny_spurs(
        lines,
        min_length=min_spur_length,
    )

    if not lines:
        return MultiLineString([])

    # 6. Merge connected parts where possible.
    merged = safe_linemerge(unary_union(lines))

    # 7. Final safety clipping after merge.
    final_lines = keep_lines_inside_polygon(
        get_line_parts(merged),
        polygon,
        min_length=inside_min_length,
    )

    if not final_lines:
        return MultiLineString([])

    merged = safe_linemerge(unary_union(final_lines))

    return force_multilinestring(merged)


def check_street_polygon(street_geom: Polygon, parcel_geom: Polygon, tolerance: float = 0.01) -> bool:
    if street_geom is None:
        return False
        
    expanded_street_geom = street_geom.buffer(tolerance)
    if expanded_street_geom.contains(parcel_geom):
        return True

    bbox = box(*cfg.bbox)
    street_inter = expanded_street_geom.intersection(bbox)
    parcel_inter = parcel_geom.intersection(bbox) 

    intersection_area = street_inter.intersection(parcel_inter)
    area = intersection_area.area
    
    if parcel_geom.area > 0 and area / parcel_geom.area >= 0.3:
        return True

    return False

def multilinestring_to_polylines(centerlines):
    """
    Convert LineString / MultiLineString to a list of polylines.
    Each polyline is a list of (x, y) tuples.
    """
    if isinstance(centerlines, LineString):
        return [list(centerlines.coords)]

    if isinstance(centerlines, MultiLineString):
        return [list(line.coords) for line in centerlines.geoms if len(line.coords) >= 2]

    raise TypeError("centerlines must be LineString or MultiLineString")


def build_graph(lines):
    """
    Build undirected weighted graph from all lines.
    """
    graph = defaultdict(list)

    for line in lines:
        for a, b in zip(line, line[1:]):
            w = math.dist(a, b)
            graph[a].append((b, w))
            graph[b].append((a, w))

    return graph


def connected_components(graph):
    """
    Return connected components as a list of node sets.
    """
    visited = set()
    components = []

    for node in graph:
        if node in visited:
            continue

        comp = set()
        q = deque([node])
        visited.add(node)

        while q:
            u = q.popleft()
            comp.add(u)
            for v, _ in graph[u]:
                if v not in visited:
                    visited.add(v)
                    q.append(v)

        components.append(comp)

    return components


def induced_subgraph(graph, nodes):
    """
    Build subgraph containing only given nodes.
    """
    sub = defaultdict(list)
    for u in nodes:
        for v, w in graph[u]:
            if v in nodes:
                sub[u].append((v, w))
    return sub


def farthest_node(start, graph):
    """
    For a tree-like component, find farthest node by cumulative edge length.
    Returns (node, distance, parent_map).
    """
    stack = [start]
    dist = {start: 0.0}
    parent = {start: None}

    while stack:
        u = stack.pop()
        for v, w in graph[u]:
            if v not in dist:
                dist[v] = dist[u] + w
                parent[v] = u
                stack.append(v)

    farthest = max(dist, key=dist.get)
    return farthest, dist[farthest], parent


def longest_path_in_component(subgraph):
    """
    Extract the weighted longest path from one connected component.
    Assumes the component is tree-like.
    """
    start = next(iter(subgraph))
    u, _, _ = farthest_node(start, subgraph)
    v, total_length, parent = farthest_node(u, subgraph)

    path = [v]
    while path[-1] != u:
        path.append(parent[path[-1]])
    path.reverse()

    return path, total_length


def extract_major_centerlines(multiline: MultiLineString):
    """
    Process a shapely MultiLineString with multiple disconnected groups.

    Parameters
    ----------
    multiline : shapely.geometry.MultiLineString
        Input multiline geometry.

    Returns
    -------
    cleaned_multiline : shapely.geometry.MultiLineString
        One longest centerline per connected component.
    lengths : list[float]
        Length of each extracted centerline.
    cleaned_lines : list[list[tuple[float, float]]]
        Coordinate lists for each extracted centerline.
    """
    if not isinstance(multiline, MultiLineString):
        multiline = util.make_multilinestring(multiline)

    lines = []
    for geom in multiline.geoms:
        coords = list(geom.coords)
        if len(coords) >= 2:
            lines.append([(float(x), float(y)) for x, y in coords])

    if not lines:
        raise ValueError("No valid linework found")

    graph = build_graph(lines)
    comps = connected_components(graph)

    cleaned_lines = []
    lengths = []

    for comp_nodes in comps:
        sub = induced_subgraph(graph, comp_nodes)
        path, length = longest_path_in_component(sub)
        cleaned_lines.append(path)
        lengths.append(length)

    cleaned_multiline = MultiLineString(cleaned_lines)
    return cleaned_multiline, lengths, cleaned_lines


def _point_line_distance(p, a, b):
    return Point(p).distance(LineString([a, b]))

def _is_removable(p1, p2, p3, angle_tol_deg=10.0, dist_tol=0.25):
    # remove if p2 is very close to direct line p1 -> p3
    if _point_line_distance(p2, p1, p3) <= dist_tol:
        return True

    # also remove if angle is near 180
    v1x = p1[0] - p2[0]
    v1y = p1[1] - p2[1]
    v2x = p3[0] - p2[0]
    v2y = p3[1] - p2[1]

    len1 = math.hypot(v1x, v1y)
    len2 = math.hypot(v2x, v2y)

    if len1 == 0 or len2 == 0:
        return True

    cos_angle = (v1x * v2x + v1y * v2y) / (len1 * len2)
    cos_angle = max(-1.0, min(1.0, cos_angle))

    angle = math.degrees(math.acos(cos_angle))

    return abs(angle - 180.0) <= angle_tol_deg


def simplify_linestring_vertices(line, angle_tol_deg=10.0, dist_tol=0.25, max_iter=20):
    coords = list(line.coords)

    if len(coords) <= 2:
        return line

    for _ in range(max_iter):
        changed = False
        new_coords = [coords[0]]

        for i in range(1, len(coords) - 1):
            p1 = new_coords[-1]
            p2 = coords[i]
            p3 = coords[i + 1]

            if _is_removable(p1, p2, p3, angle_tol_deg, dist_tol):
                changed = True
                continue

            new_coords.append(p2)

        new_coords.append(coords[-1])

        if not changed:
            break

        coords = new_coords

        if len(coords) <= 2:
            break

    return LineString(coords)


def simplify_multilinestring_vertices(geom, angle_tol_deg=10.0, dist_tol=0.25):
    if isinstance(geom, LineString):
        return simplify_linestring_vertices(geom, angle_tol_deg, dist_tol)

    if isinstance(geom, MultiLineString):
        return MultiLineString([
            simplify_linestring_vertices(line, angle_tol_deg, dist_tol)
            for line in geom.geoms
            if not line.is_empty
        ])

    return geom


def extract_main_center_line(geom):

    geom = util.make_multipolygon(geom)

    perim = geom.length
    interp = max(0.25, perim / 400.0)

    try:
        centerline = Centerline(geom, interpolation_distance=interp)
        centerlines = centerline.geometry   # LineString or MultiLineString

        cleaned_multiline, lengths, cleaned_lines = extract_major_centerlines(centerlines)
        cleaned_multiline = simplify_multilinestring_vertices(cleaned_multiline, angle_tol_deg=5.0, dist_tol=0.1)
        return cleaned_multiline
    except Exception as e:
        print("Centerline exception: ", e)
        return None

def points_every_threshold(multiline: MultiLineString, threshold: float):
    points = []
    if multiline is None:
        return points

    for line in multiline.geoms:
        sub_points = []
        d = threshold / 2
        while d <= line.length - threshold:
            sub_points.append(line.interpolate(d))
            d += threshold
        
        if len(sub_points) < 1:
            sub_points.append(line.interpolate(line.length / 2))
        else:
            sub_points.append(line.interpolate(line.length - threshold))
        
        points = points + sub_points

    return points

def _requests_proxy_url(proxy_line):
    """Build a 'http://user:pass@host:port' URL for the requests library.

    proxies.txt format is 'host:port:user:pass' (plain 'host:port' is also
    accepted). Mirrors 2_fetch_different_parcel.py.
    """
    parts = proxy_line.split(":")
    host = parts[0]
    port = parts[1]
    username = parts[2] if len(parts) > 2 else None
    password = parts[3] if len(parts) > 3 else None
    if username is not None:
        return f"http://{username}:{password}@{host}:{port}"
    return f"http://{host}:{port}"

def get_street_line(url, proxy_line, max_retries=3, retry_delay=1):
    proxy_url = _requests_proxy_url(proxy_line)

    for attempt in range(max_retries):
        try:
            print(f"Send WMS Request: {url}")
            print(f"           proxy: {proxy_line}")
            print(f"           attempt: {attempt + 1}/{max_retries}")
            proxies = {
                "http": proxy_url,
                "https": proxy_url,
            }

            response = requests.get(url, proxies=proxies, timeout=30)
            response.raise_for_status()

            data = response.json()

            lines = []
            street_name_list = []
            for feature in data["features"]:
                geom_json = feature["geometry"]
                geom = shape(geom_json)
                if geom_json["type"] == 'MultiLineString':
                    lines.extend(geom.geoms)
                elif geom_json["type"] == 'LineString':
                    lines.append(geom)

                if "properties" in feature and "name" in feature["properties"]:
                    street_name_list.append(feature["properties"]["name"])

            feature_count = len(data["features"])
            street_name = Counter(street_name_list).most_common(1)[0][0] if len(street_name_list) > 0 else ""
            print(f"Response feature count: {feature_count}, street_name: {street_name}")

            return MultiLineString(lines), street_name
        except Exception as e:
            print(f"WMS request attempt {attempt + 1} failed: {e}")
            if attempt < max_retries - 1:
                time.sleep(retry_delay)
            else:
                raise

# Send wms request to get street center name list (one polygon can takes multiple street names)
def get_street_center_from_wms(id, geom, mul: MultiLineString, line_threshold: float) -> Optional[MultiLineString]:
    print(f"================================{id}")
    pts = points_every_threshold(mul, line_threshold)

    bbox_size = 128.7841796875
    url_header = cfg.street_center_wms_url
    proxy_list = common.load_proxies()

    street_center_list = []
    street_name_list = []

    for idx, p in enumerate(pts):
        print(f"POINT ({p.x}  {p.y})")
        minx = p.x - bbox_size
        miny = p.y - bbox_size
        maxx = p.x + bbox_size
        maxy = p.y + bbox_size

        wms_url = f"{url_header}{minx},{miny},{maxx},{maxy}"

        geom_mul, street_name = None, None
        for proxy_idx, proxy_line in enumerate(proxy_list):
            try:
                geom_mul, street_name = get_street_line(wms_url, proxy_line, max_retries=3, retry_delay=1)
                print(f"Successfully got response from proxy {proxy_idx + 1}")
                break
            except Exception as e:
                print(f"Proxy {proxy_idx + 1} failed: {e}")
                if proxy_idx == len(proxy_list) - 1:
                    print(f"All {len(proxy_list)} proxies failed for point {idx}")
                    failed_requests.append(common.request_error(e, wms_url))

        if geom_mul is not None and not geom_mul.is_empty:
            street_center_list.append(geom_mul)
            street_name_list.append(street_name)
    
    # Remove street_center and street_name from list when there is only one street name with a different name
    if len([name for name in street_name_list if name]) > 1:
        street_name_counts = Counter(name for name in street_name_list if name)

        street_name_duplicates = [name for name, count in street_name_counts.items() if count > 1]
        if len(street_name_duplicates) > 0:
            filtered_centers = []
            filtered_names = []
            for center, name in zip(street_center_list, street_name_list):
                if name in street_name_duplicates:
                    filtered_centers.append(center)
                    filtered_names.append(name)
            street_center_list = filtered_centers
            street_name_list = filtered_names
        elif len(street_name_counts) > 3:
            # remove first and last street info
            street_center_list = street_center_list[1:-1]
            street_name_list = street_name_list[1:-1]

    grouped = defaultdict(list)
    
    can_skip_short = False
    for center_wkt, street_name in zip(street_center_list, street_name_list):
        grouped[street_name].append(center_wkt)
        if center_wkt.length > cfg.street_center_min_length:
            can_skip_short = True

    street_dict = {}

    endpoint_tolerance = 1.0
    nearby_distance_tol = 11.0
    min_spur_length = 3.0
    inside_min_length = 0.5

    for street_name, lines in grouped.items():
        multiline = clean_one_street(
            lines,
            geom,
            endpoint_tolerance=endpoint_tolerance,
            nearby_distance_tol=nearby_distance_tol,
            min_spur_length=min_spur_length,
            inside_min_length=inside_min_length,
        )

        if can_skip_short and multiline.length < cfg.street_center_min_length:
            continue
        street_dict[street_name] = multiline

    return street_dict


def remove_near_duplicate_parts(
    source_line,
    reference_line,
    near_duplicate_dist=1.5,
    min_keep_length=0.5,
):
    """
    Remove parts of source_line that are already close to reference_line.

    source_line      = geometry to partially keep, e.g. geom_list[0]
    reference_line   = trusted geometry, e.g. geom_list[1]
    near_duplicate_dist = distance tolerance in CRS units
    min_keep_length  = remove tiny fragments
    """

    duplicate_zone = reference_line.buffer(
        near_duplicate_dist,
        cap_style=2,   # flat cap
        join_style=2,  # mitre join
    )

    remaining = source_line.difference(duplicate_zone)

    kept = [
        part
        for part in get_line_parts(remaining)
        if part.length >= min_keep_length
    ]

    if not kept:
        return MultiLineString([])

    return unary_union(kept)


# ===============================================
# Keep base_line fully.
# Add only non-duplicate parts from add_line.
# ===============================================
def merge_without_near_duplicates(
    base_line,
    add_line,
    near_duplicate_dist=1.5,
    min_keep_length=0.5,
):

    add_remaining = remove_near_duplicate_parts(
        source_line=add_line,
        reference_line=base_line,
        near_duplicate_dist=near_duplicate_dist,
        min_keep_length=min_keep_length,
    )

    merged = unary_union([base_line, add_remaining])

    try:
        merged = linemerge(merged)
    except Exception:
        pass

    return force_multilinestring(merged), add_remaining


# =====================================
# Remove near by segments
# =====================================
def remove_nearby_duplicate_line_parts(
    multiline,
    main_index=0,
    near_dist=5.0,
    overlap_ratio_threshold=0.80,
):
    parts = get_line_parts(multiline)
    if not parts:
        return MultiLineString([]), []

    main = parts[main_index]
    main_buffer = main.buffer(
        near_dist,
        cap_style=2,
        join_style=2,
    )

    kept = []
    removed_info = []

    for i, part in enumerate(parts):
        if i == main_index:
            kept.append(part)
            continue

        inside_len = part.intersection(main_buffer).length
        overlap_ratio = inside_len / part.length if part.length > 0 else 0
        min_distance = part.distance(main)

        is_duplicate = (
            min_distance <= near_dist
            and overlap_ratio >= overlap_ratio_threshold
        )

        if is_duplicate:
            removed_info.append({
                "part_index": i,
                "length": part.length,
                "distance_to_main": min_distance,
                "overlap_ratio": overlap_ratio,
            })
        else:
            kept.append(part)

    cleaned = MultiLineString(kept)

    return cleaned, removed_info


# =====================================
# Bridge disconnected center line parts
# =====================================
def connect_lines_inside_polygon(multiline, polygon):
    """
    Connect disconnected center line parts.

    When the center line is a MultiLineString with more than one part, the
    parts are ordered by chaining the spatially nearest part. For each
    consecutive pair (previous/next line) the nearest points are linked with a
    straight connector. The connector is only kept when it lies inside the
    parcel polygon (geom). Connectors are merged back so the result has fewer,
    longer and more stable center lines.
    """
    parts = get_line_parts(multiline)
    if len(parts) <= 1:
        return force_multilinestring(multiline)

    # order parts so "previous" and "next" are spatially adjacent
    ordered = [parts[0]]
    remaining = parts[1:]
    while remaining:
        last = ordered[-1]
        nearest_idx = min(
            range(len(remaining)),
            key=lambda i: last.distance(remaining[i]),
        )
        ordered.append(remaining.pop(nearest_idx))

    connectors = []
    for prev_line, next_line in zip(ordered, ordered[1:]):
        p1, p2 = nearest_points(prev_line, next_line)
        if p1.equals(p2):
            continue
        connector = LineString([p1, p2])
        if polygon.covers(connector):
            connectors.append(connector)

    merged = unary_union(parts + connectors)
    try:
        merged = linemerge(merged)
    except Exception:
        pass

    return force_multilinestring(merged)


def process_parcel_range(parcel_list, counter, run_id, db_cadastral_param):
    conn = db_crud.create_connection(db_cadastral_param)

    for parcel_data in parcel_list:
        id, geometry, org_geometry = parcel_data
        #############testing#############
        # if id != "09068900000205000000":
        #    continue
        #################################
        geom = wkt.loads(geometry)
        org_geom = wkt.loads(org_geometry)
        
        is_street = False
        is_main_street = False
        street_name = ""
        center_geoms = None
        org_center_geoms = None

        # Step 1: Check it is street or parcel
        query = f"""
			WITH street AS (
                SELECT ST_Union(geom) AS geom
                FROM by_bbox_landkreis_{run_id}
                WHERE nutzart IN ('Weg', 'Straßenverkehr')
            ),
            parcel AS (
                SELECT geom
                FROM by_bbox_parcel_{run_id}
                WHERE id = %s
            )
            SELECT ST_Area(parcel.geom), ST_Area(street.geom), ST_Area(ST_Intersection(parcel.geom, street.geom)) AS geom
            FROM parcel, street
            WHERE ST_Intersects(parcel.geom, street.geom)
        """

        street_data = db_crud.run_query(conn, query, (id, ))
        if street_data is not None and len(street_data) > 0:
            parcel_area = street_data[0][0]
            street_area = street_data[0][1]
            intersection_area = street_data[0][2]

            if intersection_area >= parcel_area * 0.6 or intersection_area >= street_area * 0.8:
                is_street = True

                # Step 2: Get Street Type and Street Name
                query = f"""
                    WITH parcel AS (
                        SELECT geom
                        FROM by_bbox_parcel_{run_id}
                        WHERE id = %s
                    )
                    SELECT
                        r.id,
                        nutzart,
                        COALESCE(name, ''),
                        ST_Area(ST_Intersection(r.geom, p.geom)) AS intersection_area
                    FROM by_bbox_landkreis_{run_id} r
                    JOIN parcel p ON r.geom && p.geom AND ST_Intersects(r.geom, p.geom)
                    WHERE r.nutzart IN ('Weg', 'Straßenverkehr')
                    ORDER BY intersection_area DESC
                    LIMIT 1;
                """

                street_info = db_crud.run_query(conn, query, (id, ))
                if street_info is not None and len(street_info) > 0:
                    street_id = street_info[0][0]
                    nutzart = street_info[0][1]
                    street_name = street_info[0][2]
                    is_main_street = nutzart == 'Straßenverkehr' or (street_name is not None and street_name != '')

                # Step 3: Get center line
                center_geoms = extract_main_center_line(geom)
                org_center_geoms = extract_main_center_line(org_geom)
                db_street_name = street_name
                if center_geoms is not None:
                    if cfg.street_center_wms_available:
                        # set street name if wms value is valid
                        street_dict = get_street_center_from_wms(id, geom, org_center_geoms, cfg.street_center_wms_point_distance)
                        if len(street_dict) > 0:
                            # insert street name/geo info
                            for wms_street_name, street_center_geom in street_dict.items():
                                # if wms found a single street segment but couldn't resolve its name,
                                # fall back to the street_name looked up from the DB in Step 2
                                if len(street_dict) == 1 and not wms_street_name:
                                    street_name = db_street_name
                                else:
                                    street_name = wms_street_name

                                # merge street_center to street_center_wms if there are only one street name
                                # because sometimes, street_center_wms doesn't contain all center line
                                if len(street_dict) == 1 and center_geoms.length - street_center_geom.length > 150:
                                    street_center_geom, _ = merge_without_near_duplicates(center_geoms, street_center_geom)
                                    # get longest line index
                                    longest_idx, _ = max(
                                        enumerate(street_center_geom.geoms),
                                        key=lambda item: item[1].length
                                    )
                                    street_center_geom, _ = remove_nearby_duplicate_line_parts(street_center_geom, longest_idx, 10)

                                # bridge disconnected parts with connectors that
                                # stay inside the parcel, so the center line is
                                # simpler and more stable
                                street_center_geom = connect_lines_inside_polygon(street_center_geom, geom)

                                if not street_center_geom.is_empty:
                                    if street_name != "":
                                        insert_street_query = f"""
                                            INSERT INTO by_bbox_parcel_street_{run_id}
                                            (parcel_id, street_name, street_center_wms, street_name_source)
                                            VALUES
                                            (%s, %s, ST_GeomFromText(%s, 25832), 'wms')
                                        """
                                        db_crud.run_query(conn, insert_street_query, (id, street_name, street_center_geom.wkt))
                                    else:
                                        center_geoms = street_center_geom

                    elif street_name is not None:
                        insert_street_query = f"""
                            INSERT INTO by_bbox_parcel_street_{run_id}
                            (parcel_id, street_name, street_center_wms, street_name_source)
                            VALUES
                            (%s, %s, ST_GeomFromText(%s, 25832), 'manual')
                        """
                        db_crud.run_query(conn, insert_street_query, (id, street_name, center_geoms.wkt))
                        print(f"{id} center geom is set by manually")

        center_wkt = center_geoms.wkt if center_geoms is not None else None
        org_center_wkt = org_center_geoms.wkt if org_center_geoms is not None else None

        update_parcel_query = f"""
            UPDATE by_bbox_parcel_{run_id}
            SET 
                is_street = %s,
                street_center = ST_SetSRID(ST_GeomFromText(%s), 25832),
                is_main_street= %s,
                org_street_center = ST_SetSRID(ST_GeomFromText(%s), 25832)
            WHERE id = %s;
        """
        db_crud.run_query(conn, update_parcel_query, (is_street, center_wkt, is_main_street, org_center_wkt, id))
        
        counter['processed'] += 1

        if counter['processed'] % 50 == 0:
            now = time.time()
            print(f"Processed: {counter['processed']} / {len(parcel_list)}, Elapsed: {now - counter['start_time']:.2f} sec")

    db_crud.close_connection(conn)


def suppress_nearby_street_labels(conn, run_id, ratio):
    """
    Cluster street rows whose underlying parcels share more than `ratio` of the
    smaller perimeter along their boundary AND carry the same street_name. Within
    each connected component, keep only the row with the longest street_center_wms
    (parcel area breaks ties); mark the rest with is_street_name_suppressed=true so
    downstream labeling skips them.
    """
    start = time.time()

    db_crud.run_query(conn, f"""
        UPDATE by_bbox_parcel_street_{run_id}
        SET is_street_name_suppressed = false
    """)

    rows_query = f"""
        SELECT s.id,
               COALESCE(ST_Length(s.street_center_wms), 0) AS center_len,
               ST_Area(p.geom) AS area
        FROM by_bbox_parcel_street_{run_id} s
        JOIN by_bbox_parcel_{run_id} p ON p.id = s.parcel_id
        WHERE s.street_name IS NOT NULL AND s.street_name <> ''
    """
    rows = db_crud.run_query(conn, rows_query) or []
    if not rows:
        print("Nearby-street suppression: no candidate rows.")
        return

    row_attrs = {r[0]: (float(r[1] or 0), float(r[2] or 0)) for r in rows}

    edges_query = f"""
        WITH street_rows AS (
            SELECT s.id AS row_id,
                   s.parcel_id,
                   s.street_name,
                   p.geom AS parcel_geom,
                   ST_Perimeter(p.geom) AS perim
            FROM by_bbox_parcel_street_{run_id} s
            JOIN by_bbox_parcel_{run_id} p ON p.id = s.parcel_id
            WHERE s.street_name IS NOT NULL AND s.street_name <> ''
        )
        SELECT a.row_id, b.row_id,
               ST_Length(ST_Intersection(ST_Boundary(a.parcel_geom), ST_Boundary(b.parcel_geom))) AS shared_len,
               LEAST(a.perim, b.perim) AS min_perim
        FROM street_rows a
        JOIN street_rows b
          ON a.row_id < b.row_id
         AND a.street_name = b.street_name
         AND a.parcel_id <> b.parcel_id
         AND ST_Touches(a.parcel_geom, b.parcel_geom)
    """
    edges = db_crud.run_query(conn, edges_query) or []

    g = nx.Graph()
    g.add_nodes_from(row_attrs.keys())
    kept_edges = 0
    for id_a, id_b, shared, min_perim in edges:
        shared = float(shared or 0)
        min_perim = float(min_perim or 0)
        if min_perim <= 0:
            continue
        if shared / min_perim > ratio:
            g.add_edge(id_a, id_b)
            kept_edges += 1

    losers = []
    for component in nx.connected_components(g):
        if len(component) < 2:
            continue
        ranked = sorted(
            component,
            key=lambda rid: row_attrs[rid],  # (center_len, area), both larger = better
            reverse=True,
        )
        losers.extend(ranked[1:])

    if losers:
        loser_csv = ",".join(str(x) for x in losers)
        db_crud.run_query(conn, f"""
            UPDATE by_bbox_parcel_street_{run_id}
            SET is_street_name_suppressed = true
            WHERE id IN ({loser_csv})
        """)

    print(
        f"Nearby-street suppression: ratio={ratio}, edges_kept={kept_edges}, "
        f"suppressed_rows={len(losers)}, elapsed={time.time() - start:.2f}s"
    )

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python preare_parcel_street.py <id>")
        print('Example command format: "python preare_parcel_street.py by_101125"')
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

    start_time = time.time()
    print('----------------Checking if the parcel is street----------------')

    conn = db_crud.create_connection(db_cadastral_param)

    # Initialize is_street and street_name
    update_query = f"""
    UPDATE by_bbox_parcel_{run_id}
    SET 
        is_street = false,
        street_center = null;
    """
    db_crud.run_query(conn, update_query)

    delete_query = f"""
    DELETE FROM by_bbox_parcel_street_{run_id}
    """
    db_crud.run_query(conn, delete_query)

    # Get all parcel data to process
    query = f"SELECT id, ST_AsText(geom), ST_AsText(org_geom) FROM by_bbox_parcel_{run_id}"
    parcel_data = db_crud.run_query(conn, query)

    print("Table street property and street name field initialized")

    counter = {'processed': 0, 'start_time': start_time}
    process_parcel_range(parcel_data, counter, run_id, db_cadastral_param)

    processed_count = counter["processed"]
    print(f"Completed {processed_count}, Total Elapsed: {time.time() - start_time:.2f} sec")
    common.exit_if_failed_requests("labeling/prepare_parcel_street.py", failed_requests, run_id)

    # Find hidden streets (exist in tn, not exist in cadastral)
    start_time = time.time()
    query = f"""
        SELECT l.id, l.gml_id, l.name, l.nutzart, ST_AsText(l.geom)
        FROM by_bbox_landkreis_{run_id} l
        WHERE l.nutzart IN ('Straßenverkehr', 'Weg')
            AND COALESCE(
                (
                    SELECT ST_Area(
                        ST_UnaryUnion(
                            ST_Collect(
                                ST_Intersection(l.geom, p.org_geom)
                            )
                        )
                    )
                    FROM by_bbox_parcel_{run_id} p
                    WHERE p.is_street = true
                    AND l.geom && p.org_geom
                    AND ST_Intersects(l.geom, p.org_geom)
                ),
                0
            ) < 0.5 * ST_Area(
                ST_Intersection(
                    ST_MakeEnvelope(%s, %s, %s, %s,25832),
                    l.geom
                )
            );
    """

    hidden_street_list = db_crud.run_query(conn, query, cfg.bbox)
    for street_info in hidden_street_list:
        id, gml_id, street_name, nutzart, geom_wkt = street_info

        # find near boundary_id - it needs to generate parcel id
        query = f"""
            SELECT boundary_id
            FROM by_bbox_parcel_{run_id}
            WHERE ST_Intersects(geom, ST_GeomFromText(%s, 25832))
        """
        boundary_list = db_crud.run_query(conn, query, (geom_wkt, ))
        boundary_id = "000000"
        if len(boundary_list) > 0:
            boundary_id = boundary_list[0][0]

        parcel_id = boundary_id + str(id).zfill(14)
        org_geom = wkt.loads(geom_wkt)
        bbox = box(*cfg.bbox)
        geom = org_geom.intersection(bbox)

        # get street center, street center wms
        center_geoms = extract_main_center_line(geom)
        org_center_geoms = extract_main_center_line(org_geom)
        if center_geoms is not None:
            if cfg.street_center_wms_available:
                # set street name if wms value is valid
                street_dict = get_street_center_from_wms(id, geom, org_center_geoms, cfg.street_center_wms_point_distance)
                if len(street_dict) > 0:
                    # insert street name/geo info
                    for street_name, street_geom in street_dict.items():
                        # bridge disconnected parts with connectors that stay
                        # inside the parcel, so the center line is simpler and
                        # more stable
                        street_geom = connect_lines_inside_polygon(street_geom, geom)

                        if not street_geom.is_empty:
                            insert_street_query = f"""
                                INSERT INTO by_bbox_parcel_street_{run_id}
                                (parcel_id, street_name, street_center_wms, street_name_source)
                                VALUES
                                (%s, %s, ST_GeomFromText(%s, 25832), 'hidden-wms')
                            """
                            db_crud.run_query(conn, insert_street_query, (id, street_name, street_geom.wkt))

                    center_wkt = center_geoms.wkt if center_geoms is not None else None
                    org_center_wkt = org_center_geoms.wkt if org_center_geoms is not None else None

                    created_at = datetime.now()
                    is_main_street = nutzart == 'Straßenverkehr' or (street_name is not None and street_name != '')

                    print(f"================== {parcel_id}         *****************")
                    update_street_query = f"""
                        INSERT INTO by_bbox_parcel_{run_id}
                            (id, boundary_id, gml_id, geom, timestamp, 
                            is_street, street_center, org_geom, org_street_center, 
                            is_main_street, is_dashed)
                        VALUES
                            (%s, %s, %s, ST_GeomFromText(%s, 25832), %s, 
                            %s, ST_GeomFromText(%s, 25832), ST_GeomFromText(%s, 25832), ST_GeomFromText(%s, 25832), 
                            %s, %s);
                    """
                    db_crud.run_query(conn, update_street_query, 
                                    (parcel_id, boundary_id, gml_id, geom.wkt, created_at,
                                    True, center_wkt, org_geom.wkt, org_center_wkt,
                                    is_main_street, False))
            elif street_name is not None:
                insert_street_query = f"""
                    INSERT INTO by_bbox_parcel_street_{run_id}
                    (parcel_id, street_name, street_center_wms, street_name_source)
                    VALUES
                    (%s, %s, ST_GeomFromText(%s, 25832), 'hidden-wms')
                """
                db_crud.run_query(conn, insert_street_query, (id, street_name, center_geoms.wkt))


    print(f"Found and inserted hidden street count: {len(hidden_street_list)}, Total Elapsed: {time.time() - start_time:.2f} sec")
    common.exit_if_failed_requests("labeling/prepare_parcel_street.py", failed_requests, run_id)

    suppress_nearby_street_labels(conn, run_id, cfg.nearby_street_overlap_ratio)

    db_crud.close_connection(conn)

