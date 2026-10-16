import importlib
import os
import re
import shutil
import sys
import time

import psycopg2
from psycopg2 import sql

from read_settings import load_config
from requests_log import mark_deleted

# Runs whose settings_<run_id>.ini is older than this are cleaned up.
RETENTION_DAYS = 30

# Only plain ids are accepted, so a malformed name such as "settings_.ini" (or an empty
# argument) can never turn into a path like download/vertex/ and wipe the whole directory.
RUN_ID_PATTERN = r"[A-Za-z0-9_-]+"
RUN_ID_RE = re.compile(rf"^{RUN_ID_PATTERN}$")
SETTINGS_FILE_RE = re.compile(rf"^settings_({RUN_ID_PATTERN})\.ini$")

# nas/*.py write <run_id>_by_nas_<name>.xml into cfg.xml_path
NAS_XML_NAMES = [
    "polygon_parcel",
    "polygon_building",
    "polygon_calculated_label",
    "polygon_road_classification",
    "line_boundary",
    "line_dashed",
    "line_arrow",
    "label_building",
    "label_parcel",
    "vertex",
    "symbol",
]

# by_bbox_<name>_<run_id> tables in cfg.dbname: the same list 8_drop_tables.py drops at the end of a run.
TABLE_PREFIXES = importlib.import_module("8_drop_tables").TABLE_PREFIXES

# Don't let a lock held on an old table (or database) hang run_bbox.py.
DB_LOCK_TIMEOUT = "10s"


def list_settings_run_ids(cfg):
    """{run_id: modification time} of every settings_<run_id>.ini in settings_backup."""
    run_ids = {}
    if not os.path.isdir(cfg.backup_ini_path):
        return run_ids
    for name in os.listdir(cfg.backup_ini_path):
        match = SETTINGS_FILE_RE.match(name)
        if not match:
            continue
        # run_bbox.py deletes and rewrites settings_<run_id>.ini on every run,
        # so its modification time is the time the file was created.
        try:
            run_ids[match.group(1)] = os.path.getmtime(os.path.join(cfg.backup_ini_path, name))
        except FileNotFoundError:
            continue
    return run_ids


def get_run_artifacts(cfg, run_id, kept_run_ids):
    """Existing temporary files/folders one run_bbox.py run left in logs/, download/ and alkis_xml/."""
    paths = [
        os.path.join(cfg.log_path, f"bbox_{run_id}.log"),      # run_bbox.py
        os.path.join(cfg.log_path, f"by_alkis_{run_id}.log"),  # nas/make_lst.py -> alkis-import
        os.path.join(cfg.building_path, run_id),               # building_color/runtime.py
        os.path.join(cfg.vertex_path, run_id),                 # vertex/runtime.py
        os.path.join(cfg.lst_path, f"by_{run_id}.lst"),        # nas/make_lst.py
    ]

    # nas/*.py. alkis-import reads each xml in place, so GDAL may leave a .gfs next to it.
    for name in NAS_XML_NAMES:
        for ext in ("xml", "gfs"):
            paths.append(os.path.join(cfg.xml_path, f"{run_id}_by_nas_{name}.{ext}"))

    # dashed_detection/download_wms.py + detect_lines.py write <run_id>_*.png. Every file with
    # that prefix is taken (also names from older script versions), except files that can
    # also be read as <kept run>_*: run "by_1011" must not take "by_1011_5_1_wms.png" of run "by_1011_5".
    if os.path.isdir(cfg.dashed_wms_path):
        for name in sorted(os.listdir(cfg.dashed_wms_path)):
            path = os.path.join(cfg.dashed_wms_path, name)
            if (
                name.startswith(f"{run_id}_")
                and not any(name.lower().startswith(f"{kept}_") for kept in kept_run_ids)
                and not os.path.isdir(path)
            ):
                paths.append(path)

    return [p for p in paths if os.path.lexists(p)]


def remove_path(path):
    """Returns False if the path could not be removed."""
    try:
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        else:
            os.remove(path)
    except FileNotFoundError:
        pass  # already gone, e.g. removed by a parallel run
    except OSError as e:
        print(f"  [WARN] Could not remove {path}: {e}")
        return False
    return True


def connect(cfg, dbname, autocommit=False):
    try:
        conn = psycopg2.connect(
            dbname=dbname,
            user=cfg.user,
            password=cfg.password,
            host=cfg.host,
            port=cfg.port,
            options=f"-c lock_timeout={DB_LOCK_TIMEOUT}",
        )
    except psycopg2.Error as e:
        print(f"  [WARN] Could not connect to database {dbname}: {e}")
        return None
    conn.autocommit = autocommit
    return conn


def drop_run_tables(conn, run_id, kept_run_ids):
    """Drops the by_bbox tables of run_id. Returns (ok, number of dropped tables)."""
    if conn is None:
        return False, 0

    # Tables are created unquoted, so PostgreSQL stores their names in lower case.
    # Prefixes overlap ("by_bbox_parcel_" + "street_abc" == "by_bbox_parcel_street_" + "abc"),
    # so never drop a name that also reads as a table of a run that is kept.
    names = sorted(
        name for name in {f"{prefix}{run_id}".lower() for prefix in TABLE_PREFIXES}
        if not any(name.startswith(p) and name[len(p):] in kept_run_ids for p in TABLE_PREFIXES)
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename = ANY(%s)",
                (names,),
            )
            existing = [row[0] for row in cur.fetchall()]
            for name in existing:
                cur.execute(sql.SQL("DROP TABLE IF EXISTS public.{} CASCADE").format(sql.Identifier(name)))
        conn.commit()
    except psycopg2.Error as e:
        conn.rollback()
        print(f"  [WARN] Could not drop by_bbox tables of {run_id}: {e}")
        return False, 0
    return True, len(existing)


def drop_run_db(conn, run_id, kept_run_ids):
    """Drops by_alkis_<run_id>. Returns (ok, name of the dropped database or None)."""
    if conn is None:
        return False, None

    # 7_create_alkis_db.py creates it unquoted, so PostgreSQL stores the name in lower case.
    if run_id.lower() in kept_run_ids:
        return True, None
    db_name = f"by_alkis_{run_id}".lower()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (db_name,))
            if cur.fetchone() is None:
                return True, None
            # FORCE disconnects sessions still open on the old database (PostgreSQL 13+)
            cur.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(db_name)))
    except psycopg2.Error as e:
        print(f"  [WARN] Could not drop database {db_name}: {e}")
        return False, None
    return True, db_name


def cleanup_runs(cfg, run_ids, kept_run_ids):
    """Removes files, by_bbox tables and by_alkis db of each run. Returns True if all runs were fully removed."""
    all_ok = True
    conn_cadastral = connect(cfg, cfg.dbname)
    conn_postgres = connect(cfg, "postgres", autocommit=True)  # DROP DATABASE can't run in a transaction
    try:
        for run_id in run_ids:
            settings_path = os.path.join(cfg.backup_ini_path, f"settings_{run_id}.ini")
            has_settings = os.path.exists(settings_path)

            artifacts = get_run_artifacts(cfg, run_id, kept_run_ids)
            files_ok = all([remove_path(p) for p in artifacts])
            tables_ok, table_count = drop_run_tables(conn_cadastral, run_id, kept_run_ids)
            db_ok, db_name = drop_run_db(conn_postgres, run_id, kept_run_ids)

            # The settings file goes last: if anything above failed, it stays so the
            # run is selected again (and retried) next time.
            if files_ok and tables_ok and db_ok and remove_path(settings_path):
                summary = f"{len(artifacts)} file(s)/folder(s), {table_count} table(s)"
                if db_name:
                    summary += f", database {db_name}"
                if has_settings:
                    summary = "settings, " + summary
                try:
                    if mark_deleted(cfg, run_id):
                        summary += ", requests.log updated"
                except OSError as e:
                    print(f"  [WARN] Could not update requests.log for {run_id}: {e}")
                print(f"  {run_id}: removed {summary}")
            else:
                all_ok = False
                print(f"  {run_id}: incomplete, kept {settings_path} to retry next time")
    finally:
        for conn in (conn_cadastral, conn_postgres):
            if conn is not None:
                conn.close()
    return all_ok


def cleanup_old_runs(cfg):
    """Cleans up every run whose settings file is older than RETENTION_DAYS."""
    settings = list_settings_run_ids(cfg)
    cutoff = time.time() - RETENTION_DAYS * 24 * 60 * 60
    run_ids = sorted(r for r, mtime in settings.items() if mtime < cutoff)
    if not run_ids:
        print(f"[CLEANUP] No runs older than {RETENTION_DAYS} days.")
        return

    print(f"[CLEANUP] Removing temporary files, tables and databases of {len(run_ids)} run(s) older than {RETENTION_DAYS} days ...")
    kept_run_ids = {r.lower() for r in settings if r not in run_ids}
    # Incomplete runs are retried on the next call, so they don't count as a failure here.
    cleanup_runs(cfg, run_ids, kept_run_ids)


def cleanup_run(cfg, run_id):
    """Cleans up only run_id, whatever its age."""
    if not RUN_ID_RE.match(run_id):
        print(f"[ERROR] Invalid run_id {run_id!r}: only letters, digits, '_' and '-' are allowed.")
        return False

    print(f"[CLEANUP] Removing temporary files, tables and database of run {run_id} ...")
    # Every other run is kept, so none of its files/tables is ever taken by name overlap.
    kept_run_ids = {r.lower() for r in list_settings_run_ids(cfg) if r != run_id}
    return cleanup_runs(cfg, [run_id], kept_run_ids)


if __name__ == "__main__":
    # python 0_cleanup_old_runs.py           -> every run older than RETENTION_DAYS (what run_bbox.py does)
    # python 0_cleanup_old_runs.py <run_id>  -> only that run, whatever its age
    cfg = load_config()
    if len(sys.argv) > 1:
        sys.exit(0 if cleanup_run(cfg, sys.argv[1]) else 1)
    cleanup_old_runs(cfg)
