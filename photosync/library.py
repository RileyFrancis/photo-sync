"""Searchable index of a phone's backup, plus thumbnails and browser-friendly copies.

Everything here is a cache in ~/.cache/photo-sync/<UDID>/ and can be deleted at any time; it is
rebuilt from the backup folder. Nothing in the backup itself is modified.
"""

import datetime
import os
import re
import sqlite3
import subprocess
import threading
from contextlib import closing

import imageio_ffmpeg
import pillow_heif
from PIL import Image, ImageOps

from . import core

pillow_heif.register_heif_opener()
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
CACHE_ROOT = os.environ.get("PHOTOSYNC_CACHE") or os.path.expanduser("~/.cache/photo-sync")
INDEX_VERSION = 2  # bump to re-read metadata for every file (2: GPS coordinates)

KINDS = {
    ".HEIC": "photo", ".HEIF": "photo", ".JPG": "photo", ".JPEG": "photo", ".DNG": "photo",
    ".WEBP": "photo", ".GIF": "photo", ".TIF": "photo", ".TIFF": "photo",
    ".PNG": "screenshot",
    ".MOV": "video", ".MP4": "video", ".M4V": "video", ".3GP": "video",
}
# Formats every browser can show as-is; anything else is converted to JPEG for viewing.
BROWSER_IMAGE_EXTS = {".JPG", ".JPEG", ".PNG", ".GIF", ".WEBP"}
LIVE_VIDEO_MAX_SECONDS = 4.5
THUMB_SIZE = 480
VIEW_SIZE = 2560

SORTS = {
    "taken_desc": "taken_at DESC, name DESC",
    "taken_asc": "taken_at ASC, name ASC",
    "name_asc": "name ASC",
    "name_desc": "name DESC",
    "size_desc": "size DESC",
    "size_asc": "size ASC",
    "duration_desc": "duration DESC NULLS LAST, taken_at DESC",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS media (
    id INTEGER PRIMARY KEY,
    path TEXT UNIQUE NOT NULL,      -- relative to the phone's backup folder, e.g. DCIM/100APPLE/IMG_0001.HEIC
    name TEXT NOT NULL,
    folder TEXT NOT NULL,
    ext TEXT NOT NULL,
    kind TEXT NOT NULL,             -- photo, screenshot or video
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    inode INTEGER,                  -- changes when a file is rewritten (e.g. repaired) at the same size and time
    taken_at TEXT,                 -- local wall-clock time "YYYY-MM-DD HH:MM:SS"
    taken_source TEXT,              -- exif, video or file (fell back to the file's modified time)
    width INTEGER,
    height INTEGER,
    duration REAL,
    codec TEXT,
    camera TEXT,
    has_location INTEGER NOT NULL DEFAULT 0,
    lat REAL,                       -- GPS position in degrees, when the file has one
    lon REAL,
    live_video_id INTEGER,          -- on a Live Photo's still: the id of its motion clip
    is_live_video INTEGER NOT NULL DEFAULT 0,
    index_version INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS media_taken ON media (taken_at);
CREATE INDEX IF NOT EXISTS media_kind ON media (kind);
"""


# ---------------------------------------------------------------- metadata

def _exif_time(value):
    try:
        return datetime.datetime.strptime(str(value).strip()[:19], "%Y:%m:%d %H:%M:%S").strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def photo_metadata(path):
    meta = {}
    with Image.open(path) as im:
        meta["width"], meta["height"] = im.size
        exif = im.getexif()
        sub = exif.get_ifd(0x8769)  # Exif IFD
        taken = _exif_time(sub.get(36867) or sub.get(36868) or exif.get(306) or "")
        if taken:
            meta["taken_at"], meta["taken_source"] = taken, "exif"
        if exif.get(274) in (5, 6, 7, 8):  # rotated 90°: show portrait dimensions
            meta["width"], meta["height"] = meta["height"], meta["width"]
        model = exif.get(272)
        meta["camera"] = str(model).strip("\x00 ") if model else None
        meta.update(_exif_position(exif.get_ifd(0x8825)))
    return meta


def _exif_position(gps):
    """Latitude/longitude from an EXIF GPS block (degrees, minutes, seconds plus N/S/E/W)."""
    try:
        def degrees(dms, ref, negative):
            d, m, sec = (float(x) for x in dms)
            value = d + m / 60 + sec / 3600
            return -value if str(ref).strip("\x00 ").upper() == negative else value
        lat, lon = degrees(gps[2], gps.get(1, "N"), "S"), degrees(gps[4], gps.get(3, "E"), "W")
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return {"has_location": 0}
    return _position(lat, lon)


def _position(lat, lon):
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
        return {"has_location": 0}  # 0,0 is what some apps write when they don't know
    return {"has_location": 1, "lat": round(lat, 6), "lon": round(lon, 6)}


def video_metadata(path):
    # ffmpeg with no output prints the container metadata to stderr (and exits non-zero).
    out = subprocess.run([FFMPEG, "-hide_banner", "-i", path], capture_output=True, text=True,
                         errors="replace", timeout=60).stderr
    meta = {}
    if m := re.search(r"com\.apple\.quicktime\.creationdate\s*:\s*(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)", out):
        meta["taken_at"], meta["taken_source"] = m.group(1).replace("T", " "), "video"
    elif m := re.search(r"creation_time\s*:\s*(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)", out):
        utc = datetime.datetime.fromisoformat(m.group(1)).replace(tzinfo=datetime.timezone.utc)
        if utc.year > 1971:  # some apps write 1970 when they don't know
            meta["taken_at"], meta["taken_source"] = utc.astimezone().strftime("%Y-%m-%d %H:%M:%S"), "video"
    if m := re.search(r"Duration:\s*(\d+):(\d\d):(\d\d(?:\.\d+)?)", out):
        meta["duration"] = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    if m := re.search(r"Video:\s*(\w+).*?,\s*(\d{2,5})x(\d{2,5})", out):
        meta["codec"] = m.group(1)
        meta["width"], meta["height"] = int(m.group(2)), int(m.group(3))
        if re.search(r"rotation of -?90", out):
            meta["width"], meta["height"] = meta["height"], meta["width"]
    if m := re.search(r"com\.apple\.quicktime\.model\s*:\s*(.+)", out):
        meta["camera"] = m.group(1).strip()
    # ISO 6709, e.g. "+41.7960-072.2344+201.745/" (latitude, longitude, altitude)
    if m := re.search(r"location(?:\.ISO6709)?\s*:\s*([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)", out):
        meta.update(_position(float(m.group(1)), float(m.group(2))))
    return meta


def read_metadata(full_path, kind, mtime):
    try:
        meta = video_metadata(full_path) if kind == "video" else photo_metadata(full_path)
    except Exception:
        meta = {}  # unreadable or unsupported; still list it, dated by the file
    if not meta.get("taken_at"):
        meta["taken_at"] = datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
        meta["taken_source"] = "file"
    return meta


# ---------------------------------------------------------------- the library

class Library:
    """One phone's backup. Safe to use from several threads."""

    def __init__(self, udid):
        self.udid = udid
        self.phone_dir = core.phone_dir_for(udid)
        self.cache_dir = os.path.join(CACHE_ROOT, udid)
        for sub in ("thumbs", "view", "video"):
            os.makedirs(os.path.join(self.cache_dir, sub), exist_ok=True)
        self.db_path = os.path.join(self.cache_dir, "library.db")
        self.index_lock = threading.Lock()
        self.convert_locks = {}
        self.convert_locks_guard = threading.Lock()
        self.ffmpeg_slots = threading.Semaphore(max(2, (os.cpu_count() or 4) // 2))
        self.indexing = {"running": False, "done": 0, "total": 0, "error": None,
                         "version": 0}  # bumps whenever the index changes, so pages know to reload
        with closing(self.db()) as db:
            db.executescript(SCHEMA)
            if "inode" not in {r["name"] for r in db.execute("PRAGMA table_info(media)")}:
                db.execute("ALTER TABLE media ADD COLUMN inode INTEGER")  # databases from before inode tracking
            if "lat" not in {r["name"] for r in db.execute("PRAGMA table_info(media)")}:
                # Databases from before coordinates were stored: only files known to have a
                # location need re-reading, so mark the rest as already up to date.
                db.execute("ALTER TABLE media ADD COLUMN lat REAL")
                db.execute("ALTER TABLE media ADD COLUMN lon REAL")
                db.execute("UPDATE media SET index_version = 2 WHERE index_version = 1 AND has_location = 0")
                db.commit()

    def db(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    # ------------------------------------------------ indexing

    def refresh(self):
        """Bring the index up to date with the backup folder. Runs in the caller's thread;
        returns immediately if another refresh is already running."""
        if not self.index_lock.acquire(blocking=False):
            return
        try:
            self.indexing.update(running=True, done=0, total=0, error=None)
            self._refresh()
        except Exception as e:
            self.indexing["error"] = str(e)
            raise
        finally:
            self.indexing["running"] = False
            self.index_lock.release()

    def _refresh(self):
        on_disk = {}
        for root, _, files in os.walk(os.path.join(self.phone_dir, "DCIM")):
            for name in files:
                ext = os.path.splitext(name)[1].upper()
                if name.startswith(".") or ext not in KINDS:
                    continue  # rsync temp files, .AAE edit sidecars, etc.
                full = os.path.join(root, name)
                st = os.stat(full)
                on_disk[os.path.relpath(full, self.phone_dir)] = (st.st_size, st.st_mtime, st.st_ino)

        with closing(self.db()) as db:
            known = {r["path"]: r for r in db.execute("SELECT id, path, size, mtime, inode, index_version FROM media")}
            gone = [r["id"] for p, r in known.items() if p not in on_disk]
            todo = [p for p, (size, mtime, inode) in on_disk.items()
                    if p not in known or known[p]["size"] != size or known[p]["mtime"] != mtime
                    or known[p]["inode"] != inode or known[p]["index_version"] != INDEX_VERSION]
            self.indexing["total"] = len(todo)

            for media_id in gone:
                db.execute("DELETE FROM media WHERE id = ?", (media_id,))
                self._drop_cached(media_id)
            for i, path in enumerate(sorted(todo), 1):
                size, mtime, inode = on_disk[path]
                ext = os.path.splitext(path)[1].upper()
                kind = KINDS[ext]
                meta = read_metadata(os.path.join(self.phone_dir, path), kind, mtime)
                row = {"path": path, "name": os.path.basename(path), "folder": os.path.dirname(path),
                       "ext": ext, "kind": kind, "size": size, "mtime": mtime, "inode": inode,
                       "width": None, "height": None, "duration": None, "codec": None, "camera": None,
                       "has_location": 0, "lat": None, "lon": None, "index_version": INDEX_VERSION, **meta}
                old = known.get(path)
                if old and (old["size"], old["mtime"], old["inode"]) != (size, mtime, inode):
                    self._drop_cached(old["id"])  # the file itself changed, not just the index format
                cols = ", ".join(row)
                db.execute(f"INSERT INTO media ({cols}) VALUES ({', '.join('?' * len(row))}) "
                           f"ON CONFLICT(path) DO UPDATE SET " + ", ".join(f"{c}=excluded.{c}" for c in row),
                           list(row.values()))
                self.indexing["done"] = i
                if i % 200 == 0:
                    db.commit()
            db.commit()
            if gone or todo:
                self.indexing["version"] += 1
                self._pair_live_photos(db)
                db.commit()

    def _pair_live_photos(self, db):
        """A Live Photo is a still plus a short clip with the same name in the same folder."""
        db.execute("UPDATE media SET live_video_id = NULL, is_live_video = 0")
        stills = {}
        for r in db.execute("SELECT id, folder, name FROM media WHERE kind = 'photo'"):
            stills[(r["folder"], os.path.splitext(r["name"])[0])] = r["id"]
        pairs = []
        for r in db.execute("SELECT id, folder, name, duration FROM media WHERE kind = 'video'"):
            still = stills.get((r["folder"], os.path.splitext(r["name"])[0]))
            if still and (r["duration"] or 0) <= LIVE_VIDEO_MAX_SECONDS:
                pairs.append((still, r["id"]))
        db.executemany("UPDATE media SET live_video_id = ? WHERE id = ?", [(v, s) for s, v in pairs])
        db.executemany("UPDATE media SET is_live_video = 1 WHERE id = ?", [(v,) for _, v in pairs])

    # ------------------------------------------------ queries

    def search(self, ext="", kind="", date_from="", date_to="", camera="", sort="taken_desc",
               include_live_clips=False, offset=0, limit=200):
        clause, args = self._filters(ext, kind, date_from, date_to, camera, include_live_clips)
        order = SORTS.get(sort, SORTS["taken_desc"])
        with closing(self.db()) as db:
            total, total_bytes = db.execute(f"SELECT COUNT(*), COALESCE(SUM(size), 0) FROM media {clause}", args).fetchone()
            rows = db.execute(
                "SELECT *, (SELECT v.codec FROM media v WHERE v.id = media.live_video_id) AS live_codec, "
                "(SELECT v.inode FROM media v WHERE v.id = media.live_video_id) AS live_inode "
                f"FROM media {clause} ORDER BY {order} LIMIT ? OFFSET ?", args + [limit, offset]).fetchall()
        return {"total": total, "total_bytes": total_bytes, "items": [dict(r) for r in rows]}

    def places(self, ext="", kind="", date_from="", date_to="", camera="", include_live_clips=False):
        """Every matching item with a GPS position, newest first, as compact [id, lat, lon, inode] rows."""
        clause, args = self._filters(ext, kind, date_from, date_to, camera, include_live_clips, located=True)
        with closing(self.db()) as db:
            rows = db.execute(f"SELECT id, lat, lon, inode FROM media {clause} ORDER BY taken_at DESC, name DESC", args).fetchall()
            missing = db.execute("SELECT COUNT(*) FROM media WHERE has_location = 1 AND lat IS NULL").fetchone()[0]
        return {"points": [list(r) for r in rows], "pending": missing}

    def get_many(self, ids):
        """Full rows for these ids, in the order given (unknown ids are skipped)."""
        with closing(self.db()) as db:
            rows = {r["id"]: dict(r) for r in db.execute(
                "SELECT *, (SELECT v.codec FROM media v WHERE v.id = media.live_video_id) AS live_codec, "
                "(SELECT v.inode FROM media v WHERE v.id = media.live_video_id) AS live_inode "
                f"FROM media WHERE id IN ({','.join('?' * len(ids))})", ids)} if ids else {}
        return [rows[i] for i in ids if i in rows]

    @staticmethod
    def _filters(ext, kind, date_from, date_to, camera, include_live_clips, located=False):
        where, args = [], []
        if located:
            where.append("lat IS NOT NULL")
        if not include_live_clips:
            where.append("is_live_video = 0")
        if kind == "live":
            where.append("live_video_id IS NOT NULL")
        elif kind:
            where.append("kind = ?")
            args.append(kind)
        if ext:
            where.append("ext = ?")
            args.append("." + ext.lstrip(".").upper())
        if date_from:
            where.append("taken_at >= ?")
            args.append(date_from)
        if date_to:
            where.append("taken_at < date(?, '+1 day')")
            args.append(date_to)
        if camera:
            where.append("camera = ?")
            args.append(camera)
        return ("WHERE " + " AND ".join(where)) if where else "", args

    def facets(self):
        with closing(self.db()) as db:
            kinds = {r[0]: r[1] for r in db.execute(
                "SELECT kind, COUNT(*) FROM media WHERE is_live_video = 0 GROUP BY kind")}
            kinds["live"] = db.execute("SELECT COUNT(*) FROM media WHERE live_video_id IS NOT NULL").fetchone()[0]
            cameras = [{"name": r[0], "count": r[1]} for r in db.execute(
                "SELECT camera, COUNT(*) c FROM media WHERE camera IS NOT NULL AND is_live_video = 0 "
                "GROUP BY camera ORDER BY c DESC")]
            exts = [{"name": r[0].lstrip("."), "count": r[1]} for r in db.execute(
                "SELECT ext, COUNT(*) c FROM media WHERE is_live_video = 0 GROUP BY ext ORDER BY c DESC")]
            first, last = db.execute("SELECT MIN(taken_at), MAX(taken_at) FROM media").fetchone()
            total, size = db.execute("SELECT COUNT(*), COALESCE(SUM(size), 0) FROM media").fetchone()
        return {"kinds": kinds, "cameras": cameras, "exts": exts, "first": first, "last": last,
                "total": total, "total_bytes": size}

    def get(self, media_id):
        with closing(self.db()) as db:
            row = db.execute("SELECT * FROM media WHERE id = ?", (media_id,)).fetchone()
        return dict(row) if row else None

    def full_path(self, item):
        return os.path.join(self.phone_dir, item["path"])

    # ------------------------------------------------ derived files

    def _drop_cached(self, media_id):
        for sub, ext in (("thumbs", "jpg"), ("view", "jpg"), ("video", "mp4")):
            try:
                os.remove(os.path.join(self.cache_dir, sub, f"{media_id}.{ext}"))
            except FileNotFoundError:
                pass

    def _lock_for(self, key):
        with self.convert_locks_guard:
            return self.convert_locks.setdefault(key, threading.Lock())

    def _cached(self, sub, item, ext, make):
        """Path of a cached derived file, making it first if needed (once, even if several
        requests ask at the same time)."""
        out = os.path.join(self.cache_dir, sub, f"{item['id']}.{ext}")
        if os.path.exists(out):
            return out
        with self._lock_for(out):
            if not os.path.exists(out):
                tmp = out + ".tmp." + ext
                make(self.full_path(item), tmp)
                os.replace(tmp, out)
        return out

    def thumbnail(self, item):
        def make(src, dst):
            if item["kind"] == "video":
                self._video_frame(src, dst, item.get("duration") or 0)
            else:
                with Image.open(src) as im:
                    im = ImageOps.exif_transpose(im)
                    im.thumbnail((THUMB_SIZE, THUMB_SIZE))
                    im.convert("RGB").save(dst, "JPEG", quality=80)
        return self._cached("thumbs", item, "jpg", make)

    def _video_frame(self, src, dst, duration):
        seek = "0.5" if duration > 1 else "0"
        with self.ffmpeg_slots:
            subprocess.run([FFMPEG, "-v", "error", "-ss", seek, "-i", src, "-frames:v", "1",
                            "-vf", f"scale={THUMB_SIZE}:{THUMB_SIZE}:force_original_aspect_ratio=decrease",
                            "-f", "image2", "-c:v", "mjpeg", "-q:v", "4", "-y", dst],
                           check=True, capture_output=True, timeout=120)

    def viewable_image(self, item):
        """A file the browser can show: the original if it's a web format, else a JPEG copy."""
        if item["ext"] in BROWSER_IMAGE_EXTS:
            return self.full_path(item)

        def make(src, dst):
            with Image.open(src) as im:
                im = ImageOps.exif_transpose(im)
                im.thumbnail((VIEW_SIZE, VIEW_SIZE))
                im.convert("RGB").save(dst, "JPEG", quality=88)
        return self._cached("view", item, "jpg", make)

    def compatible_video(self, item):
        """An H.264 MP4 copy for browsers that can't play the original (usually HEVC)."""
        def make(src, dst):
            with self.ffmpeg_slots:
                subprocess.run([FFMPEG, "-v", "error", "-i", src, "-map", "0:v:0", "-map", "0:a:0?",
                                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
                                "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", "-f", "mp4", "-y", dst],
                               check=True, capture_output=True)
        return self._cached("video", item, "mp4", make)
