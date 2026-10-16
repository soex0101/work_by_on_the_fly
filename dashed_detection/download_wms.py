import os
import sys
import random
import time
from urllib.parse import urlparse
from typing import Optional, List
from PIL import Image

import requests
from seleniumwire import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.firefox.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from read_settings import load_config
from common import load_proxies, request_error, exit_if_failed_requests


def access_shadow_root(driver, host_element):
    return driver.execute_script("return arguments[0].shadowRoot", host_element)

def _parse_proxy(proxy_line: str):
    """
    Parse a proxy line. proxies.txt format is 'host:port:user:pass'
    (plain 'host:port' is also accepted, and a leading scheme is ignored).
    Returns (host, port, user, pass); user/pass are None without credentials.
    """
    if "://" in proxy_line:
        proxy_line = proxy_line.split("://", 1)[1]
    parts = proxy_line.split(":")
    host = parts[0]
    port = parts[1]
    username = parts[2] if len(parts) > 2 else None
    password = parts[3] if len(parts) > 3 else None
    return host, port, username, password

def _requests_proxy_url(proxy_line: str) -> str:
    """
    Build a 'http://user:pass@host:port' URL. Both requests and Selenium-Wire
    accept credentials embedded in the URL this way for http/https proxying.
    """
    host, port, username, password = _parse_proxy(proxy_line)
    if username is not None:
        return f"http://{username}:{password}@{host}:{port}"
    return f"http://{host}:{port}"

def choose_random_proxy(proxies: List[str]) -> Optional[str]:
    if not proxies:
        return None
    return _requests_proxy_url(random.choice(proxies))

def get_proxy_url(proxy_line: str) -> Optional[str]:
    """Convert a proxy line to a requests-compatible URL."""
    if not proxy_line:
        return None
    return _requests_proxy_url(proxy_line)

def build_firefox_driver(*, firefox_binary_path, gecko_path, proxy_url=None, headless=True):
    opts = Options()
    if firefox_binary_path:
        opts.binary_location = firefox_binary_path
    if headless:
        opts.add_argument("--headless")
    opts.add_argument("--width=1920")
    opts.add_argument("--height=1080")
    opts.set_preference(
        "general.useragent.override",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:134.0) Gecko/20100101 Firefox/134.0"
    )

    service = Service(executable_path=gecko_path, log_path="geckodriver.log")

    seleniumwire_options = {}
    if proxy_url:
        seleniumwire_options = {
            "proxy": {
                "http": proxy_url,
                "https": proxy_url,
            },
            "connection_timeout": 300,
        }

    driver = webdriver.Firefox(service=service, options=opts, seleniumwire_options=seleniumwire_options)
    driver.set_page_load_timeout(120)
    return driver


def atlas_login_and_get_cookie_header(
    *,
    username: str,
    password: str,
    test_wms_url: str,
    firefox_binary_path: str,
    gecko_path: str,
    proxy_url: Optional[str] = None,
    headless: bool = True,
    timeout_sec: int = 45,
) -> str:
    """
    Logs into https://atlas.bayern.de and loads test_wms_url to trigger a WMS request.
    Returns the full Cookie header string captured from the outgoing WMS request.
    """
    driver = build_firefox_driver(
        firefox_binary_path=firefox_binary_path,
        gecko_path=gecko_path,
        proxy_url=proxy_url,
        headless=headless,
    )

    try:
        driver.get("https://atlas.bayern.de")
        wait = WebDriverWait(driver, timeout_sec)

        # Open login modal (ba-navigation-rail shadow root)
        nav_host = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "ba-navigation-rail")))
        nav_shadow = access_shadow_root(driver, nav_host)

        auth_button = nav_shadow.find_element(By.CSS_SELECTOR, "#authButton > span")
        driver.execute_script("arguments[0].click();", auth_button)

        # Login form inside modal shadow roots
        modal_host = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "body > ba-modal")))
        modal_shadow = access_shadow_root(driver, modal_host)

        panel_host = modal_shadow.find_element(By.CSS_SELECTOR, "ba-auth-password-credential-panel")
        panel_shadow = access_shadow_root(driver, panel_host)

        user_in = panel_shadow.find_element(By.CSS_SELECTOR, "#credential_username")
        pass_in = panel_shadow.find_element(By.CSS_SELECTOR, "#credential_password")
        user_in.clear()
        user_in.send_keys(username)
        pass_in.clear()
        pass_in.send_keys(password)

        btn_host = panel_shadow.find_element(By.CSS_SELECTOR, "#authenticate-credential-button")
        btn_shadow = access_shadow_root(driver, btn_host)
        submit_btn = btn_shadow.find_element(By.CSS_SELECTOR, "button.button.primary")
        driver.execute_script("arguments[0].click();", submit_btn)

        # Wait for login success: logout button appears
        def logout_present(_drv):
            try:
                nav_host2 = _drv.find_element(By.CSS_SELECTOR, "ba-navigation-rail")
                nav_shadow2 = access_shadow_root(_drv, nav_host2)
                return len(nav_shadow2.find_elements(By.CSS_SELECTOR, "#authButton.logout")) > 0
            except Exception:
                return False

        wait.until(logout_present)
        print("Login Success")

        # Trigger one WMS request so Selenium-Wire captures Cookie header
        driver.requests.clear()
        driver.get(test_wms_url)


        # Find outgoing request with BA_AUTH_COOKIE
        deadline = time.time() + timeout_sec
        cookie_header = None

        parsed = urlparse(test_wms_url)
        target_host = parsed.netloc.lower()

        while time.time() < deadline and not cookie_header:
            for req in driver.requests:
                if not req.url:
                    continue
                try:
                    req_host = urlparse(req.url).netloc.lower()
                except Exception:
                    continue

                if target_host and req_host != target_host:
                    continue

                hdrs = req.headers or {}
                ck = hdrs.get("Cookie") or hdrs.get("cookie")
                if ck and any(x in ck for x in ("JSESSIONID=", "BA_AUTH_COOKIE=")):
                    cookie_header = ck
                    break
            if not cookie_header:
                time.sleep(0.25)

        if not cookie_header:
            raise RuntimeError("Could not capture BA_AUTH_COOKIE or JSESSIONID from outgoing WMS request.")

        return cookie_header

    finally:
        try:
            driver.quit()
        except Exception:
            pass


def download_wms_png(*, wms_url: str, cookie_header: str, out_path: str, proxy_url: Optional[str] = None):
    """
    Download the WMS image using requests, sending the captured Cookie header.
    Uses the same proxy (if provided).
    """
    try:
        headers = {
            "Cookie": cookie_header,
            "User-Agent": "Mozilla/5.0",
            "Accept": "image/png,image/*;q=0.8,*/*;q=0.5",
            "Referer": "https://atlas.bayern.de/",
        }

        proxies = None
        if proxy_url:
            proxies = {"http": proxy_url, "https": proxy_url}

        with requests.Session() as s:
            resp = s.get(wms_url, headers=headers, proxies=proxies, timeout=120)
            resp.raise_for_status()

            ct = resp.headers.get("Content-Type", "")
            if "image" not in ct.lower():
                snippet = (resp.text or "")[:300]
                raise RuntimeError(f"Expected image response, got Content-Type={ct}. Body starts with:\n{snippet}")

            os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
            with open(out_path, "wb") as f:
                f.write(resp.content)

        return out_path
    except Exception as e:
        print("Error: ", e)
        # Raised so the caller retries and can report the status code and message
        raise

def get_image_size(minx, miny, maxx, maxy):
    img_width = (maxx - minx) * 4010 / 587
    img_height = (maxy - miny) * 4010 / 587

    return int(img_width), int(img_height)
def split_bbox_on_global_pixel_grid(
    full_bbox,
    full_width,
    full_height,
    tile_max_width=600,
    tile_max_height=600,
    pad_x=50,
    pad_y=50,
):
    """
    Split a bbox using the ORIGINAL full-image pixel grid.

    Parameters
    ----------
    full_bbox : tuple
        (minx, miny, maxx, maxy)
    full_width : int
        Width of the original full image in pixels
    full_height : int
        Height of the original full image in pixels
    tile_max_width : int
        Max inner tile width in pixels
    tile_max_height : int
        Max inner tile height in pixels
    pad_x : int
        Horizontal padding in pixels
    pad_y : int
        Vertical padding in pixels

    Returns
    -------
    list of dict
        Each dict contains both pixel coords and bbox coords.
    """
    minx, miny, maxx, maxy = full_bbox

    res_x = (maxx - minx) / full_width
    res_y = (maxy - miny) / full_height

    tiles = []

    for py0 in range(0, full_height, tile_max_height):
        py1 = min(py0 + tile_max_height, full_height)

        for px0 in range(0, full_width, tile_max_width):
            px1 = min(px0 + tile_max_width, full_width)

            # buffered pixel window
            bpx0 = max(0, px0 - pad_x)
            bpy0 = max(0, py0 - pad_y)
            bpx1 = min(full_width, px1 + pad_x)
            bpy1 = min(full_height, py1 + pad_y)

            # convert pixel window -> bbox
            # image origin is top-left, map origin is bottom-left
            tile_bbox = (
                minx + px0 * res_x,
                maxy - py1 * res_y,
                minx + px1 * res_x,
                maxy - py0 * res_y,
            )

            buffered_bbox = (
                minx + bpx0 * res_x,
                maxy - bpy1 * res_y,
                minx + bpx1 * res_x,
                maxy - bpy0 * res_y,
            )

            tiles.append({
                "tile_px": (px0, py0, px1, py1),
                "buffered_tile_px": (bpx0, bpy0, bpx1, bpy1),
                "tile": tile_bbox,
                "buffered_tile": buffered_bbox,
                "request_width": bpx1 - bpx0,
                "request_height": bpy1 - bpy0,
                "crop_box": (
                    px0 - bpx0,
                    py0 - bpy0,
                    px1 - bpx0,
                    py1 - bpy0,
                ),
                "paste_xy": (px0, py0),
            })

    return tiles


def merge_buffered_tiles_exact(tiles, image_paths, full_width, full_height, output_path):
    """
    Merge downloaded buffered tile images back into one exact full image.

    Parameters
    ----------
    tiles : list[dict]
        Output from split_bbox_on_global_pixel_grid
    image_paths : list[str]
        Same order as tiles
    full_width : int
    full_height : int
    output_path : str
    """
    if len(tiles) != len(image_paths):
        raise ValueError("tiles and image_paths length mismatch")

    merged = Image.new("RGBA", (full_width, full_height), (255, 255, 255, 0))

    for tile, img_path in zip(tiles, image_paths):
        img = Image.open(img_path).convert("RGBA")

        req_w = tile["request_width"]
        req_h = tile["request_height"]

        # Safety: enforce exact requested size
        if img.size != (req_w, req_h):
            img = img.resize((req_w, req_h), Image.Resampling.BILINEAR)

        crop_left, crop_top, crop_right, crop_bottom = tile["crop_box"]
        cropped = img.crop((crop_left, crop_top, crop_right, crop_bottom))

        paste_x, paste_y = tile["paste_xy"]
        merged.alpha_composite(cropped, (paste_x, paste_y))

    merged.save(output_path)
    return output_path

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python download_wms.py <id>")
        print('Example command format: "python download_wms.py 20251113_050000"')
        sys.exit(1)

    run_id = sys.argv[1]
    cfg = load_config(run_id)

    # --- Credentials ---
    username = cfg.atlas_username
    password = cfg.atlas_password

    # --- Driver paths ---
    firefox_path = cfg.firefox_path
    gecko_path = cfg.gecko_path

    # --- Proxies ---
    proxy_list = load_proxies(cfg)
    if not proxy_list:
        print("No proxies configured; connecting directly.")
        proxy_list = [None]

    proxy_idx = 0

    # --- WMS URLs ---
    full_width, full_height = get_image_size(*cfg.bbox)
    tiles = split_bbox_on_global_pixel_grid(
        full_bbox=cfg.bbox,
        full_width=full_width,
        full_height=full_height,
        tile_max_width=5000,
        tile_max_height=4000,
        pad_x=50,
        pad_y=50,
    )

    image_paths = []
    # Tiles that failed on every proxy
    failed_requests = []

    for idx, tile in enumerate(tiles):
        bbox = tile["buffered_tile"]
        width = tile["request_width"]
        height = tile["request_height"]
        BASE_URL = (
            "https://services.atlas.bayern.de/proxy/wms/map/a29d0fce-6901-43c0-90ed-ac73b7780479"
            "?REQUEST=GetMap"
            "&SERVICE=WMS"
            "&VERSION=1.1.1"
            "&FORMAT=image/png"
            "&STYLES="
            "&TRANSPARENT=false"
            "&LAYERS=by_alkis_flurkarte_umr_schwarz"
            "&SRS=EPSG:25832"
            f"&BBOX={','.join(map(str, bbox))}"
        )
        TEST_WMS_URL = BASE_URL + f"&WIDTH=256&HEIGHT=256"
        WMS_URL = BASE_URL + f"&WIDTH={width}&HEIGHT={height}"
        print(f"WMS_URL: {WMS_URL}")

        out_path = os.path.join(cfg.dashed_wms_path, run_id + f"_1_wms_{idx}.png")
        if os.path.exists(out_path):
            print(f"Skipping tile {idx}: already exists at {out_path}")
            image_paths.append(out_path)
            continue

        success = False
        attempted_proxies = 0
        last_error = None

        while attempted_proxies < len(proxy_list) and not success:
            proxy_line = proxy_list[proxy_idx % len(proxy_list)]
            proxy_url = get_proxy_url(proxy_line)

            if proxy_url:
                print(f"Trying proxy: {proxy_line} (index {proxy_idx % len(proxy_list)})")
            else:
                print(f"Trying direct connection (index {proxy_idx % len(proxy_list)})")

            for attempt in range(1, 4):  # 3 attempts per proxy
                try:
                    print(f"Attempt {attempt}/3")

                    cookie_hdr = atlas_login_and_get_cookie_header(
                        username=username,
                        password=password,
                        test_wms_url=TEST_WMS_URL,
                        firefox_binary_path=firefox_path,
                        gecko_path=gecko_path,
                        proxy_url=proxy_url,
                        headless=True,
                    )
                    print("Captured cookie: ", cookie_hdr)

                    out_file = download_wms_png(
                        wms_url=WMS_URL,
                        cookie_header=cookie_hdr,
                        out_path=out_path,
                        proxy_url=proxy_url,
                    )
                    if out_file is not None:
                        print(f"Saved: {out_file}")
                        image_paths.append(out_file)
                        success = True
                        break
                except Exception as e:
                    last_error = e
                    print(f"Attempt {attempt} failed: {e}")
                    if attempt < 3:
                        time.sleep(1)

            if not success:
                proxy_idx += 1
                attempted_proxies += 1

        if not success:
            print(f"Failed to download tile {idx}: all {len(proxy_list)} proxies exhausted")
            if last_error is not None:
                failed_requests.append(request_error(last_error, WMS_URL))

    exit_if_failed_requests("dashed_detection/download_wms.py", failed_requests, run_id)

    # Merge tiles to one
    merged_path = os.path.join(cfg.dashed_wms_path, run_id + "_1_wms.png")
    if os.path.exists(merged_path):
        print(f"Skipping merge: already exists at {merged_path}")
    else:
        merge_buffered_tiles_exact(
            tiles=tiles,
            image_paths=image_paths,
            full_width=full_width,
            full_height=full_height,
            output_path=merged_path,
        )
        print(f"Merged saved: {merged_path}")
