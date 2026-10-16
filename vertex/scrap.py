import argparse
import math
from operator import index
import os
import sys
import time
from multiprocessing import Pool, Manager
import random
import requests
import json
from datetime import datetime

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from read_settings import load_config
import common
from utils import debug_info, divide_bound, divide_bound_x, get_header_proxy
import epsg_transformer
from by_get_cookie import get_cookie

import logging

cfg = load_config()
run_id = ""
COOKIE_TTL = 900  # 15 minutes

BASEURL = (
    'https://services.atlas.bayern.de/proxy/wms/map/a29d0fce-6901-43c0-90ed-ac73b7780479?REQUEST=GetMap&SERVICE=WMS'
    '&VERSION=1.1.1&FORMAT=image/png&STYLES=&TRANSPARENT=true&LAYERS=by_alkis_flurkarte_umr_gelb&SRS=EPSG:3857&&WIDTH=4010&HEIGHT=4010'
)

proxy_list = common.load_proxies()

#proxy_list = ["u5_175_254_131:Jrn99mUdO20nBKiBxlkLw4eC@5.175.254.131:3128",
#              "u5_175_254_132:3Ub8rVqSznhL7KVc1tmgCBt5@5.175.254.132:3128",
#              "u5_175_254_133:6H76BxEn1JTCqUJ8yPjvVLQj@5.175.254.133:3128",
#              "u5_175_254_134:IkhGTs9rYQEmP44plgB7jpmQ@5.175.254.134:3128",
#              "u5_175_254_135:Xsg0WmSCNHEVrlsL7XCEBMNU@5.175.254.135:3128"]
# proxy_list = ['207.229.93.68:1027',
#               '207.229.93.68:1028',
#               '207.229.93.68:1029',
#               '207.229.93.68:1026',
#               '207.229.93.68:1025']

class BY:
    def __init__(self):
        self.bound = cfg.by_whole_bbox
        self.initial_list = {}
        self.cookie_ttl = COOKIE_TTL
        self.max_retries = 100
        # Shared (Manager-backed) state. Populated in start_with_pool once the
        # Manager exists; these proxies are picklable and refer back to the
        # manager process, so every worker process sees the same state.
        self.cookie_store = None
        self.cookie_store_lock = None
        # Serialize Selenium cookie fetches: only one headless browser runs at a
        # time (the by_get_cookie flow is too timing-fragile to run in parallel).
        self.cookie_create_lock = None
        self.proxy_idx = None
        self.proxy_idx_lock = None
        # Each worker process keeps its own current proxy index (replaces
        # threading.local). It only moves to the next proxy when the current
        # cookie/request fails.
        self._current_idx = None

    def _setup_shared(self, manager):
        self.cookie_store = manager.dict()
        self.cookie_store_lock = manager.Lock()
        self.cookie_create_lock = manager.Lock()
        self.proxy_idx = manager.Value('i', 0)
        self.proxy_idx_lock = manager.Lock()
        # Pre-create one entry per proxy so workers never need the Manager
        # handle directly. Each entry is itself a manager.dict, so mutating its
        # cookie/created_at is visible across all processes.
        for idx in range(len(proxy_list)):
            proxy = self.parse_proxy(self.get_proxy(idx))
            entry = manager.dict(
                index=idx,
                proxy=proxy,
                cookie=None,
                created_at=0.0,
                lock=manager.Lock(),
            )
            self.cookie_store[idx] = entry

    def get_proxy(self, index):
        proxy = None
        if len(proxy_list) > 0:
            proxy = proxy_list[index]
        return proxy

    def parse_proxy(self, proxy_line):
        parts = proxy_line.split(":")
        host = parts[0]
        port = parts[1]
        username = parts[2] if len(parts) > 2 else None
        password = parts[3] if len(parts) > 3 else None
        return f"{username}:{password}@{host}:{port}"

    def get_or_create_cookie_entry(self, index):
        entry = self.cookie_store.get(index)
        if entry is not None:
            print(f"========[COOKIE STORE] Existing entry - {index}")
            return entry

        # All entries are pre-created in _setup_shared; this is a safety net.
        with self.cookie_store_lock:
            entry = self.cookie_store.get(index)
            if entry is not None:
                return entry
            raise Exception(f"No cookie entry for proxy index {index}")

    def is_cookie_valid(self, entry):
        if not entry['cookie']:
            return False
        print(f"========[COOKIE] Remaining TTL: {self.cookie_ttl - (time.time() - entry['created_at'])} seconds")
        return (time.time() - entry['created_at']) < self.cookie_ttl

    def ensure_cookie(self, entry):
        # Fast path - valid cookie, no locking needed
        if self.is_cookie_valid(entry):
            print("========[COOKIE] Use existing valid cookie")
            return entry['cookie']

        # Serialize cookie creation per proxy: the first process creates the
        # cookie while the others block on the lock, then re-check and reuse it.
        with entry['lock']:
            if self.is_cookie_valid(entry):
                print("========[COOKIE] Use existing valid cookie")
                return entry['cookie']

            print(f"========[COOKIE] Creating new cookie for proxy index {entry['index']} - {entry['proxy']}")
            # One browser at a time across all proxies.
            with self.cookie_create_lock:
                entry['cookie'] = get_cookie(entry['proxy'])
                entry['created_at'] = time.time()
            print(f"========[COOKIE] Success: {entry['cookie']}")
            return entry['cookie']

    def get_next_cookie_entry(self):
        if not proxy_list:
            raise Exception("No proxies configured")

        with self.proxy_idx_lock:
            idx = self.proxy_idx.value
            self.proxy_idx.value = (idx + 1) % len(proxy_list)

        print(f"========[PID {os.getpid()}] Switch to proxy index {idx}")
        return self.get_or_create_cookie_entry(idx)

    def get_worker_cookie_entry(self):
        if self._current_idx is None:
            entry = self.get_next_cookie_entry()
            self._current_idx = entry['index']
            return entry
        print(f"========[PID {os.getpid()}] Reuse proxy index {self._current_idx}")
        return self.cookie_store.get(self._current_idx)

    def switch_worker_proxy(self, old_entry=None):
        if old_entry is not None:
            with old_entry['lock']:
                old_entry['cookie'] = None
                old_entry['created_at'] = 0.0

        new_entry = self.get_next_cookie_entry()
        self._current_idx = new_entry['index']
        return new_entry

    def scraping(self, uri, params=None, formatted='json'):
        if params is None:
            params = {}
        retries = 0

        while retries < self.max_retries:
            entry = None
            try:
                # Keep using this process's proxy/cookie while it is valid.
                entry = self.get_worker_cookie_entry()
                cookie = self.ensure_cookie(entry)
                print(f"PID {os.getpid()} uses proxy index {entry['index']}")

                headers = get_header_proxy('services.atlas.bayern.de', cookie, 'services.atlas.bayern.de')
                proxy = {'http': f'http://{entry["proxy"]}', 'https': f'http://{entry["proxy"]}'}
                response = requests.get(uri, params, headers=headers, verify=False, proxies=proxy, stream=True)
                response.raise_for_status()

                if formatted == 'json':
                    json_data = json.loads(response.content)
                    return json_data
                return response

            except requests.HTTPError as err:
                retries += 1
                print(err.__str__())

                if err.response is not None and err.response.status_code == 401:
                    print("cookie expired. switch to next proxy and create a new cookie")
                else:
                    print(f"request failed. switch to next proxy and create a new cookie: {err}")

                # Current proxy/cookie failed. Next retry will use next proxy.
                self.switch_worker_proxy(entry)
                continue

            except Exception as e:
                retries += 1
                print(e.__str__())
                print("Unexpected error. switch to next proxy and create a new cookie")

                # Current proxy/cookie failed. Next retry will use next proxy.
                self.switch_worker_proxy(entry)
                continue

        raise Exception(f"Failed to scrap {uri} after {self.max_retries} retries")

    def initialize(self):
        path = os.path.join(cfg.vertex_path, run_id)
        if not os.path.exists(path):
            os.makedirs(path, exist_ok=True)
        if os.path.exists(path):
            filenames = next(os.walk(path))[2]
            for name in filenames:
                file_name, file_extension = os.path.splitext(name)
                if file_extension == '.png':
                    self.initial_list[file_name] = 1

        if not os.path.exists(path + '/wrong_list.txt'):
            with open(path + '/wrong_list.txt', "w") as f:
                f.write("Log file created!")
        else:
            print("Log file already exists.")

        with open(path + '/wrong_list.txt') as log_file:
            for line in log_file:
                line = line.strip()
                if line.startswith("empty="):
                    file_name = line.split("empty=", 1)[1]
                    if file_name:
                        self.initial_list[file_name] = 1

    def start_with_pool(self, bound, count=3):
        self.initialize()
        step = math.ceil((bound[3] - bound[1]) / count)
        bounds = divide_bound(bound, step)
        bound_strs = ['_'.join([str(b) for b in bounds[i]]) for i in range(0, count)]

        # The Manager must stay alive for the whole pool run, so keep it in this
        # local scope (it is intentionally not stored on self: a SyncManager is
        # not picklable, but the proxies it hands out are).
        with Manager() as manager:
            self._setup_shared(manager)
            with Pool(processes=count) as pool:
                results = []
                for p in bound_strs:
                    results.append(pool.apply_async(self.start, args=(p,)))
                    time.sleep(1)
                for result in results:
                    result.get()

    def start(self, bound_str):
        path = os.path.join(cfg.vertex_path, run_id)
        bound_ps = bound_str.split('_')
        bounds = []
        for bound1 in bound_ps:
            bounds.append(float(bound1))

        step = 1000
        bound1s = divide_bound(bounds, step)
        with open(path + '/wrong_list.txt', 'a') as log_file:
            for bound1 in bound1s:
                step_bound = divide_bound_x(bound1, step)
                for step_bound2 in step_bound:
                    bound = step_bound2
                    expanded_bbox = [bound[0], bound[1], bound[2] + 10, bound[3] + 10]
                    # if not (485410 < bound[0] < 486210 and 5884032 < bound[3] < 5884832):
                    #     continue
                    bbox = ','.join(str(v) for v in expanded_bbox)

                    filename = '_'.join(str(v) for v in bound)

                    if filename in self.initial_list:
                        print('skip---')
                        continue
                    # Skip if the file was already downloaded on disk.
                    if os.path.exists(f'{path}/{filename}.png'):
                        print(f'skip--- file exists: {filename}.png')
                        continue
                    try:
                        pl = self.scraping(BASEURL + '&BBOX=' + bbox, None, 'stream')
                        # print(len(pl.content))
                        # if length is 437 and check - Unauthorized error
                        if len(pl.content) <= 71625:
                            log_file.write("empty=" + filename + "\n")
                            log_file.flush()
                            continue
                        with open(f'{path}/{filename}.png', 'wb') as f:
                            for chunk in pl.iter_content(chunk_size=8192):
                                f.write(chunk)
                    except Exception as e:
                        log_file.write("wrong=" + bbox + "\n")
                        log_file.flush()
                        continue
        print('scrap done--------------')


if __name__ == "__main__":
    #########################################
    # python scrap.py 20251113_050000
    # python scrap.py 20251113_050000 --debug
    # python scrap.py 20251113_050000 --bbox=491906.3,5217361.5,852660.1,5610307.7
    # python scrap.py 20251113_050000 --debug --bbox=491906.3,5217361.5,852660.1,5610307.7
    #########################################
    parser = argparse.ArgumentParser(description="Scrape Bayern ALKIS flurkarte tiles.")
    parser.add_argument("run_id", help='Run id, e.g. "20251113_050000"')
    parser.add_argument("--debug", action="store_true",
                        help="Run in debug mode (single process).")
    parser.add_argument("--bbox",
                        help="Bounding box in EPSG:25832 as 'xmin,ymin,xmax,ymax'. "
                             "Defaults to cfg.by_whole_bbox.")
    args = parser.parse_args()

    run_id = args.run_id
    is_production = not args.debug

    log_path = os.path.join(cfg.log_path, f"vertex_scrap_{run_id}.log")
    open(log_path, "w").close()
    logging.basicConfig(filename=log_path, level=logging.INFO)

    cls = BY()

    if args.bbox:
        bbox = tuple(float(v.strip()) for v in args.bbox.split(","))
    else:
        bbox = cfg.by_whole_bbox

    square_bbox = epsg_transformer.convert_bbox_epsg("25832", "3857", bbox)

    cls.start_with_pool(list(square_bbox), min(8, cfg.cpu_cores) if is_production else 1)
