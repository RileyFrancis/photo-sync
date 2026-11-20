"""Entry point for the hosted gallery on manifold, run by gunicorn (see deploy/photo-sync.service).

Gallery only: phone sync, verify and push stay on the laptop. The whole site sits behind
Caddy, which handles HTTPS and the login prompt (deploy/Caddyfile).
"""

import os

DATA = os.environ.get("PHOTOSYNC_DATA", "/srv/photo-sync/data")
# Must be set before the app modules are imported: they read these at import time.
os.environ.setdefault("PHOTOSYNC_HOSTED", "1")
os.environ.setdefault("PHOTOSYNC_LIBRARY", "/srv/photo-sync/library")
os.environ.setdefault("PHOTOSYNC_CACHE", os.path.join(DATA, "cache"))

from .web import app, refresh_libraries  # noqa: E402

refresh_libraries()  # index anything new in the background on startup
