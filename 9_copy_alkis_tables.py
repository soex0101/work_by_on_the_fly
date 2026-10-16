import sys

import psycopg2

from read_settings import load_config

cfg = None
run_id = ""

TABLE_PREFIXES = [
    "by_bbox_address_",
    "by_bbox_parcel_",
    "by_bbox_parcel_street_",
    "by_bbox_building_",
    "by_bbox_building_labels_",
    "by_bbox_building_color_",
    "by_bbox_boundary_",
    "by_bbox_landkreis_",
    "by_bbox_labels_",
    "by_bbox_lines_",
    "by_bbox_symbols_",
    "by_bbox_parcel_changed_",
    "by_bbox_parcel_check_",
    "by_bbox_parcel_confirmed_",
    "by_bbox_parcel_sync_",
    "by_bbox_vertex_",
]

FDW_SERVER = "by_cadastral_fdw"
FOREIGN_SCHEMA = "by_cadastral_src"


def copy_tables(run_id: str):
    table_names = [f"{prefix}{run_id}" for prefix in TABLE_PREFIXES]

    conn = psycopg2.connect(
        dbname=f"by_alkis_{run_id}",
        user=cfg.user,
        password=cfg.password,
        host=cfg.host,
        port=cfg.port,
    )
    conn.autocommit = True
    cur = conn.cursor()

    # ---- Set up a foreign-data-wrapper link back to by_cadastral ----
    cur.execute("CREATE EXTENSION IF NOT EXISTS postgres_fdw;")
    cur.execute(f"DROP SERVER IF EXISTS {FDW_SERVER} CASCADE;")
    cur.execute(
        f"CREATE SERVER {FDW_SERVER} FOREIGN DATA WRAPPER postgres_fdw "
        f"OPTIONS (host %s, port %s, dbname %s)",
        (cfg.host, str(cfg.port), cfg.dbname),
    )
    cur.execute(
        f"CREATE USER MAPPING FOR CURRENT_USER SERVER {FDW_SERVER} "
        f"OPTIONS (user %s, password %s)",
        (cfg.user, cfg.password),
    )

    cur.execute(f"DROP SCHEMA IF EXISTS {FOREIGN_SCHEMA} CASCADE;")
    cur.execute(f"CREATE SCHEMA {FOREIGN_SCHEMA};")
    # LIMIT TO silently skips any names that don't exist in the source schema
    limit_clause = ", ".join(table_names)
    cur.execute(
        f"IMPORT FOREIGN SCHEMA public LIMIT TO ({limit_clause}) "
        f"FROM SERVER {FDW_SERVER} INTO {FOREIGN_SCHEMA};"
    )

    for table_name in table_names:
        cur.execute("SELECT to_regclass(%s) IS NOT NULL", (f"{FOREIGN_SCHEMA}.{table_name}",))
        if not cur.fetchone()[0]:
            print(f"Skipped (table not found in {cfg.dbname}): {table_name}")
            continue

        cur.execute(f"DROP TABLE IF EXISTS public.{table_name};")
        cur.execute(f"CREATE TABLE public.{table_name} AS SELECT * FROM {FOREIGN_SCHEMA}.{table_name};")
        print(f"Copied: {table_name}")

    # ---- Tear down the fdw link ----
    cur.execute(f"DROP SCHEMA IF EXISTS {FOREIGN_SCHEMA} CASCADE;")
    cur.execute(f"DROP SERVER IF EXISTS {FDW_SERVER} CASCADE;")

    cur.close()
    conn.close()

    print("\n[OK] All tables copied successfully.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python 9_copy_alkis_tables.py <run_id>")
        print("Example: python 9_copy_alkis_tables.py by_101125")
        sys.exit(1)

    run_id = sys.argv[1]
    cfg = load_config()

    try:
        copy_tables(run_id)
    except Exception as e:
        print(f"\n[ERROR] {e}")
        sys.exit(1)
