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

parcel_polygon_map = {
    "Wohnbaufläche": {
        "layer": "AX_Wohnbauflaeche"
    },
    "Industrie- und Gewerbefläche": {
        "layer": "AX_IndustrieUndGewerbeflaeche",
        "art": "FKT"
    },
    "Halde": {
        "layer": "AX_Halde"
    },
    "Bergbaubetrieb": {
        "layer": "AX_Bergbaubetrieb"
    },
    "Tagebau, Grube, Steinbruch": {
        "layer": "AX_TagebauGrubeSteinbruch",
        "art": "FKT"
    },
    "Fläche gemischter Nutzung": {
        "layer": "AX_FlaecheGemischterNutzung"
    },
    "Fläche besonderer funktionaler Prägung": {
        "layer": "AX_FlaecheBesondererFunktionalerPraegung"
    },
    "Sport-, Freizeit- und Erholungsfläche": {
        "layer": "AX_SportFreizeitUndErholungsflaeche",
        "art": "FKT"
    },
    "Friedhof": {
        "layer": "AX_Friedhof"
    },
    "Straßen- und Wegeverkehr": {
        "layer": "AX_Strassenverkehr",
        "condition": {
            "zustand": "=0",
            "funktion": "<>5130"
        }
    },
    "Platz": {
        "layer": "AX_Platz",
        "art": "FKT",
        "Raststätte, Autohof": {
            "condition": {
                "funktion": "<>5130"
            }
        },
        "Fußgängerzone": {
            "condition": {
                "funktion": "=5130"
            }
        }
    },
    "Weg": {
        "layer": "AX_Weg",
        "art": "FKT"
    },
    "Straßenverkehr": {
        "layer": "AX_Strassenverkehr",
        "condition": {
            "zustand": "=0",
            "funktion": "<>5130"
        },
        "Fußgängerzone": {
            "condition": {
                "zustand": "=0",
                "funktion": "=5130"
            }
        }
    },
    "Bahnverkehr": {
        "layer": "AX_Bahnverkehr",
        "art": "BKT",
        "condition": {
            "zustand": "=0",
            "funktion": "<>2322"
        },
    },
    "Flugverkehr": {
        "layer": "AX_Flugverkehr",
        "art": "ART",
        "condition": {
            "zustand": "<>4000",
        },
        "Internationaler Flughafen": {
            "condition": {
                "zustand": "<>4000",
            },
        },
        "Landeplatz, Sonderlandeplatz": {
            "condition": {
                "zustand": "<>4000",
            },
        },
        "Regionalflughafen": {
            "condition": {
                "zustand": "<>4000",
            },
        },
        "Verkehrslandeplatz": {
            "condition": {
                "zustand": "<>4000",
            },
        },
        "Hubschrauberlandeplatz": {
            "condition": {
                "zustand": "<>4000",
            },
        },
        "Segelfluggelände": {
            "condition": {
                "zustand": "<>4000",
            },
        },
    },
    "Schiffsverkehr": {
        "layer": "AX_Schiffsverkehr",
        "condition": {
            "zustand": "<>4000",
        },
        "Anlegestelle": {
            "condition": {
                "zustand": "<>4000",
            },
        },
        "Fähranlage": {
            "condition": {
                "zustand": "<>4000",
            },
        },
        "Hafenanlage (Landfläche)": {
            "condition": {
                "zustand": "<>4000",
            },
        },
        "Schleuse (Landfläche)": {
            "condition": {
                "zustand": "<>4000",
            },
        },
    },
    "Landwirtschaft": {
        "layer": "AX_Landwirtschaft",
        "art": "VEG",
        "condition": {
            "vegetationsmerkmal": "=0"
        },
        "Ackerland": {
            "condition": {
                "vegetationsmerkmal": "=0"
            }
        },
        "Hopfen": {
            "condition": {
                "vegetationsmerkmal": "=0"
            }
        },
        "Grünland": {
            "condition": {
                "vegetationsmerkmal": "=1020"
            }
        },
        "Baumschule": {
            "condition": {
                "vegetationsmerkmal": "=1020"
            }
        },
        "Obst- und Nussplantage": {
            "condition": {
                "vegetationsmerkmal": "=1020"
            }
        },
    },
    "Wald": {
        "layer": "AX_Wald",
        "art": "VEG"
    },
    "Gehölz": {
        "layer": "AX_Gehoelz",
        "art": "VEG"
    },
    "Heide": {
        "layer": "AX_Heide",
        "art": "Heide"
    },
    "Moor": {
        "layer": "AX_Moor",
        "art": "Moor"
    },
    "Sumpf": {
        "layer": "AX_Sumpf",
        "art": "Sumpf"
    },
    "Unland, Vegetationslose Fläche": {
        "layer": "AX_UnlandVegetationsloseFlaeche",
        "art": "OFM"
    },
    "Unland/Vegetationslose Fläche": {
        "layer": "AX_UnlandVegetationsloseFlaeche",
        "art": "OFM"
    },
    "Fließgewässer": {
        "layer": "AX_Fliessgewaesser",
        "art": "FKT"
    },
    "Hafenbecken": {
        "layer": "AX_Hafenbecken",
        "art": "FKT"
    },
    "Stehendes Gewässer": {
        "layer": "AX_StehendesGewaesser",
        "art": "FKT"
    },
    "Meer": {
        "layer": "AX_Meer",
        "art": "FKT"
    },
    "Unkultivierte Fläche": {
        "layer": "AX_UnlandVegetationsloseFlaeche",
        "art": "OFM"
    }
}

ns_gml = "http://www.opengis.net/gml/3.2"
ns_adv = "http://www.adv-online.de/namespaces/adv/gid/6.0"
ns_xlink = "http://www.w3.org/1999/xlink"

# Register namespaces
ET.register_namespace("gml", ns_gml)
ET.register_namespace("adv", ns_adv)


def add_parcel_polygon_feature_member_parcel(feature_member, gml_id, geom_wkb, numerator, denominator, index, label_id):
    layer_name = 'AX_Flurstueck'

    # Create layer
    ax_element = util.create_common_tag(feature_member, layer_name, gml_id)

    # attributes
    val_gemarkungsnummer = str(index // 1000).zfill(4)
    val_land = "09"
    val_flurstueckskennzeichen = (
        val_land
        + val_gemarkungsnummer
        + str(index % 1000).zfill(3)
        + numerator.zfill(5)
        + (denominator.zfill(4) if denominator is not None else "0000")
    )
    val_flurstueckskennzeichen = val_flurstueckskennzeichen.ljust(20, "_")
    val_gemeindezugehoerigkeit_map = {
        "gemeinde": str(index).zfill(3),
        "kreis": str(index).zfill(2),
        "land": "09",
        "regierungsbezirk": "0"
    }

    # adv:gemarkung
    gemarkung = ET.SubElement(ax_element, f"{{{ns_adv}}}gemarkung")
    ax_gemarkung_schluessel = ET.SubElement(gemarkung, f"{{{ns_adv}}}AX_Gemarkung_Schluessel")

    ## adv:land
    land = ET.SubElement(ax_gemarkung_schluessel, f"{{{ns_adv}}}land")
    land.text = val_land

    ## adv:val_gemarkungsnummer
    gemarkungsnummer = ET.SubElement(ax_gemarkung_schluessel, f"{{{ns_adv}}}gemarkungsnummer")
    gemarkungsnummer.text = val_gemarkungsnummer

    # adv:flurstuecksnummer
    flurstuecksnummer = ET.SubElement(ax_element, f"{{{ns_adv}}}flurstuecksnummer")
    ax_flurstuecksnummer = ET.SubElement(flurstuecksnummer, f"{{{ns_adv}}}AX_Flurstuecksnummer")

    ## adv:zaehler
    zaehler = ET.SubElement(ax_flurstuecksnummer, f"{{{ns_adv}}}zaehler")
    zaehler.text = numerator

    ## adv:nenner
    if denominator is not None and denominator != "":
        nenner = ET.SubElement(ax_flurstuecksnummer, f"{{{ns_adv}}}nenner")
        nenner.text = denominator

    # adv:amtlicheflaeche - parcel area (square meters)
    geom = wkb.loads(bytes(geom_wkb))
    amtlicheFlaeche = ET.SubElement(ax_element, f"{{{ns_adv}}}amtlicheFlaeche")
    amtlicheFlaeche.set("uom", "urn:adv:uom:m2")
    amtlicheFlaeche.text = f"{geom.area:.2f}"

    # adv:flurstueckskennzeichen - generated by unique number
    flurstueckskennzeichen = ET.SubElement(ax_element, f"{{{ns_adv}}}flurstueckskennzeichen")
    flurstueckskennzeichen.text = val_flurstueckskennzeichen

    # adv:gemeindezugehoerigkeit
    gemeindezugehoerigkeit = ET.SubElement(ax_element, f"{{{ns_adv}}}gemeindezugehoerigkeit")
    ax_gemeindekennzeichen = ET.SubElement(gemeindezugehoerigkeit, f"{{{ns_adv}}}AX_Gemeindekennzeichen")
    for key, val in val_gemeindezugehoerigkeit_map.items():
        item = ET.SubElement(ax_gemeindekennzeichen, f"{{{ns_adv}}}{key}")
        item.text = val

    # adv:position
    util.add_position_tag(ax_element, geom_wkb)

    # if label not exist, set endet as HIDDEN to avoid default label rendering in NAS
    if label_id is None:
        # adv:lebenszeitintervall
        lebenszeitintervall = ET.SubElement(ax_element, f"{{{ns_adv}}}lebenszeitintervall")
        # AA_Lebenszeitintervall
        aa_lebenszeitintervall = ET.SubElement(lebenszeitintervall, f"{{{ns_adv}}}AA_Lebenszeitintervall")
        # adv:endet
        endet = ET.SubElement(aa_lebenszeitintervall, f"{{{ns_adv}}}endet")
        endet.text = "HIDDEN"

def add_parcel_polygon_feature_member_tn(feature_member, bez, layer_info, gml_id, geom_wkb):
    layer_name = layer_info['layer']

    # Create layer
    ax_element = util.create_common_tag(feature_member, layer_name, gml_id)

    # adv:position
    util.add_position_tag(ax_element, geom_wkb)

    # add condition
    condition = layer_info["condition"] if "condition" in layer_info else None
    if bez in layer_info:
        condition = layer_info[bez]["condition"]
        
    if condition is not None:
        for key, value in condition.items():
            condition_element = ET.SubElement(ax_element, f"{{{ns_adv}}}{key}")
            if value.startswith("<>"):
                condition_element.text = "0"
            elif value.startswith("="):
                condition_element.text = value[1:]

def add_label_feature_member_darstellung(feature_member, index, gml_id, art_text):
    layer_name = 'AP_Darstellung'
    layer_gml_id = util.generate_gml_id('Darstellung', index, f"DEby")

    # Create layer
    ax_element = util.create_common_tag(feature_member, layer_name, layer_gml_id)

    # adv:dientZurDarstellungVon
    dientZurDarstellungVon = ET.SubElement(ax_element, f"{{{ns_adv}}}dientZurDarstellungVon")
    dientZurDarstellungVon.set(f"{{{ns_xlink}}}href", f"urn:adv:oid:{gml_id}")
    
    # adv:art
    art = ET.SubElement(ax_element, f"{{{ns_adv}}}art")
    art.text = art_text
    
    # adv:signaturnummer
    signaturnummer = ET.SubElement(ax_element, f"{{{ns_adv}}}signaturnummer")
    signaturnummer.text = "6000"
    

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

    index = 0
    # Insert TN polygons data
    query = f"""
        SELECT id, gml_id, nutzart, bez, ST_AsBinary(geom) FROM by_bbox_landkreis_{run_id}
    """

    data = db_crud.run_query(conn, query)
    for row in data:
        id, gml_id, nutzart, bez, geom_wb = row
        
        layer_info = parcel_polygon_map[nutzart]
        if gml_id is not None:
            gml_id = util.convert_alkis_gml_id(gml_id)
        else:
            gml_id = util.generate_gml_id(layer_info["layer"], id, "TN")

        # TN xml
        feature_member = ET.SubElement(
            root,
            f"{{{ns_gml}}}featureMember"
        )

        add_parcel_polygon_feature_member_tn(feature_member, bez, layer_info, gml_id, geom_wb)
        if "art" in layer_info:
            add_label_feature_member_darstellung(feature_member, index, gml_id, layer_info["art"])

        index += 1

    # Insert by_parcel polygons
    query = f"""
        SELECT 
            parcel.id, 
            parcel.gml_id, 
            COALESCE(parcel.numerator, ''), 
            COALESCE(parcel.denominator, ''), 
            ST_AsBinary(parcel.geom),
            label.id label_id
        FROM by_bbox_parcel_{run_id} parcel
        LEFT OUTER JOIN by_bbox_labels_{run_id} label ON label.source_id = parcel.id
    """
    data = db_crud.run_query(conn, query)
    for row in data:
        _, gml_id, numerator, denominator, geom_wb, label_id = row
        
        gml_id = util.convert_alkis_gml_id(gml_id)
        
        # parcel xml
        feature_member = ET.SubElement(
            root,
            f"{{{ns_gml}}}featureMember"
        )
        
        add_parcel_polygon_feature_member_parcel(feature_member, gml_id, geom_wb, numerator, denominator, index, label_id)
        index += 1
        

    db_crud.close_connection(conn)

    rough_string = ET.tostring(root, encoding="utf-8").decode("utf-8")
    rough_string = rough_string.replace("&amp;#32;", "&#32;")
    
    os.makedirs(cfg.xml_path, exist_ok=True)
    util.save_nas_xml(rough_string, os.path.join(cfg.xml_path, f"{run_id}_by_nas_polygon_parcel.xml"))

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python polygon_parcel.py <id>")
        print('Example command format: "python polygon_parcel.py by_101125"')
        sys.exit(1)

    run_id = sys.argv[1]
    cfg = load_config(run_id)

    generate_nas_xml()