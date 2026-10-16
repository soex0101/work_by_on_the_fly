# pip install requests tqdm geopandas pyogrio
import os, requests
from tqdm import tqdm
import pyogrio
import sys

from read_settings import load_config

def main(run_id):
    cfg = load_config()
    url = cfg.gpkg_url
    out_dir = cfg.gpkg_out_dir
    os.makedirs(out_dir, exist_ok=True)
    
    download_name = f"nutzung_kreis_{run_id}.gpkg"

    # Check file exist first
    if not os.path.exists(os.path.join(out_dir, download_name)):
        
        downloading_name = f"nutzung_kreis_{run_id}_downloading.gpkg"

        with requests.get(url, stream=True, timeout=60) as r:
            r.raise_for_status()
            dest = os.path.join(out_dir, downloading_name)
            if not os.path.exists(dest):
                total = int(r.headers.get("content-length", 0))
                with open(dest, "wb") as f, tqdm(
                    total=total, unit="B", unit_scale=True, unit_divisor=1024, disable=(total == 0)
                ) as bar:
                    for chunk in r.iter_content(chunk_size=1024 * 256):
                        if chunk:
                            f.write(chunk)
                            bar.update(len(chunk))
            else:
                print("Use a file that has already been downloaded")

        os.rename(os.path.join(out_dir, downloading_name), os.path.join(out_dir, download_name))
        dest = os.path.join(out_dir, download_name)

        print("Saved to:", download_name)
    else:
        print("Use a file that has already been downloaded")
        dest = os.path.join(out_dir, download_name)

    # ---- Read it with GeoPandas (optional) ----
    # GeoPackages can have multiple layers; list them first:
    try:
        layers = pyogrio.list_layers(dest)
        print("Layers:", [lyr[0] for lyr in layers])  # names only
        # Read the first layer (or pick by name)
        import geopandas as gpd
        gdf = gpd.read_file(dest, engine="pyogrio", layer=layers[0][0])
        print(gdf.head())
    except Exception as e:
        print("To read the file, install geopandas & pyogrio. Error:", e)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python 3_download_tn.py <id>")
        print('Example command format: "python 3_download_tn.py 20251113_050000"')
        sys.exit(1)

    run_id = sys.argv[1]

    main(run_id)