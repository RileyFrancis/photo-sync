# photo-sync

Backs up iPhone photos to a self-hosted photo server (`manifold`, at frantierphotos.duckdns.org)
and shows them in a web gallery.

- **`desktop/`**: the Photo Sync desktop app for Windows and Linux. Plug in the phone, press
  **Back up now**, and new photos go straight to the server. See [desktop/README.md](desktop/README.md)
  for installing and building it.
- **`photosync/`**: the server. It runs the gallery website plus the upload API that the desktop
  app (and the website's **Back up photos** button) sends photos through.
- **`deploy/`**: setup and deployment for manifold (Caddy for HTTPS and the login, gunicorn, systemd).
  Deploy with `sh deploy/deploy.sh`.

## Running the gallery locally (for testing)

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt   # first time only
.venv/bin/python app.py
```

Then open http://localhost:8765.
