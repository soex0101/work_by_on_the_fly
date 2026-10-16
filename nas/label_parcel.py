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

def add_label_feature_member_pto(feature_member, index, name, geom_wkb, label_angle, gml_id, is_overlap):
    layer_name = 'AP_PTO'
    layer_gml_id = util.generate_gml_id('ParcelLabel', index, f"DEby")

    # Create layer
    ax_element = util.create_common_tag(feature_member, layer_name, layer_gml_id)

    # adv:position
    util.add_position_tag(ax_element, geom_wkb)

    # adv:drehwinkel
    drehwinkel = ET.SubElement(ax_element, f"{{{ns_adv}}}drehwinkel")
    drehwinkel.set("uom", "urn:adv:uom:rad")
    drehwinkel.text = f"{math.radians(label_angle):.2f}"

    # adv:dientZurDarstellungVon
    dientZurDarstellungVon = ET.SubElement(ax_element, f"{{{ns_adv}}}dientZurDarstellungVon")
    dientZurDarstellungVon.set(f"{{{ns_xlink}}}href", f"urn:adv:oid:{gml_id}")
    
    # adv:art
    art = ET.SubElement(ax_element, f"{{{ns_adv}}}art")
    art.text = "ZAE_NEN"
    
    # adv:skalierung
    skalierung = ET.SubElement(ax_element, f"{{{ns_adv}}}skalierung")
    skalierung.text = "1.0"
    
    # adv:signaturnummer
    signaturnummer = ET.SubElement(ax_element, f"{{{ns_adv}}}signaturnummer")
    signaturnummer.text = "6000" if not cfg.force_draw_overlap and is_overlap else "4113"
    
    # adv:fontSperrung
    fontSperrung = ET.SubElement(ax_element, f"{{{ns_adv}}}fontSperrung")
    fontSperrung.text = "0.0"
    
    # adv:horizontaleAusrichtung
    horizontaleAusrichtung = ET.SubElement(ax_element, f"{{{ns_adv}}}horizontaleAusrichtung")
    horizontaleAusrichtung.text = "zentrisch"
    
    # adv:vertikaleAusrichtung
    vertikaleAusrichtung = ET.SubElement(ax_element, f"{{{ns_adv}}}vertikaleAusrichtung")
    vertikaleAusrichtung.text = "Mitte"

    if cfg.force_null_zaehler_in_nas_xml:
        # adv:schriftinhalt
        schriftinhalt = ET.SubElement(ax_element, f"{{{ns_adv}}}schriftinhalt")
        schriftinhalt.text = name

def add_label_feature_member_darstellung(feature_member, index, font_size, gml_id, is_overlap):
    layer_name = 'AP_Darstellung'
    layer_gml_id = util.generate_gml_id('Darstellung', index, f"DEby")

    # Create layer
    ax_element = util.create_common_tag(feature_member, layer_name, layer_gml_id)

    # adv:dientZurDarstellungVon
    dientZurDarstellungVon = ET.SubElement(ax_element, f"{{{ns_adv}}}dientZurDarstellungVon")
    dientZurDarstellungVon.set(f"{{{ns_xlink}}}href", f"urn:adv:oid:{gml_id}")
    
    # adv:art
    art = ET.SubElement(ax_element, f"{{{ns_adv}}}art")
    art.text = "ZAE_NEN"
    
    # adv:signaturnummer
    signaturnummer = ET.SubElement(ax_element, f"{{{ns_adv}}}signaturnummer")
    sig_val = ""
    if not cfg.force_draw_overlap and is_overlap:
        sig_val = "6000"
    elif font_size >= cfg.font_size_parcel[0]:
        sig_val = "4113"
    elif font_size >= cfg.font_size_parcel[1]:
        sig_val = "4071"
    elif font_size >= cfg.font_size_parcel[2]:
        sig_val = "4060"
    signaturnummer.text = sig_val
    
    
def add_label_feature_member_lage(feature_member, index):
    layer_name = 'AX_LagebezeichnungOhneHausnummer'
    layer_gml_id = util.generate_gml_id('OhneHausnummer', index, f"DEby")

    # Create layer
    ax_element = util.create_common_tag(feature_member, layer_name, layer_gml_id)

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

    return layer_gml_id


def add_label_feature_member_street_pto(feature_member, index, name, geom_wkb, font_size, label_angle, gml_id, is_overlap):
    layer_name = 'AP_PTO'
    layer_gml_id = util.generate_gml_id('StreetLabel', index, f"DEby")

    # Create layer
    ax_element = util.create_common_tag(feature_member, layer_name, layer_gml_id)

    # adv:position
    util.add_position_tag(ax_element, geom_wkb)
    
    # adv:drehwinkel
    drehwinkel = ET.SubElement(ax_element, f"{{{ns_adv}}}drehwinkel")
    drehwinkel.set("uom", "urn:adv:uom:rad")
    drehwinkel.text = f"{math.radians(label_angle):.2f}"

    # adv:dientZurDarstellungVon
    dientZurDarstellungVon = ET.SubElement(ax_element, f"{{{ns_adv}}}dientZurDarstellungVon")
    dientZurDarstellungVon.set(f"{{{ns_xlink}}}href", f"urn:adv:oid:{gml_id}")
    
    # adv:art
    art = ET.SubElement(ax_element, f"{{{ns_adv}}}art")
    art.text = "Strasse"
    
    # adv:skalierung
    skalierung = ET.SubElement(ax_element, f"{{{ns_adv}}}skalierung")
    skalierung.text = "1.0"
    
    # adv:signaturnummer
    signaturnummer = ET.SubElement(ax_element, f"{{{ns_adv}}}signaturnummer")
    sig_val = ""
    if not cfg.force_draw_overlap and is_overlap:
        sig_val = "6000"
    elif font_size >= cfg.font_size_street[0]:
        sig_val = "4107"
    elif font_size >= cfg.font_size_street[1]:
        sig_val = "4074"
    elif font_size >= cfg.font_size_street[2]:
        sig_val = "4064"
    elif font_size >= cfg.font_size_street[3]:
        sig_val = "4044"
    signaturnummer.text = sig_val
    
    # adv:fontSperrung
    fontSperrung = ET.SubElement(ax_element, f"{{{ns_adv}}}fontSperrung")
    fontSperrung.text = "0.0"
    
    # adv:horizontaleAusrichtung
    horizontaleAusrichtung = ET.SubElement(ax_element, f"{{{ns_adv}}}horizontaleAusrichtung")
    horizontaleAusrichtung.text = "zentrisch"
    
    # adv:vertikaleAusrichtung
    vertikaleAusrichtung = ET.SubElement(ax_element, f"{{{ns_adv}}}vertikaleAusrichtung")
    vertikaleAusrichtung.text = "Mitte"

    # adv:schriftinhalt
    schriftinhalt = ET.SubElement(ax_element, f"{{{ns_adv}}}schriftinhalt")
    schriftinhalt.text = name
    
def generate_nas_xml():

    db_cadastral_params = {
        "dbname": cfg.dbname,
        "user": cfg.user,
        "password": cfg.password,
        "host": cfg.host,
        "port": cfg.port
    }

    # Read parcel polygon data
    # Select minimum font_size for that parcel name group
    query = f"""
        SELECT 
            p.gml_id, 
            p.name parcel_name,
            l.source_id, 
            l.name render_name, 
            ST_AsBinary(l.label_position), 
            l.font_size,
            MIN(l.font_size) OVER (
                PARTITION BY l.name
            ) AS min_font_size,
            l.alkis_angle,
            l.is_overlap
        FROM by_bbox_labels_{run_id} l
        LEFT JOIN by_bbox_parcel_{run_id} p ON l.source_id = p.id 
        WHERE source IN ('parcel', 'street')
    """

    conn = db_crud.create_connection(db_cadastral_params)
    data = db_crud.run_query(conn, query)
    db_crud.close_connection(conn)

    # Create root element
    root = ET.Element(f"{{{ns_gml}}}FeatureCollection", nsmap={"xlink": ns_xlink, "gml": ns_gml, "adv": ns_adv})

    index = 0
    added_gml_id = []
    for row in data:
        gml_id, parcel_name, _, render_name, geom_wkb, font_size, min_font_size, label_angle, is_overlap = row
        
        gml_id = util.convert_alkis_gml_id(gml_id)

        if all(c.isdigit() or c == '/' for c in render_name): # check parcel name or street label
            ############ parcel name ############
            # AP_PTO
            feature_member = ET.SubElement(
                root,
                f"{{{ns_gml}}}featureMember"
            )

            add_label_feature_member_pto(feature_member, index, render_name, geom_wkb, label_angle, gml_id, is_overlap)
        
            if gml_id is not None and not gml_id in added_gml_id:
                # AP_Darstellung
                feature_member = ET.SubElement(
                    root,
                    f"{{{ns_gml}}}featureMember"
                )

                add_label_feature_member_darstellung(feature_member, index, min_font_size, gml_id, is_overlap)
                added_gml_id.append(gml_id)
        else:
            ############ street name ############
            # AX_LagebezeichnungOhneHausnummer
            feature_member = ET.SubElement(
                root,
                f"{{{ns_gml}}}featureMember"
            )

            layer_gml_id = add_label_feature_member_lage(feature_member, index)
            
            # AP_PTO
            feature_member = ET.SubElement(
                root,
                f"{{{ns_gml}}}featureMember"
            )

            add_label_feature_member_street_pto(feature_member, index, render_name, geom_wkb, font_size, label_angle, layer_gml_id, is_overlap)

        index += 1
    
    rough_string = ET.tostring(root, encoding="utf-8").decode("utf-8")
    rough_string = rough_string.replace("> <", ">&#32;<")
    
    os.makedirs(cfg.xml_path, exist_ok=True)
    util.save_nas_xml(rough_string, os.path.join(cfg.xml_path, f"{run_id}_by_nas_label_parcel.xml"))

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python label_parcel.py <id>")
        print('Example command format: "python label_parcel.py by_101125"')
        sys.exit(1)

    run_id = sys.argv[1]
    cfg = load_config(run_id)

    generate_nas_xml()