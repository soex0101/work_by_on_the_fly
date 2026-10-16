import sys
import os
from datetime import datetime
from lxml import etree as ET
import util
from shapely.geometry import MultiLineString
from shapely import wkb

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from labeling import db_crud
from read_settings import load_config

cfg = None
run_id = ""

ns_gml = "http://www.opengis.net/gml/3.2"
ns_adv = "http://www.adv-online.de/namespaces/adv/gid/6.0"

# Register namespaces
ET.register_namespace("gml", ns_gml)
ET.register_namespace("adv", ns_adv)

def add_boundary_polygon_feature_member(feature_member, gml_id, layer_info, geom_wkb):
    layer_name = layer_info['layer']

    # Create layer
    ax_element = util.create_common_tag(feature_member, layer_name, gml_id)

    # adv:position
    util.add_position_tag(ax_element, geom_wkb)

    # add condition
    for key, value in layer_info.items():
        if key == 'layer' or key == 'signaturenummer':
            continue
        condition_element = ET.SubElement(ax_element, f"{{{ns_adv}}}{key}")
        condition_element.text = value

def generate_parcel_xml():
    db_cadastral_params = {
        "dbname": cfg.dbname,
        "user": cfg.user,
        "password": cfg.password,
        "host": cfg.host,
        "port": cfg.port
    }

    # Read boundary lines. Kinds are already separated and de-duplicated by
    # labeling/prepare_boundary.py, so every row is drawn exactly once.
    query = f"""
        SELECT id, adf, signaturnummer, ST_AsBinary(geom)
        FROM by_bbox_boundary_{run_id}
    """

    conn = db_crud.create_connection(db_cadastral_params)
    data = db_crud.run_query(conn, query)
    db_crud.close_connection(conn)

    # Create root element
    root = ET.Element(f"{{{ns_gml}}}FeatureCollection", nsmap={"gml": ns_gml, "adv": ns_adv})

    for row in data:
        id, adf, signaturnummer, geom_wkb = row

        if isinstance(geom_wkb, memoryview):
            geom_wkb = geom_wkb.tobytes()

        geom = wkb.loads(geom_wkb)
        if geom.is_empty:
            continue
        if geom.geom_type == "LineString":
            geom = MultiLineString([geom])

        gml_id = util.generate_gml_id('bound', id, f"DEby")
        layer_info = {
            "layer": "AX_BesondereFlurstuecksgrenze",
            "signaturenummer": str(signaturnummer), # It is just description, not add in nas xml
            "artDerFlurstuecksgrenze": str(adf)
        }

        feature_member = ET.SubElement(
            root,
            f"{{{ns_gml}}}featureMember"
        )

        add_boundary_polygon_feature_member(feature_member, gml_id, layer_info, geom.wkb)


    rough_string = ET.tostring(root, encoding="utf-8").decode("utf-8")

    os.makedirs(cfg.xml_path, exist_ok=True)
    util.save_nas_xml(rough_string, os.path.join(cfg.xml_path, f"{run_id}_by_nas_line_boundary.xml"))

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python line_boundary.py <id>")
        print('Example command format: "python line_boundary.py by_101125"')
        sys.exit(1)

    run_id = sys.argv[1]
    cfg = load_config(run_id)

    generate_parcel_xml()
