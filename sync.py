"""Back up an iPhone's photos into ~/iphone-backups/<UDID>/ and push them to the remote server.

The web app (app.py) does the same thing from a browser; see photosync/core.py for the
backup layout and how syncing works.
"""

import argparse
import shutil
import sys
import time

from photosync import core
from photosync.core import fmt_bytes, fmt_duration


class TerminalReporter(core.Reporter):
    """A status line that redraws in place on a terminal. When output goes to a file or pipe
    it prints a plain line every PLAIN_INTERVAL seconds instead."""
    PLAIN_INTERVAL = 30

    def __init__(self):
        self.tty = sys.stdout.isatty()
        self.shown = False
        self.last_plain = 0.0

    def color(self, text, code):
        return f"\x1b[{code}m{text}\x1b[0m" if self.tty else text

    def stage(self, text):
        if self.tty:
            width = shutil.get_terminal_size().columns
            sys.stdout.write("\r" + text[: width - 1] + "\x1b[K")
            sys.stdout.flush()
            self.shown = True
        elif time.monotonic() - self.last_plain >= self.PLAIN_INTERVAL:
            print(text, flush=True)
            self.last_plain = time.monotonic()

    def progress(self, p):
        bar = "█" * round(p["fraction"] * 20) + "░" * (20 - round(p["fraction"] * 20))
        eta = fmt_duration(p["eta"]) if p["eta"] is not None else "--:--"
        if p["phase"] in ("copy", "verify"):
            head = "Verifying" if p["phase"] == "verify" else f"Attempt {p['attempt']}/{p['max_attempts']}"
            amounts = (f"{p['files_done']:,}/{p['total_files']:,} files  "
                       f"{fmt_bytes(p['bytes_done'])}/{fmt_bytes(p['total_bytes'])}")
            if p.get("mismatched"):
                amounts += f"  {p['mismatched']:,} damaged"
        else:
            head = "Pushing"
            amounts = f"{p['files_done']:,} files  {fmt_bytes(p['bytes_done'])}"
        text = (f"{head} {bar} {p['fraction']:4.0%}  {amounts}  {fmt_bytes(p['speed'])}/s  "
                f"{fmt_duration(p['elapsed'])} elapsed, {eta} left")
        if p["unreadable"]:
            text += f"  ! {p['unreadable']} unreadable"
        if p["last_file"]:
            text += f"  {p['last_file']}"
        self.stage(text)

    def clear(self):
        if self.shown:
            sys.stdout.write("\r\x1b[K")
            sys.stdout.flush()
            self.shown = False

    def say(self, text, kind="info"):
        symbol = {"info": "•", "ok": self.color("✓", "32"), "warn": self.color("!", "33"),
                  "error": self.color("✗", "31")}[kind]
        self.clear()
        print(f"{symbol} {text}", flush=True)


ui = TerminalReporter()


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--udid", help="Which phone to back up, if more than one is connected")
    parser.add_argument("--remote", default=core.DEFAULT_REMOTE,
                        help=f"Where to push the backup, as ssh-host:path (default {core.DEFAULT_REMOTE})")
    parser.add_argument("--no-push", action="store_true", help="Back up locally only")
    parser.add_argument("--push-only", action="store_true",
                        help="Push the existing local backup to the server without reading the phone")
    parser.add_argument("--verify", action="store_true",
                        help="Re-read every backed-up file from the phone, compare byte for byte, "
                             "and re-copy any damaged ones (slow: reads the whole backup from the phone)")
    parser.add_argument(
        "--import-legacy", nargs="+", metavar="FOLDER",
        help="Instead of syncing, move old dated backup folders from this phone into its folder "
             "(e.g. --import-legacy ~/iphone-backups/2026-09-17*)",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="With --import-legacy: report what would be moved without moving anything")
    return parser.parse_args()


def main():
    args = parse_args()
    if args.dry_run and not args.import_legacy:
        raise SystemExit("--dry-run only applies to --import-legacy")

    if args.push_only:
        udid = args.udid or next((d["udid"] for d in core.backed_up_devices()), None)
        if not udid:
            raise RuntimeError("No backups to push")
        core.run_push(ui, udid, args.remote)
        return

    if args.import_legacy:
        udid = core.pick_udid(args.udid)
        core.require_paired(udid)
        core.run_import(ui, udid, args.import_legacy, args.dry_run)
        return

    if args.verify:
        record, record_path = core.run_verify(ui, args.udid, None if args.no_push else args.remote)
        if record["status"] != "success":
            print(f"  Details in {record_path}")
            sys.exit(1)
        return

    record, record_path = core.run_sync(ui, args.udid, None if args.no_push else args.remote)
    if record["status"] == "partial":
        for path in record["failed"][:10]:
            print(f"    {path}")
        if len(record["failed"]) > 10:
            print(f"    … and {len(record['failed']) - 10:,} more")
        print(f"  Full list in {record_path}")
    if record["status"] == "partial" or record.get("remote", {}).get("status") == "failed":
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        ui.say("Stopped. Files copied so far are kept; run again to continue.", "warn")
        sys.exit(130)
    except RuntimeError as e:
        ui.say(str(e), "error")
        sys.exit(1)
