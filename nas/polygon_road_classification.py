import sys
import os
from shapely import wkb
from lxml import etree as ET
import util
from datetime import datetime

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from labeling import db_crud
from read_settings import load_config

cfg = None
run_id = ""

ns_gml = "http://www.opengis.net/gml/3.2"
ns_adv = "http://www.adv-online.de/namespaces/adv/gid/6.0"
ns_xlink = "http://www.w3.org/1999/xlink"

# Register namespaces
ET.register_namespace("gml", ns_gml)
ET.register_namespace("adv", ns_adv)

def add_parcel_polygon_feature_member(feature_member, gml_id, classification, geom_wkb):
    layer_name = "ax_klassifizierungnachstrassenrecht"
    # Create layer
    ax_element = util.create_common_tag(feature_member, layer_name, gml_id)

    # adv:position
    util.add_position_tag(ax_element, geom_wkb)

    condition_element = ET.SubElement(ax_element, f"{{{ns_adv}}}artderfestlegung")
    # you can refer this values from https://www.baysis.bayern.de/internet/strasseninformationen/index.html
    if classification == "BAB":
        condition_element.text = "1110" # Bundesautobahn, set signaturnummer as 1701 in 71001.sql
    elif classification == "B":
        condition_element.text = "1120" # Bundesstraße, set signaturnummer as 1701 in 71001.sql
    elif classification == "St":
        condition_element.text = "1130" # Landes- oder Staatsstraße, set signaturnummer as 1702 in 71001.sql
    elif classification == "K":
        condition_element.text = "1140" # Kreisstraße, signaturnummer is None


def generate_nas_xml():
    db_cadastral_params = {
        "dbname": cfg.dbname,
        "user": cfg.user,
        "password": cfg.password,
        "host": cfg.host,
        "port": cfg.port
    }
    conn = db_crud.create_connection(db_cadastral_params)

    # Create root element
    root = ET.Element(f"{{{ns_gml}}}FeatureCollection", nsmap={"gml": ns_gml, "adv": ns_adv})

    # Insert road classification polygons data
    query = f"""
        SELECT id, ST_AsBinary(geom), road_classification FROM by_bbox_parcel_{run_id} WHERE road_classification IS NOT NULL
    """

    data = db_crud.run_query(conn, query)
    for row in data:
        id, geom_wb, classification = row
        
        gml_id = util.generate_gml_id("klassifizierungnachstrassenrecht", id, "")

        # TN xml
        feature_member = ET.SubElement(
            root,
            f"{{{ns_gml}}}featureMember"
        )

        add_parcel_polygon_feature_member(feature_member, gml_id, classification, geom_wb)

    db_crud.close_connection(conn)

    rough_string = ET.tostring(root, encoding="utf-8").decode("utf-8")
    rough_string = rough_string.replace("&amp;#32;", "&#32;")
    
    os.makedirs(cfg.xml_path, exist_ok=True)
    util.save_nas_xml(rough_string, os.path.join(cfg.xml_path, f"{run_id}_by_nas_polygon_road_classification.xml"))

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python polygon_road_classification.py <id>")
        print('Example command format: "python polygon_road_classification.py by_101125"')
        sys.exit(1)

    run_id = sys.argv[1]
    cfg = load_config(run_id)

    generate_nas_xml()