import sys

import psycopg2
from psycopg2 import sql

from read_settings import load_config

cfg = None
run_id = ""


def drop_db_forcefully(cfg, db_name):
    """Drop `db_name` even if other processes are still connected to it.

    Connects to the `postgres` maintenance database (you cannot drop a
    database you are connected to) and uses DROP DATABASE ... WITH (FORCE),
    which terminates any existing connections server-side (PostgreSQL 13+).
    """
    conn_param = {
        "dbname": "postgres",
        "user": cfg.user,
        "password": cfg.password,
        "host": cfg.host,
        "port": cfg.port,
    }

    conn = psycopg2.connect(**conn_param)
    conn.autocommit = True  # DROP DATABASE cannot run inside a transaction
    cur = conn.cursor()

    try:
        cur.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE);").format(
                sql.Identifier(db_name)
            )
        )
        print(f"[OK] Database '{db_name}' dropped successfully.")

    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python 10_drop_db.py <run_id>")
        print("Example: python 10_drop_db.py 0623")
        sys.exit(1)

    run_id = sys.argv[1]
    cfg = load_config()
    db_name = f"by_alkis_{run_id}"

    try:
        drop_db_forcefully(cfg, db_name)
    except Exception as e:
        print(f"\n[ERROR] {e}")
        sys.exit(1)
