import os
import shutil
import math
from datetime import date, datetime
import json
import matplotlib.pyplot as plt
import logging

# plt.switch_backend('TkAgg')


# input xmin, ymin, xmax,ymax
# output xmin,yd,
def separate_region_4(bound):
    xd = (bound[0] + bound[2]) / 2
    yd = (bound[1] + bound[3]) / 2
    return [
        [bound[0], bound[1], xd, yd],
        [bound[0], yd, xd, bound[3]],
        [xd, bound[1], bound[2], yd],
        [xd, yd, bound[2], bound[3]]
    ]


def divide_bound(bound, step):
    bounds = []
    count = range(int((bound[3] - bound[1]) / step))
    if len(count) > 1:
        for i in count:
            bounds.append([bound[0], bound[1] + i * step, bound[2], bound[1] + (i + 1) * step])
        if bound[3] - (bound[1] + step * len(count)) > 0:
            bounds.append([bound[0], bound[1] + len(count) * step, bound[2], bound[1] + (len(count) + 1) * step])
    else:
        bound[3] = bound[1] + step
        bounds.append(bound)
    return bounds


def divide_bound_x(bound, step=10000):
    bounds = []
    count = range(int((bound[2] - bound[0]) / step))
    if len(count) > 1:
        for i in count:
            bounds.append([bound[0] + i * step, bound[1], bound[0] + (i + 1) * step, bound[3]])
        if bound[2] - (bound[0] + step * len(count)) > 0:
            bounds.append([bound[0] + len(count) * step, bound[1], bound[0] + (len(count) + 1) * step, bound[3]])
    else:
        bound[2] = bound[0] + step
        bounds.append(bound)
    return bounds


def read_file(uri, json_format=True):
    fin = open(uri)
    data = fin.read()
    if json_format:
        return json.loads(data)
    return data


def is_valid_val(_val):
    if _val is None:
        return False
    if not isinstance(_val, str):
        return True
    _val = _val.strip()
    return _val != '' and _val != 'None'


def get_proxy_list():
    proxies = []
    parent_path = os.path.dirname(os.path.realpath(__file__))
    f = open(parent_path + '/proxies.csv', mode='r', encoding='utf-8')
    lines = f.readlines()
    f.close()
    for line in lines:
        if is_valid_val(line):
            proxies.append(line.strip())
    return proxies


def read_file_by_line(path):
    f = open(path, mode='r', encoding='utf-8')
    lines = f.readlines()
    f.close()
    return lines


def convert_rad_2_deg(value):
    result = []
    for ind_val, val in enumerate(value):
        if ind_val < 4:
            val = val * 180 / math.pi
        result.append(val)
    return result


def convert_deg_2_rad(value):
    result = []
    for ind_val, val in enumerate(value):
        if ind_val < 4:
            val = val * math.pi / 180
        result.append(val)
    return result


def intersects(R1, R2, requiredConvert=False):
    # return True
    # R1 x1,y1,x2,y2
    # R2 a1,b1, a2,b2
    if not requiredConvert:
        if (R1[0] >= R2[2]) or (R1[2] <= R2[0]) or (R1[3] <= R2[1]) or (R1[1] >= R2[3]):
            return False
        else:
            return True
    else:
        # R1 x1,x2,y1,y2
        # R2 a1,a2, b1,b2
        if (R1[0] >= R2[1]) or (R1[1] <= R2[0]) or (R1[3] <= R2[2]) or (R1[2] >= R2[3]):
            return False
        else:
            return True


def contains(r1, r2):
    if r1[0] <= r2[0] < r2[2] <= r1[2] and r1[1] <= r2[1] < r2[3] <= r1[3]:
        print('true')
    else:
        print('false')


def get_path_root(uri, remove_file=True):
    segs = uri.split('/')
    params = []
    for index, seg in enumerate(segs):
        if seg != '.' and seg != '..' and seg != '':
            # if getting only base path without file path, remove last item
            if not (remove_file and index == len(segs) - 1):
                params.append(seg)
    return '/' + '/'.join(params)


def remove(paths):
    if isinstance(paths, list):
        for path in paths:
            remove(path)
    elif os.path.exists(paths):
        if os.path.isdir(paths):
            shutil.rmtree(paths)
        else:
            os.remove(paths)


def check_bounding_volume(bound, input_bound):
    # del bound[-2:]
    return intersects(bound, input_bound)


def divide_chunks(list, n):
    # looping till length l
    return [list[i * n:(i + 1) * n] for i in range((len(list) + n - 1) // n)]


def debug_info(msg):
    logging.info(msg + '-' + datetime.now().strftime("%H:%M:%S"))


def download_json_file(json_data, uri, base_uri):
    if json_data is None:
        return False
    d_file_name = '12'
    if not os.path.exists(base_uri):
        os.mkdir(base_uri)
    file_full_path = base_uri + 'tileset/'
    if not os.path.exists(file_full_path):
        os.mkdir(file_full_path)
    wf = open(base_uri + 'tileset/' + d_file_name, 'w')

    wf.write(json.dumps(json_data))
    wf.close()


def draw_plat_state_zip(path, extension='.zip'):
    if os.path.exists(path):
        filenames = next(os.walk(path))[2]
        for name in filenames:
            file_name, file_extension = os.path.splitext(name)
            if file_extension == extension:
                x, y = map(float, file_name.split("_"))
                plt.scatter(x, y, s=1)

    plt.show()


def draw_plat_state_zip_split(path, extension='.zip', batch_size=1000):
    if not os.path.exists(path):
        print(f"Path does not exist: {path}")
        return

    filenames = [f for f in os.listdir(path) if f.lower().endswith(extension)]
    filenames.sort()  # optional: for consistent order

    total = len(filenames)
    print(f"Found {total} zip files.")

    for batch_start in range(0, total, batch_size):
        batch = filenames[batch_start:batch_start + batch_size]
        xs, ys = [], []

        for name in batch:
            file_name, _ = os.path.splitext(name)
            try:
                x_str, y_str = file_name.split("_")
                x, y = float(x_str), float(y_str)
                xs.append(x)
                ys.append(y)
            except ValueError:
                print(f"Skipped invalid filename: {name}")
                continue

        if xs and ys:
            plt.scatter(xs, ys, s=1)
            plt.title(f"Batch {batch_start // batch_size + 1}")
            # plt.xlabel("X")
            # plt.ylabel("Y")
            plt.show()

            input("Press Enter to continue to the next batch...")  # Wait for user to proceed
        else:
            print("No valid coordinates found in this batch.")


def draw_plot_state(path):
    if os.path.exists(path):
        filenames = next(os.walk(path))[2]
        for name in filenames:
            file_name, file_extension = os.path.splitext(name)
            if file_extension == '.tfw' or file_extension == '.pgw':
                lines = read_file_by_line(path + name)
                x = float(lines[4].rstrip()) / 1000
                y = float(lines[5].rstrip()) / 1000
                print(f'{x},{y}')
                plt.scatter(x, y, s=1)

    plt.scatter(3, 9, s=1000)
    plt.show()


def draw_plat_split_by_filenames(filenames, batch_size=10000000000):
    total = len(filenames)
    print(f"Found {total} files.")
    xs, ys = [], []
    for batch_start in range(0, total, batch_size):
        batch = filenames[batch_start:batch_start + batch_size]
        for name in batch:
            # remove .png and split by "_"
            parts = name.replace(".png", "").split("_")
            xmin, ymin, xmax, ymax = map(float, parts)

            # compute centroid
            xc = (xmin + xmax) / 2
            yc = (ymin + ymax) / 2
            xs.append(xc)
            ys.append(yc)
            continue

        if xs and ys:
            plt.scatter(xs, ys, s=1)
            plt.title(f"Batch {batch_start // batch_size + 1}")
            # plt.xlabel("X")
            # plt.ylabel("Y")
            plt.show()

            input("Press Enter to continue to the next batch...")  # Wait for user to proceed
        else:
            print("No valid coordinates found in this batch.")


def test_draw():
    plt.scatter(1, 1, s=1)


def merge_array_values(arr1, arr2):
    for i in range(0, 3):
        arr1[i] += arr2[i]

    return arr1


def get_header_proxy(referer='', cookie='', host=''):
    headers = {
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,'
                  '*/*;q=0.8, application/signed-exchange;v=b3;q=0.9',
        'Accept-Encoding': 'gzip, deflate, br',
        'Accept-Language': 'en,ko;q=0.9,zh-CN;q=0.8,zh;q=0.7',
        'Cache-Control': 'no-cache',
        'Connection': 'keep-alive',
        'Pragma': 'no-cache',
        # 'sec-ch-ua': HeaderProxyMgr.get_sec_ch_ua(),
        'sec-ch-ua': '"Google Chrome";v="95", "Chromium";v="95", ";Not A Brand";v="99"',
        # 'sec-ch-ua-mobile': HeaderProxyMgr.get_sec_ch_ua_mobile(),
        'sec-ch-ua-mobile': '?0',
        # 'sec-ch-ua-platform': HeaderProxyMgr.get_sec_ch_ua_platform(),
        'sec-ch-ua-platform': '"Windows"',
        'Sec-Fetch-Dest': 'document',
        'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-Site': 'none',
        'Sec-Fetch-User': '?1',
        'Upgrade-Insecure-Requests': '1',
        # 'User-Agent': HeaderProxyMgr.get_User_Agent()
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) '
                      ' Chrome/95.0.4638.69 Safari/537.36'
    }

    if is_valid_val(cookie):
        headers['Cookie'] = cookie

    if is_valid_val(host):
        headers['Host'] = host

    if is_valid_val(referer):
        headers['Referer'] = referer

    return headers


def get_file_list(state, path, extension):
    filenames = [f for f in os.listdir(path) if f.lower().endswith('.' + extension)]
    filenames.sort()  # optional: for consistent order
    with open(state + "_filenames.txt", "w", encoding="utf-8") as f:
        for name in filenames:
            f.write(name + "\n")


def load_file_list(state, flag=False):
    # Load from the file
    filename = state + "_filenames.txt"
    if flag:
        filename = state + ".txt"
    with open(filename, "r", encoding="utf-8") as f:
        loaded_filenames = [line.strip() for line in f]
    # print(len(loaded_filenames))
    draw_plat_split_by_filenames(loaded_filenames)
