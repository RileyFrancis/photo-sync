"""One backup run: read new photos off the phone and send them straight to the server.

Nothing is stored on this computer except a cached copy of the phone's Photos database, used to
skip hidden and recently deleted photos. Each file is streamed from the phone to the server in 8 MB pieces using the
server's resumable upload API, then checked against the SHA-1 the phone computes for it.
"""

import asyncio
import os
import posixpath
import time

import httpx
from pymobiledevice3 import exceptions as pmd_exc

from .client import Client, ServerError, friendly
from .phone import Phone, PhoneError, connected_udids

CHUNK_BYTES = 8 * 1024 * 1024
MAX_FAILURES_IN_A_ROW = 5  # after this many, something's wrong with the connection: stop
# Kept in sync with KINDS in photosync/library.py: other files (e.g. .AAE edit data) are skipped.
KINDS = {".HEIC", ".HEIF", ".JPG", ".JPEG", ".DNG", ".WEBP", ".GIF", ".TIF", ".TIFF", ".PNG",
         ".MOV", ".MP4", ".M4V", ".3GP"}
# Losing the phone or the server mid-run stops the whole backup rather than failing file by file.
FATAL = (PhoneError, pmd_exc.ConnectionTerminatedError, pmd_exc.NoDeviceConnectedError,
         ConnectionResetError, BrokenPipeError)


class Cancelled(Exception):
    pass


class Reporter:
    """Receives progress. The window shows it; all methods are called from the backup thread."""

    def stage(self, text): ...
    def progress(self, p): ...
    def log(self, text, kind="info"): ...


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


class Backup:
    def __init__(self, settings, reporter, cancel, cache_dir):
        self.settings = settings
        self.r = reporter
        self.cancel = cancel  # threading.Event
        self.cache_dir = cache_dir
        self.summary = {"status": None, "uploaded": 0, "already": 0, "failed": [], "bytes": 0, "phone": None}

    def run(self):
        """Run the whole backup (blocking). Returns the summary dict."""
        return asyncio.run(self._run())

    def _check_cancel(self):
        if self.cancel.is_set():
            raise Cancelled()

    async def _run(self):
        s = self.summary
        client = Client(self.settings["server"], self.settings["username"], self.settings["password"])
        phone = None
        udid = None
        try:
            self.r.stage("Connecting to the server…")
            await client.check()

            self.r.stage("Looking for the iPhone…")
            udids = await connected_udids()
            if not udids:
                raise PhoneError("No iPhone found. Plug it in with a cable and unlock it.")
            if len(udids) > 1:
                self.r.log(f"{len(udids)} iPhones are plugged in; backing up the first one.", "warn")
            udid = udids[0]
            self.r.stage("Connecting to the iPhone (unlock it and tap Trust if asked)…")
            phone = await Phone.connect(udid)
            s["phone"] = phone.info
            await client.register(udid, phone.info)

            self.r.stage("Asking the server what's already backed up…")
            on_server = await client.manifest(udid)
            self._check_cancel()

            started = time.monotonic()
            self.r.stage("Listing photos on the phone…")
            listing = await phone.list_dcim(
                skip=lambda p: p in on_server or posixpath.splitext(p)[1].upper() not in KINDS,
                on_progress=lambda n: self.r.stage(f"Listing photos on the phone… {n:,} files "
                                                   f"({fmt_duration(time.monotonic() - started)})"))
            wanted = {p: m for p, m in listing.items()
                      if p not in on_server and posixpath.splitext(p)[1].upper() in KINDS}
            empty = [p for p, m in wanted.items() if not m["size"]]
            wanted = {p: m for p, m in wanted.items() if m["size"]}
            if empty:
                self.r.log(f"Skipping {len(empty)} empty file(s) on the phone.", "warn")
            if wanted:
                excluded = await phone.excluded_paths(wanted, os.path.join(self.cache_dir, udid, "photos-db"),
                                                      on_stage=self.r.stage)
                wanted = {p: m for p, m in wanted.items() if p not in excluded}
                note = f" ({len(excluded):,} hidden or recently deleted skipped)" if excluded else ""
            else:
                note = ""
            photos = sum(1 for p in listing if posixpath.splitext(p)[1].upper() in KINDS)
            self.r.log(f"{photos:,} photos and videos on the phone, {len(wanted):,} not backed up yet{note}.")
            if not wanted:
                s["status"] = "no_changes"
                return s

            await self._upload_all(phone, client, udid, wanted)
            s["status"] = "partial" if s["failed"] else "success"
            return s
        except Cancelled:
            s["status"] = "interrupted"
            self.r.log("Stopped. Everything finished so far is safely on the server.", "warn")
            return s
        except Exception:
            s["status"] = "failed"
            raise
        finally:
            if udid and s["phone"]:
                try:
                    await client.finished(udid, s["status"] or "failed", s["uploaded"], len(s["failed"]))
                except Exception:
                    pass
            if phone:
                await phone.close()
            await client.close()

    async def _upload_all(self, phone, client, udid, wanted):
        s = self.summary
        # Newest first, so the most recent photos are safe soonest.
        order = sorted(wanted, key=lambda p: (wanted[p]["mtime"] or 0, p), reverse=True)
        total_bytes = sum(m["size"] for m in wanted.values())
        state = {"done_bytes": 0, "started": time.monotonic()}
        failures_in_a_row = 0

        def report(path, index, file_bytes):
            done = state["done_bytes"] + file_bytes
            elapsed = time.monotonic() - state["started"]
            speed = done / elapsed if elapsed > 1 else 0
            self.r.progress({
                "file": path, "files_done": index, "total_files": len(order),
                "bytes_done": done, "total_bytes": total_bytes,
                "fraction": done / total_bytes if total_bytes else 1,
                "speed": speed, "eta": (total_bytes - done) / speed if speed else None,
            })

        for index, path in enumerate(order):
            self._check_cancel()
            size = wanted[path]["size"]
            report(path, index, 0)
            try:
                result = await self._upload_one(phone, client, udid, path, wanted[path],
                                                lambda n: report(path, index, n))
                failures_in_a_row = 0
                if result == "exists":
                    s["already"] += 1
                else:
                    s["uploaded"] += 1
                    s["bytes"] += size
            except (Cancelled, *FATAL):
                raise
            except (ServerError, httpx.HTTPError, pmd_exc.AfcException, OSError) as e:
                if isinstance(e, ServerError) and e.status == 401:
                    raise
                failures_in_a_row += 1
                s["failed"].append(path)
                self.r.log(f"Couldn't back up {path}: {friendly(e)}", "error")
                if failures_in_a_row >= MAX_FAILURES_IN_A_ROW:
                    raise RuntimeError(f"{failures_in_a_row} files in a row failed; stopping. "
                                       f"Last error: {friendly(e)}") from e
            state["done_bytes"] += size
        report(None, len(order), 0)

    async def _upload_one(self, phone, client, udid, path, meta, on_bytes):
        """Send one file. Returns "saved" or "exists" (the server already had it)."""
        size = meta["size"]
        handle = await phone.open(path)
        try:
            fingerprint = await phone.quick_fingerprint(handle, size)
            sha1 = await phone.sha1(path)
            for attempt in (1, 2):
                started = await client.start(
                    udid, name=posixpath.basename(path), size=size, phone_path=path, sha1=sha1,
                    modified=int((meta["mtime"] or 0) * 1000), fingerprint=fingerprint)
                if started["status"] == "exists":
                    return "exists"
                await self._send(phone, client, udid, handle, started["id"], started["offset"], size, on_bytes)
                try:
                    return (await client.finish(udid, started["id"]))["status"]
                except ServerError as e:
                    if e.status == 422 and attempt == 1:  # arrived damaged: the server dropped it; resend
                        self.r.log(f"{path} arrived damaged; sending it again.", "warn")
                        continue
                    raise
        finally:
            try:
                await phone.fclose(handle)
            except Exception:
                pass

    async def _send(self, phone, client, udid, handle, upload_id, offset, size, on_bytes):
        """Stream the file from `offset`, reading the next piece from the phone while the current
        one uploads."""
        def read(at):
            return asyncio.ensure_future(phone.pread(handle, min(CHUNK_BYTES, size - at), at))

        pending = read(offset) if offset < size else None
        while offset < size:
            self._check_cancel()
            data = await pending
            if not data:
                raise PhoneError("The phone stopped sending the file partway through.")
            after = offset + len(data)
            pending = read(after) if after < size else None
            try:
                offset = (await client.chunk(udid, upload_id, offset, data))["offset"]
            except ServerError as e:
                if e.status == 409 and "offset" in e.data:  # server holds a different amount: resume there
                    if pending:
                        await pending  # let the in-flight read finish; AFC handles one request at a time
                    offset = int(e.data["offset"])
                    pending = read(offset) if offset < size else None
                    continue
                if pending:
                    await pending
                raise
            on_bytes(offset)
            if offset != after and pending:  # server is somewhere else; re-read from there
                await pending
                pending = read(offset) if offset < size else None
