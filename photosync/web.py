"""Local web app: sync the phone, push to the server, and browse the backup as a gallery.

Run with:  .venv/bin/python app.py   then open http://localhost:8765
"""

import argparse
import os
import threading
import time
import traceback

from flask import Flask, abort, jsonify, render_template, request, send_file

from . import core
from .library import SORTS, Library

app = Flask(__name__)
REMOTE = core.DEFAULT_REMOTE


# ---------------------------------------------------------------- background jobs

class WebReporter(core.Reporter):
    """Keeps the latest progress of the running job for the browser to poll."""

    def __init__(self, job):
        self.job = job

    def stage(self, text):
        self.job["stage"] = text

    def progress(self, p):
        self.job["progress"] = p
        self.job["stage"] = ""

    def say(self, text, kind="info"):
        self.job["log"].append({"time": time.time(), "text": text, "kind": kind})
        self.job["log"] = self.job["log"][-200:]


class Jobs:
    """At most one sync or push runs at a time."""

    def __init__(self):
        self.lock = threading.Lock()
        self.cancel = threading.Event()
        self.current = None

    def start(self, kind, fn):
        with self.lock:
            if self.current and self.current["running"]:
                return False
            self.cancel.clear()
            job = {"kind": kind, "running": True, "stage": "Starting…", "progress": None, "log": [],
                   "started_at": time.time(), "finished_at": None, "error": None, "result": None}
            self.current = job

        def run():
            reporter = WebReporter(job)
            try:
                job["result"] = fn(reporter, self.cancel)
            except KeyboardInterrupt:
                job["error"] = "Stopped. Files copied so far are kept; sync again to continue."
            except Exception as e:
                job["error"] = str(e)
                if not isinstance(e, RuntimeError):
                    traceback.print_exc()
            finally:
                job.update(running=False, stage="", progress=None, finished_at=time.time())
                refresh_libraries()

        threading.Thread(target=run, daemon=True).start()
        return True


jobs = Jobs()


def sync_job(reporter, cancel):
    record, _ = core.run_sync(reporter, remote=REMOTE, cancel=cancel)
    return {"udid": record["device"]["udid"], "status": record["status"],
            "copied": len(record["copied"]), "failed": len(record["failed"]),
            "remote": (record.get("remote") or {}).get("status")}


def verify_job(reporter, cancel):
    record, _ = core.run_verify(reporter, remote=REMOTE, cancel=cancel)
    return {"udid": record["device"]["udid"], "status": record["status"],
            "mismatched": len(record["mismatched"]), "repaired": len(record["repaired"]),
            "remote": (record.get("remote") or {}).get("status")}


def push_job(udid):
    def run(reporter, cancel):
        record, _ = core.run_push(reporter, udid, REMOTE, cancel=cancel)
        return {"udid": udid, "status": record["status"]}
    return run


# ---------------------------------------------------------------- libraries

_libraries = {}
_libraries_lock = threading.Lock()


def library(udid):
    if not os.path.isdir(os.path.join(core.phone_dir_for(udid), "DCIM")):
        abort(404, "No backup for that phone")
    with _libraries_lock:
        if udid not in _libraries:
            _libraries[udid] = Library(udid)
        return _libraries[udid]


def refresh_libraries():
    """Re-index every backup in the background (only new or changed files are read)."""
    def run():
        for device in core.backed_up_devices():
            try:
                library(device["udid"]).refresh()
            except Exception:
                traceback.print_exc()
    threading.Thread(target=run, daemon=True).start()


_phone_cache = {"at": 0.0, "value": None}
_device_names = {}


def phone_status():
    """The connected phone, checked at most every 3 seconds."""
    if time.monotonic() - _phone_cache["at"] < 3:
        return _phone_cache["value"]
    udids = core.connected_udids()
    value = None
    if udids:
        udid = udids[0]
        paired = core.is_paired(udid)
        if paired and udid not in _device_names:
            _device_names[udid] = core.device_info(udid)
        value = {"udid": udid, "paired": paired, "count": len(udids), **_device_names.get(udid, {})}
    _phone_cache.update(at=time.monotonic(), value=value)
    return value


# ---------------------------------------------------------------- routes

@app.get("/")
def index():
    refresh_libraries()  # pick up changes made outside the app, e.g. a verify run from the terminal
    return render_template("index.html", sorts=list(SORTS))


@app.get("/api/status")
def status():
    devices = core.backed_up_devices()
    for device in devices:
        lib = _libraries.get(device["udid"])
        device["indexing"] = dict(lib.indexing) if lib else None
    return jsonify(phone=phone_status(), devices=devices, job=jobs.current, remote=REMOTE)


@app.post("/api/sync")
def start_sync():
    if not jobs.start("sync", sync_job):
        return jsonify(error="A sync is already running"), 409
    return jsonify(ok=True)


@app.post("/api/verify")
def start_verify():
    if not jobs.start("verify", verify_job):
        return jsonify(error="A sync is already running"), 409
    return jsonify(ok=True)


@app.post("/api/push/<udid>")
def start_push(udid):
    if not jobs.start("push", push_job(udid)):
        return jsonify(error="A sync is already running"), 409
    return jsonify(ok=True)


@app.post("/api/cancel")
def cancel():
    jobs.cancel.set()
    return jsonify(ok=True)


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
    global REMOTE
    parser = argparse.ArgumentParser(description="photo-sync web app")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--remote", default=core.DEFAULT_REMOTE,
                        help=f"Where to push backups, as ssh-host:path (default {core.DEFAULT_REMOTE})")
    args = parser.parse_args()
    REMOTE = args.remote
    refresh_libraries()
    print(f"photo-sync running at http://localhost:{args.port}  (Ctrl-C to stop)")
    app.run(host="127.0.0.1", port=args.port, threaded=True)
