"""Reading an iPhone over USB with pymobiledevice3 (works the same on Windows and Linux).

The phone is reached through usbmuxd: on Linux the system `usbmuxd` service, on Windows Apple's
"Apple Mobile Device Service", which comes with the Apple Devices app or iTunes. Files are read
with AFC, the protocol Finder/iTunes use for the camera roll. Nothing on the phone is changed.
"""

import asyncio
import hashlib
import os
import posixpath
import re
import sqlite3
import sys
from contextlib import closing

from pymobiledevice3 import exceptions as pmd_exc
from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.services.afc import AfcService
from pymobiledevice3.usbmux import list_devices

QUICK_BYTES = 64 * 1024  # must match photosync/uploads.py quick_fingerprint on the server
PHOTOS_DB_FILES = ("Photos.sqlite", "Photos.sqlite-wal", "Photos.sqlite-shm")


class PhoneError(Exception):
    """Something the person can fix (plug in, unlock, tap Trust, install drivers)."""


def explain(e):
    """A plain-language message for an error from pymobiledevice3."""
    if isinstance(e, PhoneError):
        return str(e)
    if isinstance(e, (pmd_exc.ConnectionFailedToUsbmuxdError, ConnectionRefusedError, FileNotFoundError)):
        if sys.platform == "win32":
            return ("Can't talk to Apple's iPhone driver. Install the free \"Apple Devices\" app from the "
                    "Microsoft Store (or iTunes), then try again.")
        return ("Can't talk to the iPhone service (usbmuxd). Install it with your package manager, "
                "e.g. `sudo apt install usbmuxd`, then plug the phone in again.")
    if isinstance(e, (pmd_exc.PasswordRequiredError,)):
        return "Unlock your iPhone, then try again."
    if isinstance(e, (pmd_exc.PairingDialogResponsePendingError, pmd_exc.NotPairedError, pmd_exc.NotTrustedError)):
        return "Unlock your iPhone and tap \"Trust\" when it asks about this computer."
    if isinstance(e, pmd_exc.UserDeniedPairingError):
        return "The iPhone said \"Don't Trust\". Unplug it, plug it back in and tap \"Trust\"."
    if isinstance(e, (pmd_exc.NoDeviceConnectedError, pmd_exc.DeviceNotFoundError,
                      pmd_exc.ConnectionTerminatedError, ConnectionResetError, BrokenPipeError)):
        return "The iPhone was disconnected. Plug it back in and keep it unlocked."
    return f"{type(e).__name__}: {e}" if str(e) else type(e).__name__


async def connected_udids():
    """UDIDs of iPhones plugged in by USB (Wi-Fi connections are ignored)."""
    return sorted({d.serial for d in await list_devices() if d.connection_type == "USB"})


class Phone:
    """An open connection to one phone. Use with `async with Phone.connect(udid) as phone`."""

    def __init__(self, lockdown, afc):
        self.lockdown = lockdown
        self.afc = afc
        values = lockdown.all_values
        self.info = {
            "udid": lockdown.udid,
            "name": values.get("DeviceName"),
            "product_type": values.get("ProductType"),
            "ios_version": values.get("ProductVersion"),
        }

    @classmethod
    async def connect(cls, udid=None, pair_timeout=120):
        # autopair: on first use the phone shows "Trust This Computer?"; this waits for the answer.
        lockdown = await create_using_usbmux(serial=udid, autopair=True, connection_type="USB",
                                             pair_timeout=pair_timeout)
        afc = AfcService(lockdown)
        await afc.__aenter__()
        return cls(lockdown, afc)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def close(self):
        try:
            await self.afc.aclose()
        except Exception:
            pass
        try:
            close = getattr(self.lockdown, "aclose", None) or getattr(self.lockdown, "close", None)
            if close:
                result = close()
                if asyncio.iscoroutine(result):
                    await result
        except Exception:
            pass

    # ------------------------------------------------ listing

    async def list_dcim(self, skip=lambda path: False, on_progress=None):
        """{path: {"size", "mtime"}} for camera-roll files, e.g. "DCIM/100APPLE/IMG_0001.HEIC".
        Files for which skip(path) is true are listed with size None and not stat'ed (much faster:
        each stat is a round trip to the phone)."""
        found = {}
        for folder in sorted(await self.afc.listdir("/DCIM")):
            if folder in (".", "..", "") or folder.startswith("."):
                continue
            base = f"/DCIM/{folder}"
            try:
                names = await self.afc.listdir(base)
            except pmd_exc.AfcException:
                continue  # not a folder
            for name in names:
                if name in (".", "..", "") or name.startswith("."):
                    continue
                path = f"DCIM/{folder}/{name}"
                if skip(path):
                    found[path] = {"size": None, "mtime": None}
                else:
                    st = await self.afc.stat("/" + path)
                    if st.get("st_ifmt") == "S_IFDIR":
                        continue
                    when = st.get("st_birthtime") or st.get("st_mtime")
                    found[path] = {"size": st["st_size"], "mtime": when.timestamp() if when else None}
                if on_progress and len(found) % 100 == 0:
                    on_progress(len(found))
        return found

    # ------------------------------------------------ reading

    async def open(self, path):
        return await self.afc.fopen("/" + path, "r")

    async def pread(self, handle, size, offset):
        return await self.afc.pread(handle, size, offset)

    async def fclose(self, handle):
        await self.afc.fclose(handle)

    async def quick_fingerprint(self, handle, size):
        """SHA-256 over the size and the first and last 64 KiB, as the server computes it."""
        h = hashlib.sha256(str(size).encode())
        h.update(await self.pread(handle, min(QUICK_BYTES, size), 0))
        tail_start = max(0, size - QUICK_BYTES)
        h.update(await self.pread(handle, size - tail_start, tail_start))
        return h.hexdigest()

    async def sha1(self, path):
        """SHA-1 of the whole file, computed by the phone itself (no need to read it twice)."""
        digest = await self.afc.get_file_hash("/" + path)
        return digest.hex() if len(digest) == 20 else None

    # ------------------------------------------------ hidden / recently deleted photos

    async def excluded_paths(self, paths, cache_dir, on_stage=None):
        """Paths among `paths` that belong to items hidden in the Photos app or sitting in Recently
        Deleted (including a Live Photo's clip and edited copies). Raises PhoneError if that can't
        be worked out, so a backup never sends those photos by accident.

        The phone's Photos database (often ~1 GB) is cached in cache_dir and only re-read when
        it changes; the write-ahead log (recent changes) is always re-read."""
        os.makedirs(cache_dir, exist_ok=True)
        try:
            for name in PHOTOS_DB_FILES:
                src, dst = f"/PhotoData/{name}", os.path.join(cache_dir, name)
                try:
                    st = await self.afc.stat(src)
                except pmd_exc.AfcFileNotFoundError:
                    if name == "Photos.sqlite":
                        raise
                    if os.path.exists(dst):
                        os.remove(dst)  # a stale log from an older copy would corrupt the snapshot
                    continue
                mtime = st["st_mtime"].timestamp()
                if name == "Photos.sqlite" and os.path.exists(dst):
                    cached = os.stat(dst)
                    if cached.st_size == st["st_size"] and int(cached.st_mtime) == int(mtime):
                        continue
                if on_stage:
                    on_stage(f"Reading the phone's Photos library to skip hidden and deleted photos "
                             f"({st['st_size'] / 1e6:,.0f} MB, only needed when it changes)…")
                await self._pull(src, dst + ".tmp", st["st_size"])
                os.utime(dst + ".tmp", (mtime, mtime))
                os.replace(dst + ".tmp", dst)
            db_path = os.path.join(cache_dir, "Photos.sqlite")
            # Read-only, so SQLite doesn't fold the log into the cached main file on close.
            with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as db:
                rows = db.execute("SELECT ZDIRECTORY, ZFILENAME FROM ZASSET "
                                  "WHERE ZHIDDEN = 1 OR ZTRASHEDSTATE != 0").fetchall()
        except (OSError, sqlite3.Error, pmd_exc.PyMobileDevice3Exception) as e:
            raise PhoneError(f"Couldn't read the phone's Photos library to find hidden and deleted photos ({explain(e)}). "
                             f"Stopping so they aren't backed up by mistake.") from e
        excluded_keys = {asset_key(f"{d}/{f}") for d, f in rows if d and f}
        return {p for p in paths if asset_key(p) in excluded_keys}

    async def _pull(self, src, dst, size, chunk=4 << 20):
        handle = await self.afc.fopen(src, "r")
        try:
            with open(dst, "wb") as f:
                offset = 0
                while offset < size:
                    data = await self.afc.pread(handle, min(chunk, size - offset), offset)
                    if not data:
                        break
                    f.write(data)
                    offset += len(data)
        finally:
            await self.afc.fclose(handle)


# IMG_1234.HEIC, IMG_1234.MOV (Live Photo clip), IMG_1234.AAE (edit data) and IMG_E1234.HEIC
# (edited copy) are all the same photo. This reduces a file name to that shared "IMG_1234".
_ASSET_STEM_RE = re.compile(r"^(.*?_)[EO]?(\d+)$")


def asset_key(path):
    folder, name = posixpath.split(path)
    stem = posixpath.splitext(name)[0].upper()
    m = _ASSET_STEM_RE.match(stem)
    return folder, (m.group(1) + m.group(2)) if m else stem
