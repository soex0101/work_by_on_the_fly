import os
import sys
from contextlib import contextmanager
from datetime import datetime

# One line per run_bbox.py request:
#   {run_id} {create_datetime} {bbox} {scale}
# 0_cleanup_old_runs.py appends " {delete_datetime}" once the run has been deleted.
REQUESTS_LOG_NAME = "requests.log"
DATETIME_FORMAT = "%Y-%m-%dT%H:%M:%S"


def requests_log_path(cfg):
    return os.path.join(cfg.log_path, REQUESTS_LOG_NAME)


@contextmanager
def _locked(path):
    """Exclusive lock on <path>.lock, so parallel runs never lose each other's lines."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(f"{path}.lock", "a") as lock:
        if sys.platform == "win32":
            import msvcrt
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)


def append_request(cfg, run_id, bbox, scale):
    """bbox as "minx,miny,maxx,maxy" (no spaces: fields are separated by spaces)."""
    path = requests_log_path(cfg)
    line = f"{run_id} {datetime.now().strftime(DATETIME_FORMAT)} {bbox} {scale}\n"
    with _locked(path):
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)


def mark_deleted(cfg, run_id):
    """Appends the delete datetime to every line of run_id that has none yet. Returns the number of updated lines."""
    path = requests_log_path(cfg)
    if not os.path.exists(path):
        return 0

    deleted_at = datetime.now().strftime(DATETIME_FORMAT)
    with _locked(path):
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()

        updated = 0
        for i, line in enumerate(lines):
            fields = line.split()
            if len(fields) == 4 and fields[0] == run_id:
                lines[i] = f"{line.rstrip()} {deleted_at}\n"
                updated += 1

        if updated:
            # Write a new file and swap it in, so a crash never leaves a half-written log.
            tmp_path = f"{path}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.writelines(lines)
            os.replace(tmp_path, path)
    return updated
