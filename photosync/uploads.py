"""Photos and videos uploaded from a browser (e.g. from a phone, over the internet).

An upload is sent in pieces so a dropped connection resumes instead of starting over:

  1. start:  the browser describes the file (name, size, date, and a quick fingerprint).
             If the library already has it, nothing is sent. Otherwise the reply says how
             much the server already holds (0, or more when resuming).
  2. chunk:  pieces are appended in order; each must start where the last one ended.
  3. finish: the server checks the file is complete and matches its fingerprint, then
             files it under Uploads/<year-month>/ with the photo's date as its file time.

The desktop app (desktop/photosync_app) uses the same steps, and also sends the file's path on
the phone (DCIM/100APPLE/IMG_0001.HEIC) and the SHA-1 the phone computed for it. The file is then
filed at Uploads/DCIM/... instead, the whole file is checked against that SHA-1, and the path is
remembered so the app knows not to send it again (see phone_manifest).

The quick fingerprint is SHA-256 over the size and the first and last 64 KiB. Two files with the
same size and fingerprint are treated as the same photo, so already-backed-up photos are skipped
(re-selecting photos is harmless).
"""

import datetime
import hashlib
import json
import os
import posixpath
import re
import shutil
import threading
import time

from . import core
from .library import KINDS, UPLOADS_PREFIX

QUICK_BYTES = 64 * 1024
CHUNK_BYTES = 8 * 1024 * 1024        # what the browser is told to send per request
MAX_CHUNK_BYTES = 32 * 1024 * 1024   # what the server accepts per request
MAX_FILE_BYTES = 20 * 1024 ** 3
STALE_SECONDS = 7 * 24 * 3600        # unfinished uploads older than this are thrown away


class UploadError(Exception):
    def __init__(self, message, status=400, **extra):
        super().__init__(message)
        self.status = status
        self.extra = extra


def quick_fingerprint(path, size=None):
    size = os.path.getsize(path) if size is None else size
    h = hashlib.sha256(str(size).encode())
    with open(path, "rb") as f:
        h.update(f.read(QUICK_BYTES))
        f.seek(max(0, size - QUICK_BYTES))
        h.update(f.read(QUICK_BYTES))
    return h.hexdigest()


def safe_phone_path(path):
    """A path on the phone like DCIM/100APPLE/IMG_0001.HEIC, checked so it can't leave DCIM/."""
    path = posixpath.normpath(str(path).replace("\\", "/"))
    parts = path.split("/")
    if (len(parts) < 2 or parts[0] != "DCIM" or any(p in ("", ".", "..") or p.startswith(".") for p in parts)
            or not re.fullmatch(r"[\w .()+-]+", "".join(parts[1:]))):
        raise UploadError("Bad path on the phone.")
    return path


def file_sha1(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        while block := f.read(1 << 20):
            h.update(block)
    return h.hexdigest()


def safe_name(name):
    """A plain file name: no folders, no odd characters, extension kept."""
    name = os.path.basename(name.replace("\\", "/")).strip()
    stem, ext = os.path.splitext(name)
    stem = re.sub(r"[^A-Za-z0-9 ._()+-]", "_", stem).strip(" .") or "photo"
    return stem[:120] + ext.lower()


class Uploads:
    """In-progress uploads for one library. Thread-safe."""

    def __init__(self, library):
        self.library = library
        self.tmp_dir = os.path.join(library.upload_dir, ".incoming")
        os.makedirs(self.tmp_dir, exist_ok=True)
        self.lock = threading.Lock()
        self.ledger_lock = threading.Lock()  # separate: _remember is also called with self.lock held
        self._clean_stale()

    # ------------------------------------------------ steps

    def start(self, name, size, modified_ms, fingerprint, phone_path=None, sha1=None):
        if phone_path:
            phone_path = safe_phone_path(phone_path)
            name = posixpath.basename(phone_path)
        if sha1 is not None and not re.fullmatch(r"[0-9a-f]{40}", sha1):
            raise UploadError("Bad SHA-1.")
        ext = os.path.splitext(name)[1].upper()
        if ext not in KINDS:
            raise UploadError(f"{ext or 'This kind of file'} isn't a photo or video this app keeps.", 415)
        if not (0 < size <= MAX_FILE_BYTES):
            raise UploadError("File is empty or too large.", 413)
        if not re.fullmatch(r"[0-9a-f]{64}", fingerprint or ""):
            raise UploadError("Missing fingerprint.")
        if disk_free(self.library.upload_dir) < size + 2 * 1024 ** 3:
            raise UploadError("The server is out of space. Ask Riley to make room.", 507)
        if self.find_existing(size, fingerprint):
            self._remember(phone_path, size)
            return {"status": "exists"}
        upload_id = hashlib.sha256(
            f"{name}\0{size}\0{modified_ms}\0{fingerprint}\0{phone_path}".encode()).hexdigest()[:32]
        meta = {"name": name, "size": size, "modified_ms": modified_ms, "fingerprint": fingerprint,
                "phone_path": phone_path, "sha1": sha1}
        with self.lock:
            with open(self._meta(upload_id), "w") as f:
                json.dump(meta, f)
            part = self._part(upload_id)
            if not os.path.exists(part):
                open(part, "wb").close()
            offset = os.path.getsize(part)
        return {"status": "upload", "id": upload_id, "offset": offset, "chunk": CHUNK_BYTES}

    def chunk(self, upload_id, offset, stream, length):
        meta = self._load(upload_id)
        if length is None or length <= 0 or length > MAX_CHUNK_BYTES:
            raise UploadError("Chunk is empty or too large.", 413)
        with self.lock:
            part = self._part(upload_id)
            have = os.path.getsize(part)
            if offset != have:
                raise UploadError("Out of order; resume from the server's offset.", 409, offset=have)
            if have + length > meta["size"]:
                raise UploadError("More data than the file's size.", 400)
            written = 0
            with open(part, "ab") as f:
                while written < length:
                    block = stream.read(min(1024 * 1024, length - written))
                    if not block:
                        break
                    f.write(block)
                    written += len(block)
            if written != length:  # connection dropped mid-chunk: drop the partial piece
                with open(part, "ab") as f:
                    f.truncate(have)
                raise UploadError("Chunk was cut off; send it again.", 400, offset=have)
            os.utime(self._meta(upload_id))  # keeps active uploads from being cleaned up
            return {"offset": have + length}

    def finish(self, upload_id):
        meta = self._load(upload_id)
        with self.lock:
            part = self._part(upload_id)
            have = os.path.getsize(part)
            if have != meta["size"]:
                raise UploadError("Upload isn't complete yet.", 409, offset=have)
            if quick_fingerprint(part, have) != meta["fingerprint"]:
                os.remove(part)
                raise UploadError("The uploaded file doesn't match the original; it will be sent again.", 422)
            if meta.get("sha1") and file_sha1(part) != meta["sha1"]:
                os.remove(part)
                raise UploadError("The uploaded file doesn't match the phone's copy; it will be sent again.", 422)
            phone_path = meta.get("phone_path")
            existing = self.find_existing(meta["size"], meta["fingerprint"])
            if existing:
                os.remove(part)
                os.remove(self._meta(upload_id))
                self._remember(phone_path, meta["size"])
                return {"status": "exists"}
            when = meta["modified_ms"] / 1000 if meta.get("modified_ms") else time.time()
            if phone_path:  # keep the phone's folder layout: Uploads/DCIM/100APPLE/IMG_0001.HEIC
                dest_dir = os.path.join(self.library.upload_dir, *posixpath.dirname(phone_path).split("/"))
            else:
                dest_dir = os.path.join(self.library.upload_dir, time.strftime("%Y-%m", time.localtime()))
            os.makedirs(dest_dir, exist_ok=True)
            # A phone path's name was already checked by safe_phone_path; keep it exactly as on the phone.
            dest = self._free_name(dest_dir, meta["name"] if phone_path else safe_name(meta["name"]))
            os.replace(part, dest)
            os.utime(dest, (when, when))
            os.remove(self._meta(upload_id))
            if phone_path and os.path.basename(dest) != meta["name"]:
                self._remember(phone_path, meta["size"])  # renamed to avoid a clash: not findable by path
            self._note_upload()
        threading.Thread(target=self.library.refresh, daemon=True).start()
        return {"status": "saved", "path": UPLOADS_PREFIX + os.path.relpath(dest, self.library.upload_dir)}

    def info(self):
        try:
            with open(os.path.join(self.library.upload_dir, ".last-upload.json")) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {"last_upload_at": None, "uploaded": 0}

    def phone_manifest(self):
        """{path on the phone: size} for everything already backed up from this phone: the
        rsync'd backup (older laptop syncs), files the desktop app uploaded, and ones it found were
        already here under another name. The desktop app only sends what isn't listed."""
        manifest = {**core.index_dcim(self.library.phone_dir), **core.index_dcim(self.library.upload_dir)}
        return {**{p.replace(os.sep, "/"): s for p, s in manifest.items()}, **self._ledger()}

    # ------------------------------------------------ helpers

    def _ledger_path(self):
        return os.path.join(self.library.upload_dir, ".phone-ledger.json")

    def _ledger(self):
        try:
            with open(self._ledger_path()) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _remember(self, phone_path, size):
        """Note that this phone file is backed up even though it isn't stored at its own path."""
        if not phone_path:
            return
        with self.ledger_lock:
            ledger = self._ledger()
            ledger[phone_path] = size
            path = self._ledger_path()
            with open(path + ".tmp", "w") as f:
                json.dump(ledger, f)
            os.replace(path + ".tmp", path)

    def find_existing(self, size, fingerprint):
        for path in self.library.paths_with_size(size):
            try:
                if quick_fingerprint(path, size) == fingerprint:
                    return path
            except OSError:
                pass
        return None

    def _note_upload(self):
        info = self.info()
        info["uploaded"] = info.get("uploaded", 0) + 1
        info["last_upload_at"] = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
        path = os.path.join(self.library.upload_dir, ".last-upload.json")
        with open(path + ".tmp", "w") as f:
            json.dump(info, f)
        os.replace(path + ".tmp", path)

    @staticmethod
    def _free_name(folder, name):
        stem, ext = os.path.splitext(name)
        candidate, n = os.path.join(folder, name), 2
        while os.path.exists(candidate):
            candidate = os.path.join(folder, f"{stem} ({n}){ext}")
            n += 1
        return candidate

    def _part(self, upload_id):
        return os.path.join(self.tmp_dir, f"{upload_id}.part")

    def _meta(self, upload_id):
        return os.path.join(self.tmp_dir, f"{upload_id}.json")

    def _load(self, upload_id):
        if not re.fullmatch(r"[0-9a-f]{32}", upload_id):
            raise UploadError("Unknown upload.", 404)
        try:
            with open(self._meta(upload_id)) as f:
                return json.load(f)
        except (OSError, ValueError):
            raise UploadError("Unknown upload; start it again.", 404)

    def _clean_stale(self):
        cutoff = time.time() - STALE_SECONDS
        for name in os.listdir(self.tmp_dir):
            path = os.path.join(self.tmp_dir, name)
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
            except OSError:
                pass


def disk_free(path):
    return shutil.disk_usage(path).free
