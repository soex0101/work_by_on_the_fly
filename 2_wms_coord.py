###########################################
# It is deprecated
# Get parcel geoms using WMS
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
import sys
from read_settings import load_config

limit = 1
proxy_list = common.load_proxies()
cfg = None
run_id = ""
db = None
cookie_list = []
parcel_table_name = "by_parcel"

def get_cookie():
    url = 'https://geodatenonline.bayern.de/geodatenonline/anwendungen3/flurkartenauszug'

    print('getting cookie')
    # Set up Firefox options
    firefox_options = Options()
    firefox_options.binary_location = cfg.firefox_path
    firefox_options.add_argument("--disable-extensions")
    firefox_options.add_argument("--headless")
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
            CREATE TABLE IF NOT EXISTS by_bbox_sync_parcel_new_{run_id} AS
            SELECT * FROM {parcel_table_name}
            WITH NO DATA;

            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1
                    FROM pg_constraint
                    WHERE conname = 'by_bbox_sync_parcel_new_pkey_{run_id}'
                ) THEN
                    ALTER TABLE by_bbox_sync_parcel_new_{run_id}
                    ADD CONSTRAINT by_bbox_sync_parcel_new_pkey_{run_id} PRIMARY KEY (id);
                END IF;
            END $$;

            DELETE FROM by_bbox_sync_parcel_new_{run_id};
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

async def get(obj, url, session):

    header = {  
            'Host': 'geodatenonline.bayern.de',
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 14.7; rv:134.0) Gecko/20100101 Firefox/134.0',
            }

    print('trying:', url)
    error_count = 0
    proxy = 'http://{0}'.format(str(random.choice(proxy_list)))
    while error_count < 3:
        error_count += 1
        header['Cookie'] = random.choice(cookie_list)
        try:
            async with session.get(url, headers=header, proxy=proxy) as resp:
                if resp.status == 403:
                    cookie_list.remove(header['Cookie'])
                    cookie_value = get_cookie()
                    if cookie_value is not None:
                        cookie_list.append(cookie_value)
                    header['Cookie'] = random.choice(cookie_list)
                    raise Exception('403res')
                elif resp.status == 500:
                    print('500response:', url)
                    return
                elif resp.status != 200:
                    print(resp.status)
                    print(url)
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


                        rw_pointonsurface_x = ele['rw']
                        hw_pointonsurface_y = ele['hw']
                        geom = ele['ewkt']

                        print(parcel_id, geom)

                        sql = f"INSERT INTO by_bbox_sync_parcel_new_{run_id} (id, boundary_id, corridor, numerator, denominator, name, official_area, gml_id, geom) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, ST_GeomFromEWKT(%s))"
                        cursor.execute(sql, (parcel_id, boundary_id, None, numerator, denominator, name, None, gml_id, str(geom)))
                        
                    db.commit()
                return
        except Exception as e:
            print('EXC2', str(e))
            error_count += 1
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
        for row in q:
            rw = row[0]
            hw = row[1]
            url = 'https://geodatenonline.bayern.de/gdoflurkarte2/rest/flurstueck/koor/{0}/{1}?'.format(rw, hw)
            task = asyncio.ensure_future(bound_fetch(sem, row, url, session))
            tasks.append(task)
        await asyncio.gather(*tasks)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python 2_by_coord.py <id>")
        print('Example command format: "python 2_by_coord.py by_101125"')
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
        SELECT rw, hw
        FROM by_bbox_sync_parcel_changed_{run_id}
    """

    with db.cursor() as cursor:
        cursor.execute(stmt)
        q = cursor.fetchall()
    print(f"{len(q)} data processing...")

    if len(q) == 0:
        print('No changes')
    else:
        while len(cookie_list) < limit:
            cookie_value = get_cookie()
            if cookie_value is not None:
                cookie_list.append(cookie_value)
                print("cookie: ", cookie_value)
            else:
                print("cookie is None")

        loop = asyncio.get_event_loop()
        future = asyncio.ensure_future(main())
        loop.run_until_complete(future)
        db.commit()
        db.close()

        print('Done')
