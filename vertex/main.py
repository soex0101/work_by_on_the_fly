import io
import os
import sys
import logging
from multiprocessing import Pool, Process, Queue, cpu_count
import psycopg2
from pyproj import Transformer
from circle import CircleDetector
import datetime

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from read_settings import load_config
import common

# -------------------------------------------------
# Configuration
# -------------------------------------------------

cfg = load_config()
run_id = sys.argv[1]

WORKER_COUNT = min(cpu_count() - 2, 8)   # DO NOT USE ALL CORES
BATCH_SIZE = 50000

SOURCE_PATH = os.path.join(cfg.vertex_path, run_id)
IMPORT_LOG_PATH = os.path.join(cfg.log_path, f"vertex_circle_import_done_{run_id}.txt")
LOG_FILE = os.path.join(cfg.log_path, f"vertex_circle_{run_id}.log")
RESULT_QUEUE = None

DB_NAME = f"{cfg.dbname_vertex}_{run_id}"

DB_CONFIG = {
    "user": cfg.user,
    "password": cfg.password,
    "host": cfg.host,
    "port": cfg.port,
    "dbname": DB_NAME
}

# -------------------------------------------------
# Logging
# -------------------------------------------------

logging.basicConfig(filename=LOG_FILE, level=logging.INFO)

# -------------------------------------------------
# Circle detection
# -------------------------------------------------

def init_worker(queue):
    global RESULT_QUEUE
    RESULT_QUEUE = queue

def detect_circles(image_path, start_point, resolution):
    detector = CircleDetector(
        image_path=image_path,
        kernel_size=7,
        threshold=1,
        start_point=start_point,
        resolution=resolution
    )
    return detector.detect_circles()

# -------------------------------------------------
# Worker process
# -------------------------------------------------

def process_file(args):
    filename, imported = args
    global RESULT_QUEUE

    name, ext = os.path.splitext(filename)
    if ext != ".png" or name in imported:
        return

    try:
        parts = name.split("_")
        xmin = float(parts[0])
        xmax = float(parts[2]) + 10
        ymax = float(parts[3]) + 10
        resolution = (xmax - xmin) / 4010

        image_path = os.path.join(SOURCE_PATH, filename)
        circles = detect_circles(image_path, (xmin, ymax), resolution)

        if circles:
            RESULT_QUEUE.put([(c[0], c[1]) for c in circles])

        RESULT_QUEUE.put(("DONE_FILE", name))

    except Exception as e:
        logging.error(f"Failed {filename}: {e}")

# -------------------------------------------------
# Database writer (FAST)
# -------------------------------------------------

def db_writer(result_queue):
    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor()

    transformer = Transformer.from_crs("EPSG:3857", "EPSG:25832", always_xy=True)

    buffer = []
    processed_files = []

    with open(IMPORT_LOG_PATH, "a") as log_file:
        while True:
            item = result_queue.get()

            if item == "STOP":
                break

            if isinstance(item, tuple) and item[0] == "DONE_FILE":
                processed_files.append(item[1])
                continue

            buffer.extend(item)

            if len(buffer) >= BATCH_SIZE:
                flush_buffer(cur, transformer, buffer)
                # print data with current timestamp
                print(f"[{datetime.datetime.now()}] Inserted {len(buffer)} vertices")
                conn.commit()
                buffer.clear()

            for f in processed_files:
                log_file.write(f + "\n")
            log_file.flush()
            processed_files.clear()

    if buffer:
        flush_buffer(cur, transformer, buffer)
        conn.commit()

    conn.close()

def flush_buffer(cursor, transformer, points):
    xs, ys = zip(*points)
    xs_t, ys_t = transformer.transform(xs, ys)

    data = "\n".join(f"{x}\t{y}" for x, y in zip(xs_t, ys_t))
    cursor.copy_from(
        file=io.StringIO(data),
        table="vertex",
        columns=("x", "y")
    )

# -------------------------------------------------
# Helpers
# -------------------------------------------------

def load_imported():
    if not os.path.exists(IMPORT_LOG_PATH):
        return set()
    with open(IMPORT_LOG_PATH) as f:
        return set(line.strip() for line in f)

def create_table():
    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS vertex (
            id SERIAL PRIMARY KEY,
            x DOUBLE PRECISION,
            y DOUBLE PRECISION
        );
    """)
    conn.commit()
    conn.close()

# -------------------------------------------------
# Main
# -------------------------------------------------

def main():
    common.create_db(cfg, DB_NAME)
    create_table()

    imported = load_imported()
    files = os.listdir(SOURCE_PATH)
    print(f"Source path: {SOURCE_PATH}")

    result_queue = Queue(maxsize=WORKER_COUNT * 4)

    writer = Process(target=db_writer, args=(result_queue,))
    writer.start()

    args = [(f, imported) for f in files]

    with Pool(
        processes=WORKER_COUNT,
        initializer=init_worker,
        initargs=(result_queue,)
    ) as pool:
        pool.map(process_file, args)

    result_queue.put("STOP")
    writer.join()

    print("Import finished. Creating UNIQUE index...")

    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor()
    cur.execute("CREATE UNIQUE INDEX ON vertex (x, y);")
    conn.commit()
    conn.close()

    print("Done.")

# -------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python main.py <run_id>")
        sys.exit(1)
    main()
