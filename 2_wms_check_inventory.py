###########################################
# It is deprecated
# Get differing parcel using WMS
###########################################
import psycopg2
import time
import asyncio
import aiohttp
import random
from aiohttp import ClientSession, client_exceptions
from decimal import Decimal
from seleniumwire import webdriver
from selenium.webdriver import Firefox
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.firefox.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.by import By
import random
import common
from read_settings import load_config
import sys
import os

os.environ["MOZ_HEADLESS"] = "1"   # ← forces Firefox to never use X11

limit = 2
proxy_list = common.load_proxies()
cfg = None
db = None
cookie_list = []
parcel_table_name = "by_parcel"

def get_cookie():
    url = 'https://geodatenonline.bayern.de/geodatenonline/anwendungen3/flurkartenauszug'
    
    print('getting cookie')
    # Set up Firefox options
    firefox_options = Options()
    firefox_options.binary_location = cfg.firefox_path
    firefox_options.add_argument("--headless")
    firefox_options.add_argument("--disable-extensions")

    firefox_options.set_preference("browser.tabs.remote.autostart", False)
    firefox_options.set_preference("toolkit.printing.use_system_dialog", False)
    firefox_options.set_preference("layers.acceleration.disabled", True)
    firefox_options.set_preference("general.useragent.override",
                               "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:134.0) Gecko/20100101 Firefox/134.0")
    
    firefox_options.add_argument("--width=1920")
    firefox_options.add_argument("--height=1080")
    firefox_options.set_preference("general.useragent.override", "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:134.0) Gecko/20100101 Firefox/134.0")

    # Set up the Firefox service
    firefox_service = Service(executable_path=cfg.gecko_path)

    try:
        proxy_ip = random.choice(proxy_list)
        proxy_options = {
                'proxy': {
                    'http': 'https://' + proxy_ip,
                    'https': 'https://' + proxy_ip
                },
                'connection_timeout': 300
            }
        driver = webdriver.Firefox(service=firefox_service, seleniumwire_options=proxy_options, options=firefox_options)

        # Open the URL
        driver.get(url)
        driver.set_window_size(1920, 2000)
        
        wait = WebDriverWait(driver, 30)

        close_button = wait.until(EC.element_to_be_clickable((By.XPATH, '/html/body/div[1]/div[6]/div[2]/div/div/div[2]/div/div/div[8]/p/a')))
        close_button.click()
        print('close_button')
        
        cookie = None
        for req_idx, request in enumerate(driver.requests):
            if request.url.startswith('https://geodatenonline.bayern.de/gdoflurkarte2'):
                cookie = request.headers['Cookie']
        try:
            driver.close()
        except: pass
        try:
            driver.quit()
        except: pass
        if cookie is not None:
            return cookie
    
    except Exception as e:
        print('Exception: ', str(e))
        try:
            driver.close()
        except: pass
        try:
            driver.quit()
        except: pass

def prepare_tables():
    with db.cursor() as cursor:
        cursor.execute(f"""
            CREATE TABLE IF NOT EXISTS by_bbox_sync_parcel_changed_{run_id} (
                id VARCHAR(255) PRIMARY KEY,
                rw VARCHAR(255),
                hw VARCHAR(255),
                CONSTRAINT parcel_changed_unique_{run_id} UNIQUE (rw, id)
            );
            
            CREATE TABLE IF NOT EXISTS by_bbox_sync_parcel_not_changed_{run_id} AS
            SELECT * FROM {parcel_table_name}
            WITH NO DATA;
            
            DELETE FROM by_bbox_sync_parcel_changed_{run_id};
            DELETE FROM by_bbox_sync_parcel_not_changed_{run_id};
        """)
        
        db.commit()


def is_float_same(float1, float2, precision='0.01'):
    precision = Decimal(precision)
    float1 = Decimal(float1)
    float2 = Decimal(float2)
    if float1.quantize(precision) == float2.quantize(precision):
        return True
    else:
        return False
def is_bbox_same(bbox_as_list, bbox_as_box2d):
    
    box2d_as_list = bbox_as_box2d.replace('BOX(', '').replace(')', '').replace(' ', ',').split(',')
    i = 0
    for coord in bbox_as_list:
        if is_float_same(coord, box2d_as_list[i]) is False:
            return False
        i += 1
    return True

def bbox_intersects(bbox_ele, bbox_setting):
    ax1, ay1, ax2, ay2 = bbox_ele
    bx1, by1, bx2, by2 = bbox_setting
    # (optional) sanity check
    if ax1 > ax2 or ay1 > ay2 or bx1 > bx2 or by1 > by2:
        raise ValueError("Invalid bbox: expected (minx, miny, maxx, maxy)")
    # no-gap 2D overlap test
    return not (ax2 < bx1 or bx2 < ax1 or ay2 < by1 or by2 < ay1)

async def get(obj, url, session):
    #'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
    header = {  
                'Host': 'geodatenonline.bayern.de',
                'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'
                }
    
    error_count = 0
    proxy = 'http://{0}'.format(str(random.choice(proxy_list)))

    while error_count < 3:
        header['Cookie'] = random.choice(cookie_list)
        try:
            async with session.get(url, headers=header, proxy=proxy) as resp:
                if resp.status == 403:
                    cookie_list.remove(header['Cookie'])
                    cookie = get_cookie()
                    cookie_list.append(cookie)
                    header['Cookie'] = cookie
                    raise Exception('403res')
                elif resp.status != 200:
                    proxy = 'http://{0}'.format(str(random.choice(proxy_list)))
                    raise Exception('no200')
                resp = await resp.json()
                with db.cursor() as cursor:
                    for ele in resp['results']:
                        id = ele['id']
                        gml_id = ele['gmlid']
                        boundary_id = ele['gmkgnr']
                        name = ele['flstnr']

                        try:
                            numerator = name.split('/')[0]
                            denominator = name.split('/')[1]
                        except:
                            numerator = name
                            denominator = None

                        if denominator is None:
                            parcel_id = boundary_id + '000' + numerator.zfill(5) + '000000'
                        else:
                            parcel_id = boundary_id + '000' + numerator.zfill(5) + denominator.zfill(4) + '00'
                        if ((parcel_dict.get(parcel_id, None) is not None) and (parcel_dict[parcel_id]['processed'] is True)):
                            continue

                        rw_pointonsurface_x = ele['rw']
                        hw_pointonsurface_y = ele['hw']
                        ewkt_bbox = ele['bbox']
                        if parcel_id in parcel_dict and (is_bbox_same(ewkt_bbox, parcel_dict[parcel_id]['bbox']) is True) and (is_float_same(rw_pointonsurface_x, parcel_dict[parcel_id]['rw'], '0.1') is True) and (is_float_same(hw_pointonsurface_y, parcel_dict[parcel_id]['hw'], '0.1') is True) and (gml_id == parcel_dict[parcel_id]['gml_id']) and (name == parcel_dict[parcel_id]['name']) and (boundary_id == parcel_dict[parcel_id]['boundary_id']):
                            sql = f"INSERT INTO by_bbox_sync_parcel_not_changed_{run_id} SELECT * FROM {parcel_table_name} WHERE id = %s"
                            cursor.execute(sql, (parcel_id,))
                            parcel_dict[parcel_id]['processed'] = True
                        elif bbox_intersects(ele['bbox'], cfg.input_bbox):
                            sql = f"INSERT INTO by_bbox_sync_parcel_changed_{run_id} (rw, hw, id) VALUES (%s, %s, %s)"
                            cursor.execute(sql, (rw_pointonsurface_x, hw_pointonsurface_y, str(id)))

                            parcel_dict[parcel_id] = {
                                'processed': True
                            }
                    db.commit()
                return
        except Exception as e:
            db.rollback()
            print('Exception: ', str(e))
            error_count += 1
            print('EXC', str(e), obj['id'], str(e))
    return

async def bound_fetch(sem, row, url, session):
    async with sem:
        return await get(row, url, session)


async def main():
    sem = asyncio.Semaphore(limit)
    session_timeout = aiohttp.ClientTimeout(total=3)
    conn = aiohttp.TCPConnector(ssl=False, limit=limit+1)
    tasks = []
    async with ClientSession(connector=conn, timeout=session_timeout) as session:
        for parcel_id in parcel_dict.keys():
            if parcel_dict[parcel_id]['processed'] is True: continue
            url = 'https://geodatenonline.bayern.de/gdoflurkarte2/rest/flurstueck/{0}%20{1}?&srid=25832'.format(parcel_dict[parcel_id]['boundary_id'], parcel_dict[parcel_id]['name'].replace('/', '-'))
            task = asyncio.ensure_future(bound_fetch(sem, parcel_dict[parcel_id], url, session))
            tasks.append(task)
        await asyncio.gather(*tasks)


if __name__ == "__main__":

    if len(sys.argv) < 2:
        print("Usage: python 2_by_check_inventory.py <id>")
        print('Example command format: "python 2_by_check_inventory.py by_101125"')
        sys.exit(1)

    run_id = sys.argv[1]
    cfg = load_config(run_id)

    db = psycopg2.connect(  user=cfg.user,
                            password=cfg.password,
                            host=cfg.host,
                            port=cfg.port,
                            database=cfg.dbname)

    prepare_tables()

    stmt = f"""
        SELECT id, gml_id, boundary_id, name, BOX2D(geom), ST_X(ST_POINTONSURFACE(geom)), ST_Y(ST_POINTONSURFACE(geom)) 
        FROM {parcel_table_name}
        WHERE geom && ST_MakeEnvelope(%s, %s, %s, %s, 25832)
    """

    parcel_dict = {}
    with db.cursor() as cursor:
        cursor.execute(stmt, cfg.bbox)
        q = cursor.fetchall()

    print(f"{len(q)} data processing...")

    for row in q:
        parcel_dict[row[0]] = { 'id': row[0], 
                                'gml_id': row[1], 
                                'boundary_id': row[2],
                                'name': row[3],
                                'bbox': row[4],
                                'rw': row[5],
                                'hw': row[6],
                                'processed': False
                            }
        
    while len(cookie_list) < limit:
        cookie = get_cookie()
        if cookie is not None:
            cookie_list.append(cookie)
            print("cookie: ", cookie)
        else:
            print("cookie is None")

    loop = asyncio.get_event_loop()
    future = asyncio.ensure_future(main())
    loop.run_until_complete(future)

    print('Parcel difference check done.')

    # Check parcel difference detected
    query = f"""
        SELECT COUNT(*) FROM by_bbox_sync_parcel_changed_{run_id}
    """
    with db.cursor() as cursor:
        cursor.execute(query, cfg.bbox)
        q = cursor.fetchall()

    db.commit()
    db.close()

    difference_count = q[0][0]
    ret_value = 1 if difference_count > 0 else 0

    sys.exit(ret_value)