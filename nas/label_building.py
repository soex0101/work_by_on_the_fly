import sys
import os
from lxml import etree as ET
import math
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

def add_label_feature_member_lage(feature_member, index, gml_id, name):
    layer_name = 'AX_LagebezeichnungMitHausnummer'

    # Create layer
    ax_element = util.create_common_tag(feature_member, layer_name, gml_id)
    
    # adv:lagebezeichnung
    lagebezeichnung = ET.SubElement(ax_element, f"{{{ns_adv}}}lagebezeichnung")
    
    ## adv:AX_Lagebezeichnung
    ax_lagebezeichnung = ET.SubElement(lagebezeichnung, f"{{{ns_adv}}}AX_Lagebezeichnung")
    
    ### adv:verschluesselt
    verschluesselt = ET.SubElement(ax_lagebezeichnung, f"{{{ns_adv}}}verschluesselt")
    
    ### adv:AX_VerschluesselteLagebezeichnung
    ax_verschluesselteLagebezeichnung = ET.SubElement(verschluesselt, f"{{{ns_adv}}}AX_VerschluesselteLagebezeichnung")

    val_verschluesselteLagebezeichnung_map = {
        "gemeinde": str(index).zfill(3),
        "kreis": str(index).zfill(2),
        "lage": str(index).zfill(3),
        "land": "09",
        "regierungsbezirk": "0"
    }
    for key, val in val_verschluesselteLagebezeichnung_map.items():
        item = ET.SubElement(ax_verschluesselteLagebezeichnung, f"{{{ns_adv}}}{key}")
        item.text = val

    # adv:hausnummer
    hausnummer = ET.SubElement(ax_element, f"{{{ns_adv}}}hausnummer")
    hausnummer.text = name

def add_label_feature_member_pto(feature_member, index, id, name, geom_wkb, font_size, label_angle, gml_id):
    layer_name = 'AP_PTO'
    layer_gml_id = util.generate_gml_id('buldlbl', index, f"DEby")

    # Create layer
    ax_element = util.create_common_tag(feature_member, layer_name, layer_gml_id)

    # adv:position
    util.add_position_tag(ax_element, geom_wkb)

    # adv:schriftinhalt
    if name != '':
        schriftinhalt = ET.SubElement(ax_element, f"{{{ns_adv}}}schriftinhalt")
        schriftinhalt.text = name
    
    # adv:drehwinkel
    drehwinkel = ET.SubElement(ax_element, f"{{{ns_adv}}}drehwinkel")
    drehwinkel.set("uom", "urn:adv:uom:rad")
    drehwinkel.text = f"{math.radians(label_angle):.2f}"

    # adv:dientZurDarstellungVon
    dientZurDarstellungVon = ET.SubElement(ax_element, f"{{{ns_adv}}}dientZurDarstellungVon")
    dientZurDarstellungVon.set(f"{{{ns_xlink}}}href", f"urn:adv:oid:{gml_id}")
    
    # adv:art
    art = ET.SubElement(ax_element, f"{{{ns_adv}}}art")
    art.text = "HNR"
    
    # adv:signaturnummer
    signaturnummer = ET.SubElement(ax_element, f"{{{ns_adv}}}signaturnummer")
    sig = "4070"
    if font_size == cfg.font_size_building[1]:
        sig = "4060"
    elif font_size == cfg.font_size_building[2]:
        sig = "4040"
    elif font_size == cfg.font_size_building[3]:
        sig = "4030"
    signaturnummer.text = sig
    
    # adv:skalierung
    skalierung = ET.SubElement(ax_element, f"{{{ns_adv}}}skalierung")
    skalierung.text = "1.0"
    
    # adv:fontSperrung
    fontSperrung = ET.SubElement(ax_element, f"{{{ns_adv}}}fontSperrung")
    fontSperrung.text = "0.0"
    
    # adv:horizontaleAusrichtung
    horizontaleAusrichtung = ET.SubElement(ax_element, f"{{{ns_adv}}}horizontaleAusrichtung")
    horizontaleAusrichtung.text = "zentrisch"
    
    # adv:vertikaleAusrichtung
    vertikaleAusrichtung = ET.SubElement(ax_element, f"{{{ns_adv}}}vertikaleAusrichtung")
    vertikaleAusrichtung.text = "Mitte"
    
def generate_nas_xml():

    db_cadastral_params = {
        "dbname": cfg.dbname,
        "user": cfg.user,
        "password": cfg.password,
        "host": cfg.host,
        "port": cfg.port
    }

    # Read parcel polygon data
    ##################### Add general building label #####################
    query = f"""
        SELECT source_id, source, name, ST_AsBinary(label_position), font_size, alkis_angle
        FROM by_bbox_labels_{run_id}
        WHERE source IN ('building', 'building_no_geom', 'building_no_street')
    """

    conn = db_crud.create_connection(db_cadastral_params)
    data = db_crud.run_query(conn, query)
    db_crud.close_connection(conn)

    # Create root element
    root = ET.Element(f"{{{ns_gml}}}FeatureCollection", nsmap={"xlink": ns_xlink, "gml": ns_gml, "adv": ns_adv})

    index = 0
    for row in data:
        id, source, name, geom_wkb, font_size, label_angle = row

        if source == 'building':
            gml_id = util.generate_gml_id('build', id, f"DEby")
        else:
            gml_id = util.convert_alkis_gml_id(id)
        # AX_LagebezeichnungMitHausnummer
        feature_member_lage = ET.SubElement(
            root,
            f"{{{ns_gml}}}featureMember"
        )

        add_label_feature_member_lage(feature_member_lage, index, gml_id, name)

        if source == 'building_no_geom':
            name = ''
        
        # AP_PTO
        feature_member = ET.SubElement(
            root,
            f"{{{ns_gml}}}featureMember"
        )

        add_label_feature_member_pto(feature_member, index, id, name, geom_wkb, font_size, label_angle, gml_id)
        
        index += 1
    
    rough_string = ET.tostring(root, encoding="utf-8").decode("utf-8")
    rough_string = rough_string.replace("> <", ">&#32;<")
    
    os.makedirs(cfg.xml_path, exist_ok=True)
    util.save_nas_xml(rough_string, os.path.join(cfg.xml_path, f"{run_id}_by_nas_label_building.xml"))

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python label_building.py <id>")
        print('Example command format: "python label_building.py by_101125"')
        sys.exit(1)

    run_id = sys.argv[1]
    cfg = load_config(run_id)

    generate_nas_xml()