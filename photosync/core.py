"""Phone backup engine shared by the command line (sync.py) and the web app (photosync.web).

Standard library only, so sync.py keeps working with the system Python.

Layout on disk (the same layout is mirrored to the remote server):
    ~/iphone-backups/<UDID>/
        DCIM/...                 every photo ever backed up from this phone, one merged tree
        .sync-meta/
            README.txt           what the files in here are
            device.json          the phone's details and a summary of its sync history
            syncs/<id>.json      one record per run: when, status, which files were copied/failed
            replaced/<id>/...    old copies of files that a later sync overwrote (rare)
"""

import collections
import datetime
import json
import os
import queue
import re
import shlex
import shutil
import socket
import sqlite3
import subprocess
import threading
import time
import types
from contextlib import closing

MAX_RSYNC_ATTEMPTS = 5
RETRY_DELAY_SECONDS = 5
MAX_MOUNT_ATTEMPTS = 3
MOUNT_RETRY_DELAY_SECONDS = 2
# Where phone backups live; the hosted gallery on manifold points this elsewhere (see server.py).
BACKUPS_ROOT = os.environ.get("PHOTOSYNC_LIBRARY") or os.path.expanduser("~/iphone-backups")
IPHONE_DIR = os.path.expanduser("~/iphone")
# rsync destination as "ssh-host:path"; the phone's folder goes inside it. A relative path is
# relative to the remote user's home directory. The host is an alias from ~/.ssh/config.
# /srv/photo-sync/library is what the hosted gallery on manifold shows.
DEFAULT_REMOTE = "manifold:/srv/photo-sync/library"
# Not pushed: Verify's set-aside damaged copies are only useful locally, and can be large.
PUSH_EXCLUDES = ["--exclude=/.sync-meta/replaced/"]
SSH_COMMAND = "ssh -o BatchMode=yes -o ConnectTimeout=15"
META_DIR_NAME = ".sync-meta"
META_FORMAT_VERSION = 1

DEVICE_INFO_KEYS = {
    "name": "DeviceName",
    "product_type": "ProductType",
    "ios_version": "ProductVersion",
    "ios_build": "BuildVersion",
    "serial_number": "SerialNumber",
}

META_README = """\
This folder holds sync metadata for the photo backup in the parent folder.
It is written by photo-sync (~/Code/photo-sync); don't edit it by hand.

device.json       The phone's UDID, name, model, iOS version and serial number as of the
                  last sync, plus first/last sync times, the last sync's status, the last push
                  to the remote server, and how many photos (and bytes) the backup holds.
syncs/<id>.json   One record per run, named by its start time. "type" is "sync", "push"
                  (copy to the remote server only), "verify" (every file re-read from the phone
                  and compared byte for byte; damaged copies re-copied and listed in "repaired")
                  or "import" (legacy dated backups moved in).
                  "status" is one of: in_progress, success, no_changes, partial (some files
                  never copied; listed in "failed"), failed (error, see "error"), interrupted.
                  "copied" lists each file copied with its size; "replaced_previous" means an
                  older copy with a different size existed and was moved to replaced/<id>/
                  instead of being deleted. "remote" describes the push to the remote server.
replaced/<id>/    Older copies of files that sync <id> overwrote, kept at their original paths.

A photo counts as already backed up when DCIM/ has a file with the same path and size.
Photos and videos hidden in the iPhone's Photos app are never copied ("skipped_hidden" in a
record counts them); the hidden list comes from the phone's Photos library database.
Nothing is ever deleted from the backup, locally or on the remote server.
"""


class Reporter:
    """Receives progress from a sync. The command line and the web app each show it their way."""

    def stage(self, text):
        """What's happening right now (transient), e.g. "Scanning phone… 4,210 files"."""

    def progress(self, p):
        """Copy/push progress: a dict with phase, attempt, files_done, total_files, bytes_done,
        total_bytes, fraction, speed, elapsed, eta, unreadable, last_file."""

    def say(self, text, kind="info"):
        """A permanent message; kind is info, ok, warn or error."""


def now():
    return datetime.datetime.now().astimezone()


def fmt_bytes(n):
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1000:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000
    return f"{n:.2f} TB"


def fmt_duration(seconds):
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


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


# ---------------------------------------------------------------- the phone

def connected_udids():
    try:
        out = subprocess.run(["idevice_id", "-l"], capture_output=True, text=True).stdout
    except FileNotFoundError:  # libimobiledevice isn't installed (e.g. on the server)
        return []
    return sorted(set(out.split()))


def pick_udid(requested=None):
    udids = connected_udids()
    if requested:
        if requested not in udids:
            raise RuntimeError(f"Phone {requested} not connected (connected: {udids or 'none'})")
        return requested
    if not udids:
        raise RuntimeError("Phone not connected!")
    if len(udids) > 1:
        raise RuntimeError(f"Several phones connected ({', '.join(udids)}); pick one with --udid")
    return udids[0]


def is_paired(udid):
    result = subprocess.run(["idevicepair", "-u", udid, "validate"], capture_output=True, text=True)
    return "SUCCESS" in result.stdout


def require_paired(udid):
    if not is_paired(udid):
        raise RuntimeError("Phone not paired! Unlock it and tap Trust, then try again.")


def device_info(udid):
    info = {"udid": udid}
    for field, key in DEVICE_INFO_KEYS.items():
        result = subprocess.run(["ideviceinfo", "-u", udid, "-k", key], capture_output=True, text=True)
        info[field] = result.stdout.strip() if result.returncode == 0 else None
    return info


def remount_iphone(iphone_dir, udid):
    subprocess.run(["fusermount", "-uz", iphone_dir], capture_output=True)
    subprocess.run(["pkill", "-9", "-f", f"ifuse {iphone_dir}"], capture_output=True)
    time.sleep(1)
    # Only after unmounting: a dead mount left by an earlier run can't even be stat'ed, so
    # creating the folder before this fails with "File exists".
    os.makedirs(iphone_dir, exist_ok=True)

    last_stderr = ""
    for _ in range(MAX_MOUNT_ATTEMPTS):
        # -s: single-threaded FUSE. ifuse reads a file as "seek, then read" on a shared handle, so
        # with the kernel's parallel read-ahead, multi-threaded ifuse sometimes returns the 128 KB
        # chunk from 256 KB further into the file. rsync can't detect that and saves the wrong
        # bytes, which shows up as green/garbled bands in photos. Single-threaded avoids the race.
        result = subprocess.run(["ifuse", iphone_dir, "-u", udid, "-s"], capture_output=True, text=True)
        if result.returncode == 0:
            return
        last_stderr = result.stderr
        time.sleep(MOUNT_RETRY_DELAY_SECONDS)
    hint = ""
    if "is not a socket" in last_stderr:
        hint = (" (AppArmor blocks fusermount3 when started from a snap-confined terminal such as "
                "VS Code's; start it from a regular terminal instead)")
    raise RuntimeError(f"Failed to mount iPhone: {last_stderr.strip()}{hint}")


def index_dcim(base_dir, on_progress=None):
    """{path relative to base_dir: size} for every file under base_dir/DCIM.
    Dotfiles are skipped; in a backup they are rsync temp files from an interrupted copy."""
    sizes = {}
    for root, _, files in os.walk(os.path.join(base_dir, "DCIM")):
        for name in files:
            if name.startswith("."):
                continue
            path = os.path.join(root, name)
            try:
                size = os.path.getsize(path)
            except OSError:
                size = None  # can't stat it; on the phone, let rsync try and report the error
            sizes[os.path.relpath(path, base_dir)] = size
            if on_progress and len(sizes) % 50 == 0:
                on_progress(len(sizes))
    return sizes


def scan_phone(reporter):
    start = time.monotonic()
    return index_dcim(IPHONE_DIR, lambda n: reporter.stage(
        f"Scanning phone… {n:,} files ({fmt_duration(time.monotonic() - start)})"))


# ---------------------------------------------------------------- hidden photos

PHOTOS_DB_FILES = ("Photos.sqlite", "Photos.sqlite-wal", "Photos.sqlite-shm")
PHOTOS_DB_CACHE = os.path.expanduser("~/.cache/photo-sync")
# IMG_1234.HEIC, IMG_1234.MOV (Live Photo clip), IMG_1234.AAE (edit data) and IMG_E1234.HEIC
# (edited copy) are all the same photo. This reduces a file name to that shared "IMG_1234".
_ASSET_STEM_RE = re.compile(r"^(.*?_)[EO]?(\d+)$")


def asset_key(path):
    folder, name = os.path.split(path)
    stem = os.path.splitext(name)[0].upper()
    m = _ASSET_STEM_RE.match(stem)
    return folder, (m.group(1) + m.group(2)) if m else stem


def copy_photos_db(udid, reporter):
    """Copy the phone's Photos library database to a local cache and return the local path.
    The big main file is only re-copied when it changed; the write-ahead log (recent changes)
    is always copied. The phone's database is only ever read, never opened in place, so
    nothing on the phone can be modified."""
    src_dir = os.path.join(IPHONE_DIR, "PhotoData")
    dst_dir = os.path.join(PHOTOS_DB_CACHE, udid, "phone-photos-db")
    os.makedirs(dst_dir, exist_ok=True)
    for name in PHOTOS_DB_FILES:
        src, dst = os.path.join(src_dir, name), os.path.join(dst_dir, name)
        try:
            st = os.stat(src)
        except FileNotFoundError:
            if name == "Photos.sqlite":
                raise
            if os.path.exists(dst):
                os.remove(dst)  # a stale log from an older copy would corrupt the snapshot
            continue
        if name == "Photos.sqlite" and os.path.exists(dst):
            cached = os.stat(dst)
            if cached.st_size == st.st_size and int(cached.st_mtime) == int(st.st_mtime):
                continue
        reporter.stage(f"Reading the phone's Photos library to find hidden items ({fmt_bytes(st.st_size)})…")
        tmp = dst + ".tmp"
        with open(src, "rb") as fsrc, open(tmp, "wb") as fdst:
            shutil.copyfileobj(fsrc, fdst, 1 << 20)
        os.utime(tmp, (st.st_atime, st.st_mtime))
        os.replace(tmp, dst)
    return os.path.join(dst_dir, "Photos.sqlite")


def hidden_on_phone(udid, on_phone, reporter):
    """Paths in `on_phone` that belong to items hidden in the Photos app (including a Live
    Photo's clip, edit data and edited copies). Raises RuntimeError if the hidden list can't be
    read, so a sync never copies hidden items by accident."""
    try:
        db_path = copy_photos_db(udid, reporter)
        # Read-only, so SQLite doesn't fold the log into the cached main file on close; that
        # would make the cache look different from the phone's copy and force a re-copy.
        with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as db:
            rows = db.execute("SELECT ZDIRECTORY, ZFILENAME FROM ZASSET WHERE ZHIDDEN = 1").fetchall()
    except (OSError, sqlite3.Error) as e:
        raise RuntimeError(
            f"Couldn't read the phone's Photos library to find hidden photos ({e}). Stopping so "
            f"hidden photos aren't copied by mistake. Unlock the phone and try again.") from e
    hidden_keys = {asset_key(f"{d}/{f}") for d, f in rows if d and f}
    return {p for p in on_phone if asset_key(p) in hidden_keys}


# ---------------------------------------------------------------- backup folders and records

def phone_dir_for(udid):
    return os.path.join(BACKUPS_ROOT, udid)


def setup_phone_dir(udid):
    phone_dir = phone_dir_for(udid)
    meta_dir = os.path.join(phone_dir, META_DIR_NAME)
    os.makedirs(os.path.join(meta_dir, "syncs"), exist_ok=True)
    with open(os.path.join(meta_dir, "README.txt"), "w") as f:
        f.write(META_README)
    return phone_dir, meta_dir


def backed_up_devices():
    """device.json contents for every phone that has a backup folder, most recently synced first."""
    devices = []
    if os.path.isdir(BACKUPS_ROOT):
        for name in os.listdir(BACKUPS_ROOT):
            device = read_json(os.path.join(BACKUPS_ROOT, name, META_DIR_NAME, "device.json"))
            if device:
                devices.append(device)
    return sorted(devices, key=lambda d: d.get("last_sync_at") or "", reverse=True)


def update_device_json(meta_dir, phone_dir, info, record):
    path = os.path.join(meta_dir, "device.json")
    device = read_json(path)
    backed_up = index_dcim(phone_dir)
    device.update({k: v for k, v in info.items() if v is not None})
    device["format_version"] = META_FORMAT_VERSION
    if record["type"] == "verify":
        device["last_verify_at"] = record["started_at"]
        device["last_verify_status"] = record["status"]
    elif record["type"] != "push":
        device.setdefault("first_synced_at", record["started_at"])
        device["last_sync_at"] = record["started_at"]
        device["last_sync_id"] = record["sync_id"]
        device["last_sync_status"] = record["status"]
        if record["status"] in ("success", "no_changes"):
            device["last_successful_sync_at"] = record["started_at"]
    remote = record.get("remote")
    if remote and remote.get("status") not in (None, "in_progress"):
        device["remote"] = remote["target"]
        device["last_push_at"] = remote["started_at"]
        device["last_push_status"] = remote["status"]
        if remote["status"] == "success":
            device["last_successful_push_at"] = remote["started_at"]
    device["photo_count"] = len(backed_up)
    device["total_bytes"] = sum(s for s in backed_up.values() if s)
    write_json(path, device)


def start_record(meta_dir, record_type, info):
    """A new sync record, saved immediately. Returns (record, save function, record path)."""
    started = now()
    sync_id = started.strftime("%Y-%m-%dT%H-%M-%S")
    record_path = os.path.join(meta_dir, "syncs", f"{sync_id}.json")
    suffix = 1
    while os.path.exists(record_path):  # two runs started in the same second
        suffix += 1
        record_path = os.path.join(meta_dir, "syncs", f"{sync_id}_{suffix}.json")
    record = {
        "format_version": META_FORMAT_VERSION,
        "type": record_type,
        "sync_id": os.path.basename(record_path)[: -len(".json")],
        "started_at": started.isoformat(timespec="seconds"),
        "finished_at": None,
        "status": "in_progress",
        "host": socket.gethostname(),
        "device": info,
        "error": None,
    }

    def save():
        write_json(record_path, record)

    save()
    return record, save, record_path


# ---------------------------------------------------------------- rsync with progress

# rsync's --info=progress2 line, e.g. "  35,700,123,456  59%   25.63MB/s    0:22:08 (xfr#6350, ...)"
PROGRESS_RE = re.compile(r"^\s*([\d,.']+)\s+(\d+)%\s")
READ_ERROR_RE = re.compile(r'^rsync: \[sender\] read errors mapping "(.+)": (.+)$')
DONE_MARKER = "@@done "
# rsync's closing summary when any file failed; our own summary replaces it.
IGNORED_STDERR = ("rsync error: some files/attrs were not transferred",)


def _pump(stream, name, q):
    """Read a pipe, splitting on \\r as well as \\n (rsync redraws progress with \\r)."""
    buf = b""
    while chunk := os.read(stream.fileno(), 65536):
        buf += chunk
        *lines, buf = re.split(rb"[\r\n]", buf)
        for line in lines:
            if line:
                q.put((name, line.decode(errors="replace")))
    if buf:
        q.put((name, buf.decode(errors="replace")))
    q.put((name, None))


def run_rsync(args, reporter, phase, attempt=None, file_list=None, total_files=None,
              total_bytes=None, source_dir=None, cancel=None):
    """Run rsync, turning its output into reporter.progress() calls instead of printing it.
    `file_list` (paths) is fed to --files-from on stdin. Returns a namespace with returncode,
    files_done, bytes_done, elapsed, read_errors ({path: reason}) and other_errors (lines)."""
    cmd = ["rsync", "-a", "--info=progress2", f"--out-format={DONE_MARKER}%n"]
    if file_list is not None:
        cmd += ["--from0", "--files-from=-"]
    proc = subprocess.Popen(
        cmd + list(args),
        stdin=subprocess.PIPE if file_list is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    q = queue.Queue()
    for stream, name in ((proc.stdout, "out"), (proc.stderr, "err")):
        threading.Thread(target=_pump, args=(stream, name, q), daemon=True).start()
    if file_list is not None:
        stdin = proc.stdin
        assert stdin is not None

        def feed():
            try:
                stdin.write("\0".join(file_list).encode())
                stdin.close()
            except BrokenPipeError:
                pass
        threading.Thread(target=feed, daemon=True).start()

    start = time.monotonic()
    res = types.SimpleNamespace(returncode=None, files_done=0, bytes_done=0, elapsed=0,
                                read_errors={}, other_errors=[])
    pct = 0
    last_file = ""
    samples = collections.deque()  # (time, bytes) over the last few seconds, for the speed
    open_streams = 2
    last_report = 0.0

    def report():
        t = time.monotonic()
        samples.append((t, res.bytes_done))
        while len(samples) > 2 and samples[-1][0] - samples[0][0] > 10:
            samples.popleft()
        dt = samples[-1][0] - samples[0][0]
        speed = (samples[-1][1] - samples[0][1]) / dt if dt > 0 else 0
        if total_bytes:
            fraction = min(res.bytes_done / total_bytes, 1)
            remaining = total_bytes - res.bytes_done
        else:  # pushing: rsync works out the total itself and only tells us a percentage
            fraction = pct / 100
            remaining = res.bytes_done * (1 - fraction) / fraction if fraction else 0
        reporter.progress({
            "phase": phase, "attempt": attempt, "max_attempts": MAX_RSYNC_ATTEMPTS,
            "files_done": res.files_done, "total_files": total_files,
            "bytes_done": res.bytes_done, "total_bytes": total_bytes,
            "fraction": fraction, "speed": speed, "elapsed": t - start,
            "eta": remaining / speed if speed > 0 else None,
            "unreadable": len(res.read_errors), "last_file": last_file,
        })

    try:
        while open_streams:
            if cancel is not None and cancel.is_set():
                proc.terminate()
            try:
                name, line = q.get(timeout=0.25)
            except queue.Empty:
                name, line = None, None
            if line is None:
                if name:  # that stream hit end of file
                    open_streams -= 1
            elif name == "out":
                if line.startswith(DONE_MARKER):
                    path = line[len(DONE_MARKER):]
                    if not path.endswith("/"):  # rsync also logs the folders it creates
                        res.files_done += 1
                        last_file = os.path.basename(path)
                elif m := PROGRESS_RE.match(line):
                    res.bytes_done = int(re.sub(r"\D", "", m.group(1)))
                    pct = int(m.group(2))
            elif name == "err":
                if m := READ_ERROR_RE.match(line):
                    path = m.group(1)
                    if source_dir:
                        path = os.path.relpath(path, source_dir)
                    res.read_errors[path] = m.group(2)
                elif not line.startswith(IGNORED_STDERR):
                    res.other_errors.append(line)
                    reporter.say(f"rsync: {line.removeprefix('rsync: ')}", "warn")
            if time.monotonic() - last_report >= 0.25:
                report()
                last_report = time.monotonic()
        res.returncode = proc.wait()
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait()
    res.elapsed = time.monotonic() - start
    if cancel is not None and cancel.is_set():
        raise KeyboardInterrupt
    return res


# ---------------------------------------------------------------- phone -> local backup

def copy_from_phone(phone_dir, meta_dir, udid, record, save, reporter, cancel=None):
    """Copy photos the backup doesn't have yet, retrying failed ones. Fills in `record`."""
    replaced_dir = os.path.join(meta_dir, "replaced", record["sync_id"])
    record.update(attempts=0, files_on_phone=None, skipped_hidden=0, copied=[], failed=[], bytes_copied=0)
    already_backed_up = index_dcim(phone_dir)
    wanted = {}  # every file any attempt tried to copy: path -> size on the phone
    hidden = None

    try:
        for attempt in range(1, MAX_RSYNC_ATTEMPTS + 1):
            reporter.stage("Mounting phone…")
            remount_iphone(IPHONE_DIR, udid)
            on_phone = scan_phone(reporter)
            if hidden is None:
                hidden = hidden_on_phone(udid, on_phone, reporter)
            on_phone = {p: s for p, s in on_phone.items() if p not in hidden}
            backed_up = index_dcim(phone_dir)  # re-read so retries skip what earlier attempts copied
            pending = {p: s for p, s in on_phone.items() if s is None or backed_up.get(p) != s}
            record["files_on_phone"] = len(on_phone)
            record["skipped_hidden"] = len(hidden)
            if attempt == 1:
                reporter.say(f"{len(on_phone):,} photos on the phone, {len(pending):,} not backed up yet"
                             + (f" ({len(hidden):,} hidden files skipped)" if hidden else ""))
            if not pending:
                break
            wanted.update(pending)
            record["attempts"] = attempt
            save()

            res = run_rsync(
                ["--backup", f"--backup-dir={replaced_dir}", f"{IPHONE_DIR}/", f"{phone_dir}/"],
                reporter, "copy", attempt=attempt, file_list=sorted(pending),
                total_files=len(pending), total_bytes=sum(s for s in pending.values() if s),
                source_dir=IPHONE_DIR, cancel=cancel,
            )
            summary = (f"Attempt {attempt}: copied {res.files_done - len(res.read_errors):,} files "
                       f"({fmt_bytes(res.bytes_done)}) in {fmt_duration(res.elapsed)}")
            if res.read_errors:
                summary += f"; {len(res.read_errors):,} couldn't be read from the phone"
            reporter.say(summary, "warn" if res.returncode else "ok")
            if res.returncode == 0:
                break
            if attempt < MAX_RSYNC_ATTEMPTS:
                n = len(res.read_errors)
                what = f"{n:,} file{'s' if n != 1 else ''}" if n else "the failed files"
                reporter.say(f"Retrying {what} in {RETRY_DELAY_SECONDS}s "
                             f"(the phone often reads them fine after a remount)…")
                time.sleep(RETRY_DELAY_SECONDS)
    finally:
        # Work out what actually landed, whatever happened above.
        backed_up = index_dcim(phone_dir)
        for path, size in sorted(wanted.items()):
            if size is not None and backed_up.get(path) == size:
                record["copied"].append({"path": path, "size": size,
                                         "replaced_previous": path in already_backed_up})
                record["bytes_copied"] += size
            else:
                record["failed"].append(path)
        if os.path.isdir(replaced_dir):
            record["replaced_files_dir"] = os.path.relpath(replaced_dir, phone_dir)

    if not wanted:
        return "no_changes"
    return "partial" if record["failed"] else "success"


# ---------------------------------------------------------------- local backup -> remote server

def push_to_remote(phone_dir, udid, remote, record, save, reporter, cancel=None, force_paths=()):
    """Mirror this phone's backup folder to the remote server. Never deletes anything there.
    `force_paths` are re-sent even though their size and time look unchanged (repaired files).
    Fills in record["remote"]; raises RuntimeError if the push fails."""
    host, sep, base = remote.partition(":")
    if not sep or not host:
        raise RuntimeError(f"Remote must look like host:path, got {remote!r}")
    base = base.rstrip("/") or "."
    target = f"{host}:{base}/{udid}"
    info = record["remote"] = {"target": target, "status": "in_progress",
                               "started_at": now().isoformat(timespec="seconds"),
                               "finished_at": None, "files_pushed": 0, "bytes_pushed": 0, "error": None}
    save()
    try:
        reporter.stage(f"Connecting to {host}…")
        result = subprocess.run(
            shlex.split(SSH_COMMAND) + [host, "mkdir", "-p", "--", shlex.quote(f"{base}/{udid}")],
            capture_output=True, text=True, timeout=90,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Can't reach {host}: {result.stderr.strip() or 'ssh failed'}")

        res = run_rsync([*PUSH_EXCLUDES, "--chmod=D755,F644", "-e", SSH_COMMAND, f"{phone_dir}/", f"{target}/"],
                        reporter, "push", cancel=cancel)
        info["files_pushed"] = res.files_done
        info["bytes_pushed"] = res.bytes_done
        if res.returncode == 0 and force_paths:
            forced = run_rsync(["-I", "--chmod=D755,F644", "-e", SSH_COMMAND, f"{phone_dir}/", f"{target}/"], reporter, "push",
                               file_list=sorted(force_paths), cancel=cancel)
            res.returncode = forced.returncode
            res.other_errors += forced.other_errors
            info["files_pushed"] += forced.files_done
            info["bytes_pushed"] += forced.bytes_done
        if res.returncode != 0:
            detail = res.other_errors[-1] if res.other_errors else f"rsync exit code {res.returncode}"
            raise RuntimeError(f"Push to {host} failed: {detail}")
        info["status"] = "success"
        reporter.say(f"Pushed to {target} ({res.files_done:,} files changed, "
                     f"{fmt_bytes(res.bytes_done)}) in {fmt_duration(res.elapsed)}", "ok")
    except KeyboardInterrupt:
        info["status"] = "interrupted"
        raise
    except subprocess.TimeoutExpired:
        info["status"] = "failed"
        info["error"] = f"Timed out connecting to {host}"
        raise RuntimeError(info["error"])
    except Exception as e:
        info["status"] = "failed"
        info["error"] = str(e)
        raise
    finally:
        info["finished_at"] = now().isoformat(timespec="seconds")
        save()


def push_metadata(phone_dir, target):
    """Send just .sync-meta again, so the server's copy of the records includes the push itself."""
    subprocess.run(["rsync", "-a", "-e", SSH_COMMAND, f"{phone_dir}/{META_DIR_NAME}/",
                    f"{target}/{META_DIR_NAME}/"], capture_output=True, timeout=120)


# ---------------------------------------------------------------- verify the backup against the phone

def same_contents(phone_path, backup_path, on_bytes, chunk=1 << 20):
    """True if the two files are byte-for-byte identical. Raises OSError if a read fails."""
    with open(phone_path, "rb") as fp, open(backup_path, "rb") as fb:
        while True:
            x = fp.read(chunk)
            y = fb.read(chunk)
            on_bytes(len(x))
            if x != y:
                return False
            if not x:
                return True


def verify_against_phone(phone_dir, meta_dir, udid, record, save, reporter, cancel=None, repair=True):
    """Read every backed-up file back from the phone and compare. Files that differ are copied
    again (the damaged copy goes to replaced/<id>/). Fills in `record`; returns its status."""
    replaced_dir = os.path.join(meta_dir, "replaced", record["sync_id"])
    record.update(checked=0, matched=0, mismatched=[], repaired=[], still_mismatched=[],
                  unreadable=[], not_on_phone=0, size_differs=0, bytes_checked=0)

    reporter.stage("Mounting phone…")
    remount_iphone(IPHONE_DIR, udid)
    all_on_phone = scan_phone(reporter)
    hidden = hidden_on_phone(udid, all_on_phone, reporter)
    on_phone = {p: s for p, s in all_on_phone.items() if p not in hidden}  # never re-copy hidden items
    record["skipped_hidden"] = len(hidden)
    backed_up = index_dcim(phone_dir)
    record["not_on_phone"] = sum(1 for p in backed_up if p not in all_on_phone)
    record["size_differs"] = sum(1 for p, s in backed_up.items() if p in on_phone and on_phone[p] != s)
    todo = sorted(p for p, s in backed_up.items() if p in on_phone and on_phone[p] == s)
    total_bytes = sum(backed_up[p] or 0 for p in todo)
    reporter.say(f"Checking {len(todo):,} files ({fmt_bytes(total_bytes)}) against the phone. "
                 f"{record['not_on_phone']:,} backed-up files are no longer on the phone and can't be checked.")

    start = time.monotonic()
    done_bytes = 0
    samples = collections.deque()
    last_report = 0.0

    def report(files_done, last_file):
        t = time.monotonic()
        samples.append((t, done_bytes))
        while len(samples) > 2 and samples[-1][0] - samples[0][0] > 10:
            samples.popleft()
        dt = samples[-1][0] - samples[0][0]
        speed = (samples[-1][1] - samples[0][1]) / dt if dt > 0 else 0
        reporter.progress({
            "phase": "verify", "attempt": None, "max_attempts": None,
            "files_done": files_done, "total_files": len(todo),
            "bytes_done": done_bytes, "total_bytes": total_bytes,
            "fraction": done_bytes / total_bytes if total_bytes else 1, "speed": speed,
            "elapsed": t - start, "eta": (total_bytes - done_bytes) / speed if speed > 0 else None,
            "unreadable": len(record["unreadable"]), "mismatched": len(record["mismatched"]),
            "last_file": last_file,
        })

    def add_bytes(n):
        nonlocal done_bytes, last_report
        done_bytes += n
        if time.monotonic() - last_report >= 0.25:
            report(record["checked"], current)
            last_report = time.monotonic()

    for current in todo:
        if cancel is not None and cancel.is_set():
            raise KeyboardInterrupt
        try:
            ok = same_contents(os.path.join(IPHONE_DIR, current), os.path.join(phone_dir, current), add_bytes)
        except OSError as e:
            record["unreadable"].append({"path": current, "error": str(e)})
            ok = None
        record["checked"] += 1
        record["bytes_checked"] = done_bytes
        if ok:
            record["matched"] += 1
        elif ok is False:
            record["mismatched"].append(current)
        if record["checked"] % 500 == 0:
            save()
    record["bytes_checked"] = done_bytes
    save()
    reporter.say(f"Checked {record['checked']:,} files in {fmt_duration(time.monotonic() - start)}: "
                 f"{record['matched']:,} match, {len(record['mismatched']):,} damaged, "
                 f"{len(record['unreadable']):,} couldn't be read",
                 "warn" if record["mismatched"] or record["unreadable"] else "ok")

    if repair and record["mismatched"]:
        bad = record["mismatched"]
        res = run_rsync(
            ["-I", "--backup", f"--backup-dir={replaced_dir}", f"{IPHONE_DIR}/", f"{phone_dir}/"],
            reporter, "copy", attempt=1, file_list=bad, total_files=len(bad),
            total_bytes=sum(backed_up[p] or 0 for p in bad), source_dir=IPHONE_DIR, cancel=cancel,
        )
        reporter.stage("Checking the repaired files…")
        for path in bad:
            try:
                fixed = same_contents(os.path.join(IPHONE_DIR, path), os.path.join(phone_dir, path), lambda n: None)
            except OSError:
                fixed = False
            (record["repaired"] if fixed else record["still_mismatched"]).append(path)
        add_pending_push(meta_dir, record["repaired"])
        save()
        reporter.say(f"Repaired {len(record['repaired']):,} files"
                     + (f"; {len(record['still_mismatched']):,} still differ (run verify again)"
                        if record["still_mismatched"] else "")
                     + (f". Damaged copies kept in {os.path.relpath(replaced_dir, phone_dir)}"
                        if os.path.isdir(replaced_dir) else ""),
                     "warn" if record["still_mismatched"] or res.returncode else "ok")

    if record["still_mismatched"] or record["unreadable"] or (record["mismatched"] and not repair):
        return "partial"
    return "success"


def add_pending_push(meta_dir, paths):
    """Remember files whose content changed without their size or time changing, so the next
    push re-sends them (rsync would otherwise think the server's copy is up to date)."""
    if not paths:
        return
    path = os.path.join(meta_dir, "device.json")
    device = read_json(path)
    device["force_push"] = sorted(set(device.get("force_push", [])) | set(paths))
    write_json(path, device)


def pending_push(meta_dir):
    return read_json(os.path.join(meta_dir, "device.json")).get("force_push", [])


def clear_pending_push(meta_dir, paths):
    path = os.path.join(meta_dir, "device.json")
    device = read_json(path)
    device["force_push"] = sorted(set(device.get("force_push", [])) - set(paths))
    write_json(path, device)


# ---------------------------------------------------------------- whole runs

def _finish(record, save, meta_dir, phone_dir, info, status=None, error=None):
    if status:
        record["status"] = status
    if error:
        record["error"] = error
    record["finished_at"] = now().isoformat(timespec="seconds")
    save()
    update_device_json(meta_dir, phone_dir, info, record)


def run_sync(reporter, udid=None, remote: str | None = DEFAULT_REMOTE, cancel=None):
    """Back up the connected phone, then push the backup to `remote` (None to skip).
    Returns (record, record_path). A failed push is recorded in record["remote"] but doesn't
    raise: the local backup still succeeded."""
    udid = pick_udid(udid)
    require_paired(udid)
    info = device_info(udid)
    reporter.say(f"{info['name']} ({info['product_type']}, iOS {info['ios_version']})")
    phone_dir, meta_dir = setup_phone_dir(udid)
    record, save, record_path = start_record(meta_dir, "sync", info)

    try:
        status = copy_from_phone(phone_dir, meta_dir, udid, record, save, reporter, cancel)
    except KeyboardInterrupt:
        _finish(record, save, meta_dir, phone_dir, info, "interrupted")
        raise
    except Exception as e:
        _finish(record, save, meta_dir, phone_dir, info, "failed", str(e))
        raise
    _finish(record, save, meta_dir, phone_dir, info, status)

    if status == "no_changes":
        reporter.say("Everything on the phone is already backed up.", "ok")
    elif status == "success":
        reporter.say(f"Backed up {len(record['copied']):,} new files ({fmt_bytes(record['bytes_copied'])})", "ok")
    else:
        reporter.say(f"{len(record['failed']):,} files still couldn't be copied after "
                     f"{MAX_RSYNC_ATTEMPTS} attempts; sync again to retry them", "error")

    if remote:
        _push_after(phone_dir, meta_dir, udid, remote, record, save, info, reporter, cancel)
    return record, record_path


def _push_after(phone_dir, meta_dir, udid, remote, record, save, info, reporter, cancel):
    """Push as the last step of a sync or verify. A failure is recorded and reported, not raised."""
    forced = pending_push(meta_dir)
    try:
        push_to_remote(phone_dir, udid, remote, record, save, reporter, cancel, force_paths=forced)
    except RuntimeError as e:
        reporter.say(f"{e}. The local backup is fine; push again once the server is reachable.", "error")
    if record["remote"]["status"] == "success":
        clear_pending_push(meta_dir, forced)
    update_device_json(meta_dir, phone_dir, info, record)
    if record["remote"]["status"] == "success":
        push_metadata(phone_dir, record["remote"]["target"])


def run_verify(reporter, udid=None, remote: str | None = DEFAULT_REMOTE, repair=True, cancel=None):
    """Compare the whole backup with the phone, re-copy damaged files, then push. Returns (record, path)."""
    udid = pick_udid(udid)
    require_paired(udid)
    info = device_info(udid)
    reporter.say(f"{info['name']} ({info['product_type']}, iOS {info['ios_version']})")
    phone_dir, meta_dir = setup_phone_dir(udid)
    record, save, record_path = start_record(meta_dir, "verify", info)
    try:
        status = verify_against_phone(phone_dir, meta_dir, udid, record, save, reporter, cancel, repair)
    except KeyboardInterrupt:
        _finish(record, save, meta_dir, phone_dir, info, "interrupted")
        raise
    except Exception as e:
        _finish(record, save, meta_dir, phone_dir, info, "failed", str(e))
        raise
    _finish(record, save, meta_dir, phone_dir, info, status)
    if remote and repair:
        _push_after(phone_dir, meta_dir, udid, remote, record, save, info, reporter, cancel)
    return record, record_path


def run_push(reporter, udid, remote=DEFAULT_REMOTE, cancel=None):
    """Push an existing backup to the remote server without the phone. Returns (record, path)."""
    phone_dir = phone_dir_for(udid)
    meta_dir = os.path.join(phone_dir, META_DIR_NAME)
    if not os.path.isdir(meta_dir):
        raise RuntimeError(f"No backup for phone {udid}")
    device = read_json(os.path.join(meta_dir, "device.json"))
    info = {k: device.get(k) for k in ("udid", *DEVICE_INFO_KEYS)}
    record, save, record_path = start_record(meta_dir, "push", info)
    forced = pending_push(meta_dir)
    try:
        push_to_remote(phone_dir, udid, remote, record, save, reporter, cancel, force_paths=forced)
        clear_pending_push(meta_dir, forced)
    except KeyboardInterrupt:
        _finish(record, save, meta_dir, phone_dir, info, "interrupted")
        raise
    except Exception as e:
        _finish(record, save, meta_dir, phone_dir, info, "failed", str(e))
        raise
    _finish(record, save, meta_dir, phone_dir, info, "success")
    push_metadata(phone_dir, record["remote"]["target"])
    return record, record_path


def import_legacy(phone_dir, folders, record, reporter, dry_run):
    """Move files from old dated backup folders into this phone's folder. Never overwrites:
    files already present (same path and size) are left behind as duplicates, and files that
    differ from what's already there are left behind as conflicts."""
    record.update(source_folders=[], imported=[], duplicates_left=[], conflicts_left=[], bytes_imported=0)
    backed_up = index_dcim(phone_dir)
    for folder in folders:
        folder = os.path.abspath(os.path.expanduser(folder))
        if not os.path.isdir(folder):
            raise RuntimeError(f"Not a folder: {folder}")
        record["source_folders"].append(folder)
        for path, size in sorted(index_dcim(folder).items()):
            entry = {"path": path, "size": size, "from": folder}
            if path in backed_up:
                record["duplicates_left" if backed_up[path] == size else "conflicts_left"].append(entry)
                continue
            if not dry_run:
                dest = os.path.join(phone_dir, path)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                os.rename(os.path.join(folder, path), dest)
            backed_up[path] = size
            record["imported"].append(entry)
            record["bytes_imported"] += size or 0
        if not dry_run:
            # Remove directories the move emptied, including the folder itself if nothing is left.
            for root, _, _ in os.walk(folder, topdown=False):
                try:
                    os.rmdir(root)
                except OSError:
                    pass

    reporter.say(f"{'Would import' if dry_run else 'Imported'} {len(record['imported']):,} files "
                 f"({fmt_bytes(record['bytes_imported'])}); left behind {len(record['duplicates_left']):,} "
                 f"duplicates and {len(record['conflicts_left']):,} conflicts", "ok")
    for entry in record["conflicts_left"]:
        reporter.say(f"conflict (different size from backup): {entry['from']}/{entry['path']}", "warn")
    return "success"


def run_import(reporter, udid, folders, dry_run=False):
    info = device_info(udid)
    phone_dir = phone_dir_for(udid)
    if dry_run:
        import_legacy(phone_dir, folders, {}, reporter, True)
        return
    phone_dir, meta_dir = setup_phone_dir(udid)
    record, save, _ = start_record(meta_dir, "import", info)
    try:
        status = import_legacy(phone_dir, folders, record, reporter, False)
    except Exception as e:
        _finish(record, save, meta_dir, phone_dir, info, "failed", str(e))
        raise
    _finish(record, save, meta_dir, phone_dir, info, status)
