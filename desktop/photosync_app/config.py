"""Settings (server address and login) and a little state (when the last backup ran).

Stored in the usual per-user place: %APPDATA%\\PhotoSync on Windows, ~/.config/PhotoSync on Linux.
The password goes in the system keychain (Windows Credential Manager, or the Secret Service on
Linux, e.g. GNOME Keyring) when one is available, and in the settings file otherwise.
"""

import json
import os

import platformdirs

APP_NAME = "PhotoSync"
DEFAULT_SERVER = "https://frantierphotos.duckdns.org"
KEYRING_SERVICE = "PhotoSync"

CONFIG_DIR = platformdirs.user_config_dir(APP_NAME, appauthor=False)
CACHE_DIR = platformdirs.user_cache_dir(APP_NAME, appauthor=False)
SETTINGS_FILE = os.path.join(CONFIG_DIR, "settings.json")


def _keyring():
    try:
        import keyring
        from keyring.backends import fail
        if isinstance(keyring.get_keyring(), fail.Keyring):
            return None
        return keyring
    except Exception:
        return None


def load():
    try:
        with open(SETTINGS_FILE) as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    data.setdefault("server", DEFAULT_SERVER)
    data.setdefault("username", "")
    password = data.pop("password", None)
    kr = _keyring()
    if kr and data["username"]:
        try:
            password = kr.get_password(KEYRING_SERVICE, data["username"]) or password
        except Exception:
            pass
    data["password"] = password or ""
    return data


def save(data):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    stored = {k: v for k, v in data.items() if k != "password"}
    kr = _keyring()
    saved_in_keyring = False
    if kr and data.get("username"):
        try:
            kr.set_password(KEYRING_SERVICE, data["username"], data.get("password", ""))
            saved_in_keyring = True
        except Exception:
            pass
    if not saved_in_keyring:
        stored["password"] = data.get("password", "")
    tmp = SETTINGS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(stored, f, indent=2)
    try:
        os.chmod(tmp, 0o600)  # may hold the password; no effect on Windows
    except OSError:
        pass
    os.replace(tmp, SETTINGS_FILE)


def is_complete(data):
    return bool(data.get("server") and data.get("username") and data.get("password"))
