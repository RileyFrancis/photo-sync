"""Photos and videos uploaded from a browser (e.g. from a phone, over the internet).

An upload is sent in pieces so a dropped connection resumes instead of starting over:

  1. start:  the browser describes the file (name, size, date, and a quick fingerprint).
             If the library already has it, nothing is sent. Otherwise the reply says how
             much the server already holds (0, or more when resuming).
  2. chunk:  pieces are appended in order; each must start where the last one ended.
  3. finish: the server checks the file is complete and matches its fingerprint, then
             files it under Uploads/<year-month>/ with the photo's date as its file time.

The quick fingerprint is SHA-256 over the size and the first and last 64 KiB. Two files with the
same size and fingerprint are treated as the same photo, so already-backed-up photos are skipped
(re-selecting photos is harmless).
"""

import datetime
import hashlib
import json
import os
import re
import shutil
import threading
import time

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
        self._clean_stale()

    # ------------------------------------------------ steps

    def start(self, name, size, modified_ms, fingerprint):
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
            return {"status": "exists"}
        upload_id = hashlib.sha256(f"{name}\0{size}\0{modified_ms}\0{fingerprint}".encode()).hexdigest()[:32]
        meta = {"name": name, "size": size, "modified_ms": modified_ms, "fingerprint": fingerprint}
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
            existing = self.find_existing(meta["size"], meta["fingerprint"])
            if existing:
                os.remove(part)
                os.remove(self._meta(upload_id))
                return {"status": "exists"}
            when = meta["modified_ms"] / 1000 if meta.get("modified_ms") else time.time()
            folder = time.strftime("%Y-%m", time.localtime())
            dest_dir = os.path.join(self.library.upload_dir, folder)
            os.makedirs(dest_dir, exist_ok=True)
            dest = self._free_name(dest_dir, safe_name(meta["name"]))
            os.replace(part, dest)
            os.utime(dest, (when, when))
            os.remove(self._meta(upload_id))
            self._note_upload()
        threading.Thread(target=self.library.refresh, daemon=True).start()
        return {"status": "saved", "path": UPLOADS_PREFIX + os.path.relpath(dest, self.library.upload_dir)}

    def info(self):
        try:
            with open(os.path.join(self.library.upload_dir, ".last-upload.json")) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {"last_upload_at": None, "uploaded": 0}

    # ------------------------------------------------ helpers

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
