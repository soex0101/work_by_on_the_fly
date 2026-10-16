'''
In cadastral DB, run following queries:
-------------------------------------
CREATE EXTENSION IF NOT EXISTS dblink;
-------------------------------------
'''
import sys
import os
import db_crud
import time

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from read_settings import load_config

if len(sys.argv) < 2:
    print("Usage: python preare.py <id>")
    print('Example command format: "python preare.py by_101125"')
    sys.exit(1)

run_id = sys.argv[1]
cfg = load_config(run_id)

start_time = time.time()
print('----------------Pre-processing----------------')

db_cadastral_param = {
    "dbname": cfg.dbname,
    "user": cfg.user,
    "password": cfg.password,
    "host": cfg.host,
    "port": cfg.port
}
db_tn_param = {
    "dbname": cfg.dbname_tn,
    "user": cfg.user,
    "password": cfg.password,
    "host": cfg.host,
    "port": cfg.port
}

db_building_param = {
    "dbname": cfg.dbname_building,
    "user": cfg.user,
    "password": cfg.password,
    "host": cfg.host,
    "port": cfg.port
}

db_address_param = {
    "dbname": cfg.dbname_address,
    "user": cfg.user,
    "password": cfg.password,
    "host": cfg.host,
    "port": cfg.port
}

db_vertex_param = {
    "dbname": cfg.dbname_vertex,
    "user": cfg.user,
    "password": cfg.password,
    "host": cfg.host,
    "port": cfg.port
}


connection_cadastral = db_crud.create_connection(db_cadastral_param)

############################
# Reference
# by_alkis_building2 => EPSG:25832
# cadastral => EPSG:25832
# tn => EPSG:25832
############################

########################################################
# Create by_bbox_parcel_
########################################################
db_crud.drop_table_if_exist(connection_cadastral, f"by_bbox_parcel_{run_id}")
db_crud.copy_table_with_bbox(connection_cadastral, f"by_parcel", f"by_bbox_parcel_{run_id}", cfg.bbox)

add_geometry_query = f"""
ALTER TABLE by_bbox_parcel_{run_id}
ADD COLUMN IF NOT EXISTS is_street boolean,
ADD COLUMN IF NOT EXISTS street_center geometry,
ADD COLUMN IF NOT EXISTS org_geom geometry,
ADD COLUMN IF NOT EXISTS org_street_center geometry,
ADD COLUMN IF NOT EXISTS is_main_street boolean,
ADD COLUMN IF NOT EXISTS is_dashed boolean,
ADD COLUMN IF NOT EXISTS solid_boundary geometry,
ADD COLUMN IF NOT EXISTS dashed_boundary geometry,
ADD COLUMN IF NOT EXISTS road_classification character varying(10);
"""
db_crud.run_query(connection_cadastral, add_geometry_query)
total_cnt = db_crud.get_table_row_count(connection_cadastral, f"by_bbox_parcel_{run_id}")

default_val_query = f"""
UPDATE by_bbox_parcel_{run_id} SET is_street = FALSE, is_main_street = FALSE, is_dashed = FALSE, org_geom = geom;
"""
db_crud.run_query(connection_cadastral, default_val_query)

# Update geom as intersection with bbox
update_query = f"""
    UPDATE by_bbox_parcel_{run_id}
    SET geom = ST_Intersection(
        geom,
        ST_MakeEnvelope(%s, %s, %s, %s, 25832)
    );
"""
db_crud.run_query(connection_cadastral, update_query, cfg.bbox)

print(f"by_bbox_parcel_{run_id} ({total_cnt} Rows)")

########################################################
# Create by_bbox_parcel_street_
########################################################
db_crud.drop_table_if_exist(connection_cadastral, f"by_bbox_parcel_street_{run_id}")

query = f"""
CREATE TABLE IF NOT EXISTS public.by_bbox_parcel_street_{run_id} (
    id SERIAL PRIMARY KEY,
    parcel_id character(20) NOT NULL,
    street_name character varying(200),
    street_center_wms geometry,
    is_street_name_suppressed boolean NOT NULL DEFAULT false,
    street_name_source TEXT
);
"""
db_crud.run_query(connection_cadastral, query)

print(f"Created by_bbox_parcel_street_{run_id}")

########################################################
# Copy by_bbox_landkreis from tn db
########################################################
# create parcel_tn_table in cadatastral
db_crud.drop_table_if_exist(connection_cadastral, f"by_bbox_landkreis_{run_id}")
query = f"""
CREATE TABLE IF NOT EXISTS public.by_bbox_landkreis_{run_id} (
    id SERIAL PRIMARY KEY,
    gml_id character varying,
    name character varying,
    nutzart character varying,
    bez character varying,
    color character varying,
    geom geometry
);
"""
db_crud.run_query(connection_cadastral, query)

connection_tn_string = "host={} dbname={} user={} password={}".format(
    cfg.host,
    cfg.dbname_tn,
    cfg.user,
    cfg.password
)
query = f"""
INSERT INTO public.by_bbox_landkreis_{run_id} (id, gml_id, name, nutzart, bez, geom)
SELECT 
    src.id,
    src.gml_id,
    src.name,
    src.nutzart,
    src.bez,
    src.geom
FROM dblink(%s, 'SELECT id, gml_id, name, nutzart, bez, geom FROM public.nutzung_kreis') 
    AS src(
        id integer,
        gml_id character varying,
        name character varying, 
        nutzart character varying,
        bez character varying,
        geom geometry)
    WHERE ST_Intersects(src.geom, ST_MakeEnvelope(%s, %s, %s, %s, 25832));
"""
db_crud.run_query(connection_cadastral, query, (
    connection_tn_string, 
    *cfg.bbox
))

total_cnt = db_crud.get_table_row_count(connection_cadastral, f"by_bbox_landkreis_{run_id}")
print(f"by_bbox_landkreis_{run_id} ({total_cnt} Rows)")

############################################################
# Copy building color table from building db
############################################################
if cfg.building_source != 'runtime':
    db_crud.drop_table_if_exist(connection_cadastral, f"by_bbox_building_color_{run_id}")
    query = f"""
    CREATE TABLE IF NOT EXISTS public.by_bbox_building_color_{run_id} (
        id SERIAL PRIMARY KEY,
        geom geometry,
        color character varying
    );
    """
    db_crud.run_query(connection_cadastral, query)

    connection_building_string = "host={} dbname={} user={} password={}".format(
        db_building_param["host"],
        db_building_param["dbname"],
        db_building_param["user"],
        db_building_param["password"]
    )
    query = f"""
    INSERT INTO public.by_bbox_building_color_{run_id} (id, geom, color)
    SELECT 
        src.gid,
        src.geom,
        src.color
    FROM dblink(%s, 'SELECT gid, geom, color FROM public.building') 
        AS src(
            gid integer,
            geom geometry,
            color character varying)
        WHERE ST_Intersects(src.geom, ST_MakeEnvelope(%s, %s, %s, %s, 25832));
    """
    db_crud.run_query(connection_cadastral, query, (
        connection_building_string, 
        *cfg.bbox
    ))

    total_cnt = db_crud.get_table_row_count(connection_cadastral, f"by_bbox_building_color_{run_id}")
    print(f"by_bbox_building_color_{run_id} ({total_cnt} Rows)")

# Set building color from r,g,b type to hex
query = f"""
UPDATE by_bbox_building_color_{run_id} 
SET color = 
        '#' ||
        lpad(to_hex(split_part(color, ',', 1)::int), 2, '0') ||
        lpad(to_hex(split_part(color, ',', 2)::int), 2, '0') ||
        lpad(to_hex(split_part(color, ',', 3)::int), 2, '0')
WHERE color not like '#%'
"""
db_crud.run_query(connection_cadastral, query)
print(f"Updated building color")


###################################
# Copy address.remote_address to cadastral (house+street relation)
###################################
connection_address_string = "host={} dbname={} user={} password={}".format(
    db_address_param["host"],
    db_address_param["dbname"],
    db_address_param["user"],
    db_address_param["password"]
)

db_crud.drop_table_if_exist(connection_cadastral, f"by_bbox_address_{run_id}")
query = f"""
CREATE TABLE IF NOT EXISTS public.by_bbox_address_{run_id} (
    id character varying PRIMARY KEY,
    street_id character varying,
    street_name character varying,
    house_number TEXT,
    quality TEXT,
    full_address TEXT,
    house_center_point_geom geometry(Point, 25832),
    house_bbox_geom geometry(Polygon, 25832),
    street_center_point_geom geometry(Point, 25832),
    street_bbox_geom geometry(Polygon, 25832),
    is_geom_updated boolean DEFAULT FALSE
);
"""
db_crud.run_query(connection_cadastral, query)

query = f"""
INSERT INTO public.by_bbox_address_{run_id} (
    id,
    street_id,
    street_name,
    house_number,
    quality,
    full_address,
    house_center_point_geom,
    house_bbox_geom,
    street_center_point_geom,
    street_bbox_geom
)
SELECT 
    src.id,
    src.street_id,
    src.street_name,
    src.house_number,
    src.quality,
    src.full_address,
    src.house_center_point_geom,
    src.house_bbox_geom,
    src.street_center_point_geom,
    src.street_bbox_geom
FROM dblink(
    %s,
    $$
    SELECT 
        id,
        street_id,
        street_name,
        house_number,
        NULL::text AS quality,
        CONCAT_WS(
            ', ',
            CONCAT_WS(' ', street_name, house_number),
            CONCAT_WS(' ', postcode, locality)
        ) AS full_address,
        house_center_point_geom,
        house_bbox_geom,
        street_center_point_geom,
        street_bbox_geom
    FROM public.remote_address
    $$
) AS src(
    id character varying, 
    street_id character varying, 
    street_name character varying,
    house_number text, 
    quality text,
    full_address text,
    house_center_point_geom geometry(Point, 25832), 
    house_bbox_geom geometry(Polygon, 25832),
    street_center_point_geom geometry(Point, 25832),
    street_bbox_geom geometry(Polygon, 25832)
)
WHERE ST_Intersects(
    src.house_bbox_geom,
    ST_MakeEnvelope(%s, %s, %s, %s, 25832)
);
"""

db_crud.run_query(connection_cadastral, query, (
    connection_address_string,
    *cfg.bbox
))

total_cnt = db_crud.get_table_row_count(connection_cadastral, f"by_bbox_address_{run_id}")
print(f"by_bbox_address_{run_id} ({total_cnt} Rows)")


###################################
# Copy address.hausumringe to cadastral (building polygons)
# It copies only entire building polygon exist in bbox inner.
###################################
db_crud.drop_table_if_exist(connection_cadastral, f"by_bbox_building_{run_id}")
query = f"""
CREATE TABLE IF NOT EXISTS public.by_bbox_building_{run_id} (
    id integer PRIMARY KEY,
    color character varying,
    geom geometry(MultiPolygon, 25832),
    is_dashed boolean,
    solid_boundary geometry,
    dashed_boundary geometry
);
"""
db_crud.run_query(connection_cadastral, query)

query = f"""
INSERT INTO public.by_bbox_building_{run_id} (id, geom)
SELECT 
    src.ogc_fid,
    src.geom
FROM dblink(%s, 'SELECT ogc_fid, geom FROM public.hausumringe') 
    AS src(
        ogc_fid integer, 
        geom geometry(MultiPolygon, 25832))
    WHERE ST_Intersects(src.geom, ST_MakeEnvelope(%s, %s, %s, %s, 25832)) AND
        ST_Area(ST_Intersection(src.geom, ST_MakeEnvelope(%s, %s, %s, %s, 25832))) >= ST_Area(src.geom) * 0.9;
"""
db_crud.run_query(connection_cadastral, query, (
    connection_address_string,
    *cfg.bbox,
    *cfg.bbox
))

total_cnt = db_crud.get_table_row_count(connection_cadastral, f"by_bbox_building_{run_id}")
print(f"by_bbox_building_{run_id} ({total_cnt} Rows)")

########################################################
# Create by_bbox_building_labels_
########################################################
db_crud.drop_table_if_exist(connection_cadastral, f"by_bbox_building_labels_{run_id}")

query = f"""
CREATE TABLE IF NOT EXISTS public.by_bbox_building_labels_{run_id} (
    id SERIAL PRIMARY KEY,
    building_id integer,
    street_parcel_id TEXT,
    house_number character varying(10),
    position geometry,
    street_bbox_geom geometry
);
"""
db_crud.run_query(connection_cadastral, query)

print(f"Created by_bbox_building_labels_{run_id}")

############################################################
# Copy vertex table from vertex db
############################################################
if cfg.vertex_source != 'runtime':
    db_crud.drop_table_if_exist(connection_cadastral, f"by_bbox_vertex_{run_id}")
    query = f"""
    CREATE TABLE IF NOT EXISTS public.by_bbox_vertex_{run_id} (
        id character varying PRIMARY KEY,
        geom geometry
    );
    """
    db_crud.run_query(connection_cadastral, query)

    connection_boundary_string = "host={} dbname={} user={} password={}".format(
        db_vertex_param["host"],
        db_vertex_param["dbname"],
        db_vertex_param["user"],
        db_vertex_param["password"]
    )
    query = f"""
    INSERT INTO public.by_bbox_vertex_{run_id} (id, geom)
    SELECT 
        src.id,
        ST_SetSRID(ST_MakePoint(x, y), 25832)
    FROM dblink(%s, 'SELECT id, x, y FROM public.vertex') 
        AS src(
            id character varying,
            x double precision,
            y double precision)
        WHERE ST_Intersects(ST_SetSRID(ST_MakePoint(src.x, src.y), 25832), ST_MakeEnvelope(%s, %s, %s, %s, 25832));
    """
    db_crud.run_query(connection_cadastral, query, (
        connection_boundary_string, 
        *cfg.bbox
    ))

    total_cnt = db_crud.get_table_row_count(connection_cadastral, f"by_bbox_vertex_{run_id}")
    print(f"by_bbox_vertex_{run_id} ({total_cnt} Rows)")

###########################################################################################
# Create label position table because of there can be multiple label position
###########################################################################################
db_crud.drop_table_if_exist(connection_cadastral, f"by_bbox_labels_{run_id}")
query = f"""
CREATE TABLE IF NOT EXISTS public.by_bbox_labels_{run_id} (
    id SERIAL PRIMARY KEY,
    source_id character varying(20),
    source character varying(20),
    name TEXT,
    label_position geometry(Point, 25832),
    font_size int,
    label_angle real,
    alkis_angle real,
    calculated_label_area geometry(Polygon, 25832),
    is_street boolean,
    is_overlap boolean
);
"""
db_crud.run_query(connection_cadastral, query)
print(f"by_bbox_labels_{run_id} created")

###########################################################################################
# Create line geom table because of there can be multiple label position
###########################################################################################
db_crud.drop_table_if_exist(connection_cadastral, f"by_bbox_lines_{run_id}")
query = f"""
CREATE TABLE IF NOT EXISTS public.by_bbox_lines_{run_id} (
    id SERIAL PRIMARY KEY,
    source_id character(20),
    source character(20),
    geom geometry(LineString, 25832),
    is_street bool
);
"""
db_crud.run_query(connection_cadastral, query)
print(f"by_bbox_lines_{run_id} created")

###########################################################################################
# Create symbol table
###########################################################################################
db_crud.drop_table_if_exist(connection_cadastral, f"by_bbox_symbols_{run_id}")
query = f"""
CREATE TABLE IF NOT EXISTS public.by_bbox_symbols_{run_id} (
    id SERIAL PRIMARY KEY,
    parcel_id character(20),
    symbol_name character(30),
    position geometry(Point, 25832),
    size int,
    angle real,
    alkis_angle real,
    geom geometry
);
"""
db_crud.run_query(connection_cadastral, query)
print(f"by_bbox_symbols_{run_id} created")

print(f'Completed, Total Elapsed: {time.time() - start_time:.2f} sec')

db_crud.close_connection(connection_cadastral)
