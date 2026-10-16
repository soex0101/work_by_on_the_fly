from lxml import etree as ET
from osgeo import ogr
from pathlib import Path
import re
import xml.dom.minidom
from datetime import datetime
import string

ns_gml = "http://www.opengis.net/gml/3.2"
ns_adv = "http://www.adv-online.de/namespaces/adv/gid/6.0"
ns_xlink = "http://www.w3.org/1999/xlink"

def add_position_tag(parent_element, geom_wkb):
    position = ET.SubElement(parent_element, f"{{{ns_adv}}}position")
    geometry = ogr.CreateGeometryFromWkb(bytes(geom_wkb))
    geometry_gml = geometry.ExportToGML(options=["FORMAT=GML3"])

    # Parse the GML geometry
    wrapped_gml = f'<root xmlns:gml="{ns_gml}">{geometry_gml}</root>'
    root_elem = ET.fromstring(wrapped_gml)
    geometry_elem = list(root_elem)[0]

    # Normalize namespace prefixes to "gml"
    for elem in geometry_elem.iter():
        if elem.tag.startswith("{" + ns_gml):
            local = elem.tag.split("}", 1)[1]
            elem.tag = f"{{{ns_gml}}}{local}"

    # Remove any nsX declarations that lxml might auto-add
    for key in list(geometry_elem.attrib.keys()):
        if key.startswith("xmlns:"):
            del geometry_elem.attrib[key]

    # Append to adv:position
    geometry_elem.attrib.clear()  # remove redundant xmlns
    position.append(geometry_elem)

def generate_gml_id(layer_name, id, prefix):
    clean_layer = ''.join(c for c in layer_name if c.isalnum())

    # Pad the numeric ID with leading zeros
    id_str = str(id).zfill(6)
    if len(id_str) > 6:
        id_str = id_str[-6:]

    # Start building the ID
    gml_id = f"{prefix.upper()}{clean_layer}{id_str}"

    # If longer than 16 → trim from the middle of the layer name
    if len(gml_id) > 16:
        overflow = len(gml_id) - 16
        # remove overflow chars from layer name part
        clean_layer = clean_layer[:-overflow] if overflow < len(clean_layer) else ''
        gml_id = f"{prefix.upper()}{clean_layer}{id_str}"

    # If shorter than 16 → pad with random uppercase letters
    elif len(gml_id) < 16:
        gml_id = gml_id.ljust(16, '0')

    return gml_id

def save_nas_xml(content, output_path, head_path = './alkis_nas_xml_head_7.1.xml', tail_path = './alkis_nas_xml_tail_7.1.xml'):
    head = Path(head_path).read_text(encoding="utf-8").strip().replace("\n", "").replace("\t", "")
    tail = Path(tail_path).read_text(encoding="utf-8").strip().replace("\n", "").replace("\t", "")

    # Combine them
    combined = f"{head}\n{content}\n{tail}\n"

    combined = re.sub(r'>\s+<', '><', combined)

    dom = xml.dom.minidom.parseString(combined)
    pretty_xml = dom.toprettyxml(indent="  ", encoding="utf-8")

    Path(output_path).write_bytes(pretty_xml)

def convert_alkis_gml_id(gml_id):
    if len(gml_id) == 16:
        return gml_id
    if len(gml_id) > 16:
        return gml_id[:16]
    if len(gml_id) < 16:
        return gml_id.ljust(16, '0')
    
def create_common_tag(feature_member, layer_name, gml_id):
    # AP_PTO
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

    return ax_element

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
