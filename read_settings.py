from __future__ import annotations
import json
import os
import platform
from dataclasses import dataclass
from typing import Dict, Tuple, Any, List
from multiprocessing import cpu_count
import configparser

def _parse_bbox(s: str) -> Tuple[float, float, float, float]:
    parts = [p.strip() for p in s.split(",")]
    if len(parts) != 4:
        raise ValueError(f"bbox must have 4 comma-separated numbers, got: {s!r}")
    nums = [float(p) for p in parts]
    x_min, y_min, x_max, y_max = nums
    if x_max <= x_min or y_max <= y_min:
        raise ValueError(f"bbox has invalid extents: {nums}")
    return x_min, y_min, x_max, y_max


def _parse_json_dict(value: str, expect_pairs: bool = False) -> Dict[int, Any]:
    """
    Parse a JSON object with string keys into a dict with int keys.
    If expect_pairs=True, values should be 2-item lists/tuples.
    """
    try:
        raw = json.loads(value)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON mapping: {e.msg}") from e
    if not isinstance(raw, dict):
        raise ValueError("Expected a JSON object (e.g. {\"500\": 2.0, ...})")
    out: Dict[int, Any] = {}
    for k, v in raw.items():
        try:
            ik = int(k)
        except Exception:
            raise ValueError(f"Scale keys must be integers, found {k!r}")
        if expect_pairs:
            if not (isinstance(v, (list, tuple)) and len(v) == 2):
                raise ValueError(f"Value for key {k!r} must be a 2-item list/tuple")
            v = (float(v[0]), float(v[1]))
        else:
            # allow int/float
            if isinstance(v, (int, float)):
                v = float(v)
            elif isinstance(v, str):
                # try numeric string
                try:
                    v = float(v)
                except ValueError:
                    pass
        out[ik] = v
    return out

def _pick_gecko_paths(cfg: configparser.ConfigParser) -> Dict[str, str]:
    mode = cfg.get("gecko", "use", fallback="auto").lower()
    if mode == "auto":
        system = platform.system().lower()  # 'windows', 'linux', 'darwin'
        section = "gecko.windows" if system == "windows" else "gecko.debian"
    elif mode in ("windows", "debian"):
        section = f"gecko.{mode}"
    else:
        section = "gecko.debian"

    if not cfg.has_section(section):
        raise ValueError(f"Missing gecko section: [{section}]")

    return {
        "gecko_path": cfg.get(section, "gecko_path"),
        "firefox_path": cfg.get(section, "firefox_path")
    }


def _pick_chrome_paths(cfg: configparser.ConfigParser) -> Dict[str, str]:
    mode = cfg.get("chrome", "use", fallback="auto").lower()
    if mode == "auto":
        system = platform.system().lower()  # 'windows', 'linux', 'darwin'
        section = "chrome.windows" if system == "windows" else "chrome.debian"
    elif mode in ("windows", "debian"):
        section = f"chrome.{mode}"
    else:
        section = "chrome.debian"

    if not cfg.has_section(section):
        raise ValueError(f"Missing chrome section: [{section}]")

    return {
        "chrome_path": cfg.get(section, "chrome_path"),
    }


def _pick_font_paths(cfg: configparser.ConfigParser) -> Dict[str, str]:
    mode = cfg.get("fonts", "use", fallback="auto").lower()
    if mode == "auto":
        system = platform.system().lower()  # 'windows', 'linux', 'darwin'
        section = "fonts.windows" if system == "windows" else "fonts.debian"
    elif mode in ("windows", "debian"):
        section = f"fonts.{mode}"
    else:
        section = "fonts.debian"

    if not cfg.has_section(section):
        raise ValueError(f"Missing font section: [{section}]")

    return {
        "font_path": cfg.get(section, "font_path"),
        "font_bold_path": cfg.get(section, "font_bold_path"),
        "font_italic_path": cfg.get(section, "font_italic_path"),
        "font_bold_italic_path": cfg.get(section, "font_bold_italic_path"),
    }


@dataclass
class Config:
    # area / scale
    input_bbox: Tuple[float, float, float, float]
    bbox: Tuple[float, float, float, float]
    by_whole_bbox: Tuple[float, float, float, float]
    scale: int
    dpi: float

    # service_availability
    alkis_parcel_sync_wms_available: bool
    atlas_dashed_wms_available: bool
    street_center_wms_available: bool
    atlas_search_available: bool

    # db_connection
    host: str
    port: int
    user: str
    password: str
    dbname: str
    dbname_address: str
    dbname_tn: str
    dbname_boundary: str
    dbname_building: str
    dbname_vertex: str

    # lod2d
    fail_script: bool
    lod_host: str
    lod_port: int
    lod_user: str
    lod_password: str
    lod_dbname: str

    # alkis_import
    sh_path: str
    xml_path: str
    lst_path: str

    # proxy
    proxy_file: str
    launcher_url: str

    # parcel_difference
    api_url: str
    continue_xpath: str

    # building_color
    building_source: str
    building_url: str
    building_path: str

    # vertex
    vertex_source: str
    vertex_path: str

    # atlas_account
    atlas_username: str
    atlas_password: str

    # dashed_line_detection
    dashed_wms_path: str

    # log
    log_path: str

    # backup path
    backup_ini_path: str
    parcel_snapshots_path: str

    # nas_xml
    force_null_zaehler_in_nas_xml: str

    # gpkg
    gpkg_url: str
    gpkg_out_dir: str

    # shape
    shape_path: str

    # compute
    cpu_cores: int

    # splitting
    primary_splitter: str
    auxiliary_splitter: str

    # parcel sync
    parcel_sample_step_m: float

    # labels & sizes
    force_draw_overlap: bool
    merge_building_labels: bool
    font_size_parcel: List[int]
    font_size_street: List[int]
    font_size_building: List[int]
    scale_distance_parcel: Dict[int, float]
    scale_arrow_length_map: Dict[int, float]
    min_arrow_length: float
    building_label_padding: float
    parcel_label_padding: float
    street_label_distance: float
    free_parcel_area_threshold: Dict[int, float]

    # street_center_wms
    street_center_wms_url: str
    street_center_wms_point_distance: float
    street_center_min_length: int

    # db_type
    db_type: str

    # symbols
    symbol_name_parcel_part: str
    symbol_size_parcel_part: Dict[int, float]

    # tolerance / checks
    scale_street_line_segment: Dict[int, float]
    building_polygon_simplify_area: Dict[int, float]
    check_street_length_area_ratio: float
    check_street_draw_name_area: float
    link_parcel_symbol_distance: float
    link_parcel_symbol_near_length: float
    street_min_area: float
    parcel_min_area: float
    nearby_street_overlap_ratio: float
    min_free_geom_area: float
    building_difference_area: float

    # gecko
    gecko_path: str
    firefox_path: str

    # chrome
    chrome_path: str

    # fonts
    font_path: str
    font_bold_path: str
    font_italic_path: str
    font_bold_italic_path: str

    # debug
    show_label_rect: bool
    show_highway: bool


def load_config(run_id: str = "") -> Config:
    path = "settings.ini"
    if run_id != "":
        cfg = load_config("")
        path = os.path.join(cfg.backup_ini_path, f"settings_{run_id}.ini")

    cfg = configparser.ConfigParser()
    if not cfg.read(path):
        raise FileNotFoundError(f"Could not read INI at {os.path.abspath(path)}")

    # --- area / scale ---
    input_bbox = _parse_bbox(cfg.get("area", "input_bbox"))
    bbox = _parse_bbox(cfg.get("area", "bbox"))
    by_whole_bbox = _parse_bbox(cfg.get("area", "by_whole_bbox"))
    scale = cfg.getint("area", "scale", fallback=1000)
    dpi = cfg.getfloat("area", "dpi", fallback=96.0)

    # --- service availability (online/offline state) ---
    alkis_parcel_sync_wms_available = cfg.getboolean("service_availability", "alkis_parcel_sync_wms_available", fallback=True)
    atlas_dashed_wms_available = cfg.getboolean("service_availability", "atlas_dashed_wms_available", fallback=True)
    street_center_wms_available = cfg.getboolean("service_availability", "street_center_wms_available", fallback=True)
    atlas_search_available = cfg.getboolean("service_availability", "atlas_search_available", fallback=True)

    # --- connection ---
    host = cfg.get("db_connection", "host")
    port = cfg.getint("db_connection", "port")
    user = cfg.get("db_connection", "user")
    password = cfg.get("db_connection", "password")
    dbname = cfg.get("db_connection", "dbname")
    dbname_address = cfg.get("db_connection", "dbname_address")
    dbname_tn = cfg.get("db_connection", "dbname_tn")
    dbname_boundary = cfg.get("db_connection", "dbname_boundary")
    dbname_building = cfg.get("db_connection", "dbname_building")
    dbname_vertex = cfg.get("db_connection", "dbname_vertex")

    # --- lod2d ---
    fail_script = cfg.getboolean("lod2d", "fail_script", fallback=False)
    lod_host = cfg.get("lod2d", "lod_host")
    lod_port = cfg.getint("lod2d", "lod_port")
    lod_user = cfg.get("lod2d", "lod_user")
    lod_password = cfg.get("lod2d", "lod_password")
    lod_dbname = cfg.get("lod2d", "lod_dbname")

    # --- alkis_import
    sh_path = cfg.get("alkis_import", "sh_path")
    xml_path = cfg.get("alkis_import", "xml_path")
    lst_path = cfg.get("alkis_import", "lst_path")

    # --- proxy ---
    proxy_file = cfg.get("proxy", "proxy_file")
    launcher_url = cfg.get("proxy", "launcher_url")

    # --- parcel_difference ---
    api_url = cfg.get("parcel_difference", "api_url")
    continue_xpath = cfg.get("parcel_difference", "continue_xpath")

    # --- building_color ---
    building_source = cfg.get("building_color", "building_source", fallback="runtime")
    building_url = cfg.get("building_color", "building_url")
    building_path = cfg.get("building_color", "building_path")

    # --- vertex ---
    vertex_source = cfg.get("vertex", "vertex_source", fallback="runtime")
    vertex_path = cfg.get("vertex", "vertex_path")

    # --- atlas account ---
    atlas_username = cfg.get("atlas_account", "atlas_username")
    atlas_password = cfg.get("atlas_account", "atlas_password")

    # --- dashed_wms_path ---
    dashed_wms_path = cfg.get("dashed_line_detection", "dashed_wms_path")

    # --- log ---
    log_path = cfg.get("log", "log_path")

    # --- backup path ---
    backup_ini_path = cfg.get("backup_path", "backup_ini_path")
    parcel_snapshots_path = cfg.get("backup_path", "parcel_snapshots_path")

    # --- nas_xml---
    force_null_zaehler_in_nas_xml = cfg.getboolean("nas_xml", "force_null_zaehler_in_nas_xml")

    # --- gpkg ---
    gpkg_url = cfg.get("gpkg", "gpkg_url")
    gpkg_out_dir = cfg.get("gpkg", "gpkg_out_dir")

    # --- shape ---
    shape_path = cfg.get("shape", "shape_path")

    # --- compute ---
    cpu_setting = cfg.get("compute", "cpu_cores", fallback="auto").strip().lower()
    cpu_cap = cfg.getint("compute", "cpu_cores_max", fallback=16)
    if cpu_setting == "auto":
        cpu_cores = max(1, min(cpu_count(), cpu_cap))
    else:
        cpu_cores = max(1, int(cpu_setting))

    # --- splitting ---
    primary_splitter = cfg.get("splitting", "primary_splitter", fallback="/")
    auxiliary_splitter = cfg.get("splitting", "auxiliary_splitter", fallback="/")

    # --- parcel sync ---
    parcel_sample_step_m = cfg.getfloat("parcel_sync", "sample_step_m", fallback=10.0)

    # --- labels & sizes (JSON mappings) ---
    force_draw_overlap = cfg.getboolean("labels", "force_draw_overlap", fallback=False)
    merge_building_labels = cfg.getboolean("labels", "merge_building_labels", fallback=False)
    font_size_parcel = json.loads(cfg.get("labels", "font_size_parcel"))
    font_size_street = json.loads(cfg.get("labels", "font_size_street"))
    font_size_building = json.loads(cfg.get("labels", "font_size_building"))
    scale_distance_parcel = _parse_json_dict(cfg.get("labels", "scale_distance_parcel"))
    scale_arrow_length_map = _parse_json_dict(cfg.get("labels", "scale_arrow_length_map"))
    min_arrow_length = cfg.getfloat("labels", "min_arrow_length")
    building_label_padding =cfg.getfloat("labels", "building_label_padding")
    parcel_label_padding = cfg.getfloat("labels", "parcel_label_padding")
    street_label_distance = cfg.getfloat("labels", "street_label_distance")
    free_parcel_area_threshold = _parse_json_dict(cfg.get("labels", "free_parcel_area_threshold"))

    # --- street_center_wms ---
    street_center_wms_url = cfg.get("street_center_wms", "street_center_wms_url")
    street_center_wms_point_distance = cfg.getfloat("street_center_wms", "street_center_wms_point_distance")
    street_center_min_length = cfg.getint("street_center_wms", "street_center_min_length")

    # --- db_type ---
    db_type = cfg.get("db_type", "db_type")

    # --- symbols ---
    symbol_name_parcel_part = cfg.get("symbols", "symbol_name_parcel_part")
    symbol_size_parcel_part = _parse_json_dict(cfg.get("symbols", "symbol_size_parcel_part"))

    # --- tolerance / checks ---
    scale_street_line_segment = _parse_json_dict(cfg.get("tolerance", "scale_street_line_segment"))
    building_polygon_simplify_area = _parse_json_dict(cfg.get("tolerance", "building_polygon_simplify_area"))
    check_street_length_area_ratio = cfg.getfloat("tolerance", "check_street_length_area_ratio")
    check_street_draw_name_area = cfg.getfloat("tolerance", "check_street_draw_name_area")
    link_parcel_symbol_distance = cfg.getfloat("tolerance", "link_parcel_symbol_distance")
    link_parcel_symbol_near_length = cfg.getfloat("tolerance", "link_parcel_symbol_near_length")
    street_min_area = cfg.getfloat("tolerance", "street_min_area")
    parcel_min_area = cfg.getfloat("tolerance", "parcel_min_area")
    nearby_street_overlap_ratio = cfg.getfloat("tolerance", "nearby_street_overlap_ratio", fallback=0.333)
    min_free_geom_area = cfg.getfloat("tolerance", "min_free_geom_area", fallback=60.0)
    building_difference_area = cfg.getfloat("tolerance", "building_difference_area", fallback=5.0)

    # --- gecko/firefox ---
    gecko_firefox = _pick_gecko_paths(cfg)

    # --- chrome ---
    chrome = _pick_chrome_paths(cfg)

    # --- fonts ---
    fonts = _pick_font_paths(cfg)

    # --- debug ---
    show_label_rect = cfg.getboolean("debug", "show_label_rect")
    show_highway = cfg.getboolean("debug", "show_highway", fallback=False)

    return Config(
        input_bbox=input_bbox,
        bbox=bbox,
        by_whole_bbox=by_whole_bbox,
        scale=scale,
        dpi=dpi,
        alkis_parcel_sync_wms_available=alkis_parcel_sync_wms_available,
        atlas_dashed_wms_available=atlas_dashed_wms_available,
        street_center_wms_available=street_center_wms_available,
        atlas_search_available=atlas_search_available,
        host=host,
        port=port,
        user=user,
        password=password,
        dbname=dbname,
        dbname_address=dbname_address,
        dbname_tn=dbname_tn,
        dbname_boundary=dbname_boundary,
        dbname_building=dbname_building,
        dbname_vertex=dbname_vertex,
        fail_script=fail_script,
        lod_host=lod_host,
        lod_port=lod_port,
        lod_user=lod_user,
        lod_password=lod_password,
        lod_dbname=lod_dbname,
        sh_path=sh_path,
        xml_path=xml_path,
        lst_path=lst_path,
        proxy_file=proxy_file,
        launcher_url=launcher_url,
        api_url=api_url,
        continue_xpath=continue_xpath,
        building_source=building_source,
        building_url=building_url,
        building_path=building_path,
        vertex_source=vertex_source,
        vertex_path=vertex_path,
        atlas_username=atlas_username,
        atlas_password=atlas_password,
        dashed_wms_path=dashed_wms_path,
        log_path=log_path,
        backup_ini_path=backup_ini_path,
        parcel_snapshots_path=parcel_snapshots_path,
        force_null_zaehler_in_nas_xml=force_null_zaehler_in_nas_xml,
        gpkg_url=gpkg_url,
        gpkg_out_dir=gpkg_out_dir,
        shape_path=shape_path,
        cpu_cores=cpu_cores,
        primary_splitter=primary_splitter,
        auxiliary_splitter=auxiliary_splitter,
        parcel_sample_step_m=parcel_sample_step_m,
        force_draw_overlap=force_draw_overlap,
        merge_building_labels=merge_building_labels,
        font_size_parcel=font_size_parcel,
        font_size_street=font_size_street,
        font_size_building=font_size_building,
        scale_distance_parcel=scale_distance_parcel,
        scale_arrow_length_map=scale_arrow_length_map,
        min_arrow_length=min_arrow_length,
        building_label_padding=building_label_padding,
        parcel_label_padding=parcel_label_padding,
        street_label_distance=street_label_distance,
        free_parcel_area_threshold=free_parcel_area_threshold,
        street_center_wms_url=street_center_wms_url,
        street_center_wms_point_distance=street_center_wms_point_distance,
        street_center_min_length=street_center_min_length,
        db_type=db_type,
        symbol_name_parcel_part=symbol_name_parcel_part,
        symbol_size_parcel_part=symbol_size_parcel_part,
        scale_street_line_segment=scale_street_line_segment,
        building_polygon_simplify_area=building_polygon_simplify_area,
        check_street_length_area_ratio=check_street_length_area_ratio,
        check_street_draw_name_area=check_street_draw_name_area,
        link_parcel_symbol_distance=link_parcel_symbol_distance,
        link_parcel_symbol_near_length=link_parcel_symbol_near_length,
        street_min_area=street_min_area,
        parcel_min_area=parcel_min_area,
        nearby_street_overlap_ratio=nearby_street_overlap_ratio,
        min_free_geom_area=min_free_geom_area,
        building_difference_area=building_difference_area,
        gecko_path=gecko_firefox["gecko_path"],
        firefox_path=gecko_firefox["firefox_path"],
        chrome_path=chrome["chrome_path"],
        font_path=fonts["font_path"],
        font_bold_path=fonts["font_bold_path"],
        font_italic_path=fonts["font_italic_path"],
        font_bold_italic_path=fonts["font_bold_italic_path"],
        show_label_rect=show_label_rect,
        show_highway=show_highway
    )
