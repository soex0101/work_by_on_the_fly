#!/usr/bin/env python3
import os
import time
import random
import logging
import asyncio
import threading
import queue
from typing import Optional, Dict, Tuple, List

import psycopg2
import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool

import aiohttp
from aiohttp import ClientSession, client_exceptions

from playwright.sync_api import sync_playwright
from urllib.parse import urljoin

# ----------------------------
# optional shapely (for runtime in-memory "contains" checks)
# ----------------------------
try:
    from shapely import wkt as shapely_wkt
    from shapely.geometry import Point as ShapelyPoint
    SHAPELY_OK = True
except Exception:
    SHAPELY_OK = False


# ----------------------------
# CONFIG
# ----------------------------
CONCURRENCY = int(os.getenv("CONCURRENCY", "12"))
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "6"))

DB_DSN = os.getenv("DB_DSN", "user=alkis password=mXV5UJBVfP9m83 host=localhost port=5432 dbname=by_cadastral")
CHROMIUM_PATH = os.getenv("CHROMIUM_PATH", "/usr/bin/google-chrome")

LAUNCHER_URL = "https://geodatenonline.bayern.de/geodatenonline/anwendungen3/flurkartenauszug"
CONTINUE_XPATH = "/html/body/div[1]/div[6]/div[2]/div/div/div[2]/div/div/div[8]/p/a"
REST_URL_TMPL = "https://geodatenonline.bayern.de/gdoflurkarte2/rest/flurstueck/koor/{rw}/{hw}?"

PROXY_LIST: List[Dict[str, str]] = [
    {
        "host": "dc.decodo.com",
        "port": "10000",
        "username": "user-spbqgcluj9-country-ro",
        "password": "8AneDb7mh2gB8Dttg=",
    },
]

BASE_HEADERS = {
    "Host": "geodatenonline.bayern.de",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "X-Requested-With": "XMLHttpRequest",
    "Origin": "https://geodatenonline.bayern.de",
    "Referer": LAUNCHER_URL,
}

LOGFILE = time.strftime("fetch_by_coords_confirm-%Y-%m-%d.log")

logging.basicConfig(
    level=logging.INFO,
    filename=LOGFILE,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("fetch_by_coords_confirm")
console = logging.StreamHandler()
console.setLevel(logging.INFO)
console.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
log.addHandler(console)

if not SHAPELY_OK:
    log.warning("Shapely not available -> runtime (uncommitted) contains-check disabled; DB-only coverage check will be used.")


# ----------------------------
# PROXY HELPERS
# ----------------------------
def proxy_key(p: Dict[str, str]) -> str:
    return f"{p['host']}:{p['port']}|{p.get('username','')}|{p.get('password','')}"


def proxy_to_aiohttp_url(p: Dict[str, str]) -> str:
    u = p.get("username")
    pw = p.get("password")
    if u and pw:
        return f"http://{u}:{pw}@{p['host']}:{p['port']}"
    return f"http://{p['host']}:{p['port']}"


def proxy_to_playwright_proxy(p: Dict[str, str]) -> Dict[str, str]:
    d = {"server": f"http://{p['host']}:{p['port']}"}
    if p.get("username"):
        d["username"] = p["username"]
    if p.get("password"):
        d["password"] = p["password"]
    return d


def pick_proxy() -> Dict[str, str]:
    return random.choice(PROXY_LIST)


# ----------------------------
# PLAYWRIGHT COOKIE MINT
# ----------------------------
def _cookies_to_header(cookies: List[dict]) -> str:
    parts = []
    for c in cookies:
        n = c.get("name")
        v = c.get("value")
        if n and v is not None:
            parts.append(f"{n}={v}")
    return "; ".join(parts)


def mint_cookie_header_blocking(proxy_entry: Dict[str, str]) -> Optional[str]:
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                executable_path=CHROMIUM_PATH,
                args=["--disable-dev-shm-usage", "--no-sandbox"],
                timeout=30000,
            )

            ctx = browser.new_context(
                proxy=proxy_to_playwright_proxy(proxy_entry),
                user_agent=BASE_HEADERS["User-Agent"],
                locale="de-DE",
                viewport={"width": 1600, "height": 900},
            )
            page = ctx.new_page()

            page.goto(LAUNCHER_URL, wait_until="domcontentloaded", timeout=45000)
            try:
                page.wait_for_load_state("networkidle", timeout=12000)
            except Exception:
                pass

            loc = page.locator(f"xpath={CONTINUE_XPATH}").first
            loc.wait_for(state="attached", timeout=30000)

            href = loc.get_attribute("href")
            if not href:
                log.warning("cookie-mint: continue link has no href")
                try:
                    page.screenshot(path=f"/tmp/gdo_no_href_{int(time.time())}.png", full_page=True)
                except Exception:
                    pass
                ctx.close()
                browser.close()
                return None

            cont_url = urljoin(page.url, href)
            page.goto(cont_url, wait_until="domcontentloaded", timeout=45000)
            try:
                page.wait_for_load_state("networkidle", timeout=12000)
            except Exception:
                pass

            try:
                page.goto("https://geodatenonline.bayern.de/gdoflurkarte2/", wait_until="domcontentloaded", timeout=45000)
            except Exception:
                pass

            cookies = ctx.cookies(
                [
                    "https://geodatenonline.bayern.de/",
                    "https://geodatenonline.bayern.de/gdoflurkarte2/",
                ]
            )
            cookie_header = _cookies_to_header(cookies)

            ctx.close()
            browser.close()

            if not cookie_header:
                log.warning("cookie-mint: empty cookie header")
                return None
            return cookie_header

    except Exception as e:
        log.warning("cookie-mint failed (proxy=%s): %s", proxy_key(proxy_entry), e)
        return None


class CookieBank:
    def __init__(self):
        self._lock = asyncio.Lock()
        self._cookie_by_proxykey: Dict[str, str] = {}

    async def get_cookie(self, proxy_entry: Dict[str, str]) -> str:
        k = proxy_key(proxy_entry)
        async with self._lock:
            if k in self._cookie_by_proxykey:
                return self._cookie_by_proxykey[k]

        cookie = await asyncio.to_thread(mint_cookie_header_blocking, proxy_entry)
        if not cookie:
            raise RuntimeError(f"Could not mint cookie for proxy={k}")

        async with self._lock:
            self._cookie_by_proxykey[k] = cookie
        return cookie

    async def invalidate(self, proxy_entry: Dict[str, str]) -> None:
        k = proxy_key(proxy_entry)
        async with self._lock:
            self._cookie_by_proxykey.pop(k, None)


# ----------------------------
# RUNTIME (UNCOMMITTED) GEOM INDEX (NEW)
# ----------------------------
class RuntimeGeomIndex:
    """
    Stores fetched polygons immediately (before DB commit) so we can skip GETs for covered points.
    Exact check using shapely.contains(point).
    """
    def __init__(self):
        self._lock = asyncio.Lock()
        self._geoms = []  # list[shapely geometry]
        self._enabled = SHAPELY_OK

    async def add_wkt(self, geom_wkt: str) -> None:
        if not self._enabled:
            return
        try:
            g = shapely_wkt.loads(geom_wkt)
        except Exception:
            return
        async with self._lock:
            self._geoms.append(g)

    async def contains_point(self, x: float, y: float) -> bool:
        if not self._enabled:
            return False
        pt = ShapelyPoint(float(x), float(y))
        async with self._lock:
            # Simple linear scan is OK because "newly fetched" geoms are typically far fewer than rows.
            # If this grows large, we can switch to STRtree rebuild, but start simple & correct.
            for g in self._geoms:
                try:
                    if g.contains(pt):
                        return True
                except Exception:
                    continue
        return False


# ----------------------------
# DB IO
# ----------------------------
READ_STMT = """
SELECT t.rw, t.hw, t.id
FROM public.by_temp t
WHERE t.geom IS NOT NULL
  AND NOT EXISTS (
    SELECT 1
    FROM public.by_parcel_confirmed p
    WHERE p.geom && t.geom
      AND ST_Within(t.geom, p.geom)
  )
ORDER BY t.id
"""

INSERT_STMT = """
INSERT INTO public.by_parcel_confirmed
  (id, boundary_id, corridor, numerator, denominator, name, official_area, gml_id, geom)
VALUES
  (%s, %s, %s, %s, %s, %s, %s, %s, ST_SetSRID(ST_GeomFromText(%s), 25832))
ON CONFLICT (id) DO NOTHING
"""

COVER_CHECK_SQL = """
SELECT 1
FROM public.by_parcel_confirmed p
WHERE p.geom && ST_SetSRID(ST_Point(%s, %s), 25832)
  AND ST_Contains(p.geom, ST_SetSRID(ST_Point(%s, %s), 25832))
LIMIT 1
"""


def ensure_confirmed_geom_index() -> None:
    conn = psycopg2.connect(DB_DSN)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE INDEX IF NOT EXISTS by_parcel_confirmed_geom_gix ON public.by_parcel_confirmed USING GIST (geom)")
    finally:
        conn.close()


def point_is_covered(pool: ThreadedConnectionPool, x: float, y: float) -> bool:
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(COVER_CHECK_SQL, (x, y, x, y))
            return cur.fetchone() is not None
    finally:
        pool.putconn(conn)


def db_stream_rows():
    conn = psycopg2.connect(DB_DSN)
    conn.autocommit = False
    cur = conn.cursor(name="by_temp_stream")
    cur.itersize = 5000
    cur.execute(READ_STMT)
    try:
        while True:
            rows = cur.fetchmany(5000)
            if not rows:
                break
            for r in rows:
                yield r
    finally:
        try:
            conn.rollback()
        except Exception:
            pass
        try:
            cur.close()
        except Exception:
            pass
        conn.close()


class DBWriter(threading.Thread):
    def __init__(self, q: "queue.Queue[Optional[Tuple]]", batch_size: int = 1000):
        super().__init__(daemon=True)
        self.q = q
        self.batch_size = batch_size
        self.buf: List[Tuple] = []
        self.stop_flag = False

    def flush(self, cur):
        if not self.buf:
            return
        psycopg2.extras.execute_batch(cur, INSERT_STMT, self.buf, page_size=500)
        self.buf.clear()

    def run(self):
        conn = psycopg2.connect(DB_DSN)
        conn.autocommit = False
        try:
            with conn.cursor() as cur:
                last_flush = time.time()
                while not self.stop_flag:
                    try:
                        item = self.q.get(timeout=0.5)
                    except queue.Empty:
                        if self.buf and (time.time() - last_flush) > 2:
                            self.flush(cur)
                            conn.commit()
                            last_flush = time.time()
                        continue

                    if item is None:
                        self.flush(cur)
                        conn.commit()
                        last_flush = time.time()
                        continue

                    self.buf.append(item)
                    if len(self.buf) >= self.batch_size:
                        self.flush(cur)
                        conn.commit()
                        last_flush = time.time()

        except Exception as e:
            log.exception("DBWriter crashed: %s", e)
            try:
                conn.rollback()
            except Exception:
                pass
        finally:
            try:
                with conn.cursor() as cur:
                    self.flush(cur)
                conn.commit()
            except Exception:
                pass
            conn.close()


# ----------------------------
# HTTP FETCH
# ----------------------------
def _parse_numerator_denominator(name: str) -> Tuple[str, Optional[str]]:
    if not name:
        return "", None
    try:
        a, b = name.split("/", 1)
        return a, b
    except Exception:
        return name, None


async def fetch_one(
    session: ClientSession,
    cookie_bank: CookieBank,
    sem: asyncio.Semaphore,
    row: Tuple[float, float, str],
    outq: "queue.Queue",
    db_pool: ThreadedConnectionPool,
    runtime_index: RuntimeGeomIndex,
):
    rw, hw, row_id = row

    async with sem:
        # (1) NEW: check runtime (uncommitted) polygons first
        try:
            if await runtime_index.contains_point(rw, hw):
                log.info("skip (covered by runtime geom) id=%s", row_id)
                return
        except Exception as e:
            log.warning("runtime coverage check failed id=%s: %s", row_id, e)

        # (2) check committed DB polygons
        try:
            covered_db = await asyncio.to_thread(point_is_covered, db_pool, rw, hw)
            if covered_db:
                log.info("skip (already covered in DB) id=%s", row_id)
                return
        except Exception as e:
            log.warning("DB coverage check failed id=%s: %s", row_id, e)

        proxy_entry = pick_proxy()
        proxy_url = proxy_to_aiohttp_url(proxy_entry)

        try:
            cookie = await cookie_bank.get_cookie(proxy_entry)
        except Exception as e:
            log.warning("cookie unavailable (proxy=%s, id=%s): %s", proxy_key(proxy_entry), row_id, e)
            return

        headers = dict(BASE_HEADERS)
        headers["Cookie"] = cookie
        url = REST_URL_TMPL.format(rw=rw, hw=hw)

        try:
            async with session.get(url, headers=headers, proxy=proxy_url) as resp:
                status = resp.status

                if status == 403:
                    await cookie_bank.invalidate(proxy_entry)
                    log.warning("403 -> invalidate cookie (proxy=%s, id=%s)", proxy_key(proxy_entry), row_id)
                    return

                if status != 200:
                    log.warning("HTTP %s (proxy=%s, id=%s, url=%s)", status, proxy_key(proxy_entry), row_id, url)
                    return

                data = await resp.json(content_type=None)
                results = data.get("results") or []
                if not results:
                    log.info("no results (id=%s, proxy=%s)", row_id, proxy_key(proxy_entry))
                    return

                for ele in results:
                    ewkt = ele.get("ewkt")
                    if not ewkt or ";" not in ewkt:
                        continue

                    gml_id = ele.get("gmlid")
                    boundary_id = ele.get("gmkgnr")
                    name = ele.get("flstnr")
                    geom_wkt = ewkt.split(";", 1)[1]
                    numerator, denominator = _parse_numerator_denominator(name or "")

                    # NEW: add to runtime index BEFORE enqueue (covers uncommitted period)
                    await runtime_index.add_wkt(geom_wkt)

                    if denominator is None:
                        parcel_id = boundary_id + '000' + numerator.zfill(5) + '000000'
                    else:
                        parcel_id = boundary_id + '000' + numerator.zfill(5) + denominator.zfill(4) + '00'

                    payload = (
                        parcel_id,
                        boundary_id,
                        None,
                        numerator,
                        denominator,
                        name,
                        None,
                        gml_id,
                        str(geom_wkt),
                    )

                    try:
                        outq.put_nowait(payload)
                    except queue.Full:
                        await asyncio.to_thread(outq.put, payload)

                    log.info("confirmed inserted (id=%s, proxy=%s)", row_id, proxy_key(proxy_entry))
                    return

                log.warning("results but nothing insertable (id=%s, proxy=%s)", row_id, proxy_key(proxy_entry))

        except (asyncio.TimeoutError, client_exceptions.ClientError) as e:
            log.warning("request failed (id=%s, proxy=%s): %s", row_id, proxy_key(proxy_entry), e)
        except Exception as e:
            log.exception("unexpected error (id=%s, proxy=%s): %s", row_id, proxy_key(proxy_entry), e)


# ----------------------------
# MAIN
# ----------------------------
async def main():
    await asyncio.to_thread(ensure_confirmed_geom_index)

    sem = asyncio.Semaphore(CONCURRENCY)
    cookie_bank = CookieBank()
    runtime_index = RuntimeGeomIndex()

    max_db = max(4, CONCURRENCY * 2)
    db_pool = ThreadedConnectionPool(1, max_db, dsn=DB_DSN)

    outq: "queue.Queue" = queue.Queue(maxsize=20000)
    writer = DBWriter(outq, batch_size=1000)
    writer.start()

    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT, connect=HTTP_TIMEOUT, sock_read=HTTP_TIMEOUT)
    connector = aiohttp.TCPConnector(ssl=False, limit=CONCURRENCY, limit_per_host=CONCURRENCY)

    processed = 0
    started = time.time()

    try:
        async with ClientSession(connector=connector, timeout=timeout) as session:
            tasks = set()

            for row in db_stream_rows():
                processed += 1
                t = asyncio.create_task(fetch_one(session, cookie_bank, sem, row, outq, db_pool, runtime_index))
                tasks.add(t)
                t.add_done_callback(tasks.discard)

                if len(tasks) >= CONCURRENCY * 50:
                    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

                if processed % 5000 == 0:
                    log.info("progress: processed=%d elapsed=%.1fs", processed, time.time() - started)

            if tasks:
                await asyncio.gather(*tasks)

    finally:
        try:
            outq.put(None)
        except Exception:
            pass
        writer.stop_flag = True
        writer.join(timeout=30)

        try:
            db_pool.closeall()
        except Exception:
            pass

    log.info("Done. processed=%d elapsed=%.1fs", processed, time.time() - started)


if __name__ == "__main__":
    asyncio.run(main())
