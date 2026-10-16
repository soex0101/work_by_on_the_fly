#######################################################
# It is deprecated
# compute spatial overlaps,
# classify relationships,
# apply updates in a transaction.
###########################################
import psycopg2
from psycopg2.extras import execute_values
import sys

from read_settings import load_config

cfg = None
DB = None
run_id = ""
parcel_table_name = "by_parcel"
table_name = ""
table_org = ""     # original table (target to update)
table_new = ""

# ---------- CONFIG ----------
PK_OLD = "id"                         # primary key column in original
PK_NEW = "id"                         # primary key column in new
ATTRS = ["boundary_id", "name", "official_area", "corridor", "numerator", "denominator", "gml_id"]  # attrs to copy from table_new -> parcel
AREA_THRESHOLD = 0.90                 # how much overlap counts as “same”
MIN_INTERSECT_RATIO = 0.01            # ignore tiny slivers (<1%)

def prepare_tables(cur):
    cur.execute(f"CREATE INDEX IF NOT EXISTS {table_org.split('.')[-1]}_gix ON {table_org} USING GIST(geom);")
    cur.execute(f"CREATE INDEX IF NOT EXISTS {table_new.split('.')[-1]}_gix ON {table_new} USING GIST(geom);")
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {table_name}(
            old_id   varchar(255),
            new_id   varchar(255),
            relation text,
            r_old    double precision,
            r_new    double precision,
            a_int    double precision,
            processed boolean default false
        );
        DELETE FROM {table_name};
    """)

def main():
    conn = psycopg2.connect(**DB)
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            prepare_tables(cur)

            # 1) Build overlaps in a temp table
            cur.execute("""
                DROP TABLE IF EXISTS tmp_overlaps;
                CREATE TEMP TABLE tmp_overlaps AS
                SELECT
                    o.{PK_OLD} AS old_id,
                    n.{PK_NEW} AS new_id,
                    ST_Area(o.geom) AS a_old,
                    ST_Area(n.geom) AS a_new,
                    ST_Area(ST_Intersection(o.geom, n.geom)) AS a_int
                FROM {ORIG} o
                JOIN {NEW}  n
                  ON ST_Intersects(o.geom, n.geom);
                ALTER TABLE tmp_overlaps ADD COLUMN r_old double precision, ADD COLUMN r_new double precision;
                UPDATE tmp_overlaps
                   SET r_old = CASE WHEN a_old > 0 THEN a_int/a_old ELSE 0 END,
                       r_new = CASE WHEN a_new > 0 THEN a_int/a_new ELSE 0 END;
                -- throw away tiny overlaps
                DELETE FROM tmp_overlaps WHERE r_old < %s AND r_new < %s;
            """.format(ORIG=table_org, NEW=table_new, PK_OLD=PK_OLD, PK_NEW=PK_NEW), (MIN_INTERSECT_RATIO, MIN_INTERSECT_RATIO))

            # 2a) 1:1 (essentially same parcel)
            cur.execute(f"""
                INSERT INTO {table_name}(old_id,new_id,relation,r_old,r_new,a_int)
                SELECT old_id, new_id, '1:1', r_old, r_new, a_int
                FROM tmp_overlaps
                WHERE r_old >= %s AND r_new >= %s;
            """, (AREA_THRESHOLD, AREA_THRESHOLD))

            # 2b) Splits: one old → many new whose r_old sum ≈ 1
            cur.execute(f"""
                INSERT INTO {table_name}(old_id,new_id,relation,r_old,r_new,a_int)
                SELECT t.old_id, t.new_id, 'split', t.r_old, t.r_new, t.a_int
                FROM tmp_overlaps t
                JOIN (
                    SELECT old_id
                    FROM tmp_overlaps
                    GROUP BY old_id
                    HAVING COUNT(DISTINCT new_id) > 1 AND SUM(r_old) >= %s
                ) s ON s.old_id = t.old_id
                WHERE NOT EXISTS (
                    SELECT 1 FROM {table_name} x WHERE x.old_id=t.old_id AND x.new_id=t.new_id
                );
            """, (AREA_THRESHOLD,))

            # 2c) Merges: many old → one new whose r_new sum ≈ 1
            cur.execute(f"""
                INSERT INTO {table_name}(old_id,new_id,relation,r_old,r_new,a_int)
                SELECT t.old_id, t.new_id, 'merge', t.r_old, t.r_new, t.a_int
                FROM tmp_overlaps t
                JOIN (
                    SELECT new_id
                    FROM tmp_overlaps
                    GROUP BY new_id
                    HAVING COUNT(DISTINCT old_id) > 1 AND SUM(r_new) >= %s
                ) m ON m.new_id = t.new_id
                WHERE NOT EXISTS (
                    SELECT 1 FROM {table_name} x WHERE x.old_id=t.old_id AND x.new_id=t.new_id
                );
            """, (AREA_THRESHOLD,))

            # 2d) Anything else that overlaps -> complex
            cur.execute(f"""
                INSERT INTO {table_name}(old_id,new_id,relation,r_old,r_new,a_int)
                SELECT t.old_id, t.new_id, 'complex', t.r_old, t.r_new, t.a_int
                FROM tmp_overlaps t
                WHERE NOT EXISTS (
                    SELECT 1 FROM {table_name} x WHERE x.old_id=t.old_id AND x.new_id=t.new_id
                );
            """)

            # 3) Apply updates
            # 3a) One-to-one: update original from new
            if ATTRS:
                set_clause = ", ".join([f'{a}=n.{a}' for a in ATTRS if a != "geom"])
            else:
                set_clause = ""
            geom_set = "geom = n.geom"
            final_set = ", ".join([s for s in [geom_set, set_clause] if s])
            print("=====final set")
            print(final_set)
            cur.execute(f"""
                WITH pairs AS (
                  SELECT old_id, new_id
                  FROM {table_name}
                  WHERE relation='1:1'
                )
                UPDATE {table_org} o
                   SET {final_set}
                FROM {table_new} n
                JOIN pairs p ON p.new_id = n.{PK_NEW}
                WHERE o.{PK_OLD} = p.old_id;
            """)

            # 3b) Splits / Merges / Complex: replace olds with the set of news
            # Find involved ids
            cur.execute(f"""
                CREATE TEMP TABLE tmp_involved AS
                SELECT DISTINCT old_id, new_id
                FROM {table_name}
                WHERE relation IN ('split','merge','complex','1:1');
            """)
            cur.execute("CREATE TEMP TABLE tmp_old_ids AS SELECT DISTINCT old_id FROM tmp_involved;")
            cur.execute("CREATE TEMP TABLE tmp_new_ids AS SELECT DISTINCT new_id FROM tmp_involved;")

            # Delete old rows involved
            cur.execute(f"""
                DELETE FROM {table_org} o
                USING tmp_old_ids t
                WHERE o.{PK_OLD} = t.old_id;
            """)

            # Insert new rows for the involved new_ids
            # Note: if schemas differ, list target columns explicitly
            cols = [PK_OLD, "geom"] + [a for a in ATTRS if a not in (PK_OLD, "geom")]
            # We’ll assume you want to carry over table_new ids as the new primary keys.
            cols_sql = ", ".join(cols)
            select_sql = ", ".join([f"n.{c}" if c != PK_OLD else f"n.{PK_NEW} AS {PK_OLD}" for c in cols])

            sql = f"""
                INSERT INTO {table_org} ({cols_sql})
                SELECT {select_sql}
                FROM {table_new} n
                JOIN tmp_new_ids t ON n.{PK_NEW} = t.new_id;
            """
            print(sql)

            cur.execute(f"""
                INSERT INTO {table_org} ({cols_sql})
                SELECT {select_sql}
                FROM {table_new} n
                JOIN tmp_new_ids t ON n.{PK_NEW} = t.new_id;
            """)

            # 4) Mark processed and commit
            cur.execute(f"UPDATE {table_name} SET processed = true;")
            conn.commit()
            print("Sync complete.")

            # Print updated count
            cur.execute(f"""SELECT COUNT(*) FROM by_bbox_sync_parcel_changed_{run_id}""")
            changed_parcel_count = cur.fetchall()
            print(f"\nNote: Changed parcel count: {changed_parcel_count}")


    except Exception as e:
        conn.rollback()
        raise
    finally:
        conn.close()

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python 2_by_merge_parcel_changed.py <id>")
        print('Example command format: "python 2_by_merge_parcel_changed.py by_101125"')
        sys.exit(1)

    run_id = sys.argv[1]
    table_name = f"public.by_bbox_sync_parcel_crosswalk_{run_id}"  # lineage table (old_id,new_id,relation,ratios)
    table_org = f"public.{parcel_table_name}"                   # original table (target to update)
    table_new  = f"public.by_bbox_sync_parcel_new_{run_id}"       # new incoming table (authoritative)
    cfg = load_config(run_id)

    DB = dict(
        user=cfg.user,
        password=cfg.password,
        host=cfg.host,
        port=cfg.port,
        database=cfg.dbname
    )

    main()
