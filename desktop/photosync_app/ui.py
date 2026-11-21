"""The app window (Tkinter, which ships with Python on Windows and Linux)."""

import asyncio
import datetime
import json
import os
import queue
import shutil
import sys
import threading
import time
import tkinter as tk
import traceback
import webbrowser
from tkinter import font as tkfont, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

import httpx

from . import __version__, config
from .backup import Backup, Reporter, fmt_bytes, fmt_duration
from .client import Client, ServerError, friendly
from .phone import Phone, connected_udids, explain

STATE_FILE = os.path.join(config.CONFIG_DIR, "state.json")
POLL_SECONDS = 2
# A few common models; anything else shows its identifier (e.g. "iPhone17,1").
MODELS = {
    "iPhone14,7": "iPhone 14", "iPhone14,8": "iPhone 14 Plus", "iPhone15,2": "iPhone 14 Pro",
    "iPhone15,3": "iPhone 14 Pro Max", "iPhone15,4": "iPhone 15", "iPhone15,5": "iPhone 15 Plus",
    "iPhone16,1": "iPhone 15 Pro", "iPhone16,2": "iPhone 15 Pro Max", "iPhone17,1": "iPhone 16 Pro",
    "iPhone17,2": "iPhone 16 Pro Max", "iPhone17,3": "iPhone 16", "iPhone17,4": "iPhone 16 Plus",
    "iPhone17,5": "iPhone 16e",
}


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state):
    os.makedirs(config.CONFIG_DIR, exist_ok=True)
    with open(STATE_FILE + ".tmp", "w") as f:
        json.dump(state, f, indent=2)
    os.replace(STATE_FILE + ".tmp", STATE_FILE)


def fmt_ago(iso):
    if not iso:
        return "never"
    when = datetime.datetime.fromisoformat(iso)
    days = (datetime.datetime.now().astimezone() - when).days
    clock = when.strftime("%-I:%M %p") if os.name != "nt" else when.strftime("%#I:%M %p")
    if days == 0 and when.date() == datetime.date.today():
        return f"today at {clock}"
    if days <= 1:
        return f"yesterday at {clock}"
    return when.strftime("%b %d, %Y")


class QueueReporter(Reporter):
    """Hands progress from the backup thread to the window (Tk must only be touched from its thread)."""

    def __init__(self, q):
        self.q = q

    def stage(self, text):
        self.q.put(("stage", text))

    def progress(self, p):
        self.q.put(("progress", p))

    def log(self, text, kind="info"):
        self.q.put(("log", (text, kind)))


class App:
    def __init__(self, root):
        self.root = root
        self.settings = config.load()
        self.state = load_state()
        self.q = queue.Queue()
        self.cancel = threading.Event()
        self.running = False
        self.phone = None          # info dict of the connected phone, or None
        self.phone_message = "Looking for your iPhone…"
        self.phone_infos = {}      # udid -> info, so each phone is only connected to once
        self.stop_polling = threading.Event()

        root.title("Photo Sync")
        self._build()
        root.update_idletasks()
        # Size to the content (which scales with the screen's DPI), with room for the activity log.
        root.minsize(root.winfo_reqwidth(), root.winfo_reqheight())
        self._show_last_backup()
        threading.Thread(target=self._poll_phone, daemon=True).start()
        root.after(100, self._drain)
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        if not config.is_complete(self.settings):
            root.after(300, self.open_settings)

    # ------------------------------------------------ layout

    def _build(self):
        style = ttk.Style()
        if "clam" in style.theme_names() and os.name != "nt":
            style.theme_use("clam")
        if os.name != "nt":
            # Tk's default Linux font is often an old one missing characters like "…" and "’".
            available = set(tkfont.families())
            family = next((f for f in ("Ubuntu", "Cantarell", "Noto Sans", "DejaVu Sans", "Liberation Sans")
                           if f in available), None)
            if family:
                for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont", "TkCaptionFont"):
                    tkfont.nametofont(name).configure(family=family)
        base = tkfont.nametofont("TkDefaultFont")
        size = abs(base.cget("size")) or 10

        def derived(scale, weight="normal"):
            f = base.copy()
            f.configure(size=round(size * scale), weight=weight)
            return f
        self.fonts = {"title": derived(1.8, "bold"), "big": derived(1.3, "bold"), "small": derived(0.9)}
        style.configure("Title.TLabel", font=self.fonts["title"])
        style.configure("Big.TLabel", font=self.fonts["big"])
        style.configure("Muted.TLabel", foreground="#666")
        style.configure("Big.TButton", font=self.fonts["big"], padding=(20, 8))

        outer = ttk.Frame(self.root, padding=20)
        outer.pack(fill="both", expand=True)

        ttk.Label(outer, text="Photo Sync", style="Title.TLabel").pack(anchor="w")
        ttk.Label(outer, text="Back up your iPhone's photos to your photo server.",
                  style="Muted.TLabel").pack(anchor="w", pady=(0, 14))

        card = ttk.LabelFrame(outer, text="iPhone", padding=12)
        card.pack(fill="x")
        self.phone_title = ttk.Label(card, text="Not connected", style="Big.TLabel")
        self.phone_title.pack(anchor="w")
        self.phone_detail = ttk.Label(card, text=self.phone_message, style="Muted.TLabel")
        self.phone_detail.pack(anchor="w")
        self.last_backup = ttk.Label(card, text="", style="Muted.TLabel")
        self.last_backup.pack(anchor="w", pady=(6, 0))

        actions = ttk.Frame(outer)
        actions.pack(fill="x", pady=16)
        self.button = ttk.Button(actions, text="Back up now", style="Big.TButton", command=self.toggle_backup)
        self.button.pack(side="left")
        ttk.Button(actions, text="Open gallery", command=self.open_gallery).pack(side="right")
        ttk.Button(actions, text="Settings…", command=self.open_settings).pack(side="right", padx=(0, 8))

        self.status = ttk.Label(outer, text="")
        self.status.pack(anchor="w")
        self.bar = ttk.Progressbar(outer, mode="determinate", maximum=1000)
        self.bar.pack(fill="x", pady=(6, 2))
        self.detail = ttk.Label(outer, text="", style="Muted.TLabel")
        self.detail.pack(anchor="w")

        ttk.Label(outer, text="Activity").pack(anchor="w", pady=(14, 2))
        self.log_box = ScrolledText(outer, height=8, width=60, wrap="word", state="disabled", relief="flat",
                                    borderwidth=1, font=self.fonts["small"])
        self.log_box.pack(fill="both", expand=True)
        self.log_box.tag_configure("ok", foreground="#1a7f37")
        self.log_box.tag_configure("warn", foreground="#9a6700")
        self.log_box.tag_configure("error", foreground="#cf222e")
        ttk.Label(outer, text=f"Version {__version__}", style="Muted.TLabel").pack(anchor="e", pady=(6, 0))
        self._update_button()
        self.wrapping = [self.phone_detail, self.status, self.detail]
        outer.bind("<Configure>", lambda e: [w.configure(wraplength=max(200, e.width - 40)) for w in self.wrapping])

    def log(self, text, kind="info"):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", f"{time.strftime('%H:%M:%S')}  {text}\n", kind)
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _show_last_backup(self):
        last = self.state.get("last_backup")
        if last:
            text = f"Last backup: {fmt_ago(last.get('at'))}"
            if last.get("status") in ("partial", "failed", "interrupted"):
                text += f" ({last['status']})"
            elif last.get("uploaded"):
                text += f", {last['uploaded']:,} new"
        else:
            text = "Last backup: never"
        self.last_backup.configure(text=text)

    def _update_button(self):
        if self.running:
            self.button.configure(text="Stop", state="normal")
        else:
            self.button.configure(text="Back up now", state="normal" if self.phone else "disabled")

    def _show_phone(self):
        if self.phone:
            model = MODELS.get(self.phone.get("product_type"), self.phone.get("product_type") or "iPhone")
            self.phone_title.configure(text=self.phone.get("name") or "iPhone")
            self.phone_detail.configure(text=f"{model} · iOS {self.phone.get('ios_version') or '?'} · connected")
        else:
            self.phone_title.configure(text="Not connected")
            self.phone_detail.configure(text=self.phone_message)
        self._update_button()

    # ------------------------------------------------ phone detection (background thread)

    def _poll_phone(self):
        while not self.stop_polling.is_set():
            if not self.running:
                try:
                    info, message = asyncio.run(self._check_phone())
                except Exception as e:
                    info, message = None, explain(e)
                self.q.put(("phone", (info, message)))
            self.stop_polling.wait(POLL_SECONDS)

    async def _check_phone(self):
        udids = await connected_udids()
        if not udids:
            return None, "Plug in your iPhone with a cable and unlock it."
        udid = udids[0]
        if udid not in self.phone_infos:
            phone = await Phone.connect(udid, pair_timeout=60)
            self.phone_infos[udid] = phone.info
            await phone.close()
        return self.phone_infos[udid], ""

    # ------------------------------------------------ backup

    def toggle_backup(self):
        if self.running:
            self.cancel.set()
            self.button.configure(text="Stopping…", state="disabled")
            return
        if not config.is_complete(self.settings):
            self.open_settings()
            return
        self.running = True
        self.cancel.clear()
        self._update_button()
        self.bar.configure(mode="indeterminate")
        self.bar.start(12)
        self.detail.configure(text="")
        self.log("Backup started.")
        settings = dict(self.settings)
        threading.Thread(target=self._backup_thread, args=(settings,), daemon=True).start()

    def _backup_thread(self, settings):
        backup = Backup(settings, QueueReporter(self.q), self.cancel, config.CACHE_DIR)
        try:
            summary = backup.run()
            self.q.put(("done", (summary, None)))
        except Exception as e:
            if not isinstance(e, (RuntimeError, ServerError, httpx.HTTPError)):
                traceback.print_exc()
            message = friendly(e) if isinstance(e, (ServerError, httpx.HTTPError)) else explain(e)
            self.q.put(("done", (backup.summary, message)))

    def _finish(self, summary, error):
        self.running = False
        self.bar.stop()
        self.bar.configure(mode="determinate", value=1000 if summary.get("status") in ("success", "no_changes") else 0)
        status = summary.get("status") or "failed"
        if error:
            self.log(error, "error")
            self.status.configure(text="Backup didn't finish.")
        uploaded, failed = summary.get("uploaded", 0), len(summary.get("failed", []))
        if status == "no_changes":
            self.status.configure(text="Everything is already backed up.")
            self.log("Everything is already backed up.", "ok")
        elif status == "success":
            text = f"Done: backed up {uploaded:,} new photo{'s' if uploaded != 1 else ''} ({fmt_bytes(summary.get('bytes', 0))})."
            self.status.configure(text=text)
            self.log(text, "ok")
        elif status == "partial":
            text = f"Backed up {uploaded:,}, but {failed:,} couldn't be sent. Press Back up now to try them again."
            self.status.configure(text=text)
            self.log(text, "warn")
        elif status == "interrupted":
            self.status.configure(text=f"Stopped after {uploaded:,} photo{'s' if uploaded != 1 else ''}.")
        self.detail.configure(text="")
        if summary.get("phone"):
            self.state["last_backup"] = {"at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
                                         "status": status, "uploaded": uploaded}
            save_state(self.state)
            self._show_last_backup()
        self._update_button()

    def _show_progress(self, p):
        if str(self.bar.cget("mode")) != "determinate":
            self.bar.stop()
            self.bar.configure(mode="determinate")
        self.bar.configure(value=round(p["fraction"] * 1000))
        current = min(p["files_done"] + 1, p["total_files"]) if p.get("file") else p["files_done"]
        self.status.configure(text=f"Backing up {current:,} of {p['total_files']:,} "
                                   f"({fmt_bytes(p['bytes_done'])} of {fmt_bytes(p['total_bytes'])})")
        bits = []
        if p.get("speed"):
            bits.append(f"{fmt_bytes(p['speed'])}/s")
        if p.get("eta"):
            bits.append(f"about {fmt_duration(p['eta'])} left")
        if p.get("file"):
            bits.append(p["file"].rsplit("/", 1)[-1])
        self.detail.configure(text=" · ".join(bits))

    # ------------------------------------------------ queue from background threads

    def _drain(self):
        try:
            while True:
                kind, value = self.q.get_nowait()
                if kind == "phone":
                    info, message = value
                    if (info or {}).get("udid") != (self.phone or {}).get("udid") and info:
                        self.log(f"Connected: {info.get('name') or 'iPhone'}")
                    self.phone, self.phone_message = info, message
                    self._show_phone()
                elif kind == "stage":
                    self.status.configure(text=value)
                elif kind == "progress":
                    self._show_progress(value)
                elif kind == "log":
                    self.log(*value)
                elif kind == "done":
                    self._finish(*value)
        except queue.Empty:
            pass
        self.root.after(100, self._drain)

    # ------------------------------------------------ other buttons

    def open_gallery(self):
        webbrowser.open(self.settings.get("server") or config.DEFAULT_SERVER)

    def open_settings(self):
        SettingsDialog(self.root, self.settings, self._settings_saved)

    def _settings_saved(self, new):
        self.settings = new
        config.save(new)
        self.log("Settings saved.")

    def _on_close(self):
        if self.running and not messagebox.askyesno(
                "Photo Sync", "A backup is running. Stop it and quit?\n\nEverything sent so far is kept."):
            return
        self.cancel.set()
        self.stop_polling.set()
        self.root.destroy()


class SettingsDialog:
    def __init__(self, parent, settings, on_save):
        self.on_save = on_save
        self.top = top = tk.Toplevel(parent)
        top.title("Settings")
        top.transient(parent)
        top.resizable(False, False)
        frame = ttk.Frame(top, padding=16)
        frame.pack(fill="both", expand=True)
        self.vars = {}
        for row, (key, label, show) in enumerate((("server", "Server address", ""),
                                                  ("username", "Username", ""),
                                                  ("password", "Password", "•"))):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=4, padx=(0, 10))
            var = tk.StringVar(value=settings.get(key, ""))
            entry = ttk.Entry(frame, textvariable=var, width=40, show=show)
            entry.grid(row=row, column=1, sticky="ew", pady=4)
            self.vars[key] = var
            if row == 0:
                entry.focus_set()
        ttk.Label(frame, text="The same username and password you use for the photo website.",
                  style="Muted.TLabel").grid(row=3, column=1, sticky="w")
        self.result = ttk.Label(frame, text="")
        self.result.grid(row=4, column=0, columnspan=2, sticky="w", pady=(10, 0))
        buttons = ttk.Frame(frame)
        buttons.grid(row=5, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="Test connection", command=self.test).pack(side="left", padx=(0, 8))
        ttk.Button(buttons, text="Cancel", command=top.destroy).pack(side="left", padx=(0, 8))
        ttk.Button(buttons, text="Save", command=self.save).pack(side="left")
        top.update_idletasks()
        self.result.configure(wraplength=frame.winfo_reqwidth() - 40)
        top.bind("<Return>", lambda e: self.save())
        top.bind("<Escape>", lambda e: top.destroy())
        top.wait_visibility()
        top.grab_set()

    def values(self):
        v = {k: var.get().strip() for k, var in self.vars.items()}
        v["password"] = self.vars["password"].get()  # passwords may have spaces
        if v["server"] and "://" not in v["server"]:
            v["server"] = "https://" + v["server"]
        return v

    def test(self):
        self.result.configure(text="Checking…")
        values = self.values()

        def run():
            async def check():
                async with Client(values["server"], values["username"], values["password"]) as c:
                    await c.check()
            try:
                asyncio.run(check())
                message = "✓ Connected. The login works."
            except Exception as e:
                message = "✗ " + friendly(e)
            self.top.after(0, lambda: self.result.configure(text=message))
        threading.Thread(target=run, daemon=True).start()

    def save(self):
        values = self.values()
        if not values["server"]:
            self.result.configure(text="Enter the server address.")
            return
        self.on_save(values)
        self.top.destroy()


def add_to_app_menu():
    """When running as an AppImage, add Photo Sync to the Linux applications menu (and keep the
    entry pointing at wherever the AppImage now lives)."""
    appimage = os.environ.get("APPIMAGE")
    if not appimage or not sys.platform.startswith("linux"):
        return
    try:
        share = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
        icon = os.path.join(share, "icons", "photosync.png")
        os.makedirs(os.path.dirname(icon), exist_ok=True)
        shutil.copyfile(os.path.join(os.path.dirname(__file__), "icon.png"), icon)
        entry = ("[Desktop Entry]\nType=Application\nName=Photo Sync\n"
                 "Comment=Back up your iPhone's photos to your photo server\n"
                 f'Exec="{appimage}"\nIcon={icon}\nCategories=Graphics;Photography;Utility;\nTerminal=false\n')
        path = os.path.join(share, "applications", "photosync.desktop")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            with open(path) as f:
                if f.read() == entry:
                    return
        except OSError:
            pass
        with open(path, "w") as f:
            f.write(entry)
    except OSError:
        pass  # only a convenience


def main():
    add_to_app_menu()
    root = tk.Tk()
    icon = os.path.join(os.path.dirname(__file__), "icon.png")
    if os.path.exists(icon):
        try:
            root.iconphoto(True, tk.PhotoImage(file=icon))
        except tk.TclError:
            pass
    App(root)
    root.mainloop()
