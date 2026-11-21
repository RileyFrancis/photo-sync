# PyInstaller build for the Photo Sync desktop app (Windows and Linux).
# Run from desktop/:  pyinstaller packaging/photosync.spec
# Output: dist/PhotoSync/ (a folder: the app plus its own Python, Tk and libraries).
import os
import sys
from PyInstaller.utils.hooks import collect_submodules

ROOT = os.path.dirname(SPECPATH)  # desktop/
hiddenimports = collect_submodules("keyring.backends")  # keyring finds its backends by entry point

a = Analysis(
    [os.path.join(SPECPATH, "launch.py")],
    pathex=[ROOT],
    datas=[(os.path.join(ROOT, "photosync_app", "icon.png"), "photosync_app")],
    hiddenimports=hiddenimports,
    # Parts of pymobiledevice3 this app never uses (screen capture, developer tools, shells).
    excludes=["IPython", "matplotlib", "av", "PIL", "pytest", "jedi", "pygments.lexers.pmd"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="PhotoSync",
    console=False,  # a window app: no black console window on Windows
    icon=os.path.join(SPECPATH, "photosync.ico") if sys.platform == "win32" else None,
)
coll = COLLECT(exe, a.binaries, a.datas, name="PhotoSync")
