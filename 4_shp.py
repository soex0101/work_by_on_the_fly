import os
import sys
import shutil
import zipfile
import subprocess
from pathlib import Path
from urllib.request import urlopen
from read_settings import load_config
import common

cfg = load_config()

SHP2PGSQL = shutil.which("shp2pgsql") or r"shp2pgsql"
PSQL = shutil.which("psql") or r"psql"

SRID = "25832"
SCHEMA_TABLE = "public.building"

URLS = [
    "https://geodaten.bayern.de/odd/m/3/daten/hausumringe/bezirk/data/096_Unterfranken_Hausumringe.zip",
    "https://geodaten.bayern.de/odd/m/3/daten/hausumringe/bezirk/data/094_Oberfranken_Hausumringe.zip",
    "https://geodaten.bayern.de/odd/m/3/daten/hausumringe/bezirk/data/095_Mittelfranken_Hausumringe.zip",
    "https://geodaten.bayern.de/odd/m/3/daten/hausumringe/bezirk/data/093_Oberpfalz_Hausumringe.zip",
    "https://geodaten.bayern.de/odd/m/3/daten/hausumringe/bezirk/data/092_Niederbayern_Hausumringe.zip",
    "https://geodaten.bayern.de/odd/m/3/daten/hausumringe/bezirk/data/091_Oberbayern_Hausumringe.zip",
    "https://geodaten.bayern.de/odd/m/3/daten/hausumringe/bezirk/data/097_Schwaben_Hausumringe.zip",
]

# ---- helpers ----
def ensure_bins():
    for exe, name in [(SHP2PGSQL, "shp2pgsql"), (PSQL, "psql")]:
        try:
            subprocess.run([exe, "--help"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        except FileNotFoundError:
            sys.exit(f"ERROR: Could not find {name}. Put PostgreSQL/PostGIS bin on PATH or set {name.upper()} variable.")

def download_zip(url: str, dest: Path) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    fname = url.split("/")[-1]
    out = dest / fname
    if out.exists():
        print(f"✓ Already downloaded: {out.name}")
        return out
    
    print(f"↓ Downloading {fname} ...")
    downloading_name = dest / (os.path.splitext(fname)[0] + "_downloading" + os.path.splitext(fname)[1])

    with urlopen(url) as r, open(downloading_name, "wb") as f:
        shutil.copyfileobj(r, f)

    os.rename(downloading_name, out)
    
    print(f"  Saved to {out}")
    return out

def unzip(zip_path: Path, dest_dir: Path) -> Path:
    target = dest_dir / zip_path.stem
    if target.exists():
        print(f"✓ Already extracted: {target.name}")
        return target
    print(f"⇪ Extracting {zip_path.name} ...")
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(target)
    print(f"  Extracted to {target}")
    return target

def find_shapefile(root: Path) -> Path:
    # Many Bavarian ODD zips contain a top-level folder; search recursively
    matches = list(root.rglob("hausumringe.shp"))
    if not matches:
        # fallback: any .shp if naming differs
        matches = list(root.rglob("*.shp"))
    if not matches:
        raise FileNotFoundError(f"No .shp found under {root}")
    # Prefer the 'hausumringe.shp' if present
    matches.sort(key=lambda p: 0 if p.name.lower() == "hausumringe.shp" else 1)
    return matches[0]

def shp_to_sql(shp: Path, create: bool, sql_out: Path):
    args = [SHP2PGSQL]
    if create:
        args.append("-I")  # create table + spatial index
    else:
        args.append("-a")  # append
    args += ["-s", SRID, str(shp), SCHEMA_TABLE]
    print(f"→ Generating SQL ({'create' if create else 'append'}) from {shp} ...")
    with open(sql_out, "wb") as f:
        proc = subprocess.run(args, stdout=f, stderr=subprocess.PIPE, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"shp2pgsql failed for {shp}:\n{proc.stderr.decode(errors='ignore')}")
    print(f"  Wrote {sql_out.name}")

def run_psql(sql_file: Path, dbname: str):
    args = [PSQL, "-d", dbname, "-U", cfg.user, "-f", str(sql_file)]
    env = os.environ.copy()
    env["PGPASSWORD"] = cfg.password  # <-- add password here

    print(f"▶ Importing {sql_file.name} into {cfg.dbname_building} ...")
    proc = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"psql failed for {sql_file}:\n{proc.stderr.decode(errors='ignore')}")
    print("  Import OK")

# ---- main ----
def main(run_id):
    ensure_bins()

    base = Path(os.path.join(cfg.shape_path, run_id))
    zips_dir = base / "zips"
    extract_dir = base / "extracted"
    sql_dir = base / "sql"
    for d in (zips_dir, extract_dir, sql_dir):
        d.mkdir(parents=True, exist_ok=True)

    # Download & extract
    extracted = []
    for url in URLS:
        zip_file = download_zip(url, zips_dir)
        folder = unzip(zip_file, extract_dir)
        shp = find_shapefile(folder)
        extracted.append((folder.name, shp))

    dbname = f"{cfg.dbname_building}_{run_id}"
    # Create DB
    common.create_db(cfg, dbname)

    # First import: create (-I), rest append (-a)
    extracted_sorted = sorted(extracted, key=lambda t: t[0])  # deterministic order
    for i, (foldername, shp) in enumerate(extracted_sorted):
        create = (i == 0)
        sql_file = sql_dir / f"{foldername}_{'create' if create else 'append'}.sql"
        shp_to_sql(shp, create=create, sql_out=sql_file)
        run_psql(sql_file, dbname)

    print("\nAll done ✅")
    print(f"Data directory: {base}")
    print(f"SQL logs in:    {sql_dir}")

if __name__ == "__main__":

    if len(sys.argv) < 2:
        print("Usage: python 4_shp.py <id>")
        print('Example command format: "python 4_shp.py 20251113_050000"')
        sys.exit(1)

    run_id = sys.argv[1]

    try:
        main(run_id)
    except Exception as e:
        sys.exit(str(e))
