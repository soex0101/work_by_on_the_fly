import math
import os
import sys
import threading
import time
from threading import Thread
import random
import requests
import json

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from read_settings import load_config
from common import load_proxies, getCurPath
from utils import debug_info, divide_bound, divide_bound_x, get_header_proxy
import epsg_transformer
from by_get_cookie import get_cookie

import logging

cfg = load_config()
run_id = ""

BASEURL = (
    'https://services.atlas.bayern.de/proxy/wms/map/a29d0fce-6901-43c0-90ed-ac73b7780479?REQUEST=GetMap&SERVICE=WMS'
    '&VERSION=1.1.1&FORMAT=image/png&STYLES=&TRANSPARENT=true&LAYERS=by_alkis_flurkarte_umr_gelb&SRS=EPSG:3857&&WIDTH=4010&HEIGHT=4010'
)

proxy_list = load_proxies()

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
        self.total_count = 0
        self.cookie = ""
        self.cookie_lock = threading.Lock()  # for multiple threading, if one thread trying to get cookie, the other thread process should be wait..
        self.cookie_ready = threading.Event()
        self.max_retries = 100

    def get_proxy(self):
        proxy = None
        if len(proxy_list) > 0:
            proxy = random.choice(proxy_list)
        return {'http': f'http://{proxy}', 'https': f'http://{proxy}'}

    def scraping(self, uri, params=None, formatted='json'):
        if params is None:
            params = {}
        retries = 0

        while retries < self.max_retries:
            try:
                if not self.cookie:
                    if not self.cookie_lock.locked():
                        with self.cookie_lock:
                            print("fetching new cookie")
                            self.cookie = get_cookie()
                            print(self.cookie)
                            self.cookie_ready.set()
                    else:
                        print("waiting for cookie from another thread")
                        self.cookie_ready.wait()

                # print("request anyway???-------------------")
                proxy = self.get_proxy()
                headers = get_header_proxy('services.atlas.bayern.de', self.cookie, 'services.atlas.bayern.de')
                response = requests.get(uri, params, headers=headers, verify=False, proxies=proxy, stream=True)
                response.raise_for_status()

                if formatted == 'json':
                    json_data = json.loads(response.content)
                    return json_data
                return response
            except requests.HTTPError as err:
                print(err.__str__())
                if err.response is not None and err.response.status_code == 401:
                    print("cookie expired, refreshing")
                    with self.cookie_lock:
                        self.cookie = ""
                        self.cookie_ready.clear()
                    retries += 1
                    continue
                else:
                    print(f"request failed: {err}")
                    retries += 1
                    time.sleep(200)

            except Exception as e:
                retries += 1
                print(e.__str__())
                print("Unexpected error---------------------------------")
                time.sleep(200)

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

    def start_with_thread(self, bound, count=3):
        self.initialize()
        step = math.ceil((bound[3] - bound[1]) / count)
        bounds = divide_bound(bound, step)
        threads = []
        for i in range(0, count):
            p = '_'.join([str(i) for i in bounds[i]])
            thread = Thread(target=self.start, args=(p,))
            thread.start()
            time.sleep(1)
            threads.append(thread)
        for thread in threads:
            thread.join()

    def start(self, bound_str):
        path = os.path.join(cfg.vertex_path, run_id)
        # resp = self.scraping(BASEURL2,None,'xml')
        # f = open(path + 'cap.xml','wb')
        # f.write(resp)
        # f.close()
        # exit()
        done_list = {}
        bound_ps = bound_str.split('_')
        bounds = []
        for bound1 in bound_ps:
            bounds.append(float(bound1))
        # bounds = [990000, 5960000, 1556000, 6540000]
        # bounds = [1200000, 6060000, 1556000, 6540000]
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
                        #print('skip---')
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
                        time.sleep(0.5)
                    except Exception as e:
                        log_file.write("wrong=" + bbox + "\n")
                        log_file.flush()
                        continue
        print('scrap done--------------')


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python scrap.py <id>")
        print('Example command format: "python scrap.py 20251113_050000"')
        sys.exit(1)

    run_id = sys.argv[1]

    log_path = os.path.join(cfg.log_path, f"vertex_scrap_{run_id}.log")
    open(log_path, "w").close()
    logging.basicConfig(filename=log_path, level=logging.INFO)

    cls = BY()

    square_bbox = epsg_transformer.convert_bbox_epsg("25832", "3857", cfg.by_whole_bbox)

    cls.start_with_thread(list(square_bbox), cfg.cpu_cores)
