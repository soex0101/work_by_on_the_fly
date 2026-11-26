from pyproj import Transformer
import sys

def convert_bbox_epsg(src_epsg, dest_epsg, bbox):
    minx, miny, maxx, maxy = bbox
    transformer = Transformer.from_crs(src_epsg, dest_epsg, always_xy=True)

    new_minx, new_miny = transformer.transform(minx, miny)
    new_maxx, new_maxy = transformer.transform(maxx, maxy)

    return (new_minx, new_miny, new_maxx, new_maxy)

def convert_bbox(conversion_type, bbox):
    espg_list = ["EPSG:4326", "EPSG:3857", "EPSG:102100", "EPSG:25832", "EPSG:25833"]

    print("Converted BBOX:")
    for epsg in espg_list:
        if conversion_type in epsg:
            continue
        new_bbox = convert_bbox_epsg(conversion_type, epsg, bbox)
        new_minx, new_miny, new_maxx, new_maxy = new_bbox
        print(f"{epsg} => ({new_minx}, {new_miny}, {new_maxx}, {new_maxy})")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python epsg_transformer.py <input-epsg> <minx,miny,maxx,maxy>")
        sys.exit(1)

    conversion_type = sys.argv[1]
    bbox_str = sys.argv[2]
    minx, miny, maxx, maxy = map(float, bbox_str.split(","))

    bbox = (minx, miny, maxx, maxy)
    convert_bbox(conversion_type, bbox)
