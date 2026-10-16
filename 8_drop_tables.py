import sys
from labeling import db_crud
from read_settings import load_config

cfg = None
db_cadastral_param = None
run_id = ""

TABLE_PREFIXES = [
    "by_bbox_address_",
    "by_bbox_boundary_",
    "by_bbox_boundary_raw_",
    "by_bbox_building_",
    "by_bbox_building_labels_",
    "by_bbox_building_color_",
    "by_bbox_labels_",
    "by_bbox_landkreis_",
    "by_bbox_lines_",
    "by_bbox_parcel_",
    "by_bbox_parcel_street_",
    "by_bbox_symbols_",
    "by_bbox_parcel_changed_",
    "by_bbox_parcel_check_",
    "by_bbox_parcel_confirmed_",
    "by_bbox_parcel_sync_",
    "by_bbox_parcel_tn_",
    "by_bbox_parcel_street_",
    "by_bbox_vertex_",
]


def drop_tables(run_id: str):
    try:
        conn = db_crud.create_connection(db_cadastral_param)

        for prefix in TABLE_PREFIXES:
            table_name = f"{prefix}{run_id}"
            db_crud.drop_table_if_exist(conn, table_name)

        print("\n[OK] All tables dropped successfully.")

    except Exception as e:
        print(f"\n[ERROR] {e}")
        sys.exit(1)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python drop_run_tables.py <run_id>")
        print("Example: python drop_run_tables.py by_101125")
        sys.exit(1)

    run_id = sys.argv[1]
    cfg = load_config()
    db_cadastral_param = {
        "dbname": cfg.dbname,
        "user": cfg.user,
        "password": cfg.password,
        "host": cfg.host,
        "port": cfg.port
    }

    drop_tables(run_id)
