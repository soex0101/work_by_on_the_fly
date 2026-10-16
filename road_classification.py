import sys
import time
import requests
from shapely.geometry import MultiLineString

import common
from read_settings import load_config
from labeling import db_crud

# WFS requests that failed on every proxy
failed_requests = []

# Refer https://www.baysis.bayern.de/internet/strasseninformationen/index.html
# to get details about road classification value
# BAB: Federal motorway
# B: Federal highway
# St: State roads
# K: County road

url_prefix = (
    "https://gisportal-stmb.bayern.de/server/services/WFS/BAYSIS_Strassennetz/MapServer/WFSServer"
    "?SERVICE=WFS&version=2.0.0"
    "&request=GetFeature&typeNames=BAYSIS_Strassennetz:strassennetz&srsName=EPSG:25832"
    "&outputFormat=GEOJSON&bbox={0},{1},{2},{3},EPSG:25832"
)

def wfs_get_feature(bbox):
    url = url_prefix.format(*bbox)
    print(f"Request URL: {url}")

    # 3 attempts per proxy, then move on to the next one.
    # With no proxies configured, requests are made directly.
    proxy_list = common.load_proxies(cfg) or [None]
    max_attempts = 3
    last_error = None
    for proxy_index, proxy_line in enumerate(proxy_list):
        proxies = None
        if proxy_line is not None:
            proxy_url = common.requests_proxy_url(proxy_line)
            proxies = {"http": proxy_url, "https": proxy_url}

        for attempt in range(1, max_attempts + 1):
            try:
                # Make the GET request
                response = requests.get(url, proxies=proxies, timeout=30)

                # Raise an error if the request was unsuccessful (e.g., 404 or 500)
                response.raise_for_status()

                # Access the response data
                print(f"Status Code: {response.status_code}")
                print(f"Response Text Length: {len(response.text)}")      # Decoded string
                # print(f"JSON Data: {response.json()}")      # Use if response is JSON format

                return response.json()
            except Exception as err:
                last_error = err
                print(f"WFS request failed, proxy {proxy_index + 1}/{len(proxy_list)}, attempt {attempt}/{max_attempts}: {err}")
                time.sleep(1)

    failed_requests.append(common.request_error(last_error, url))
    return None

def update_road_classification(db_param, road_classification_list):
    conn = db_crud.create_connection(db_param)

    query_lines = ""
    for idx, road in enumerate(road_classification_list):
        wkt = road["geom"].wkt
        query_lines += f"({idx}, ST_GeomFromText('{wkt}', 25832)),"
    
    if len(query_lines) > 0:
        query_lines = query_lines[:-1]

    query = f"""
        WITH input_lines AS (
            SELECT * FROM (VALUES 
                {query_lines}
            ) AS t(line_id, geom)
        ),
        intersections AS (
            SELECT 
                t.id AS parcel_id,
                il.line_id,
                ST_Length(ST_Intersection(t.geom, il.geom)) AS intersect_len
            FROM by_bbox_parcel_{run_id} t
            JOIN input_lines il ON ST_Intersects(t.geom, il.geom)
            WHERE is_street = true
        )
        SELECT DISTINCT ON (parcel_id)
            parcel_id,
            line_id AS max_linestring_index,
            intersect_len
        FROM intersections
        ORDER BY parcel_id, intersect_len DESC
    """

    result = db_crud.run_query(conn, query)
    for row in result:
        parcel_id, line_idx, line_len = row
        query = f"""
            UPDATE by_bbox_parcel_{run_id}
            SET road_classification = %s
            WHERE id = %s
        """
        db_crud.run_query(conn, query, (road_classification_list[line_idx - 1]["classification"], parcel_id))

    db_crud.close_connection(conn)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python road_classification.py <id>")
        print('Example command format: "python road_classification.py 20251113_050000"')
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

    result = wfs_get_feature(cfg.bbox)
    common.exit_if_failed_requests("road_classification.py", failed_requests, run_id)
    road_classification_list = []
    if result is not None:
        for feature in result['features']:
            # Get coordinates (inside geometry)
            coords = feature['geometry']['coordinates']
            
            # Get Straßenklasse (inside properties)
            s_klasse = feature['properties']['Straßenklasse']
            
            multi_line = MultiLineString(coords)
            road_classification_list.append({
                "classification": s_klasse,
                "geom": multi_line
            })
            
    if len(road_classification_list) > 0:
        update_road_classification(db_cadastral_param, road_classification_list)

    print(f"Completed, Processed: {len(road_classification_list)} roads")