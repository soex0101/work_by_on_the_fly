import sys
import os

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

def add_arrow_line_feature_member(feature_member, id, geom_wkb, gml_id):
    layer_name = 'AP_LPO'
    layer_gml_id = util.generate_gml_id('LPO', id, f"DEby")

    # Create layer
    ax_element = util.create_common_tag(feature_member, layer_name, layer_gml_id)

    # adv:position
    util.add_position_tag(ax_element, geom_wkb)

    # adv:dientZurDarstellungVon
    dientZurDarstellungVon = ET.SubElement(ax_element, f"{{{ns_adv}}}dientZurDarstellungVon")
    dientZurDarstellungVon.set(f"{{{ns_xlink}}}href", f"urn:adv:oid:{gml_id}")
    
    # adv:abweichenderrechtszustand
    abweichenderrechtszustand = ET.SubElement(ax_element, f"{{{ns_adv}}}abweichenderrechtszustand")
    abweichenderrechtszustand.text = "false"
    
def generate_nas_xml():
    db_cadastral_params = {
        "dbname": cfg.dbname,
        "user": cfg.user,
        "password": cfg.password,
        "host": cfg.host,
        "port": cfg.port
    }

    # Read parcel polygon data
    # Select largest intersection per feature to get den/num
    query = f"""
        SELECT 
            l.id,
            ST_AsBinary(l.geom), 
            COALESCE(p.gml_id, b.id::text) AS gml_id
        FROM by_bbox_lines_{run_id} l
        LEFT JOIN by_bbox_parcel_{run_id} p ON l.source IN ('parcel', 'street') AND l.source_id = p.id
        LEFT JOIN by_bbox_building_{run_id} b ON l.source = 'building' AND l.source_id = b.id::text
        ORDER BY gml_id ASC
    """

    conn = db_crud.create_connection(db_cadastral_params)
    data = db_crud.run_query(conn, query)
    db_crud.close_connection(conn)

    # Create root element
    root = ET.Element(f"{{{ns_gml}}}FeatureCollection", nsmap={"xlink": ns_xlink, "gml": ns_gml, "adv": ns_adv})

    for row in data:
        id, geom_wkb, gml_id = row
        gml_id = util.convert_alkis_gml_id(gml_id)

        feature_member = ET.SubElement(
            root,
            f"{{{ns_gml}}}featureMember"
        )
            
        add_arrow_line_feature_member(feature_member, id, geom_wkb, gml_id)
    
    rough_string = ET.tostring(root, encoding="utf-8").decode("utf-8")
    
    os.makedirs(cfg.xml_path, exist_ok=True)
    util.save_nas_xml(rough_string, os.path.join(cfg.xml_path, f"{run_id}_by_nas_line_arrow.xml"))

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python line_arrow.py <id>")
        print('Example command format: "python line_arrow.py by_101125"')
        sys.exit(1)

    run_id = sys.argv[1]
    cfg = load_config(run_id)

    generate_nas_xml()