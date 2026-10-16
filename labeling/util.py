from PIL import ImageFont, ImageDraw, Image
import math
from shapely import length
from shapely.geometry import Polygon, LineString, MultiPolygon, Point, LinearRing, MultiLineString, GeometryCollection
from shapely.affinity import rotate, translate
from shapely.errors import GEOSException
from typing import List, Any
from shapely.ops import nearest_points
import os
import sys
import numpy as np

os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from read_settings import load_config

cfg = load_config()

# Split data into n_chunks round-robin (chunk i gets items i, i+n, i+2n, ...),
# so all workers progress through the data in index order and the load is
# balanced even when data is sorted (e.g. by area)
def split_into_chunks(data: List[Any], n_chunks: int) -> List[List[Any]]:
    return [data[i::n_chunks] for i in range(n_chunks)]

def make_multilinestring(geom):
    if geom.geom_type == "LineString":
        return MultiLineString([geom])
    return geom

def make_multipolygon(geom):
    if geom.geom_type == "Polygon":
        return MultiPolygon([geom])
    return geom

def get_max_area_polygon(geom):
    """
    Return the largest Polygon from a Polygon or MultiPolygon.
    """
    if isinstance(geom, Polygon):
        return geom
    if isinstance(geom, MultiPolygon):
        return max(geom.geoms, key=lambda p: p.area)
    raise TypeError("geom must be Polygon or MultiPolygon")
    
# Return angle (in degrees) between vector p1→p2 and p2→p3
def angle_between(p1, p2, p3):
    v1 = (p1[0] - p2[0], p1[1] - p2[1])
    v2 = (p3[0] - p2[0], p3[1] - p2[1])

    mag1 = math.hypot(*v1)
    mag2 = math.hypot(*v2)
    if mag1 == 0 or mag2 == 0:
        return 0

    dot = v1[0]*v2[0] + v1[1]*v2[1]
    angle_rad = math.acos(max(-1.0, min(1.0, dot / (mag1 * mag2))))
    return math.degrees(angle_rad)

# Sort by polygon area
def get_sorted_polygons(multi: MultiPolygon) -> list[Polygon]:
    if isinstance(multi, Polygon):
        return [multi]
    elif isinstance(multi, MultiPolygon):
        return sorted(multi.geoms, key=lambda p: p.area, reverse=True)
    else:
        raise TypeError("Input must be a Polygon or MultiPolygon")
    
# Helper to convert mm to pixel
def mm_to_pixels(mm):
    return (mm / 25.4) * cfg.dpi

def mm_to_meters(mm, scale=1000.0, px_per_mm=40.0):
    inches = (mm / px_per_mm) / 25.4  # effective inches
    pixels = inches * cfg.dpi
    meters = (pixels * scale) / cfg.dpi
    return meters

# return split line segment in street center line
def get_split_segment_length(scale):
    return cfg.scale_street_line_segment[scale]

def get_free_parcel_area_threshold(scale):
    return mm_to_meters(cfg.free_parcel_area_threshold[scale])

def get_symbol_size(name, scale):
    if name == "parcel_part":
        return cfg.symbol_size_parcel_part[scale]

# return arrow length, unit is px
def get_arrow_length_parcel(scale):
    return cfg.scale_arrow_length_map.get(scale, 2)

# Sort by long edge
def get_sorted_edges(polygon: Polygon):
    coords = list(polygon.exterior.coords)
    edges = [LineString([coords[i], coords[i + 1]]) for i in range(len(coords) - 1)]
    return sorted(edges, key=lambda e: -abs(e.length))

# Helper to convert pixel to meter
def pixels_to_meters(pixels, scale):
    mm_per_pixel = 25.4 / cfg.dpi
    meters_per_pixel = (mm_per_pixel * scale) / 1000  # convert mm to meters
    return pixels * meters_per_pixel

# Get label render size
# category: parcel, building
def get_label_width_height(label_text, font_size, category, italic = False, bold = False):
    if label_text is None or len(label_text.strip()) == 0:
        return (0, 0)
    
    font_path = cfg.font_path
    if italic and bold:
        font_path = cfg.font_bold_italic_path
    elif italic:
        font_path = cfg.font_italic_path
    elif bold:
        font_path = cfg.font_bold_path
    
    font = ImageFont.truetype(font_path, font_size)
    
    # Create an image to render the text
    img = Image.new("RGBA", (10, 10), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    
    # Height still from bbox
    bbox = draw.textbbox((0, 0), label_text, font=font)
    width_px = bbox[2] - bbox[0]
    height_px = bbox[3] - bbox[1]
    
    width = pixels_to_meters(width_px, cfg.scale)
    height = pixels_to_meters(height_px, cfg.scale)
        
    if category == 'building':
        if font_size == cfg.font_size_building[0]:
            width += 0.5
            height += 0.5
        elif font_size == 6:
            width += 0.1
            height += 0.1
    elif category == 'parcel':
        if font_size == cfg.font_size_parcel[0]:
            width += 0.5 * (len(label_text) - 1 if '/' in label_text else len(label_text))
            height += 1
        elif font_size == cfg.font_size_parcel[1]:
            width += 0.3 * (len(label_text) - 1 if '/' in label_text else len(label_text))
            height += 0.5
        else:
            width += 0.2 * (len(label_text) - 1 if '/' in label_text else len(label_text))
            height += 0.2
    elif category == 'street':
        if font_size == cfg.font_size_street[0]:
            width += 0.3 * len(label_text)
            height += 0.7
            if len(label_text) > 15:
                width += 0.1 * len(label_text)
            
            if has_descender(label_text):
                height *= 1.21
        elif font_size == cfg.font_size_street[1]:
            width += 0.2 * len(label_text)
            height += 0.4
            if len(label_text) > 15:
                width += 0.1 * len(label_text)
            
            if has_descender(label_text):
                height *= 1.21
        elif font_size == cfg.font_size_street[2]:
            width += 0.2 * len(label_text)
            height += 0.4
            
            if has_descender(label_text):
                height *= 1.21
        elif font_size == cfg.font_size_street[3]:
            width += 0.13 * len(label_text)
            height += 0.1
            
            if has_descender(label_text):
                height *= 1.21

    return (width, height)

def has_descender(str: str) -> bool:
    return any(ch in str for ch in "gjpqy")

def move_poly_up(poly: Polygon, angle: float, offset: float) -> Polygon:
    rad = math.radians(angle)
    
    # "Up" direction relative to rotated text/polygon
    dx = -math.sin(rad) * offset
    dy =  math.cos(rad) * offset

    return translate(poly, xoff=dx, yoff=dy)


def safe_polygon(coords):
    # Ensure it's closed
    if coords[0] != coords[-1]:
        coords.append(coords[0])

    try:
        return Polygon(coords)
    except GEOSException as e:
        print("Invalid Polygon:", coords)
        raise e
    
# Helper to create label rectangle at a point
def make_label_polygon(center_x, center_y, width_m, height_m, rotation, padding = 0):

    padded_width = width_m + padding
    padded_height = height_m + padding / 2
    bl = (center_x - padded_width / 2, center_y + padded_height / 2)
    br = (center_x + padded_width / 2, center_y + padded_height / 2)
    tr = (center_x + padded_width / 2, center_y - padded_height / 2)
    tl = (center_x - padded_width / 2, center_y - padded_height / 2)
    rect = safe_polygon([tl, tr, br, bl])

    if rotation != 0:
        rect = rotate(rect, rotation, origin='center', use_radians=False)
    return rect

# Get label polygon by center position
def get_label_polygon_from_center(label_x, label_y, label_text, font_size, category, rotation, italic, bold, padding = 0):
    width_m, height_m = get_label_width_height(label_text, font_size, category, italic, bold)
    label_polygon = make_label_polygon(label_x, label_y, width_m, height_m, rotation, padding)

    return label_polygon

# Convert radian angle to geoserver(sld) angle
# radian angle:  0->right, 90->bottom, 180->left, 270->top
# geoserver sld: 0->right, 90->top, 180->left, 270->bottom
def convert_to_geoserver_rotation(angle):
    if isinstance(angle, str):
        angle = int(float(angle))

    sld_angle = (360 - angle) % 360
    
    return sld_angle


# Create a Polygon representing the rendered label footprint.
# Returns Polygon
def text_to_polygon(name, font_size, category, padding = 0.5, is_street_name = False):
    width, height = get_label_width_height(name, font_size, category, not is_street_name, is_street_name)
    padded_width = width + padding
    padded_height = height + padding

    label_poly = Polygon([
        (0, 0), (padded_width, 0),
        (padded_width, padded_height), (0, padded_height)
    ])

    return label_poly

def translate_rotate_polygon(poly: Polygon, xoff: float, yoff: float, rotation: float) -> Polygon:
    minx, miny, maxx, maxy = poly.bounds
    width = maxx - minx
    height = maxy - miny
    result_poly = translate(poly, xoff=xoff - width / 2, yoff=yoff - height / 2)
    
    result_poly = rotate(result_poly, rotation)

    return result_poly

# Shorten leader line by moving start point toward end point with bias distance
def shorten_leader_line_from_start(leader_line: LineString, bias: float = 0) -> LineString:
    """
    Move the start point of a leader line towards the end point by bias distance.
    
    Args:
        leader_line: The LineString to shorten
        bias: Distance to move the start point towards the end point
    
    Returns:
        New LineString with start point moved
    """
    if leader_line.length <= bias:
        return leader_line
    
    # Interpolate a new start point at distance 'bias' from the start
    start_pt = Point(leader_line.coords[0])
    new_end_pt = leader_line.interpolate(1 - bias, normalized=True)
    
    return LineString([start_pt, new_end_pt])

def angle_list_around(base_angle, step=5, count=10):
    result = [base_angle]
    n = step

    while len(result) < count:
        result.append(base_angle + n)
        if len(result) < count:
            result.append(base_angle - n)
        n += step

    return result

# Rigid-rotate a point around a fixed pivot by delta_angle_deg (no boundary snapping)
def rotate_point_around_fixed(point, pivot, delta_angle_deg):
    theta = math.radians(delta_angle_deg)
    cos_t = math.cos(theta)
    sin_t = math.sin(theta)

    vx = point.x - pivot.x
    vy = point.y - pivot.y

    new_vx = vx * cos_t - vy * sin_t
    new_vy = vx * sin_t + vy * cos_t

    return Point(pivot.x + new_vx, pivot.y + new_vy)

def make_short_leader_line(start_pt, end_pt):
    leader_line = LineString([start_pt, end_pt])
        
    # Make shorter from start_pt - to avoid leader line is intersecting with label polygon
    leader_line = shorten_leader_line_from_start(leader_line, bias=0.2)

    return leader_line

# Generate leader line
# start_point is parcel side, end_point is label side
def generate_leader_line(free_geom, geom_parcel, vertex_info, placed_label, scale, angle_deg):
    leader_line = None

    # find nearest polygon in geom_parcel (if multipolygon) from placed_label
    if isinstance(geom_parcel, MultiPolygon):
        geom_parcel =  min(
            geom_parcel.geoms,
            key=lambda polygon: placed_label.distance(polygon)
        )

    start_pt, end_pt = nearest_points(geom_parcel, placed_label)

    if geom_parcel.area < cfg.parcel_min_area:
        leader_line = LineString([start_pt, end_pt])
    elif start_pt is not None and end_pt is not None:
        leader_line = make_short_leader_line(start_pt, end_pt)

        # get another leader line if it intersects with vertex
        if leader_line.intersects(vertex_info):
            buffer_zone = placed_label.buffer(placed_label.distance(geom_parcel) + 1)

            # parcel boundary edges that fall within buffer_zone
            boundary_in_zone = geom_parcel.boundary.intersection(buffer_zone)

            candidate_pts = []
            if boundary_in_zone.geom_type == "LineString":
                candidate_pts = [Point(c) for c in boundary_in_zone.coords]
            elif boundary_in_zone.geom_type == "MultiLineString":
                for line in boundary_in_zone.geoms:
                    candidate_pts.extend(Point(c) for c in line.coords)
            elif boundary_in_zone.geom_type == "GeometryCollection":
                for g in boundary_in_zone.geoms:
                    if g.geom_type == "LineString":
                        candidate_pts.extend(Point(c) for c in g.coords)
                    elif g.geom_type == "Point":
                        candidate_pts.append(g)
            elif boundary_in_zone.geom_type == "Point":
                candidate_pts = [boundary_in_zone]

            if not candidate_pts:
                candidate_pts = [Point(c) for c in geom_parcel.exterior.coords[:-1]]

            # farthest point on the intersected boundary from vertex_info
            far_vertex = max(candidate_pts, key=lambda p: vertex_info.distance(p))

            nearest_on_label, _ = nearest_points(placed_label, far_vertex)
            candidate_line = make_short_leader_line(far_vertex, nearest_on_label)

            if not candidate_line.intersects(vertex_info):
                return candidate_line, placed_label.centroid

            # arrow line perpendicular ("vertical") to the boundary edge that far_vertex lies on,
            # picking whichever perpendicular direction points most toward the label
            ext_coords = list(geom_parcel.exterior.coords)
            edge_a, edge_b = Point(ext_coords[0]), Point(ext_coords[1])
            for a, b in zip(ext_coords[:-1], ext_coords[1:]):
                if LineString([a, b]).distance(far_vertex) < 1e-6:
                    edge_a, edge_b = Point(a), Point(b)
                    break

            to_label_x = nearest_on_label.x - far_vertex.x
            to_label_y = nearest_on_label.y - far_vertex.y

            best_dir = None
            best_score = -math.inf
            ex = edge_b.x - edge_a.x
            ey = edge_b.y - edge_a.y
            edge_len = math.hypot(ex, ey)
            if edge_len > 0:
                ex, ey = ex / edge_len, ey / edge_len
                for perp in ((-ey, ex), (ey, -ex)):
                    score = perp[0] * to_label_x + perp[1] * to_label_y
                    if score > best_score:
                        best_score, best_dir = score, perp

            if best_dir is not None and math.hypot(to_label_x, to_label_y) > 0:
                perp_end = Point(far_vertex.x + best_dir[0] * cfg.min_arrow_length,
                                  far_vertex.y + best_dir[1] * cfg.min_arrow_length)
                perp_line = LineString([far_vertex, perp_end])

                gap = perp_line.distance(placed_label)
                if gap > 1:
                    move_amount = gap - 1
                    moved_label = translate(placed_label, xoff=best_dir[0] * move_amount, yoff=best_dir[1] * move_amount)
                    if free_geom.contains(moved_label):
                        placed_label = moved_label

                if not perp_line.intersects(vertex_info):
                    return perp_line, placed_label.centroid

                # still intersects: rotate the label-side point around the fixed far_vertex
                nearest_on_label2, _ = nearest_points(placed_label, far_vertex)
                for delta_angle in angle_list_around(0, 3, 10):
                    rotated_pt = rotate_point_around_fixed(nearest_on_label2, far_vertex, delta_angle)
                    rotated_line = LineString([far_vertex, rotated_pt])
                    if not rotated_line.intersects(vertex_info):
                        return rotated_line, placed_label.centroid
            return None, None
    
    moved_placed_label = placed_label
    if leader_line is not None:
        if leader_line.length > get_arrow_length_parcel(scale):
            # pull placed_label toward parcel to reach max_arrow_length
            excess = leader_line.length - get_arrow_length_parcel(scale) - 1
            s = leader_line.coords[0]
            e = leader_line.coords[1]
            seg_len = leader_line.length
            if seg_len > 0:
                ux = (s[0] - e[0]) / seg_len
                uy = (s[1] - e[1]) / seg_len
                moved_placed_label = translate(placed_label, xoff=ux * excess, yoff=uy * excess)
                start_pt, end_pt = nearest_points(geom_parcel, moved_placed_label)
                leader_line = LineString([start_pt, end_pt])
            if leader_line.length > get_arrow_length_parcel(scale):
                return None, None
        
        if leader_line.length < cfg.min_arrow_length:
            # push placed_label away from parcel to reach min_arrow_length
            deficit = cfg.min_arrow_length - leader_line.length + 1
            s = leader_line.coords[0]
            e = leader_line.coords[1]
            seg_len = leader_line.length
            if seg_len > 0:
                ux = (e[0] - s[0]) / seg_len
                uy = (e[1] - s[1]) / seg_len
                moved_placed_label = translate(placed_label, xoff=ux * deficit, yoff=uy * deficit)
                if free_geom.contains(moved_placed_label):
                    start_pt, end_pt = nearest_points(geom_parcel, moved_placed_label)
                    leader_line = make_short_leader_line(start_pt, end_pt)
                    placed_label = moved_placed_label
                else:
                    # get nearest point on moved placed label
                    _, end_pt = nearest_points(geom_parcel, moved_placed_label)
                    # get nearest point on placed label
                    end_pt, _ = nearest_points(placed_label.boundary, end_pt)
                    
                    new_leader_line = make_short_leader_line(start_pt, end_pt)

                    if placed_label.intersects(new_leader_line):
                        offset_list = [0, 0.5, -0.5, 1, -1]
                        is_break = False
                        for x_off in offset_list:
                            for y_off in offset_list:
                                moved_placed_label = translate(placed_label, x_off, y_off)
                                if not moved_placed_label.intersects(new_leader_line):
                                    leader_line = new_leader_line
                                    placed_label = moved_placed_label
                                    is_break = True
                                    break
                            if is_break:
                                break
                    else:
                        leader_line = new_leader_line
                        
        if leader_line.length < cfg.min_arrow_length:
            return None, None

        # move start_point towards polygon centroid via lerp
        start_pt = leader_line.coords[0]
        end_pt = leader_line.coords[1]

        cx = end_pt[0] - start_pt[0]
        cy = end_pt[1] - start_pt[1]
        length = math.hypot(cx, cy)
        
        if length == 0:
            return None, None

        threshold = 0.5
        threshold_list = [threshold * 2, threshold]

        for t in threshold_list:
            new_start = Point(start_pt[0] - cx * t / length, start_pt[1] - cy * t / length)
            if geom_parcel.contains(new_start) and not new_start.buffer(0.5).intersects(vertex_info):
                leader_line = LineString([new_start, end_pt])

                if leader_line.length <= get_arrow_length_parcel(scale) or leader_line.length < cfg.min_arrow_length:
                    if not geom_parcel.contains(placed_label):
                        nearest_pt, _ = nearest_points(placed_label.boundary, Point(end_pt))
                        leader_line = make_short_leader_line(Point(start_pt), nearest_pt)
                    break
        
    return leader_line, placed_label.centroid

def shift_line_toward_last_point(line: LineString, distance: float) -> LineString:
    coords = list(line.coords)
    if len(coords) != 2:
        raise ValueError("LineString must have exactly 2 points")

    (x1, y1), (x2, y2) = coords

    dx = x2 - x1
    dy = y2 - y1

    length = math.hypot(dx, dy)
    if length == 0:
        raise ValueError("Zero length line")

    # Unit direction vector (from first → last)
    ux = dx / length
    uy = dy / length

    # Shift forward (toward last point side)
    shift_x = ux * distance
    shift_y = uy * distance

    new_p1 = (x1 + shift_x, y1 + shift_y)
    new_p2 = (x2 + shift_x, y2 + shift_y)

    return LineString([new_p1, new_p2])

def rotate_linestring_90(line: LineString, clockwise: bool = False) -> LineString:
    coords = list(line.coords)
    if len(coords) != 2:
        raise ValueError("LineString must have exactly 2 points")

    (x1, y1), (x2, y2) = coords  # P2 is pivot (fixed)

    # Vector from pivot (P2) to P1
    dx = x1 - x2
    dy = y1 - y2

    # Rotate vector 90°
    if clockwise:
        new_dx = dy
        new_dy = -dx
    else:
        new_dx = -dy
        new_dy = dx

    # Translate back to pivot
    new_p1 = (x2 + new_dx, y2 + new_dy)

    return LineString([new_p1, (x2, y2)])

def parse_production_flag(args, default = True):
    value = default

    if len(args) > 2:
        arg = args[2].strip()
        if arg in ("0", "false", "debug", "False"):
            value = False
        else:
            value = True
    
    return value

def normalize_angle(angle_deg: float) -> float:
    while angle_deg > 90:
        angle_deg -= 180
    while angle_deg < -90:
        angle_deg += 180
    return angle_deg

def pca_angle(poly: Polygon) -> float:
    coords = np.array(poly.exterior.coords[:-1])
    centered = coords - coords.mean(axis=0)
    cov = np.cov(centered.T)
    vals, vecs = np.linalg.eigh(cov)
    direction = vecs[:, np.argmax(vals)]
    angle = math.degrees(math.atan2(direction[1], direction[0]))
    return normalize_angle(angle)

def pca_line_angle(line: LineString) -> float:
    coords = np.array(line.coords)
    if len(coords) < 2:
        raise ValueError("LineString must have at least 2 points")

    centered = coords - coords.mean(axis=0)
    cov = np.cov(centered.T)
    vals, vecs = np.linalg.eigh(cov)
    direction = vecs[:, np.argmax(vals)]
    angle = math.degrees(math.atan2(direction[1], direction[0]))
    return normalize_angle(angle)

def _get_main_polygon(geom):
    """Return the largest polygon from Polygon/MultiPolygon."""
    if isinstance(geom, Polygon):
        return geom
    if isinstance(geom, MultiPolygon):
        return max(geom.geoms, key=lambda g: g.area)
    raise TypeError(f"Unsupported geometry type: {geom.geom_type}")

def pca_angle_geom(geom) -> float:
    if isinstance(geom, MultiPolygon):
        return pca_angle(_get_main_polygon(geom))
    elif isinstance(geom, Polygon):
        return pca_angle(geom)
    elif isinstance(geom, LineString):
        return pca_line_angle(geom)
    elif isinstance(geom, MultiLineString):
        return pca_line_angle(geom.geoms[0])
    
    return 0

def get_linear_length(geometry):
    """
    Return the total length of all line components in a geometry.

    Polygon components use their boundary length.
    """
    if geometry is None or geometry.is_empty:
        return 0.0

    if isinstance(geometry, (LineString, MultiLineString)):
        return geometry.length

    if isinstance(geometry, (Polygon, MultiPolygon)):
        return geometry.boundary.length

    if isinstance(geometry, GeometryCollection):
        return sum(get_linear_length(part) for part in geometry.geoms)

    return 0.0