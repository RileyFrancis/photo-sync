# photo-sync

Backs up an iPhone's photos to `~/iphone-backups/<phone-id>/` and pushes them to `manifold`.

Plug in and unlock the phone, then run these from a regular terminal (not VS Code's: its sandbox blocks mounting the phone).

## Web app

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt   # first time only
.venv/bin/python app.py
```

Open http://localhost:8765 and click **Sync phone**.

## Command line

```sh
python3 sync.py              # copy new photos, then push to the server
python3 sync.py --no-push    # copy only
python3 sync.py --verify     # re-check backed-up files and repair damaged ones
```
