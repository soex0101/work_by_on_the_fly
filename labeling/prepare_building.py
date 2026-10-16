################################################
# Get building+street relation in python code
################################################

import time
import sys
import os
import re
import requests
from shapely import wkt
from shapely.geometry import LineString, box, Point, MultiPolygon, Polygon, LineString, MultiLineString
from shapely.ops import nearest_points
import math
from collections import Counter
from pyproj import Transformer

import db_crud

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import common
import util
from read_settings import load_config

cfg = None
run_id = ""
db_cadastral_param = None

# How far (meters) the label->container ray is extended past the container
# before checking whether it reaches the street.
RAY_EXTENSION_M = 1000

def strip_html_tags(text: str) -> str:
    """Return plain text from a WMS label, e.g.
    "<b>Apprichstraße</b> <b>10</b> <b>86199</b> <b>Augsburg</b>, Göggingen"
    -> "Apprichstraße 10 86199 Augsburg, Göggingen".
    """
    if text is None:
        return ""
    return re.sub(r"<[^>]+>", "", text).strip()

def parse_german_address(label: str):
    """Parse a German address into (street_name, house_number, postal_code).

    Handles both the WMS label format
    "Apprichstraße 6 86199 Augsburg, Göggingen" and the stored
    "Apprichstraße 6, 86199 Augsburg" format. Commas are treated as
    separators. Returns None if no 5-digit postal code pattern is found.
    """
    text = strip_html_tags(label).replace(",", " ")
    text = re.sub(r"\s+", " ", text).strip()
    m = re.match(
        r"^(?P<street>.+?)\s+"
        r"(?P<house>\d+\s*[a-zA-Z]?(?:\s*[-/]\s*\d+\s*[a-zA-Z]?)?)\s+"
        r"(?P<plz>\d{5})\b",
        text,
    )
    if not m:
        return None
    return (
        m.group("street").strip(),
        re.sub(r"\s+", "", m.group("house")),
        m.group("plz"),
    )

def angle_between_3_points(p1: Point, p2: Point, p3: Point) -> float:
    """
    Returns the angle in degrees at p2.

    p1, p2, p3 are Shapely Point objects.
    """

    # Vectors from p2
    v1 = (p1.x - p2.x, p1.y - p2.y)
    v2 = (p3.x - p2.x, p3.y - p2.y)

    dot = v1[0] * v2[0] + v1[1] * v2[1]

    len_v1 = math.hypot(v1[0], v1[1])
    len_v2 = math.hypot(v2[0], v2[1])

    if len_v1 == 0 or len_v2 == 0:
        # raise ValueError("Two points are the same, angle cannot be calculated")
        return 0

    cos_angle = dot / (len_v1 * len_v2)

    # Avoid floating-point precision errors
    cos_angle = max(-1, min(1, cos_angle))

    angle_rad = math.acos(cos_angle)
    angle_deg = math.degrees(angle_rad)

    return angle_deg

def extend_line(start: Point, end: Point, extension: float) -> LineString:
    """Build a line from `start` to `end`, then extend it beyond `end` by
    `extension` meters, keeping the same direction.
    """
    dx = end.x - start.x
    dy = end.y - start.y
    length = math.hypot(dx, dy)
    if length == 0:
        return LineString([(start.x, start.y), (end.x, end.y)])

    ux, uy = dx / length, dy / length
    extended_end = (end.x + ux * extension, end.y + uy * extension)
    return LineString([(start.x, start.y), extended_end])

def check_reasonable_street_parcel(conn, street_id, street_geom, container_geom):
    # - check more nearer streets than street_id existing, if distance is over 100m
    #   if near streets over 3, skip it
    # - many edge lengths are intersects with another building, skip it.
    #   to avoid one street takes several street name
    distance = street_geom.distance(container_geom)
    if distance > 50:
        query = f"""
            SELECT COUNT(*)
            FROM by_bbox_parcel_{run_id}
            WHERE is_street = true AND ST_Distance(geom, ST_GeomFromText(%s, 25832)) < %s
        """
        result = db_crud.run_query(conn, query, (container_geom.wkt, distance))
        if result[0][0] >= 3:
            return False
        
    query = f"""
        SELECT ST_AsText(geom), id
        FROM by_bbox_parcel_{run_id}
        WHERE is_street = true AND ST_Intersects(geom, ST_GeomFromText(%s, 25832)) AND id != %s
    """
    result = db_crud.run_query(conn, query, (street_geom.wkt, street_id))

    for street in result:
        geom_wkt, _ = street
        geom = wkt.loads(geom_wkt)
        intersection = street_geom.intersection(geom)
        inter_length = util.get_linear_length(intersection)
        # check near street exist
        if inter_length >= 50 and inter_length >= street_geom.boundary.length / 3 and street_geom.area < geom.area:
            return False
        
    return True


def main(is_production):
    start_time = time.time()
    print('----------------Preparing Building Labeling----------------')

    conn = db_crud.create_connection(db_cadastral_param)

    #############################################################
    # Relocate building address centers via Atlas Bayern search
    #############################################################
    if is_production and cfg.atlas_search_available:
        # Get all none-building labels (addresses whose center point is not
        # contained in any building). Same filter as building.py L728-742.
        query = f"""
            SELECT
                id,
                full_address
            FROM by_bbox_address_{run_id}
            WHERE full_address IS NOT NULL AND full_address != ''
        """
        building_address_list = db_crud.run_query(conn, query)
        print(f"{len(building_address_list)} building addresses to relocate")

        search_url = "https://services.atlas.bayern.de/search/type/location/searchText"
        proxy_list = common.load_proxies(cfg)
        # Proxies are tried round-robin, starting from the last one that worked.
        # With no proxies configured, requests are made directly.
        proxy_start_index = 0
        # Address searches that failed on every proxy
        failed_requests = []

        for address in building_address_list:
            address_id, full_address = address
            if full_address is None or full_address.strip() == "":
                continue

            stripped_address = full_address.strip()
            match = re.search(r",\s*\d{5}\s+[^,\s]+", stripped_address)
            if match:
                search_address = stripped_address[:match.end()]
            else:
                search_address = stripped_address

            # Start from the proxy that succeeded last time; 3 attempts per
            # proxy, then move on to the next one, wrapping around the list.
            result = None
            last_error = None
            max_attempts = 3
            for offset in range(len(proxy_list)):
                proxy_index = (proxy_start_index + offset) % len(proxy_list)
                proxy_line = proxy_list[proxy_index]
                proxies = None
                if proxy_line is not None:
                    proxy_url = common.requests_proxy_url(proxy_line)
                    proxies = {"http": proxy_url, "https": proxy_url}

                for attempt in range(1, max_attempts + 1):
                    try:
                        response = requests.post(
                            search_url,
                            json={"term": search_address},
                            headers={"Content-Type": "application/json"},
                            proxies=proxies,
                            timeout=30,
                        )
                        response.raise_for_status()
                        result = response.json()
                        break
                    except Exception as e:
                        last_error = e
                        print(f"Search failed for {address_id} ({search_address}), proxy {proxy_index + 1}/{len(proxy_list)}, attempt {attempt}/{max_attempts}: {e}")
                        time.sleep(1)

                if result is not None:
                    proxy_start_index = proxy_index
                    break

            if result is None:
                if last_error is not None:
                    failed = common.request_error(last_error, search_url)
                    failed['message'] = 'term="{0}": {1}'.format(search_address, failed['message'])
                    failed_requests.append(failed)
                continue

            if not result:
                print(f"No result found for {address_id} ({search_address})")
                continue

            # Parse the queried address and keep only results whose label
            # matches on street name, house number and postal code.
            target = parse_german_address(full_address)
            if target is None:
                print(f"Could not parse address {address_id} ({search_address})")
                continue

            matches = [
                r for r in result
                if isinstance(r, dict)
                and "attrs" in r
                and "coordinate" in r["attrs"]
                and parse_german_address(r["attrs"].get("label")) == target
            ]
            if len(matches) != 1:
                print(f"Expected 1 matching result, got {len(matches)} for {address_id} ({search_address})")
                continue

            coordinate = matches[0]["attrs"]["coordinate"]
            if not coordinate or len(coordinate) < 2:
                print(f"Invalid coordinate for {address_id} ({search_address}): {coordinate}")
                continue

            # coordinate is [x, y] in EPSG:3857; transform to EPSG:25832 via PostGIS.
            # Move house_center_point_geom to the new point and translate
            # house_bbox_geom by the same delta so it stays centered on it.
            x_3857, y_3857 = coordinate[0], coordinate[1]
            query = f"""
                WITH new_pt AS (
                    SELECT 
                        ST_SetSRID(ST_MakePoint(%s, %s), 25832) AS geom
                )
                UPDATE by_bbox_address_{run_id} a
                SET house_center_point_geom = new_pt.geom,
                    house_bbox_geom = ST_Translate(
                        a.house_bbox_geom,
                        ST_X(new_pt.geom) - ST_X(a.house_center_point_geom),
                        ST_Y(new_pt.geom) - ST_Y(a.house_center_point_geom)
                    ),
                    is_geom_updated = true
                FROM new_pt
                WHERE a.id = %s
            """

            transformer = Transformer.from_crs("EPSG:3857", "EPSG:25832", always_xy=True)
            x_25832, y_25832 = transformer.transform(x_3857, y_3857)

            db_crud.run_update_query(conn, query, (x_25832, y_25832, address_id))
            print(f"Relocated {address_id} ({search_address}) -> 25832({x_25832}, {y_25832})")

        common.exit_if_failed_requests("labeling/prepare_building.py", failed_requests, run_id)

    #############################################################
    # Update building color from by_bbox_building_color
    #############################################################
    if is_production:
        # Set default building color as null
        query = f"UPDATE by_bbox_building_{run_id} SET color=null"
        db_crud.run_update_query(conn, query)
        
        query = f"""
            WITH best AS (
                SELECT
                    b.id AS building_id,
                    c.color AS new_color
                FROM by_bbox_building_{run_id} b
                JOIN LATERAL (
                    SELECT
                        c.color,
                        ST_Area(ST_Intersection(b.geom, c.geom))
                        / NULLIF(
                            ST_Area(ST_Union(b.geom, c.geom)),
                            0
                        ) AS iou
                    FROM by_bbox_building_color_{run_id} c
                    WHERE b.geom IS NOT NULL
                        AND c.geom IS NOT NULL
                        AND ST_Intersects(b.geom, c.geom)
                    ORDER BY iou DESC
                    LIMIT 1
                ) c ON TRUE
            )
            UPDATE by_bbox_building_{run_id} b
            SET color = best.new_color
            FROM best
            WHERE b.id = best.building_id;
        """
        db_crud.run_update_query(conn, query)

    #############################################################
    # Initialize/Set building+street relations, Find missed street
    #############################################################
    query = f"DELETE FROM by_bbox_building_labels_{run_id}"
    db_crud.run_query(conn, query)

    # Delete parcel street info generated from address
    query = f"DELETE FROM by_bbox_parcel_street_{run_id} WHERE street_name_source LIKE '%address-%'"
    db_crud.run_query(conn, query)

    query = f"""
        SELECT
            a.id,
            street_name,
            house_number,
            ST_AsText(house_center_point_geom),
            ST_AsText(house_bbox_geom),
            ST_AsText(street_center_point_geom),
            ST_AsText(street_bbox_geom),
            p.id,
            ST_AsText(p.geom),
            b.id,
			ST_AsText(b.geom)
        FROM by_bbox_address_{run_id} a
        LEFT JOIN by_bbox_parcel_{run_id} p ON ST_Contains(p.geom, a.house_center_point_geom)
		LEFT JOIN by_bbox_building_{run_id} b ON ST_Contains(b.geom, a.house_center_point_geom)
        WHERE p.id IS NOT NULL AND NOT ST_IsEmpty(p.geom)
        ORDER BY street_name
    """
    address_list = db_crud.run_query(conn, query)
    
    street_name_list = {}
    building_relation_list = []
    all_address_list = [address[0] for address in address_list]
    for address in address_list:
        address_id, street_name, house_number, house_center_point_geom_wkt, house_bbox_geom_wkt, \
            street_center_point_geom_wkt, street_bbox_geom_wkt, \
            parcel_id, parcel_geom_wkt, \
            building_id, building_geom_wkt = address
        
        ###############testing
        # if address_id != 'DEBYvAAAAACA2J6a':
        #     continue
        ###############testing
        
        label_pos = wkt.loads(house_center_point_geom_wkt)
        parcel_geom = wkt.loads(parcel_geom_wkt)
        building_geom = wkt.loads(building_geom_wkt)

        container_geom = building_geom if building_id is not None else parcel_geom

        # Skip if label position is in center side of container geom (there will be another label)
        if not label_pos.buffer(5).intersects(container_geom.boundary):
            continue

        # Check correspond street name and polygon exist
        query = f"""
            SELECT 
                p.id, ST_AsText(p.geom)
            FROM by_bbox_parcel_street_{run_id} s
            LEFT JOIN by_bbox_parcel_{run_id} p
                ON s.parcel_id = p.id
            WHERE s.street_name = %s
        """
        street_list = db_crud.run_query(conn, query, (street_name, ))
        candidate_street_list = []

        for street in street_list:
            street_id, street_geom_wkt = street
            if street_id is None:
                continue
            street_geom = wkt.loads(street_geom_wkt)

            nearest_pt, _ = nearest_points(street_geom, container_geom)
            street_angle = angle_between_3_points(container_geom.centroid, label_pos, nearest_pt)
            
            # Cast a ray from the label position toward the container, extend it
            # past the container, and check whether it reaches the street.
            nearest_pt_on_container, _ = nearest_points(container_geom.boundary, label_pos)
            ray_line = extend_line(label_pos, nearest_pt_on_container, RAY_EXTENSION_M)
            ray_intersects_street = ray_line.intersects(street_geom)

            if ray_intersects_street and street_angle >= 20:
                candidate_street_list.append({
                    "street_id": street_id,
                    "street_angle": street_angle,
                    "dist": street_geom.distance(container_geom)
                })
                continue

            # from the container geom centroid toward label position, extend it
            ray_line = extend_line(container_geom.centroid, label_pos, RAY_EXTENSION_M)
            ray_intersects_street = ray_line.intersects(street_geom)

            if ray_intersects_street and street_angle >= 20:
                candidate_street_list.append({
                    "street_id": street_id,
                    "street_angle": street_angle,
                    "dist": street_geom.distance(container_geom)
                })
                continue

        if len(candidate_street_list) > 0:
            # insert building+street relation
            candidate_street_list.sort(key=lambda item: item["dist"])
            street_info = candidate_street_list[0]

            query = f"""
                INSERT INTO by_bbox_building_labels_{run_id}
                (building_id, street_parcel_id, house_number, position, street_bbox_geom)
                VALUES
                (%s, %s, %s, ST_GeomFromText(%s, 25832), ST_GeomFromText(%s, 25832))
            """
            db_crud.run_query(conn, query, (building_id, street_info["street_id"], house_number, house_center_point_geom_wkt, street_bbox_geom_wkt))
            building_relation_list.append(address_id)
        else:
            # find missed street and set street name
            query = f"""
                SELECT
                    id, ST_AsText(geom)
                FROM by_bbox_parcel_{run_id} p
                WHERE is_street = true AND NOT EXISTS (
                    SELECT 1
                    FROM by_bbox_parcel_street_{run_id} s
                    WHERE s.parcel_id = p.id
                )
            """
            street_list = db_crud.run_query(conn, query)

            candidate_street_list = []
            for street in street_list:
                street_id, street_geom_wkt = street
                street_geom = wkt.loads(street_geom_wkt)

                nearest_pt, _ = nearest_points(street_geom, container_geom)
                street_angle = angle_between_3_points(container_geom.centroid, label_pos, nearest_pt)

                # Cast a ray from the label position toward the container, extend it
                # past the container, and check whether it reaches the street.
                nearest_pt_on_container, _ = nearest_points(container_geom.boundary, label_pos)
                ray_line = extend_line(label_pos, nearest_pt_on_container, RAY_EXTENSION_M)
                ray_intersects_street = ray_line.intersects(street_geom)

                if ray_intersects_street and street_angle >= 20 and \
                    check_reasonable_street_parcel(conn, street_id, street_geom, container_geom):
                    candidate_street_list.append({
                        "street_id": street_id,
                        "street_name": street_name,
                        "street_angle": street_angle,
                        "ray_intersects_street": ray_intersects_street,
                        "address_id": address_id,
                        "building_id": building_id,
                        "house_number": house_number,
                        "position": house_center_point_geom_wkt,
                        "street_bbox_geom": street_bbox_geom_wkt,
                        "dist": street_geom.distance(container_geom)
                    })
            
            candidate_street_list.sort(key=lambda item: item["dist"])

            if len(candidate_street_list) > 0:
                street_info = candidate_street_list[0]

                if street_info["street_id"] not in street_name_list:
                    street_name_list[street_info["street_id"]] = []

                street_name_list[street_info["street_id"]].append(street_info)

    # Take out maximum used street name for each street id
    for street_id, info_list in street_name_list.items():
        name_counts = Counter(item["street_name"] for item in info_list)
        most_used_street_name, _ = name_counts.most_common(1)[0]

        address_id_list = list({item["address_id"] for item in info_list if item["address_id"] is not None})
        address_id_str = ",".join(str(address_id) for address_id in address_id_list)

        query = f"""
            INSERT INTO by_bbox_parcel_street_{run_id}
                (parcel_id, street_name, street_center_wms, street_name_source)
            SELECT
                p.id, %s, p.street_center, %s
            FROM by_bbox_parcel_{run_id} p
            WHERE p.id = %s
        """
        db_crud.run_query(conn, query, (most_used_street_name, 'address-' + address_id_str, street_id))

        for street_info in info_list:
            if street_info["building_id"] is not None:
                building_relation_list.append(street_info["address_id"])

                query = f"""
                    INSERT INTO by_bbox_building_labels_{run_id}
                    (building_id, street_parcel_id, house_number, position, street_bbox_geom)
                    VALUES
                    (%s, %s, %s, ST_GeomFromText(%s, 25832), ST_GeomFromText(%s, 25832))
                """
                db_crud.run_query(conn, query, (street_info["building_id"], street_id, street_info["house_number"],
                                                street_info["position"], street_info["street_bbox_geom"]))

    # add building street relation for missed address
    missed_address_id_list = list(set(all_address_list) - set(building_relation_list))
    missed_address_list = [address for address in address_list if address[0] in missed_address_id_list]
    for address in missed_address_list:
        address_id, street_name, house_number, house_center_point_geom_wkt, house_bbox_geom_wkt, \
            street_center_point_geom_wkt, street_bbox_geom_wkt, \
            parcel_id, parcel_geom_wkt, \
            building_id, building_geom_wkt = address

        # Check correspond street name and polygon exist
        query = f"""
            SELECT 
                p.id, ST_AsText(p.geom)
            FROM by_bbox_parcel_street_{run_id} s
            LEFT JOIN by_bbox_parcel_{run_id} p
                ON s.parcel_id = p.id
            WHERE s.street_name = %s
            ORDER BY ST_Distance(p.geom, ST_GeomFromText(%s, 25832)) ASC
        """
        street_list = db_crud.run_query(conn, query, (street_name, house_center_point_geom_wkt))

        street_info = [""]
        if len(street_list) > 0:
            street_info = street_list[0]
        else:
            query = f"""
                SELECT 
                    p.id
                FROM by_bbox_parcel_{run_id} p
                ORDER BY ST_Distance(p.geom, ST_GeomFromText(%s, 25832)) ASC
            """
            street_list = db_crud.run_query(conn, query, (house_center_point_geom_wkt, ))
            if len(street_list) > 0:
                street_info = street_list[0]

        query = f"""
            INSERT INTO by_bbox_building_labels_{run_id}
            (building_id, street_parcel_id, house_number, position, street_bbox_geom)
            VALUES
            (%s, %s, %s, ST_GeomFromText(%s, 25832), ST_GeomFromText(%s, 25832))
        """
        db_crud.run_query(conn, query, (building_id, street_info[0], house_number, house_center_point_geom_wkt, street_bbox_geom_wkt))

    print(f"Completed, Total Elapsed: {time.time() - start_time:.2f} sec")


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: python preare_building.py <id>")
        print('Example command format: "python preare_building.py by_101125"')
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