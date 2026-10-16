import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from read_settings import load_config

cfg = None
run_id = ""

def make_lst_file():
    lst_path = os.path.join(cfg.lst_path, f"by_{run_id}.lst")

    if os.path.exists(lst_path):
        os.remove(lst_path)

    # log file path
    log_path = os.path.join(cfg.log_path, f"by_alkis_{run_id}.log")

    with open(lst_path, "w", encoding="utf-8") as f:
        f.write(f"PG:dbname=by_alkis_{run_id} user={cfg.user} password={cfg.password}\n")
        f.write("epsg 25832\n")
        f.write("avoiddupes true\n")
        f.write("usecopy false\n")
        f.write("skipfailuresregex .*\n")
        f.write(f"render_type {cfg.db_type}\n")
        f.write("create\n")
        f.write("clean\n")
        f.write(f"log {log_path}\n")
        f.write(f"{cfg.xml_path}/{run_id}_by_nas_polygon_parcel.xml\n")
        # f.write(f"{cfg.xml_path}/{run_id}_by_nas_polygon_road_classification.xml\n")
        f.write(f"{cfg.xml_path}/{run_id}_by_nas_polygon_building.xml\n")

        if cfg.show_label_rect:
            f.write(f"{cfg.xml_path}/{run_id}_by_nas_polygon_calculated_label.xml\n")
            
        f.write(f"{cfg.xml_path}/{run_id}_by_nas_line_boundary.xml\n")
        f.write(f"{cfg.xml_path}/{run_id}_by_nas_line_dashed.xml\n")
        f.write(f"{cfg.xml_path}/{run_id}_by_nas_line_arrow.xml\n")
        f.write(f"{cfg.xml_path}/{run_id}_by_nas_label_building.xml\n")
        f.write(f"{cfg.xml_path}/{run_id}_by_nas_label_parcel.xml\n")
        f.write(f"{cfg.xml_path}/{run_id}_by_nas_vertex.xml\n")
        f.write(f"{cfg.xml_path}/{run_id}_by_nas_symbol.xml\n")
        f.write("postprocess\n")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python make_lst.py <id>")
        print('Example command format: "python make_lst.py by_101125"')
        sys.exit(1)

    run_id = sys.argv[1]
    cfg = load_config(run_id)

    make_lst_file()