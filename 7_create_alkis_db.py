import sys
import os

import psycopg2

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from read_settings import load_config

cfg = None
run_id = ""

def create_alkis_db():

    DB_NAME = f"by_alkis_{run_id}"

    # ---- Step 1: Connect to the default 'postgres' database ----
    conn = psycopg2.connect(
        dbname="postgres",
        user=cfg.user,
        password=cfg.password,
        host=cfg.host
    )
    conn.autocommit = True  # required for CREATE DATABASE

    cur = conn.cursor()

    # ---- Step 2: Create database ----
    cur.execute(f"DROP DATABASE IF EXISTS {DB_NAME} WITH (FORCE);")
    cur.execute(f"CREATE DATABASE {DB_NAME};")

    cur.close()
    conn.close()

    # ---- Step 3: Connect to the new DB and enable PostGIS ----
    conn2 = psycopg2.connect(
        dbname=DB_NAME,
        user=cfg.user,
        password=cfg.password,
        host=cfg.host
    )
    conn2.autocommit = True

    cur2 = conn2.cursor()

    cur2.execute("CREATE EXTENSION IF NOT EXISTS postgis;")

    cur2.close()
    conn2.close()

    print("Database created and PostGIS enabled!")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python make_lst.py <id>")
        print('Example command format: "python make_lst.py by_101125"')
        sys.exit(1)

    run_id = sys.argv[1]
    cfg = load_config(run_id)

    create_alkis_db()
