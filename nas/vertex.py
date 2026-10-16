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

ns_gml = "http://www.opengis.net/gml/3.2"
ns_adv = "http://www.adv-online.de/namespaces/adv/gid/6.0"
ns_xlink = "http://www.w3.org/1999/xlink"

# Register namespaces
ET.register_namespace("gml", ns_gml)
ET.register_namespace("adv", ns_adv)

def add_street_label_feature_member_grenzpunkt(feature_member, gml_id):
    layer_name = 'AX_Grenzpunkt'

    # AX_Grenzpunkt
    ax_element = ET.SubElement(
        feature_member,
        f"{{{ns_adv}}}{layer_name}",
        {f"{{{ns_gml}}}id": gml_id},
    )

    # gml:identifier
    identifier = ET.SubElement(
        ax_element,
        f"{{{ns_gml}}}identifier",
        {"codeSpace": "http://www.adv-online.de/"},
    )
    identifier.text = f"urn:adv:oid:{gml_id}"

    # adv:lebenszeitintervall
    lebenszeitintervall = ET.SubElement(ax_element, f"{{{ns_adv}}}lebenszeitintervall")
    aa_lebenszeit = ET.SubElement(lebenszeitintervall, f"{{{ns_adv}}}AA_Lebenszeitintervall")
    beginnt = ET.SubElement(aa_lebenszeit, f"{{{ns_adv}}}beginnt")
    beginnt.text = datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")

    # adv:modellart (DLKM)
    modellart1 = ET.SubElement(ax_element, f"{{{ns_adv}}}modellart")
    aa_modellart1 = ET.SubElement(modellart1, f"{{{ns_adv}}}AA_Modellart")
    advStandardModell = ET.SubElement(aa_modellart1, f"{{{ns_adv}}}advStandardModell")
    advStandardModell.text = "DLKM"

    # adv:abmarkung_marke
    abmarkung_marke = ET.SubElement(ax_element, f"{{{ns_adv}}}abmarkung_Marke")
    abmarkung_marke.text = "0"

def add_street_label_feature_member_punktortta(feature_member, index, geom_wkb, gml_id):
    layer_name = 'AX_PunktortTA'
    layer_gml_id = util.generate_gml_id('PunktortTA', index, f"DEby")

    # Create layer
    ax_element = util.create_common_tag(feature_member, layer_name, layer_gml_id)

    # adv:position
    util.add_position_tag(ax_element, geom_wkb)

    # adv:istTeilVon
    istTeilVon = ET.SubElement(ax_element, f"{{{ns_adv}}}istTeilVon")
    istTeilVon.set(f"{{{ns_xlink}}}href", f"urn:adv:oid:{gml_id}")


def generate_nas_xml():
    db_vertex_params = {
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
            id, ST_AsBinary(geom) AS geom_wkb
        FROM by_bbox_vertex_{run_id}
        WHERE ST_Intersects(geom, ST_MakeEnvelope(%s, %s, %s, %s, 25832));
    """

    conn = db_crud.create_connection(db_vertex_params)
    data = db_crud.run_query(conn, query, cfg.bbox)
    db_crud.close_connection(conn)

    # Create root element
    root = ET.Element(f"{{{ns_gml}}}FeatureCollection", nsmap={"xlink": ns_xlink, "gml": ns_gml, "adv": ns_adv})

    index = 0
    for row in data:
        id, geom_wkb = row

        gml_id = util.generate_gml_id("Grenzpunkt", id, f"DEby")
        # AX_Grenzpunkt
        feature_member_grenzpunkt = ET.SubElement(
            root,
            f"{{{ns_gml}}}featureMember"
        )
        
        add_street_label_feature_member_grenzpunkt(feature_member_grenzpunkt, gml_id)
        
        # AX_PunktortTA
        feature_member_punktortta = ET.SubElement(
            root,
            f"{{{ns_gml}}}featureMember"
        )
            
        add_street_label_feature_member_punktortta(feature_member_punktortta, index, geom_wkb, gml_id)

        index += 1
    
    rough_string = ET.tostring(root, encoding="utf-8").decode("utf-8")
    
    os.makedirs(cfg.xml_path, exist_ok=True)
    util.save_nas_xml(rough_string, os.path.join(cfg.xml_path, f"{run_id}_by_nas_vertex.xml"))

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python vertex.py <id>")
        print('Example command format: "python vertex.py by_101125"')
        sys.exit(1)

    run_id = sys.argv[1]
    cfg = load_config(run_id)

    generate_nas_xml()