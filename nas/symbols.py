import sys
import os
import string
from datetime import datetime
from lxml import etree as ET
import util
import math


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

def next_gml_id(gml_id, prefix="DEBY", suffix="FL"):
    # Extract middle part
    middle = gml_id[len(prefix):-len(suffix)]
    
    # Define base62 alphabet
    alphabet = string.digits + string.ascii_uppercase + string.ascii_lowercase
    base = len(alphabet)
    
    # Convert middle part to a number
    def to_number(s):
        n = 0
        for c in s:
            n = n * base + alphabet.index(c)
        return n
    
    # Convert number back to string
    def to_string(n, length):
        s = ""
        for _ in range(length):
            s = alphabet[n % base] + s
            n //= base
        return s.zfill(length)
    
    # Increment and rebuild
    num = to_number(middle)
    next_middle = to_string(num + 1, len(middle))
    return prefix + next_middle + suffix

def add_symbol_feature_member(feature_member, index, gml_id, symbol_name, angle, geom_wkb):
    layer_name = 'AP_PPO'
    ap_ppo_gml_id = util.generate_gml_id("Symbol", index, f"DEby")

    # AP_PPO
    ax_element = ET.SubElement(
        feature_member,
        f"{{{ns_adv}}}{layer_name}",
        {f"{{{ns_gml}}}id": ap_ppo_gml_id},
    )

    # gml:identifier
    identifier = ET.SubElement(
        ax_element,
        f"{{{ns_gml}}}identifier",
        {"codeSpace": "http://www.adv-online.de/"},
    )
    identifier.text = f"urn:adv:oid:{ap_ppo_gml_id}"

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

    # adv:position
    util.add_position_tag(ax_element, geom_wkb)

    # adv:abweichenderrechtszustand
    abweichenderrechtszustand = ET.SubElement(ax_element, f"{{{ns_adv}}}abweichenderrechtszustand")
    abweichenderrechtszustand.text = "false"
    
    # adv:art
    art = ET.SubElement(ax_element, f"{{{ns_adv}}}art")
    art.text = "Haken" if "parcel_part_top" in symbol_name else ""

    # adv:dientZurDarstellungVon
    dientZurDarstellungVon = ET.SubElement(ax_element, f"{{{ns_adv}}}dientZurDarstellungVon")
    dientZurDarstellungVon.set(f"{{{ns_xlink}}}href", f"urn:adv:oid:{gml_id}")
    
    # adv:drehwinkel
    drehwinkel = ET.SubElement(ax_element, f"{{{ns_adv}}}drehwinkel")
    drehwinkel.set("uom", "urn:adv:uom:rad")
    drehwinkel.text = f"{math.radians(angle):.2f}"
    
    
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
            p.gml_id,
            s.symbol_name,
            s.alkis_angle,
            ST_AsBinary(s.position)
        FROM by_bbox_symbols_{run_id} s
        LEFT JOIN by_bbox_parcel_{run_id} p ON s.parcel_id = p.id
    """

    conn = db_crud.create_connection(db_cadastral_params)
    data = db_crud.run_query(conn, query)
    db_crud.close_connection(conn)

    # Create root element
    root = ET.Element(f"{{{ns_gml}}}FeatureCollection", nsmap={"xlink": ns_xlink, "gml": ns_gml, "adv": ns_adv})

    index = 0
    for row in data:
        gml_id, symbol_name, angle, geom_wkb = row

        gml_id = util.convert_alkis_gml_id(gml_id)
        
        feature_member = ET.SubElement(
            root,
            f"{{{ns_gml}}}featureMember"
        )
            
        add_symbol_feature_member(feature_member, index, gml_id, symbol_name, angle, geom_wkb)
        
        index += 1
    
    rough_string = ET.tostring(root, encoding="utf-8").decode("utf-8")
    
    os.makedirs(cfg.xml_path, exist_ok=True)
    util.save_nas_xml(rough_string, os.path.join(cfg.xml_path, f"{run_id}_by_nas_symbol.xml"))

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python symbols.py <id>")
        print('Example command format: "python symbols.py by_101125"')
        sys.exit(1)

    run_id = sys.argv[1]
    cfg = load_config(run_id)

    generate_nas_xml()