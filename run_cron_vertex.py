from datetime import datetime
import subprocess
from pathlib import Path
import common
from read_settings import load_config

# List your scripts here (in execution order)
SCRIPTS = [
    "vertex/scrap.py",
    "vertex/main.py"
]

BASE_DIR = Path(__file__).parent
# Use the current virtualenv's Python executable
PYTHON = BASE_DIR / "venv" / "bin" / "python"


#run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
run_id = "20260126_082728"
for script in SCRIPTS:
    script_path = Path(script)

    print(f"\n▶ Running {script_path.name} with id={run_id} ...")

    result = subprocess.run(
        [PYTHON, str(script_path), run_id],
        check=True  # continue even if one fails
    )

    if result.returncode != 0:
        print(f"❌ {script_path.name} failed with code {result.returncode}")
    else:
        print(f"✅ {script_path.name} completed successfully")

print("\nAll scripts have been executed.")

cfg = load_config()
common.update_cron_setting(cfg.setting_path, "dbname_vertex", f"{cfg.dbname_vertex}_{run_id}")
