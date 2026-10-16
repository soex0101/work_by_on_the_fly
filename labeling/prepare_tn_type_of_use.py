import sys
import os
import time
from multiprocessing import Pool, Manager

import db_crud
import util

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from read_settings import load_config

run_id = ""
cfg = None
db_cadastral_param = None

nutzart_bez_color_map = {
    "Wohnbaufläche": {
        "default": "#ebd5d5"
    },
    "Industrie- und Gewerbefläche": {
        "default": "#f5f5f5"
    },
    "Halde":  {
        "default": "#f5f5f5"
    },
    "Bergbaubetrieb":  {
        "default": "#f5f5f5"
    },
    "Tagebau, Grube, Steinbruch":  {
        "default": "#f5f5f5"
    },
    "Fläche gemischter Nutzung":  {
        "default": "#e7d5d5"
    },
    "Fläche besonderer funktionaler Prägung": {
        "default": "#e7d5d5"
    },
    "Sport-, Freizeit- und Erholungsfläche": {
        "default": "#dce6c2"
    },
    "Friedhof": {
        "default": "#dce6c2"
    },
    "Straßen- und Wegeverkehr": {
        "default": "#ffffff"
    },
    "Platz": {
        "default": "#fefefe",
        "Parkplatz": "#fefefe",
        "Rastplatz": "#fefefe",
        "Festplatz": "#fefefe",
        "Marktplatz": "#fefefe",
        "Raststätte, Autohof": "#fefefe",
        "Fußgängerzone": "#cee7d8",
    },
    "Weg": {
        "default": "#fefefe"
    },
    "Straßenverkehr": {
        "default": "#fefefe",
        "Fußgängerzone": "#cee7d8"
    },
    "Bahnverkehr": {
        "default": "#fefefe"
    },
    "Flugverkehr": {
        "default": "#f2f4cb",
        "Internationaler Flughafen": "#f2f4cb",
        "Landeplatz, Sonderlandeplatz": "#f2f4cb",
        "Regionalflughafen": "#f2f4cb",
        "Verkehrslandeplatz":  "#f2f4cb",
        "Hubschrauberlandeplatz": "#f2f4cb",
        "Segelfluggelände": "#f3f5cc",
    },
    "Schiffsverkehr": {
        "default": "#fefefe",
        "Anlegestelle": "#fefefe",
        "Fähranlage": "#fefefe",
        "Hafenanlage (Landfläche)": "#fefefe",
        "Schleuse (Landfläche)": "#fefefe"
    },
    "Landwirtschaft": {
        "default": "#fef7db",
        "Ackerland": "#fef7db",
        "Hopfen": "#fef7db",
        "Grünland": "#f2f4cb",
        "Baumschule": "#f2f4cb",
        "Obst- und Nussplantage": "#f2f4cb"
    },
    "Wald": {
        "default": "#cee7d8",
        "Laub- und Nadelholz": "#cee7d8"
    },
    "Gehölz": {
        "default": "#cfe8d9"
    },
    "Heide": {
        "default": "#f2e2c9"
    },
    "Moor": {
        "default": "#f2e2c9"
    },
    "Sumpf": {
        "default": "#f2e2c9"
    },
    "Unland, Vegetationslose Fläche": {
        "default": "#ffffff"
    },
    "Unland/Vegetationslose Fläche": {
        "default": "#ffffff"
    },
    "Fließgewässer": {
        "default": "#bee6f8",
    },
    "Hafenbecken": {
        "default": "#bfe7f9"
    },
    "Stehendes Gewässer": {
        "default": "#bfe7f9"
    },
    "Meer": {
        "default": "#BFE7F9"
    },
    "Unkultivierte Fläche": {
        "default": "#ffffff",
        "Unland": "#ffffff",
        "vegetationslose Fläche": "#ffffff"
    }
}

# Main parcel processing function
def process_parcel_range(area_list, counter, run_id, db_cadastral_param) -> None:
    conn = db_crud.create_connection(db_cadastral_param)

    for area in area_list:
        id, nutzart, bez = area

        color = None
        if nutzart in nutzart_bez_color_map:
            if bez in nutzart_bez_color_map[nutzart]:
                color = nutzart_bez_color_map[nutzart][bez]
            else:
                color = nutzart_bez_color_map[nutzart]["default"]
        
        if color is not None:
            update_query = f"""
                UPDATE by_bbox_parcel_tn_{run_id}
                SET 
                    color = %s
                WHERE id = %s;
                """
            db_crud.run_query(conn, update_query, (color, id))

            counter['processed'] += 1
        else:
            counter['missed'] += 1
            print(f"Invalid -> {nutzart}, {bez}")

    db_crud.close_connection(conn)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python preare_tn_type_of_use.py <id>")
        print('Example command format: "python preare_tn_type_of_use.py by_101125"')
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

    start_time = time.time()
    print('----------------TN type_of_use----------------')

    connection_cadastral = db_crud.create_connection(db_cadastral_param)

    update_query = f"""
    UPDATE by_bbox_parcel_tn_{run_id}
    SET 
        color = NULL;
    """
    db_crud.run_query(connection_cadastral, update_query)

    # Get parcel info including free space geometry
    query = f"""
        SELECT 
            id, nutzart, bez
        FROM by_bbox_parcel_tn_{run_id}
    """
    data = db_crud.run_query(connection_cadastral, query)
    print(f"{len(data)} records Processing started")

    db_crud.close_connection(connection_cadastral)

    total_count = len(data)

    if cfg.cpu_cores > 1:
        manager = Manager()
        processed_counter = manager.dict()
        processed_counter['processed'] = 0
        processed_counter['missed'] = 0
        processed_counter['start_time'] = start_time
        
        chunk_list = util.split_into_chunks(data, cfg.cpu_cores)

        print(f"Total count: {total_count} ======= {cfg.cpu_cores} processes started")

        # close()+join() instead of the context manager: Pool.__exit__ calls terminate(),
        # whose _help_stuff_finish() grabs inqueue._rlock while an idle worker still holds
        # it inside SimpleQueue.get(), which can block the shutdown forever.
        pool = Pool(min(cfg.cpu_cores, len(chunk_list)))
        try:
            pool.starmap(process_parcel_range, [(chunk, processed_counter, run_id, db_cadastral_param) for chunk in chunk_list])
            pool.close()
        except BaseException:
            pool.terminate()
            raise
        finally:
            pool.join()

        print(f"Completed, Total Elapsed: {time.time() - start_time:.2f} sec, Missed: {processed_counter['missed']}")
    else:
        counter = {'processed': 0, 'missed': 0, 'start_time': start_time}
        process_parcel_range(data, counter, run_id, db_cadastral_param)
        
        print(f"Completed, Total Elapsed: {time.time() - start_time:.2f} sec, Missed: {counter['missed']}")
