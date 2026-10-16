################################################
# Copy administrative and cadastral boundary lines
# in bbox from the boundary db
################################################
'''
In cadastral DB, run following queries:
-------------------------------------
CREATE EXTENSION IF NOT EXISTS dblink;
-------------------------------------

In boundary DB (by_boundary), run following query:
-------------------------------------
GRANT SELECT ON verwaltungseinheit, katasterbezirk TO alkis;
-------------------------------------
'''
import sys
import os
import db_crud
import time

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from read_settings import load_config

if len(sys.argv) < 2:
    print("Usage: python prepare_boundary.py <id>")
    print('Example command format: "python prepare_boundary.py by_101125"')
    sys.exit(1)

run_id = sys.argv[1]
cfg = load_config(run_id)

# Boundaries digitised in verwaltungseinheit and katasterbezirk come from two
# separate shapefiles, so a shared edge is never bit identical. Lines closer
# than this (meters) to an already emitted boundary count as the same line.
COINCIDENCE_TOLERANCE_M = 0.05

# Cutting one line out of another leaves slivers at the ends. Anything shorter
# than this (meters) is dropped.
MIN_SEGMENT_LENGTH_M = 0.5

# Boundary kinds in displacement order: the first entry wins where two kinds
# run along the same line. artDerFlurstuecksgrenze and signaturnummer follow
# alkis_politischegrenzen in alkisimport postprocessing.d/1_ableitungsregeln/11002.sql,
# ordered here by the administrative hierarchy of verwaltungseinheit.level.
#
# level, artDerFlurstuecksgrenze, signaturnummer
BOUNDARY_KINDS = [
    (1, 7102, 2018),  # Grenze des Bundeslandes
    (2, 7103, 2020),  # Grenze des Regierungsbezirks
    (3, 7104, 2010),  # Grenze des Landkreises
    (4, 7108, 2026),  # Grenze der Verwaltungsgemeinschaft
    (5, 7106, 2022),  # Grenze der Gemeinde
    (6, 7003, 2014),  # Grenze der Gemarkung
]

# katasterbezirk holds no level column, it is only ever Gemarkung.
GEMARKUNG_LEVEL = 6

start_time = time.time()
print('----------------Boundary pre-processing----------------')

db_cadastral_param = {
    "dbname": cfg.dbname,
    "user": cfg.user,
    "password": cfg.password,
    "host": cfg.host,
    "port": cfg.port
}

db_boundary_param = {
    "dbname": cfg.dbname_boundary,
    "user": cfg.user,
    "password": cfg.password,
    "host": cfg.host,
    "port": cfg.port
}

connection_cadastral = db_crud.create_connection(db_cadastral_param)

connection_boundary_string = "host={} dbname={} user={} password={}".format(
    db_boundary_param["host"],
    db_boundary_param["dbname"],
    db_boundary_param["user"],
    db_boundary_param["password"]
)

minx, miny, maxx, maxy = cfg.bbox
envelope = f"ST_MakeEnvelope({minx}, {miny}, {maxx}, {maxy}, 25832)"

############################################################
# Create raw table and fill it from the boundary db
#
# The polygon rings are clipped to the bbox on the remote side, otherwise
# a single row (the Bundesland) drags the whole outline of Bayern across.
############################################################
db_crud.drop_table_if_exist(connection_cadastral, f"by_bbox_boundary_raw_{run_id}")
query = f"""
CREATE TABLE IF NOT EXISTS public.by_bbox_boundary_raw_{run_id} (
    id SERIAL PRIMARY KEY,
    level integer,
    art character varying,
    geom geometry
);
"""
db_crud.run_query(connection_cadastral, query)

remote_verwaltungseinheit = (
    f"SELECT level, art, "
    f"ST_CollectionExtract(ST_Intersection(ST_Boundary(geom), {envelope}), 2) "
    f"FROM public.verwaltungseinheit "
    f"WHERE geom && {envelope} AND ST_Intersects(ST_Boundary(geom), {envelope})"
)

remote_katasterbezirk = (
    f"SELECT {GEMARKUNG_LEVEL}, art, "
    f"ST_CollectionExtract(ST_Intersection(ST_Boundary(geom), {envelope}), 2) "
    f"FROM public.katasterbezirk "
    f"WHERE geom && {envelope} AND ST_Intersects(ST_Boundary(geom), {envelope})"
)

for remote_query in (remote_verwaltungseinheit, remote_katasterbezirk):
    query = f"""
    INSERT INTO public.by_bbox_boundary_raw_{run_id} (level, art, geom)
    SELECT src.level, src.art, src.geom
    FROM dblink(%s, %s)
        AS src(
            level integer,
            art character varying,
            geom geometry)
        WHERE src.geom IS NOT NULL AND NOT ST_IsEmpty(src.geom);
    """
    db_crud.run_query(connection_cadastral, query, (
        connection_boundary_string,
        remote_query
    ))

total_cnt = db_crud.get_table_row_count(connection_cadastral, f"by_bbox_boundary_raw_{run_id}")
print(f"by_bbox_boundary_raw_{run_id} ({total_cnt} Rows)")

############################################################
# Create boundary table, one row per connected line
############################################################
db_crud.drop_table_if_exist(connection_cadastral, f"by_bbox_boundary_{run_id}")
query = f"""
CREATE TABLE IF NOT EXISTS public.by_bbox_boundary_{run_id} (
    id SERIAL PRIMARY KEY,
    level integer,
    art character varying,
    adf integer,
    signaturnummer integer,
    geom geometry(MultiLineString, 25832)
);
"""
db_crud.run_query(connection_cadastral, query)

# Kinds are inserted in order, each one cut against everything already there,
# so a line that is both a Kreis- and a Gemarkungsgrenze is drawn once as the
# Kreisgrenze. ST_Union per level first, because two neighbouring polygons each
# carry their shared edge in their own ring.
for level, adf, signaturnummer in BOUNDARY_KINDS:
    query = f"""
    INSERT INTO public.by_bbox_boundary_{run_id} (level, art, adf, signaturnummer, geom)
    SELECT %s, u.art, %s, %s, ST_Multi(g.geom)
    FROM (
        SELECT ST_Union(geom) AS geom, min(art) AS art
        FROM public.by_bbox_boundary_raw_{run_id}
        WHERE level = %s
    ) u
    LEFT JOIN LATERAL (
        SELECT ST_Union(geom) AS geom FROM public.by_bbox_boundary_{run_id}
    ) done ON true
    CROSS JOIN LATERAL (
        SELECT (ST_Dump(ST_LineMerge(
            CASE
            WHEN done.geom IS NULL THEN u.geom
            ELSE ST_CollectionExtract(ST_Difference(u.geom, ST_Buffer(done.geom, %s)), 2)
            END
        ))).geom AS geom
    ) g
    WHERE u.geom IS NOT NULL AND ST_Length(g.geom) >= %s;
    """
    db_crud.run_query(connection_cadastral, query, (
        level,
        adf,
        signaturnummer,
        level,
        COINCIDENCE_TOLERANCE_M,
        MIN_SEGMENT_LENGTH_M
    ))

query = f"""
SELECT signaturnummer, art, count(*), round(sum(ST_Length(geom))::numeric, 1)
FROM public.by_bbox_boundary_{run_id}
GROUP BY signaturnummer, art
ORDER BY signaturnummer;
"""
rows = db_crud.run_query(connection_cadastral, query)

total_cnt = db_crud.get_table_row_count(connection_cadastral, f"by_bbox_boundary_{run_id}")
print(f"by_bbox_boundary_{run_id} ({total_cnt} Rows)")
for signaturnummer, art, cnt, length in rows or []:
    print(f"  -> {signaturnummer} {art} ({cnt} Rows, {length} m)")

db_crud.drop_table_if_exist(connection_cadastral, f"by_bbox_boundary_raw_{run_id}")
db_crud.close_connection(connection_cadastral)

print(f"----------------Boundary pre-processing finished ({time.time() - start_time:.2f} sec)----------------")
