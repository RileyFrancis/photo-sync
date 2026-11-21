# Photo Sync desktop app

Plug an iPhone into a Windows or Linux computer, press **Back up now**, and its photos and videos go
straight to the photo server (frantierphotos.duckdns.org). Nothing is saved on the computer.

- Only photos the server doesn't already have are sent. Running it again is quick.
- Each file is checked against a fingerprint the iPhone computes itself, so damaged copies are caught and re-sent.
- If a backup stops (cable pulled, Stop pressed, Wi-Fi drops), the next one carries on where it left off.
- Photos hidden in the iPhone's Photos app are never sent.

## Installing

Download the installer for your computer from the
[Releases page](https://github.com/RileyFrancis/photo-sync/releases).

### Windows
1. Install Apple's free **Apple Devices** app from the Microsoft Store (or iTunes). Windows needs
   it to talk to an iPhone.
2. Run **PhotoSync-Setup.exe**. No admin rights are needed.
3. Open **Photo Sync** from the Start menu.

### Linux
1. Make sure the iPhone service is installed (it usually is): `sudo apt install usbmuxd`
2. Download **PhotoSync-x86_64.AppImage**, then make it executable:
   `chmod +x PhotoSync-x86_64.AppImage` (or *Properties → Allow executing as program*).
3. Double-click it. After the first launch, Photo Sync also appears in your applications menu.

## First run
1. In **Settings**, enter the same username and password you use for the photo website. Press
   **Test connection** to check them, then **Save**. The password is stored in the system keychain.
2. Plug in the iPhone and unlock it. The first time, the phone asks **Trust This Computer?**:
   tap **Trust** and enter the passcode.
3. Press **Back up now**. Keep the phone unlocked and plugged in until it finishes.

## Building it yourself

From this `desktop/` folder, with Python 3.12 or newer:

```sh
python -m venv .venv
.venv/bin/pip install -r requirements.txt pyinstaller      # Windows: .venv\Scripts\pip
.venv/bin/python -m photosync_app                          # run from source
.venv/bin/pyinstaller --noconfirm packaging/photosync.spec # build dist/PhotoSync/
sh packaging/build-appimage.sh                             # Linux: dist/PhotoSync-x86_64.AppImage
iscc /DAppVersion=1.0.0 packaging\windows.iss              # Windows (Inno Setup): dist\PhotoSync-Setup.exe
```

Releases are built by GitHub Actions (`.github/workflows/build-desktop.yml`). To make one, bump
`__version__` in `photosync_app/__init__.py`, then push a tag:
`git tag desktop-v1.0.0 && git push origin desktop-v1.0.0`.

## How it works
- `phone.py` reads the iPhone over USB with [pymobiledevice3](https://github.com/doronz88/pymobiledevice3)
  (AFC, the same protocol iTunes uses for the camera roll), through usbmuxd on Linux or Apple's
  driver on Windows.
- `backup.py` asks the server which phone files it already has (`/api/<udid>/backup/manifest`),
  then streams each new file in 8 MB pieces through the server's resumable upload API
  (`/api/<udid>/upload/...`). It sends the file's path on the phone and the phone's SHA-1 along with it.
- `ui.py` is the Tkinter window; `config.py` holds the settings.
- Files land on the server under `Uploads/DCIM/...` for that phone and show up in the gallery.
