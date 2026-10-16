import sys
import os
from datetime import datetime
from lxml import etree as ET
import util

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from labeling import db_crud
from read_settings import load_config

cfg = None
run_id = ""

# Mapping
building_polygon_map = {
    '#e5e5e5': { # '229,229,229'
        'layer': 'AX_Gebaeude',
        'signaturnummer': '25051304', # skip it, just describe the color
        'hochhaus': 'false',
        'zustand': '0',
        'lageZurErdoberflaeche': '0',
        'bauweise': '0',
        'gebaeudefunktion': '2000'
    },
    '#b2b2b2': { # '178,178,178'
        'layer': 'AX_Gebaeude',
        'signaturnummer': '25051301', # skip it, just describe the color
        'hochhaus': 'false',
        'zustand': '0',
        'lageZurErdoberflaeche': '0',
        'gebaeudefunktion': '1000'
    },
    '#f48489': { # '244,132,137', '244,132,127'
        'layer': 'AX_Gebaeude',
        'signaturnummer': '25051309', # skip it, just describe the color
        'hochhaus': 'false',
        'zustand': '0',
        'lageZurErdoberflaeche': '0',
        'bauweise': '0',
        'gebaeudefunktion': '3000'
    },
    '#f4847f': { # '244,132,137', '244,132,127'
        'layer': 'AX_Gebaeude',
        'signaturnummer': '25051309', # skip it, just describe the color
        'hochhaus': 'false',
        'zustand': '0',
        'lageZurErdoberflaeche': '0',
        'bauweise': '0',
        'gebaeudefunktion': '3000'
    },
    'others': {
        'layer': 'AX_Bauteil',
        'signaturnummer': '2507', # No fill, border only # skip it, just describe the color
        'bauart': '1100',
        'lageZurErdoberflaeche': '0'
    }
}

ns_gml = "http://www.opengis.net/gml/3.2"
ns_adv = "http://www.adv-online.de/namespaces/adv/gid/6.0"

# Register namespaces
ET.register_namespace("gml", ns_gml)
ET.register_namespace("adv", ns_adv)

def add_building_polygon_feature_member(feature_member, gml_id, layer_info, geom_wkb):
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

def generate_building_xml():
    db_cadastral_params = {
        "dbname": cfg.dbname,
        "user": cfg.user,
        "password": cfg.password,
        "host": cfg.host,
        "port": cfg.port
    }

    # Read parcel polygon data
    query = f"""
        SELECT id, ST_AsBinary(geom), COALESCE(color, '#e5e5e5'), is_dashed
        FROM by_bbox_building_{run_id}
    """

    conn = db_crud.create_connection(db_cadastral_params)
    data = db_crud.run_query(conn, query)
    db_crud.close_connection(conn)

    # Create root element
    root = ET.Element(f"{{{ns_gml}}}FeatureCollection", nsmap={"gml": ns_gml, "adv": ns_adv})

    for row in data:
        id, geom_wkb, color, is_dashed = row

        gml_id = util.generate_gml_id('build', id, f"DEby")
        layer_info = building_polygon_map.get(color, building_polygon_map['others']).copy()
        
        # if dashed draw with dahsede
        if is_dashed:
            layer_info["lageZurErdoberflaeche"] = "1400"

        feature_member = ET.SubElement(
            root,
            f"{{{ns_gml}}}featureMember"
        )
        
        add_building_polygon_feature_member(feature_member, gml_id, layer_info, geom_wkb)
    
    
    rough_string = ET.tostring(root, encoding="utf-8").decode("utf-8")
    
    os.makedirs(cfg.xml_path, exist_ok=True)
    util.save_nas_xml(rough_string, os.path.join(cfg.xml_path, f"{run_id}_by_nas_polygon_building.xml"))

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python polygon_building.py <id>")
        print('Example command format: "python polygon_building.py by_101125"')
        sys.exit(1)

    run_id = sys.argv[1]
    cfg = load_config(run_id)

    generate_building_xml()