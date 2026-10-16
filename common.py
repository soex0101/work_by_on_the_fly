import os
import sys
from typing import List, Any
from read_settings import load_config
from urllib.parse import urljoin
import psycopg2
import re
import requests
from collections import Counter


def getCurPath():
    return os.path.dirname(os.path.realpath(__file__))


# ----------------------------
# FAILED SERVICE REQUESTS
# A request that still fails after its last retry is recorded with
# request_error(); exit_if_failed_requests() then prints them, marks the job
# FAILED in qgis_api's request_tracking table and exits 1, so run_bbox.py stops at
# that script.
# ----------------------------
# Error text per script; each script calls one service
SERVICE_ERROR_TEXTS = {
    'building_color/runtime.py': 'Failed to get building color',
    'vertex/runtime.py': 'Failed to get vertex',
    'dashed_detection/download_wms.py': 'Failed to detect dashed line',
    'labeling/prepare_building.py': 'Failed to get house number from address',
    'labeling/prepare_parcel_street.py': 'Failed to get street name and center line',
    'road_classification.py': 'Failed to get road classification',
}

# qgis_api's job states (alkis_scraper.request_tracking), in the same PostgreSQL as the run databases
TRACKING_DBNAME = 'alkis_scraper'
# merge_qgs runs run_bbox.py with run id qgs_<job id>_<request_tracking.tracking_id>
TRACKING_RUN_ID_PREFIX = 'qgs_'


def mark_request_failed(run_id, error_message, failed_count=None, overwrite=True):
    """Set this request's request_tracking row to FAILED. With overwrite=False an existing
    FAILED (with its failed requests) is kept. Runs not started by merge_qgs
    have no request_tracking row and are skipped."""
    tracking_id = run_id.rsplit('_', 1)[-1]
    if not run_id.startswith(TRACKING_RUN_ID_PREFIX) or not tracking_id.isdigit():
        return
    try:
        cfg = load_config(run_id)
        connection = psycopg2.connect(host=cfg.host, port=cfg.port, user=cfg.user,
                                      password=cfg.password, dbname=TRACKING_DBNAME)
        c = connection.cursor()
        query = """
            UPDATE request_tracking
            SET state = 'FAILED', error_message = %s, failed_count = %s, finished_at = NOW()
            WHERE tracking_id = %s
        """
        if not overwrite:
            query += " AND state IS DISTINCT FROM 'FAILED'"
        c.execute(query, (error_message, failed_count, int(tracking_id)))
        connection.commit()
        connection.close()
    except Exception as e:
        print(f'[WARN] Could not set request_tracking state FAILED for {run_id}: {e}')

def classify_request_error(error, status):
    """Category of a failed request for the error message"""
    # Before the connection checks: ProxyError is a ConnectionError
    if isinstance(error, requests.exceptions.ProxyError) or status == 407:
        return 'Proxy issue'
    if status is not None:
        return 'Service is unavailable' if status >= 500 else 'Service error'
    # Selenium's TimeoutException is not a requests/builtin timeout
    if isinstance(error, (requests.exceptions.Timeout, requests.exceptions.ConnectionError, TimeoutError, ConnectionError)) \
            or 'Timeout' in type(error).__name__:
        return 'Network issue'
    return 'Other error'


def request_error(error, url):
    """Return {'url', 'status', 'message', 'category'} for a request that failed with error."""
    response = getattr(error, 'response', None)
    status = response.status_code if response is not None else None
    message = str(error) or type(error).__name__
    if response is not None and response.text:
        message += ': ' + response.text[:300]
    return {'url': url, 'status': status, 'message': message, 'category': classify_request_error(error, status)}


def failed_requests_error_message(error_text, failed_requests):
    """'<error text>: <n> failed (Service is unavailable x3 [503 x3], Network issue x1)'"""
    statuses_by_category = {}
    for r in failed_requests:
        statuses_by_category.setdefault(r['category'], Counter())[r['status']] += 1
    parts = []
    for category, statuses in sorted(statuses_by_category.items(), key=lambda item: -sum(item[1].values())):
        part = '{0} x{1}'.format(category, sum(statuses.values()))
        codes = ', '.join('{0} x{1}'.format(status, count) for status, count in statuses.most_common() if status is not None)
        if codes:
            part += ' [{0}]'.format(codes)
        parts.append(part)
    return '{0}: {1} failed ({2})'.format(error_text, len(failed_requests), ', '.join(parts))


def exit_if_failed_requests(script_name, failed_requests, run_id):
    if not failed_requests:
        return
    print('============= Failed requests: ' + script_name + ' =============')
    print('Failed URLs: ' + str(len(failed_requests)))
    for r in failed_requests:
        print('  status=' + str(r['status']) + '  ' + r['category'] + '  url=' + r['url'])
        print('    message: ' + r['message'].replace('\n', ' '))
    print('============= ============')

    error_message = failed_requests_error_message(SERVICE_ERROR_TEXTS.get(script_name, script_name), failed_requests)
    print('Error message: ' + error_message)
    mark_request_failed(run_id, error_message, len(failed_requests))
    sys.exit(1)

# Load proxies from CSV file
def load_proxies(cfg=None):
    if cfg is None:
        cfg = load_config()

    file_path = os.path.join(getCurPath(), cfg.proxy_file)
    proxies = []
    if not os.path.exists(file_path):
        print(f"Proxy file not found: {file_path}")
        return proxies
    with open(file_path, 'r') as f:
        for line in f:
            line = line.strip()
            if line:
                proxies.append(line)
    return proxies

# ----------------------------
# PROXY + COOKIE HELPERS (Bayern cadastral REST API)
# ----------------------------
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def parse_proxy(proxy_line):
    """
    Parse a proxy line. proxies.txt format is 'host:port:user:pass'
    (plain 'host:port' is also accepted). Returns (host, port, user, pass);
    user/pass are None when there are no credentials.
    """
    parts = proxy_line.split(":")
    host = parts[0]
    port = parts[1]
    username = parts[2] if len(parts) > 2 else None
    password = parts[3] if len(parts) > 3 else None
    return host, port, username, password


def requests_proxy_url(proxy_line):
    """Build a 'http://user:pass@host:port' URL for the requests library."""
    host, port, username, password = parse_proxy(proxy_line)
    if username is not None:
        return f"http://{username}:{password}@{host}:{port}"
    return f"http://{host}:{port}"


def playwright_proxy(proxy_line):
    """
    Build the proxy dict Playwright expects. Playwright does NOT accept
    credentials inside the server URL, so username/password are kept separate.
    """
    host, port, username, password = parse_proxy(proxy_line)
    proxy = {"server": f"http://{host}:{port}"}
    if username is not None:
        proxy["username"] = username
        proxy["password"] = password
    return proxy


def get_cookie(proxy_line, chromium_path, launcher_url):
    """
    Mint a session cookie for the cadastral REST API using a headless browser.

    Reproduces the manual flow: open the launcher page through `proxy_line`,
    follow the "Continue" link, then hand off to /gdoflurkarte2/. Returns the
    collected cookies as a single "name=value; ..." header string, or None if
    minting failed.
    """
    from playwright.sync_api import sync_playwright
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                executable_path=chromium_path,
                args=["--disable-dev-shm-usage", "--no-sandbox"],
                timeout=30000,
            )
            context = browser.new_context(
                proxy=playwright_proxy(proxy_line),
                user_agent=USER_AGENT,
                locale="de-DE",
                viewport={"width": 1920, "height": 1080},
            )
            page = context.new_page()

            # 1) launcher page
            page.goto(launcher_url, wait_until="domcontentloaded", timeout=45000)

            # 2) read the "Continue" link (don't click, just follow its href)
            link = page.locator("a").filter(
                has_text=re.compile(r"Weiter\s+zur\s+Datenbestellung", re.I)
            ).first
            link.wait_for(state="attached", timeout=30000)
            href = link.get_attribute("href")
            if not href:
                print("get_cookie: continue link has no href")
                browser.close()
                return None

            # 3) follow it, then hand off to the map app
            page.goto(urljoin(page.url, href), wait_until="domcontentloaded", timeout=45000)
            page.goto("https://geodatenonline.bayern.de/gdoflurkarte2/",
                      wait_until="domcontentloaded", timeout=45000)

            cookies = context.cookies([
                "https://geodatenonline.bayern.de/",
                "https://geodatenonline.bayern.de/gdoflurkarte2/",
            ])
            browser.close()

            cookie_header = "; ".join(
                f"{c['name']}={c['value']}"
                for c in cookies
                if c.get("name") and c.get("value") is not None
            )
            return cookie_header or None

    except Exception as e:
        print(f"get_cookie failed via {proxy_line}: {e}")
        return None


# Proxy index of the last successful cookie mint; the next mint_cookie()
# call starts from this proxy.
_mint_proxy_start_index = 0


def mint_cookie(proxies, chromium_path, launcher_url, retries_per_proxy=3):
    """Mint a cookie, trying proxies round-robin starting from the last one
    that worked; `retries_per_proxy` attempts per proxy."""
    global _mint_proxy_start_index
    for offset in range(len(proxies)):
        proxy_index = (_mint_proxy_start_index + offset) % len(proxies)
        proxy_line = proxies[proxy_index]
        for attempt in range(1, retries_per_proxy + 1):
            cookie = get_cookie(proxy_line, chromium_path, launcher_url)
            if cookie:
                print(f"minted cookie via {proxy_line}")
                _mint_proxy_start_index = proxy_index
                return cookie
            print(f"cookie mint failed (proxy {proxy_index + 1}/{len(proxies)}, "
                  f"attempt {attempt}/{retries_per_proxy})")
    raise RuntimeError("Could not mint a cookie from any proxy")

def create_db(cfg, db_name):
    try:
        # Create DB
        conn_param = {
            "dbname": f"postgres",
            "user": cfg.user,
            "password": cfg.password,
            "host": cfg.host,
            "port": cfg.port
        }
        conn = psycopg2.connect(**conn_param)
        conn.autocommit = True

        cur = conn.cursor()

        # check db already exist
        print(f"Creating DB: {db_name}")
        cur.execute(f"SELECT 1 FROM pg_database WHERE datname = %s;", (db_name,))
        exists = cur.fetchone()

        if not exists:
            # Create the database
            cur.execute(f"CREATE DATABASE {db_name};")

            print(f"Database '{db_name}' created successfully!")
        else:
            print(f"Database '{db_name}' already created.")

        cur.close()
        conn.close()

        # Create postgis extension
        conn_param["dbname"] = db_name
        conn = psycopg2.connect(**conn_param)
        conn.autocommit = True
        cur = conn.cursor()

        cur.execute("CREATE EXTENSION IF NOT EXISTS postgis;")
        print(f"PostGIS extension enabled on '{db_name}'.")

        cur.close()
        conn.close()

    except Exception as e:
        print("Error:", e)
