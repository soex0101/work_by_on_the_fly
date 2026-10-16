import sys
import os
from shapely import wkb
from lxml import etree as ET
import util

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
        "layer": "AX_IndustrieUndGewerbeflaeche"
    },
    "Halde": {
        "layer": "AX_Halde"
    },
    "Bergbaubetrieb": {
        "layer": "AX_Bergbaubetrieb"
    },
    "Tagebau, Grube, Steinbruch": {
        "layer": "AX_TagebauGrubeSteinbruch"
    },
    "Fläche gemischter Nutzung": {
        "layer": "AX_FlaecheGemischterNutzung"
    },
    "Fläche besonderer funktionaler Prägung": {
        "layer": "AX_FlaecheBesondererFunktionalerPraegung"
    },
    "Sport-, Freizeit- und Erholungsfläche": {
        "layer": "AX_SportFreizeitUndErholungsflaeche"
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
        "layer": "AX_Weg"
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
        "condition": {
            "zustand": "=0",
            "funktion": "<>2322"
        },
    },
    "Flugverkehr": {
        "layer": "AX_Flugverkehr",
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
        "layer": "AX_Wald"
    },
    "Gehölz": {
        "layer": "AX_Gehoelz"
    },
    "Heide": {
        "layer": "AX_Heide"
    },
    "Moor": {
        "layer": "AX_Moor"
    },
    "Sumpf": {
        "layer": "AX_Sumpf"
    },
    "Unland, Vegetationslose Fläche": {
        "layer": "AX_UnlandVegetationsloseFlaeche"
    },
    "Unland/Vegetationslose Fläche": {
        "layer": "AX_UnlandVegetationsloseFlaeche"
    },
    "Fließgewässer": {
        "layer": "AX_Fliessgewaesser"
    },
    "Hafenbecken": {
        "layer": "AX_Hafenbecken"
    },
    "Stehendes Gewässer": {
        "layer": "AX_StehendesGewaesser"
    },
    "Meer": {
        "layer": "AX_Meer"
    },
    "Unkultivierte Fläche": {
        "layer": "AX_UnlandVegetationsloseFlaeche"
    }
}

ns_gml = "http://www.opengis.net/gml/3.2"
ns_adv = "http://www.adv-online.de/namespaces/adv/gid/6.0"

# Register namespaces
ET.register_namespace("gml", ns_gml)
ET.register_namespace("adv", ns_adv)


def add_label_polygon(feature_member, gml_id, geom_wkb):
    layer_name = "AX_Wohnbauflaeche"

    # Create layer
    ax_element = util.create_common_tag(feature_member, layer_name, gml_id)

    # adv:position
    util.add_position_tag(ax_element, geom_wkb)

    # add condition
    # condition = layer_info["condition"] if "condition" in layer_info else None
    # if bez in layer_info:
    #     condition = layer_info[bez]["condition"]
        
    # if condition is not None:
    #     for key, value in condition.items():
    #         condition_element = ET.SubElement(ax_element, f"{{{ns_adv}}}{key}")
    #         if value.startswith("<>"):
    #             condition_element.text = "0"
    #         elif value.startswith("="):
    #             condition_element.text = value[1:]
    
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

    # Insert by_parcel polygons
    query = f"""
        SELECT 
            id,
            ST_AsBinary(calculated_label_area)
        FROM by_bbox_labels_{run_id}
    """
    data = db_crud.run_query(conn, query)
    for row in data:
        id, geom_wkb = row
        id = util.generate_gml_id('calc', id, "DEBY")
        gml_id = util.convert_alkis_gml_id(id)
        
        # parcel xml
        feature_member = ET.SubElement(
            root,
            f"{{{ns_gml}}}featureMember"
        )
        
        add_label_polygon(feature_member, gml_id, geom_wkb)
        

    db_crud.close_connection(conn)

    rough_string = ET.tostring(root, encoding="utf-8").decode("utf-8")
    rough_string = rough_string.replace("&amp;#32;", "&#32;")
    
    os.makedirs(cfg.xml_path, exist_ok=True)
    util.save_nas_xml(rough_string, os.path.join(cfg.xml_path, f"{run_id}_by_nas_polygon_calculated_label.xml"))

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python polygon_calculated_label.py <id>")
        print('Example command format: "python polygon_calculated_label.py by_101125"')
        sys.exit(1)

    run_id = sys.argv[1]
    cfg = load_config(run_id)

    generate_nas_xml()