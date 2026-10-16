# Drop all tables related run_id
# It will be used for development env
import sys

from labeling import db_crud

from read_settings import load_config

cfg = None
db_cadastral_param = None
run_id = ""

def main():

    conn = db_crud.create_connection(db_cadastral_param)

    db_crud.drop_table_if_exist(conn, f"by_bbox_parcel_changed_{run_id}")
    db_crud.drop_table_if_exist(conn, f"by_bbox_parcel_check_{run_id}")
    db_crud.drop_table_if_exist(conn, f"by_bbox_parcel_confirmed_{run_id}")
    db_crud.drop_table_if_exist(conn, f"by_bbox_parcel_sync_{run_id}")
    db_crud.drop_table_if_exist(conn, f"by_bbox_parcel_{run_id}")
    db_crud.drop_table_if_exist(conn, f"by_bbox_parcel_street_{run_id}")
    db_crud.drop_table_if_exist(conn, f"by_bbox_parcel_tn_{run_id}")
    db_crud.drop_table_if_exist(conn, f"by_bbox_building_{run_id}")
    db_crud.drop_table_if_exist(conn, f"by_bbox_building_labels_{run_id}")
    db_crud.drop_table_if_exist(conn, f"by_bbox_address_{run_id}")
    db_crud.drop_table_if_exist(conn, f"by_bbox_labels_{run_id}")
    db_crud.drop_table_if_exist(conn, f"by_bbox_lines_{run_id}")
    db_crud.drop_table_if_exist(conn, f"by_bbox_symbols_{run_id}")

if __name__ == "__main__":

    if len(sys.argv) < 2:
        print("Usage: python 1_initialize.py <id>")
        print('Example command format: "python 1_initialize.py by_101125"')
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

    main()
    
    print('Successfully initialized.')