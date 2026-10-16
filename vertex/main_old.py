# CREATE TABLE vertex (
#     id SERIAL PRIMARY KEY,
#     x DOUBLE PRECISION,
#     y DOUBLE PRECISION
# );

import os
import sys
import logging
from multiprocessing import Pool, Manager, Process
import psycopg2
from psycopg2.extras import execute_batch
from circle import CircleDetector

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import epsg_transformer
from read_settings import load_config
import common

cfg = load_config()
run_id = ""

# Config
DB_CONFIG = None
SQL = "INSERT INTO vertex (x, y) VALUES (%s, %s) ON CONFLICT (x, y) DO NOTHING"
WORKER_COUNT = cfg.cpu_cores
SOURCE_PATH = ""
LOG_PATH = ""

# Circle detection wrapper
def circle_detection(image_path, start_point=(0, 0), resolution=0.25, kernel_size=7, threshold=1):
    #start = time.time()
    detector = CircleDetector(image_path=image_path, kernel_size=kernel_size,
                              threshold=threshold, start_point=start_point, resolution=resolution)
    circles = detector.detect_circles()
    #end = time.time()
    #logging.info(f"{image_path}: Found {len(circles)} circles in {end - start:.2f}s")
    return circles

# Load previously imported files
def initialize_import_log():
    imported = {}
    if os.path.exists(IMPORT_LOG_PATH):
        with open(IMPORT_LOG_PATH) as f:
            for line in f:
                imported[line.strip()] = 1
    return imported

# Logger process
def logger_worker(log_queue):
    with open(IMPORT_LOG_PATH, 'a') as log_file:
        while True:
            msg = log_queue.get()
            if msg == "DONE":
                break
            log_file.write(msg + "\n")
            log_file.flush()

# Worker function
def process_file(args):
    name, imported, log_queue = args
    file_name, file_extension = os.path.splitext(name)
    file_base = file_name  # without .png

    if file_extension != '.png' or imported.get(file_base) == 1:
        logging.info(f"Skipping {name}")
        return None

    try:
        parts = file_base.split("_")
        xmin = float(parts[0])
        xmax = float(parts[2]) + 10
        ymax = float(parts[3]) + 10
        resolution = (xmax - xmin) / 4010

        image_path = os.path.join(SOURCE_PATH, name)
        circles = circle_detection(image_path, [xmin, ymax], resolution)

        transformed_rows = [
            epsg_transformer.convert_bbox_epsg("3857", "25832", (x, y, x, y))[:2]
            for x, y in ((c[0], c[1]) for c in circles)
        ]

        # Create DB connection inside worker
        db = psycopg2.connect(**DB_CONFIG)
        with db.cursor() as cursor:
            execute_batch(cursor, SQL, transformed_rows)
        db.commit()
        db.close()

        log_queue.put(file_base)
        #logging.info(f"Imported {file_base}")
        return file_base

    except Exception as e:
        print(e)
        logging.error(f"Error processing {name}: {e}")
        return None

def create_vertex_table_if_not_exists():
    db = psycopg2.connect(**DB_CONFIG)
    with db.cursor() as cursor:
        create_table_query = """
        CREATE TABLE IF NOT EXISTS vertex (
            id SERIAL PRIMARY KEY,
            x DOUBLE PRECISION,
            y DOUBLE PRECISION,
            UNIQUE (x, y)
        );
        """

        cursor.execute(create_table_query)
        db.commit()
    db.close()

# Main entry point
def start():

    create_vertex_table_if_not_exists()

    imported = initialize_import_log()
    filenames = next(os.walk(SOURCE_PATH))[2]

    manager = Manager()
    log_queue = manager.Queue()

    logger = Process(target=logger_worker, args=(log_queue,))
    logger.start()

    args_list = [(name, imported, log_queue) for name in filenames]

    with Pool(processes=WORKER_COUNT) as pool:
        pool.map(process_file, args_list)

    log_queue.put("DONE")
    logger.join()

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python main.py <id>")
        print('Example command format: "python main.py 20251113_050000"')
        sys.exit(1)

    run_id = sys.argv[1]

    dbname = f"{cfg.dbname_vertex}_{run_id}"
    # Create DB
    common.create_db(cfg, dbname)

    DB_CONFIG = {
        "user": cfg.user,
        "password": cfg.password,
        "host": cfg.host,
        "port": cfg.port,
        "database": dbname
    }

    SOURCE_PATH = cfg.vertex_path + "/" + run_id
    IMPORT_LOG_PATH = os.path.join(cfg.log_path, f"vertex_circle_import_done_{run_id}.txt")
    
    log_path = os.path.join(cfg.log_path, f"vertex_circle_{run_id}.log")
    open(log_path, "w").close()

    # Setup logging
    logging.basicConfig(filename=os.path.join(cfg.log_path, f"vertex_circle_{run_id}.log"), level=logging.INFO)

    start()