"""Where phone backups live on disk, shared by the gallery (library.py), uploads and the web app.

Phones are backed up by the desktop app (desktop/photosync_app), which sends photos to the server
through the upload API. Older backups made by the retired laptop sync (rsync) are still shown:

    <BACKUPS_ROOT>/<UDID>/
        DCIM/...                 photos copied by the old laptop sync (read-only to the app)
        .sync-meta/device.json   that phone's details and sync history
"""

import datetime
import json
import os

# The old rsync'd backups; the hosted gallery on manifold points this elsewhere (see server.py).
BACKUPS_ROOT = os.environ.get("PHOTOSYNC_LIBRARY") or os.path.expanduser("~/iphone-backups")
META_DIR_NAME = ".sync-meta"


def now():
    return datetime.datetime.now().astimezone()


def write_json(path, data):
    """Write atomically so an interrupted run never leaves a half-written file."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def index_dcim(base_dir):
    """{path relative to base_dir: size} for every file under base_dir/DCIM.
    Dotfiles are skipped (temp files from an interrupted copy)."""
    sizes = {}
    for root, _, files in os.walk(os.path.join(base_dir, "DCIM")):
        for name in files:
            if name.startswith("."):
                continue
            path = os.path.join(root, name)
            try:
                sizes[os.path.relpath(path, base_dir)] = os.path.getsize(path)
            except OSError:
                pass
    return sizes


def phone_dir_for(udid):
    return os.path.join(BACKUPS_ROOT, udid)


def backed_up_devices():
    """device.json for every phone with an old laptop-sync backup, most recently synced first."""
    devices = []
    if os.path.isdir(BACKUPS_ROOT):
        for name in os.listdir(BACKUPS_ROOT):
            device = read_json(os.path.join(BACKUPS_ROOT, name, META_DIR_NAME, "device.json"))
            if device:
                devices.append(device)
    return sorted(devices, key=lambda d: d.get("last_sync_at") or "", reverse=True)
