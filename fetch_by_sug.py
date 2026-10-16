#!/usr/bin/env python3
import os
import time
import asyncio
import random
import logging
import threading
import queue
from decimal import Decimal
from typing import Optional, List, Tuple
from urllib.parse import quote, urljoin

import psycopg2
import psycopg2.extras

import aiohttp
from aiohttp import ClientSession, client_exceptions

import pathlib
from playwright.sync_api import sync_playwright


# ----------------------------
# CONFIG
# ----------------------------
CONCURRENCY = int(os.getenv("CONCURRENCY", "10"))

ROW_FETCH_SIZE = int(os.getenv("ROW_FETCH_SIZE", "10000"))
QUEUE_CHUNK_SIZE = int(os.getenv("QUEUE_CHUNK_SIZE", "500"))
ASYNC_QUEUE_MAX = int(os.getenv("ASYNC_QUEUE_MAX", "200"))  # bounded memory per-boundary producer

DB_WRITE_BATCH = int(os.getenv("DB_WRITE_BATCH", "2000"))

REQUEST_RETRIES = int(os.getenv("REQUEST_RETRIES", "4"))
BASE_BACKOFF = float(os.getenv("BASE_BACKOFF", "0.4"))
MAX_BACKOFF = float(os.getenv("MAX_BACKOFF", "6.0"))

COOKIE_POOL_TARGET = int(os.getenv("COOKIE_POOL_TARGET", "10"))
COOKIE_POOL_MIN_TO_START = int(os.getenv("COOKIE_POOL_MIN_TO_START", "2"))
COOKIE_MINT_LIMIT = int(os.getenv("COOKIE_MINT_LIMIT", "1"))
COOKIE_MINT_RETRIES = int(os.getenv("COOKIE_MINT_RETRIES", "2"))
COOKIE_PROBE_TIMEOUT = float(os.getenv("COOKIE_PROBE_TIMEOUT", "10"))

# NOTE: sync_playwright is blocking => always run in threads; this is a hard timeout for mint flow.
COOKIE_MINT_TIMEOUT = int(os.getenv("COOKIE_MINT_TIMEOUT", "120"))

# boundary job heartbeat/progress updates
JOB_HEARTBEAT_EVERY_SEC = int(os.getenv("JOB_HEARTBEAT_EVERY_SEC", "60"))

# System Chromium you already have:
CHROMIUM_PATH = os.getenv("CHROMIUM_PATH", "/usr/bin/google-chrome")

PROXY_LIST = [
    "207.229.93.68:1027",
    "207.229.93.68:1028",
    "207.229.93.68:1029",
    "207.229.93.68:1026",
    "207.229.93.68:1025",
]

BASE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
}

DB_DSN = os.getenv("DB_DSN", "user=alkis password=mXV5UJBVfP9m83 host=localhost port=5432 dbname=by_cadastral")


LAUNCHER_URL = "https://geodatenonline.bayern.de/geodatenonline/anwendungen3/flurkartenauszug"
CONTINUE_XPATH = "/html/body/div[1]/div[6]/div[2]/div/div/div[2]/div/div/div[8]/p/a"

REST_BASE = "https://geodatenonline.bayern.de/gdoflurkarte2/rest/flurstueck"
PROBE_URL = f"{REST_BASE}/000000%200?&srid=25832"
PROBE_HEADERS_EXTRA = {
    "Accept": "application/json, text/plain, */*",
    "X-Requested-With": "XMLHttpRequest",
    "Origin": "https://geodatenonline.bayern.de",
    "Referer": LAUNCHER_URL,
}

BOUNDARY_JOBS_TABLE = "public.by_parcel_boundary_jobs"

# ----------------------------
# LOGGING
# ----------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("parcel_hardener_boundary_jobs")


# ----------------------------
# DB SCHEMA + JOBS TABLE
# ----------------------------
def ensure_db_schema(dsn: str) -> None:
    """
    Ensures:
      - by_parcel exists and has id
      - by_parcel_confirmed exists (LIKE by_parcel) with PK(id)
      - by_temp exists with unique(id)
      - by_parcel_boundary_jobs exists and is seeded with distinct boundary_id
    """
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            # base table exists
            cur.execute("SELECT to_regclass('public.by_parcel')")
            if cur.fetchone()[0] is None:
                raise RuntimeError("Required table public.by_parcel does not exist. Aborting.")

            cur.execute("""
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema='public' AND table_name='by_parcel' AND column_name='id'
            """)
            if cur.fetchone() is None:
                raise RuntimeError("Required column public.by_parcel.id does not exist. Aborting.")

            # by_parcel_confirmed
            cur.execute("SELECT to_regclass('public.by_parcel_confirmed')")
            if cur.fetchone()[0] is None:
                cur.execute("""
                    CREATE TABLE public.by_parcel_confirmed
                    (LIKE public.by_parcel INCLUDING DEFAULTS INCLUDING GENERATED INCLUDING IDENTITY INCLUDING COMMENTS)
                """)
                cur.execute("""
                    ALTER TABLE public.by_parcel_confirmed
                    ADD CONSTRAINT by_parcel_confirmed_pkey PRIMARY KEY (id)
                """)

            # by_temp
            cur.execute("SELECT to_regclass('public.by_temp')")
            if cur.fetchone()[0] is None:
                cur.execute("""
                    CREATE TABLE public.by_temp (
                        rw double precision,
                        hw double precision,
                        id text NOT NULL
                    )
                """)
                cur.execute("""
                    ALTER TABLE public.by_temp
                    ADD CONSTRAINT by_temp_unique UNIQUE (id)
                """)
            else:
                cur.execute("""
                    SELECT 1
                    FROM pg_constraint c
                    JOIN pg_class t ON t.oid = c.conrelid
                    JOIN pg_namespace n ON n.oid = t.relnamespace
                    WHERE n.nspname='public'
                      AND t.relname='by_temp'
                      AND c.conname='by_temp_unique'
                      AND c.contype='u'
                    LIMIT 1
                """)
                if cur.fetchone() is None:
                    cur.execute("ALTER TABLE public.by_temp ADD CONSTRAINT by_temp_unique UNIQUE (id)")

            # boundary jobs table
            cur.execute(f"SELECT to_regclass('{BOUNDARY_JOBS_TABLE}')")
            if cur.fetchone()[0] is None:
                cur.execute(f"""
                    CREATE TABLE {BOUNDARY_JOBS_TABLE} (
                        boundary_id text PRIMARY KEY,
                        status text NOT NULL DEFAULT 'pending',
                        claimed_by text,
                        claimed_at timestamptz,
                        started_at timestamptz,
                        finished_at timestamptz,
                        last_heartbeat timestamptz,
                        error text,
                        processed_confirmed bigint NOT NULL DEFAULT 0,
                        processed_temp bigint NOT NULL DEFAULT 0,
                        processed_total bigint NOT NULL DEFAULT 0,
                        last_parcel_id text
                    )
                """)
                cur.execute(f"CREATE INDEX by_parcel_boundary_jobs_status_idx ON {BOUNDARY_JOBS_TABLE}(status)")
                cur.execute(f"CREATE INDEX by_parcel_boundary_jobs_claimed_idx ON {BOUNDARY_JOBS_TABLE}(claimed_at)")

            # seed jobs with distinct boundary_id (idempotent)
            cur.execute(f"""
                INSERT INTO {BOUNDARY_JOBS_TABLE} (boundary_id, status)
                SELECT DISTINCT boundary_id, 'pending'
                FROM public.by_parcel
                WHERE boundary_id IS NOT NULL AND boundary_id <> ''
                ON CONFLICT (boundary_id) DO NOTHING
            """)

    finally:
        conn.close()


def claim_next_boundary_job(dsn: str, worker_name: str) -> Optional[str]:
    """
    Claims next pending job using FOR UPDATE SKIP LOCKED.
    Returns boundary_id or None if no jobs remain.
    """
    conn = psycopg2.connect(dsn)
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                WITH cte AS (
                    SELECT boundary_id
                    FROM {BOUNDARY_JOBS_TABLE}
                    WHERE status IN ('pending','retry')
                    ORDER BY boundary_id
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                UPDATE {BOUNDARY_JOBS_TABLE} j
                SET status='running',
                    claimed_by=%s,
                    claimed_at=now(),
                    started_at=COALESCE(started_at, now()),
                    last_heartbeat=now(),
                    error=NULL
                FROM cte
                WHERE j.boundary_id = cte.boundary_id
                RETURNING j.boundary_id
            """, (worker_name,))
            row = cur.fetchone()
        conn.commit()
        return row[0] if row else None
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()


def update_boundary_job_progress(
    dsn: str,
    boundary_id: str,
    worker_name: str,
    delta_confirmed: int,
    delta_temp: int,
    delta_total: int,
    last_parcel_id: Optional[str],
) -> None:
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                UPDATE {BOUNDARY_JOBS_TABLE}
                SET last_heartbeat = now(),
                    processed_confirmed = processed_confirmed + %s,
                    processed_temp = processed_temp + %s,
                    processed_total = processed_total + %s,
                    last_parcel_id = COALESCE(%s, last_parcel_id),
                    claimed_by = %s
                WHERE boundary_id = %s
            """, (delta_confirmed, delta_temp, delta_total, last_parcel_id, worker_name, boundary_id))
    finally:
        conn.close()


def finish_boundary_job(dsn: str, boundary_id: str) -> None:
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                UPDATE {BOUNDARY_JOBS_TABLE}
                SET status='done',
                    finished_at=now(),
                    last_heartbeat=now()
                WHERE boundary_id=%s
            """, (boundary_id,))
    finally:
        conn.close()


def fail_boundary_job(dsn: str, boundary_id: str, err: str) -> None:
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                UPDATE {BOUNDARY_JOBS_TABLE}
                SET status='retry',
                    error=%s,
                    last_heartbeat=now()
                WHERE boundary_id=%s
            """, (err[:4000], boundary_id))
    finally:
        conn.close()


# ----------------------------
# HELPERS (comparison)
# ----------------------------
def is_float_same(a, b, precision: str = "0.01") -> bool:
    q = Decimal(precision)
    da = Decimal(str(a))
    db = Decimal(str(b))
    return da.quantize(q) == db.quantize(q)

def parse_box2d_text(box2d_text: str) -> List[str]:
    t = box2d_text.strip()
    if t.startswith("BOX(") and t.endswith(")"):
        t = t[4:-1]
    t = t.replace(",", " ")
    parts = [p for p in t.split() if p]
    if len(parts) != 4:
        raise ValueError(f"Unexpected BOX2D format: {box2d_text}")
    return parts

def is_bbox_same(bbox_as_list, bbox_as_box2d_text: str) -> bool:
    box_parts = parse_box2d_text(bbox_as_box2d_text)
    if not isinstance(bbox_as_list, list) or len(bbox_as_list) != 4:
        return False
    for i in range(4):
        if not is_float_same(bbox_as_list[i], box_parts[i], "0.01"):
            return False
    return True

def build_url(boundary_id: str, name: str) -> str:
    safe_name = name.replace("/", "-")
    left = quote(boundary_id, safe="")
    right = quote(safe_name, safe="-")
    return f"{REST_BASE}/{left}%20{right}?&srid=25832"

def pick_proxy() -> str:
    return f"http://{random.choice(PROXY_LIST)}"


# ----------------------------
# COOKIE MINTING (PLAYWRIGHT)
# ----------------------------
COOKIE_MINT_SEM = asyncio.Semaphore(COOKIE_MINT_LIMIT)

def _cookies_to_header(cookies: List[dict]) -> str:
    parts = []
    for c in cookies:
        n = c.get("name")
        v = c.get("value")
        if n and v is not None:
            parts.append(f"{n}={v}")
    return "; ".join(parts)

def mint_cookie_blocking(proxy: str) -> Optional[str]:
    """
    Mint cookie header by reproducing the Selenium flow:
      - open LAUNCHER_URL
      - find Continue link, read href (do not click)
      - goto cont_url
      - goto /gdoflurkarte2/ once (handoff)
      - return cookies as "Cookie:" header string
    """
    out_dir = pathlib.Path(os.getenv("COOKIE_DEBUG_DIR", "/tmp/parcel_cookie_debug"))
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())

    browser = None
    context = None

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                executable_path=CHROMIUM_PATH,
                args=["--disable-dev-shm-usage", "--no-sandbox"],
                timeout=30000,
            )

            context = browser.new_context(
                proxy={"server": proxy},
                user_agent=BASE_HEADERS["User-Agent"],
                locale="de-DE",
                viewport={"width": 1920, "height": 1080},
            )

            page = context.new_page()

            page.goto(LAUNCHER_URL, wait_until="domcontentloaded", timeout=45000)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass

            loc = page.locator(f"xpath={CONTINUE_XPATH}").first
            loc.wait_for(state="attached", timeout=30000)

            href = loc.get_attribute("href")
            if not href:
                page.screenshot(path=str(out_dir / f"no_href_{ts}.png"), full_page=True)
                (out_dir / f"no_href_{ts}.html").write_text(page.content(), encoding="utf-8")
                return None

            cont_url = urljoin(page.url, href)

            page.goto(cont_url, wait_until="domcontentloaded", timeout=45000)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass

            try:
                page.goto("https://geodatenonline.bayern.de/gdoflurkarte2/", wait_until="domcontentloaded", timeout=45000)
            except Exception:
                pass

            cookies = context.cookies([
                "https://geodatenonline.bayern.de/",
                "https://geodatenonline.bayern.de/gdoflurkarte2/",
            ])
            cookie_header = _cookies_to_header(cookies)
            if not cookie_header:
                page.screenshot(path=str(out_dir / f"no_cookie_{ts}.png"), full_page=True)
                (out_dir / f"no_cookie_{ts}.html").write_text(page.content(), encoding="utf-8")
                return None

            return cookie_header

    except Exception as e:
        log.warning("Cookie mint failed via %s: %s", proxy, e)
        return None

    finally:
        try:
            if context:
                context.close()
        except Exception:
            pass
        try:
            if browser:
                browser.close()
        except Exception:
            pass


async def probe_cookie(cookie_header: str, proxy: str) -> bool:
    timeout = aiohttp.ClientTimeout(total=COOKIE_PROBE_TIMEOUT, connect=8, sock_read=8)
    headers = dict(BASE_HEADERS)
    headers.update(PROBE_HEADERS_EXTRA)
    headers["Cookie"] = cookie_header

    connector = aiohttp.TCPConnector(ssl=False, limit=2, ttl_dns_cache=300, enable_cleanup_closed=True)
    async with ClientSession(connector=connector, timeout=timeout) as s:
        try:
            async with s.get(PROBE_URL, proxy=proxy, headers=headers, allow_redirects=True) as r:
                return r.status != 403
        except Exception:
            return False


async def mint_verified_cookie() -> Optional[str]:
    """
    Mint cookie on a random proxy and verify against PROBE_URL.
    """
    async with COOKIE_MINT_SEM:
        for _ in range(COOKIE_MINT_RETRIES):
            proxy = pick_proxy()
            try:
                cookie = await asyncio.wait_for(
                    asyncio.to_thread(mint_cookie_blocking, proxy),
                    timeout=COOKIE_MINT_TIMEOUT,
                )
            except asyncio.TimeoutError:
                cookie = None

            if not cookie:
                continue

            ok = await probe_cookie(cookie, proxy)
            if ok:
                return cookie

        return None


class CookiePool:
    def __init__(self, target_size: int):
        self._target_size = target_size
        self._cookies: List[str] = []
        self._lock = asyncio.Lock()

    async def warmup_to(self, n: int) -> None:
        while True:
            async with self._lock:
                have = len(self._cookies)
            if have >= n:
                return

            cookie = await mint_verified_cookie()
            if cookie:
                async with self._lock:
                    self._cookies.append(cookie)
                    log.info("Cookie pool size=%d", len(self._cookies))
            else:
                await asyncio.sleep(0.5)

    async def topup_once(self) -> None:
        cookie = await mint_verified_cookie()
        if not cookie:
            return
        async with self._lock:
            self._cookies.append(cookie)
            log.info("Cookie pool size=%d", len(self._cookies))

    async def maintain(self, interval: float = 2.0) -> None:
        while True:
            async with self._lock:
                need = self._target_size - len(self._cookies)
            if need > 0:
                await self.topup_once()
            else:
                await asyncio.sleep(interval)

    async def get(self) -> str:
        async with self._lock:
            if self._cookies:
                return random.choice(self._cookies)

        cookie = await mint_verified_cookie()
        if not cookie:
            raise RuntimeError("Could not obtain a verified cookie")
        async with self._lock:
            self._cookies.append(cookie)
            log.info("Cookie pool size=%d", len(self._cookies))
        return cookie

    async def ban(self, cookie: str) -> None:
        async with self._lock:
            try:
                self._cookies.remove(cookie)
            except ValueError:
                pass


# ----------------------------
# DB WRITER THREAD
# ----------------------------
class DBWriter(threading.Thread):
    def __init__(self, dsn: str, inq: "queue.Queue", stop_event: threading.Event):
        super().__init__(daemon=True)
        self.dsn = dsn
        self.inq = inq
        self.stop_event = stop_event
        self._confirmed_ids: List[str] = []
        self._temp_rows: List[Tuple[float, float, str]] = []
        self._pending = 0

    def _flush(self, conn) -> None:
        if not self._confirmed_ids and not self._temp_rows:
            return

        with conn.cursor() as cur:
            if self._temp_rows:
                psycopg2.extras.execute_values(
                    cur,
                    """
                    INSERT INTO by_temp (rw, hw, id)
                    VALUES %s
                    ON CONFLICT ON CONSTRAINT by_temp_unique DO NOTHING
                    """,
                    self._temp_rows,
                    page_size=1000,
                )

            if self._confirmed_ids:
                cur.execute(
                    """
                    INSERT INTO by_parcel_confirmed
                    SELECT p.*
                    FROM by_parcel p
                    JOIN unnest(%s::text[]) AS u(id) ON u.id = p.id
                    ON CONFLICT (id) DO NOTHING
                    """,
                    (self._confirmed_ids,),
                )

        conn.commit()
        self._confirmed_ids.clear()
        self._temp_rows.clear()
        self._pending = 0

    def run(self) -> None:
        conn = psycopg2.connect(self.dsn)
        conn.autocommit = False
        last_flush = time.time()

        try:
            while not self.stop_event.is_set():
                try:
                    item = self.inq.get(timeout=0.5)
                except queue.Empty:
                    if time.time() - last_flush > 2:
                        self._flush(conn)
                        last_flush = time.time()
                    continue

                if item is None:
                    self._flush(conn)
                    last_flush = time.time()
                    continue

                kind, payload = item
                if kind == "confirmed":
                    self._confirmed_ids.append(payload)
                    self._pending += 1
                elif kind == "temp":
                    self._temp_rows.append(payload)
                    self._pending += 1

                if self._pending >= DB_WRITE_BATCH:
                    self._flush(conn)
                    last_flush = time.time()

        except Exception as e:
            log.exception("DBWriter crashed: %s", e)
            try:
                conn.rollback()
            except Exception:
                pass
        finally:
            try:
                self._flush(conn)
            except Exception:
                pass
            conn.close()


async def safe_outq_put(outq: "queue.Queue", item) -> None:
    try:
        outq.put_nowait(item)
    except queue.Full:
        await asyncio.to_thread(outq.put, item)


# ----------------------------
# HTTP FETCH + PROCESS
# ----------------------------
class NeedsRebootstrap(Exception):
    pass


async def fetch_and_process_one(
    row,
    session: ClientSession,
    proxy: str,
    cookie_pool: CookiePool,
    outq: "queue.Queue",
) -> Tuple[int, int]:
    """
    Returns (confirmed_count, temp_count) that we *enqueued* (best-effort stats).
    """
    parcel_id, exp_gml_id, exp_boundary_id, exp_name, exp_bbox_text, exp_rw, exp_hw = row
    url = build_url(exp_boundary_id, exp_name)

    for attempt in range(1, REQUEST_RETRIES + 1):
        cookie = await cookie_pool.get()
        headers = dict(BASE_HEADERS)
        headers.update({
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": "https://geodatenonline.bayern.de",
            "Referer": LAUNCHER_URL,
            "Cookie": cookie,
        })

        try:
            async with session.get(url, proxy=proxy, headers=headers) as resp:
                status = resp.status

                if status == 403:
                    await cookie_pool.ban(cookie)
                    asyncio.create_task(cookie_pool.topup_once())
                    raise NeedsRebootstrap("HTTP 403")

                if status in (429, 500, 502, 503, 504):
                    raise RuntimeError(f"HTTP {status}")

                if status != 200:
                    raise RuntimeError(f"HTTP {status}")

                data = await resp.json(content_type=None)
                results = data.get("results") or []

                confirmed_added = 0
                temp_added = 0

                for ele in results:
                    got_id = ele.get("id")
                    got_gml_id = ele.get("gmlid")
                    got_boundary_id = ele.get("gmkgnr")
                    got_name = ele.get("flstnr")
                    got_rw = ele.get("rw")
                    got_hw = ele.get("hw")
                    got_bbox = ele.get("bbox")

                    is_match = True
                    if got_boundary_id != exp_boundary_id:
                        is_match = False
                    if got_name != exp_name:
                        is_match = False
                    if got_gml_id != exp_gml_id:
                        is_match = False

                    try:
                        if not is_bbox_same(got_bbox, exp_bbox_text):
                            is_match = False
                    except Exception:
                        is_match = False

                    if not is_float_same(got_rw, exp_rw, "0.1"):
                        is_match = False
                    if not is_float_same(got_hw, exp_hw, "0.1"):
                        is_match = False

                    if is_match:
                        await safe_outq_put(outq, ("confirmed", parcel_id))
                        confirmed_added += 1
                    else:
                        if got_rw is not None and got_hw is not None and got_id is not None:
                            await safe_outq_put(outq, ("temp", (got_rw, got_hw, got_id)))
                            temp_added += 1

                return confirmed_added, temp_added

        except NeedsRebootstrap:
            raise
        except (client_exceptions.ClientProxyConnectionError,
                client_exceptions.ClientConnectorError,
                asyncio.TimeoutError) as e:
            backoff = min(MAX_BACKOFF, BASE_BACKOFF * (2 ** (attempt - 1)))
            backoff *= (0.7 + random.random() * 0.6)
            if attempt == REQUEST_RETRIES:
                log.warning("Network fail parcel_id=%s via %s: %s", parcel_id, proxy, e)
                return 0, 0
            await asyncio.sleep(backoff)
        except Exception as e:
            backoff = min(MAX_BACKOFF, BASE_BACKOFF * (2 ** (attempt - 1)))
            backoff *= (0.7 + random.random() * 0.6)
            if attempt == REQUEST_RETRIES:
                log.warning("Failed parcel_id=%s via %s. Last error=%s", parcel_id, proxy, e)
                return 0, 0
            await asyncio.sleep(backoff)

    return 0, 0


# ----------------------------
# DB PRODUCER PER BOUNDARY (stream rows into a local async queue)
# ----------------------------
def _open_db_read_conn():
    conn = psycopg2.connect(DB_DSN)
    conn.autocommit = False  # named cursor requires transaction
    return conn


async def produce_rows_for_boundary(boundary_id: str, aq: "asyncio.Queue") -> None:
    """
    Streams all unconfirmed parcels for one boundary_id into aq as chunks.
    """
    conn = await asyncio.to_thread(_open_db_read_conn)
    cur = None
    try:
        await asyncio.to_thread(conn.set_session, readonly=True)
        cur = conn.cursor(name=f"parcel_stream_{boundary_id}")
        cur.itersize = ROW_FETCH_SIZE

        stmt = """
            SELECT
                p.id,
                p.gml_id,
                p.boundary_id,
                p.name,
                BOX2D(p.geom)::text AS bbox_text,
                ST_X(ST_PointOnSurface(p.geom)) AS rw,
                ST_Y(ST_PointOnSurface(p.geom)) AS hw
            FROM by_parcel p
            WHERE p.boundary_id = %s
              AND NOT EXISTS (
                SELECT 1 FROM by_parcel_confirmed c WHERE c.id = p.id
              )
            ORDER BY p.id
        """
        await asyncio.to_thread(cur.execute, stmt, (boundary_id,))

        while True:
            rows = await asyncio.to_thread(cur.fetchmany, ROW_FETCH_SIZE)
            if not rows:
                break

            for i in range(0, len(rows), QUEUE_CHUNK_SIZE):
                await aq.put(rows[i:i + QUEUE_CHUNK_SIZE])

    finally:
        try:
            await asyncio.to_thread(conn.rollback)
        except Exception:
            pass
        try:
            if cur is not None:
                await asyncio.to_thread(cur.close)
        except Exception:
            pass
        try:
            await asyncio.to_thread(conn.close)
        except Exception:
            pass

    # signal end-of-boundary stream
    await aq.put(None)


# ----------------------------
# WORKER: claim boundary job -> process -> finish -> claim next
# ----------------------------
async def worker(name: str, outq: "queue.Queue", cookie_pool: CookiePool) -> None:
    proxy = pick_proxy()
    consecutive_403 = 0

    timeout = aiohttp.ClientTimeout(total=20, connect=10, sock_read=10)
    connector = aiohttp.TCPConnector(
        ssl=False,
        limit=CONCURRENCY * 2,
        limit_per_host=CONCURRENCY,
        ttl_dns_cache=300,
        enable_cleanup_closed=True,
    )

    async with ClientSession(connector=connector, timeout=timeout) as session:
        while True:
            boundary_id = await asyncio.to_thread(claim_next_boundary_job, DB_DSN, name)
            if not boundary_id:
                log.info("%s: no more boundary jobs -> exiting", name)
                return

            log.info("%s: claimed boundary_id=%s (proxy=%s)", name, boundary_id, proxy)

            # local queue for this boundary
            aq: asyncio.Queue = asyncio.Queue(maxsize=ASYNC_QUEUE_MAX)
            producer_task = asyncio.create_task(produce_rows_for_boundary(boundary_id, aq))

            processed_total = 0
            processed_confirmed = 0
            processed_temp = 0
            last_parcel_id = None
            last_hb = time.time()

            try:
                while True:
                    chunk = await aq.get()
                    try:
                        if chunk is None:
                            break

                        for row in chunk:
                            last_parcel_id = row[0]
                            try:
                                c_add, t_add = await fetch_and_process_one(row, session, proxy, cookie_pool, outq)
                                processed_confirmed += c_add
                                processed_temp += t_add
                                processed_total += 1
                                consecutive_403 = 0
                            except NeedsRebootstrap:
                                consecutive_403 += 1
                                log.warning(
                                    "%s: boundary=%s got 403 (count=%d) via %s",
                                    name, boundary_id, consecutive_403, proxy,
                                )
                                if consecutive_403 >= 5:
                                    proxy = pick_proxy()
                                    consecutive_403 = 0
                                    log.warning("%s: rotating proxy -> %s", name, proxy)

                            # periodic heartbeat/progress
                            now = time.time()
                            if now - last_hb >= JOB_HEARTBEAT_EVERY_SEC:
                                await asyncio.to_thread(
                                    update_boundary_job_progress,
                                    DB_DSN,
                                    boundary_id,
                                    name,
                                    processed_confirmed,
                                    processed_temp,
                                    processed_total,
                                    last_parcel_id,
                                )
                                processed_confirmed = 0
                                processed_temp = 0
                                processed_total = 0
                                last_hb = now

                    finally:
                        aq.task_done()

                # ensure producer completed
                await producer_task

                # final progress flush
                if processed_confirmed or processed_temp or processed_total:
                    await asyncio.to_thread(
                        update_boundary_job_progress,
                        DB_DSN,
                        boundary_id,
                        name,
                        processed_confirmed,
                        processed_temp,
                        processed_total,
                        last_parcel_id,
                    )

                await asyncio.to_thread(finish_boundary_job, DB_DSN, boundary_id)
                log.info("%s: finished boundary_id=%s", name, boundary_id)

            except Exception as e:
                # fail boundary => retry
                try:
                    await asyncio.to_thread(fail_boundary_job, DB_DSN, boundary_id, str(e))
                except Exception:
                    pass
                log.exception("%s: boundary_id=%s failed: %s", name, boundary_id, e)

            finally:
                # drain queue quickly if needed
                try:
                    producer_task.cancel()
                    await asyncio.gather(producer_task, return_exceptions=True)
                except Exception:
                    pass


# ----------------------------
# MAIN
# ----------------------------
async def main() -> None:
    await asyncio.to_thread(ensure_db_schema, DB_DSN)

    # writer
    outq: "queue.Queue" = queue.Queue(maxsize=20000)
    stop_event = threading.Event()
    writer = DBWriter(DB_DSN, outq, stop_event)
    writer.start()

    # cookies
    cookie_pool = CookiePool(COOKIE_POOL_TARGET)

    log.info("Warming cookies to MIN=%d (TARGET=%d)...", COOKIE_POOL_MIN_TO_START, COOKIE_POOL_TARGET)
    await cookie_pool.warmup_to(COOKIE_POOL_MIN_TO_START)
    log.info("Cookie MIN reached -> starting workers")

    topup_task = asyncio.create_task(cookie_pool.maintain())

    worker_tasks = [asyncio.create_task(worker(f"W{i+1}", outq, cookie_pool)) for i in range(CONCURRENCY)]

    try:
        await asyncio.gather(*worker_tasks)
        outq.put(None)  # flush writer
    finally:
        topup_task.cancel()
        await asyncio.gather(topup_task, return_exceptions=True)
        stop_event.set()
        try:
            outq.put(None)
        except Exception:
            pass

    log.info("All done.")


if __name__ == "__main__":
    start = time.time()
    asyncio.run(main())
    end = time.time()
    print("Done")
    print(end - start)
