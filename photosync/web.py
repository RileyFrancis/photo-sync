"""The photo gallery web app, plus the upload API that phones' photos arrive through.

Hosted on manifold (see server.py and deploy/). For local testing:
    .venv/bin/python app.py   then open http://localhost:8765
Photos are sent here by the desktop app (desktop/) or picked in the browser ("Back up photos").
"""

import argparse
import os
import re
import threading
import traceback

from flask import Flask, abort, jsonify, render_template, request, send_file

from . import core
from .library import SORTS, UPLOADS_ROOT, Library, upload_dir_for
from .uploads import UploadError, Uploads

app = Flask(__name__)


@app.before_request
def require_app_header():
    """Browsers send a saved login with every request to this site, even ones another website
    triggers. Requests that change something must carry this header, which other sites can't add
    (doing so needs permission this app never grants), so they can't act on a user's behalf."""
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get("X-Photo-Sync") != "1":
        return jsonify(error="Missing X-Photo-Sync header"), 403


# ---------------------------------------------------------------- libraries

_libraries = {}
_libraries_lock = threading.Lock()


def library(udid):
    if not (os.path.isdir(os.path.join(core.phone_dir_for(udid), "DCIM"))
            or os.path.isfile(os.path.join(upload_dir_for(udid), DEVICE_FILE))):
        abort(404, "No backup for that phone")
    with _libraries_lock:
        if udid not in _libraries:
            _libraries[udid] = Library(udid)
        return _libraries[udid]


# A phone backed up by the desktop app (rather than the old rsync'd laptop backup) is described
# by this file in its upload folder; see backup_register.
DEVICE_FILE = ".device.json"
UDID_RE = re.compile(r"[0-9A-Fa-f-]{20,48}")


def devices():
    """Every backed-up phone, most recently backed up first. A phone can have an rsync'd backup
    (core.backed_up_devices), desktop-app uploads (DEVICE_FILE), or both."""
    merged = {d["udid"]: d for d in core.backed_up_devices()}
    root = UPLOADS_ROOT or core.BACKUPS_ROOT
    for udid in (os.listdir(root) if os.path.isdir(root) else []):
        extra = core.read_json(os.path.join(upload_dir_for(udid), DEVICE_FILE))
        if extra:
            device = merged.setdefault(udid, {"udid": udid})
            newer = (extra.get("last_sync_at") or "") >= (device.get("last_sync_at") or "")
            device.update({k: v for k, v in extra.items()
                           if v is not None and (newer or not k.startswith("last_sync"))})
    return sorted(merged.values(), key=lambda d: d.get("last_sync_at") or "", reverse=True)


_uploads = {}


def uploads(udid):
    lib = library(udid)
    with _libraries_lock:
        if udid not in _uploads:
            _uploads[udid] = Uploads(lib)
        return _uploads[udid]


def refresh_libraries():
    """Re-index every backup in the background (only new or changed files are read)."""
    def run():
        for device in devices():
            try:
                library(device["udid"]).refresh()
            except Exception:
                traceback.print_exc()
    threading.Thread(target=run, daemon=True).start()


# ---------------------------------------------------------------- routes

@app.get("/")
def index():
    refresh_libraries()  # pick up changes made outside the app
    return render_template("index.html", sorts=list(SORTS))


@app.get("/api/status")
def status():
    found = devices()
    for device in found:
        lib = _libraries.get(device["udid"])
        device["indexing"] = dict(lib.indexing) if lib else None
    return jsonify(devices=found)


@app.get("/api/<udid>/media")
def media(udid):
    a = request.args
    result = library(udid).search(
        ext=a.get("ext", ""), kind=a.get("kind", ""), date_from=a.get("from", ""), date_to=a.get("to", ""),
        camera=a.get("camera", ""), sort=a.get("sort", "taken_desc"),
        include_live_clips=a.get("live_clips") == "1",
        offset=max(0, a.get("offset", 0, type=int)), limit=min(500, a.get("limit", 200, type=int)),
    )
    return jsonify(result)


@app.get("/api/<udid>/places")
def places(udid):
    a = request.args
    return jsonify(library(udid).places(
        ext=a.get("ext", ""), kind=a.get("kind", ""), date_from=a.get("from", ""), date_to=a.get("to", ""),
        camera=a.get("camera", ""), include_live_clips=a.get("live_clips") == "1"))


@app.get("/api/<udid>/items")
def items(udid):
    ids = [int(x) for x in request.args.get("ids", "").split(",") if x.isdigit()][:500]
    return jsonify(items=library(udid).get_many(ids))


def _ids(d):
    ids = d.get("ids")
    if not isinstance(ids, list) or not ids or len(ids) > 5000 or not all(isinstance(i, int) for i in ids):
        abort(400, "Bad list of photos")
    return ids


@app.post("/api/<udid>/favorite")
def favorite(udid):
    d = request.get_json(force=True, silent=True) or {}
    return jsonify(changed=library(udid).set_favorite(_ids(d), bool(d.get("favorite"))))


@app.post("/api/<udid>/delete")
def delete(udid):
    """Moves the photos to the server's trash (emptied after 30 days) and makes sure the desktop
    app won't back them up again."""
    d = request.get_json(force=True, silent=True) or {}
    lib = library(udid)
    count, phone_paths = lib.delete(_ids(d))
    uploads(udid).forget_deleted(phone_paths)
    return jsonify(deleted=count, version=f"{udid}:{lib.indexing['version']}")


@app.post("/api/<udid>/upload/start")
def upload_start(udid):
    d = request.get_json(force=True, silent=True) or {}
    try:
        return jsonify(uploads(udid).start(str(d.get("name", "")), int(d.get("size") or 0),
                                           int(d.get("modified") or 0), str(d.get("fingerprint", "")),
                                           phone_path=d.get("phone_path") or None, sha1=d.get("sha1")))
    except (TypeError, ValueError):
        return jsonify(error="Bad upload request"), 400
    except UploadError as e:
        return jsonify(error=str(e), **e.extra), e.status


@app.put("/api/<udid>/upload/<upload_id>")
def upload_chunk(udid, upload_id):
    try:
        return jsonify(uploads(udid).chunk(upload_id, request.args.get("offset", -1, type=int),
                                           request.stream, request.content_length))
    except UploadError as e:
        return jsonify(error=str(e), **e.extra), e.status


@app.post("/api/<udid>/upload/<upload_id>/finish")
def upload_finish(udid, upload_id):
    try:
        return jsonify(uploads(udid).finish(upload_id))
    except UploadError as e:
        return jsonify(error=str(e), **e.extra), e.status


@app.get("/api/<udid>/upload/info")
def upload_info(udid):
    return jsonify(uploads(udid).info())


# ---------------------------------------------------------------- desktop app backups
# The desktop app (desktop/photosync_app) reads the phone over USB and sends new photos here
# with the upload API above. These routes sit behind the site login like everything else.

def _device_json_path(udid):
    if not UDID_RE.fullmatch(udid):
        abort(400, "Bad phone id")
    return os.path.join(upload_dir_for(udid), DEVICE_FILE)


def _update_device(udid, fields):
    path = _device_json_path(udid)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    device = core.read_json(path)
    device.setdefault("first_synced_at", fields.get("last_sync_at"))
    device.update({"udid": udid, **fields})
    core.write_json(path, device)
    return device


@app.post("/api/<udid>/backup/register")
def backup_register(udid):
    """Called at the start of every backup: creates the phone's library if it's new."""
    d = request.get_json(force=True, silent=True) or {}
    fields = {k: str(d[k])[:100] for k in ("name", "product_type", "ios_version") if d.get(k)}
    _update_device(udid, {**fields, "last_sync_at": core.now().isoformat(timespec="seconds"),
                          "last_sync_status": "in_progress"})
    return jsonify(ok=True)


@app.get("/api/<udid>/backup/manifest")
def backup_manifest(udid):
    return jsonify(files=uploads(udid).phone_manifest())


@app.post("/api/<udid>/backup/finished")
def backup_finished(udid):
    d = request.get_json(force=True, silent=True) or {}
    status = d.get("status")
    if status not in ("success", "no_changes", "partial", "failed", "interrupted"):
        status = "failed"
    fields = {"last_sync_status": status, "last_sync_uploaded": int(d.get("uploaded") or 0),
              "last_sync_failed": int(d.get("failed") or 0)}
    if status in ("success", "no_changes"):
        fields["last_successful_sync_at"] = core.read_json(_device_json_path(udid)).get("last_sync_at")
    _update_device(udid, fields)
    threading.Thread(target=library(udid).refresh, daemon=True).start()
    return jsonify(ok=True)


@app.get("/api/<udid>/facets")
def facets(udid):
    return jsonify(library(udid).facets())


def _item(udid, media_id):
    lib = library(udid)
    item = lib.get(media_id)
    if not item:
        abort(404)
    return lib, item


@app.get("/thumb/<udid>/<int:media_id>")
def thumb(udid, media_id):
    lib, item = _item(udid, media_id)
    try:
        path = lib.thumbnail(item)
    except Exception:
        abort(415)
    return send_file(path, mimetype="image/jpeg", max_age=31536000)


@app.get("/view/<udid>/<int:media_id>")
def view(udid, media_id):
    lib, item = _item(udid, media_id)
    try:
        return send_file(lib.viewable_image(item), max_age=86400)
    except Exception:
        abort(415)


@app.get("/video/<udid>/<int:media_id>")
def video(udid, media_id):
    lib, item = _item(udid, media_id)
    if request.args.get("compat") == "1":
        return send_file(lib.compatible_video(item), mimetype="video/mp4", max_age=86400)
    # Browsers play QuickTime files more reliably when told they're MP4 (same container family).
    return send_file(lib.full_path(item), mimetype="video/mp4", max_age=86400)


@app.get("/original/<udid>/<int:media_id>")
def original(udid, media_id):
    lib, item = _item(udid, media_id)
    return send_file(lib.full_path(item), as_attachment=True, download_name=item["name"])


def main():
    parser = argparse.ArgumentParser(description="photo-sync gallery (for local testing)")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    refresh_libraries()
    print(f"photo-sync running at http://localhost:{args.port}  (Ctrl-C to stop)")
    app.run(host="127.0.0.1", port=args.port, threaded=True)
