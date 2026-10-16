import sys
import os
from shapely.geometry import MultiLineString
from shapely import wkb
from lxml import etree as ET
import util

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

def add_dashed_line_feature_member(feature_member, gml_id, layer_info, geom_wkb):
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

def generate_nas_xml():
    db_cadastral_params = {
        "dbname": cfg.dbname,
        "user": cfg.user,
        "password": cfg.password,
        "host": cfg.host,
        "port": cfg.port
    }

    # Read parcel polygon data
    query = f"""
        SELECT CAST(id AS varchar), '' gml_id, ST_AsBinary(solid_boundary), ST_AsBinary(dashed_boundary), 'building' type
        FROM by_bbox_building_{run_id}
        WHERE is_dashed = true

        UNION

        SELECT id, gml_id, ST_AsBinary(solid_boundary), ST_AsBinary(dashed_boundary), 'parcel' type
        FROM by_bbox_parcel_{run_id}
        WHERE is_dashed = true
    """

    conn = db_crud.create_connection(db_cadastral_params)
    data = db_crud.run_query(conn, query)
    db_crud.close_connection(conn)

    # Create root element
    root = ET.Element(f"{{{ns_gml}}}FeatureCollection", nsmap={"xlink": ns_xlink, "gml": ns_gml, "adv": ns_adv})

    for row in data:
        id, gml_id, solid_wkb, dashed_wkb, type = row
        
        if isinstance(solid_wkb, memoryview):
            solid_wkb = solid_wkb.tobytes()
        if isinstance(dashed_wkb, memoryview):
            dashed_wkb = dashed_wkb.tobytes()

        # solid_geom = wkb.loads(solid_wkb)
        dashed_geom = wkb.loads(dashed_wkb)

        # if not solid_geom.is_empty and solid_geom.geom_type == "LineString":
        #     solid_geom = MultiLineString([solid_geom])
        if not dashed_geom.is_empty and dashed_geom.geom_type == "LineString":
            dashed_geom = MultiLineString([dashed_geom])

        if gml_id == '':
            gml_id = util.generate_gml_id('build', id, f"DEby")
        else:
            gml_id = util.convert_alkis_gml_id(gml_id)

        if type == "parcel":
            layer_info = {
                "layer": "AX_BesondereFlurstuecksgrenze",
                "signaturenummer": str(2008), # It is just description, not add in nas xml
                "artDerFlurstuecksgrenze": str(2001)
            }

            feature_member = ET.SubElement(
                root,
                f"{{{ns_gml}}}featureMember"
            )

            add_dashed_line_feature_member(feature_member, gml_id, layer_info, dashed_geom.wkb)
            
    rough_string = ET.tostring(root, encoding="utf-8").decode("utf-8")
    
    os.makedirs(cfg.xml_path, exist_ok=True)
    util.save_nas_xml(rough_string, os.path.join(cfg.xml_path, f"{run_id}_by_nas_line_dashed.xml"))

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python line_dashed.py <id>")
        print('Example command format: "python line_dashed.py by_101125"')
        sys.exit(1)

    run_id = sys.argv[1]
    cfg = load_config(run_id)

    generate_nas_xml()