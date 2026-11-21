"""Talking to the photo server (photosync/web.py): the site login plus the upload API."""

import httpx

TIMEOUT = httpx.Timeout(30.0, read=300.0, write=300.0)  # finish re-reads big videos to check them


class ServerError(Exception):
    def __init__(self, message, status=None, data=None):
        super().__init__(message)
        self.status = status
        self.data = data or {}


def friendly(e):
    if isinstance(e, ServerError):
        if e.status == 401:
            return "The server rejected the username or password. Check them in Settings."
        return str(e)
    if isinstance(e, httpx.ConnectError):
        return "Can't reach the server. Check the internet connection and the server address in Settings."
    if isinstance(e, httpx.TimeoutException):
        return "The server took too long to answer."
    return f"{type(e).__name__}: {e}"


class Client:
    def __init__(self, server, username, password):
        self.http = httpx.AsyncClient(
            base_url=server.rstrip("/"), auth=(username, password) if username else None,
            # Required by the server for anything that changes data (see require_app_header).
            headers={"X-Photo-Sync": "1", "User-Agent": "PhotoSync-desktop"},
            timeout=TIMEOUT, follow_redirects=True)

    async def close(self):
        await self.http.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def _call(self, method, url, **kw):
        r = await self.http.request(method, url, **kw)
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code >= 400:
            message = data.get("error") if isinstance(data, dict) else None
            raise ServerError(message or f"Server error {r.status_code}", r.status_code,
                              data if isinstance(data, dict) else {})
        return data

    async def check(self):
        """Raises if the server can't be reached or the login is wrong."""
        await self._call("GET", "/api/status")

    async def register(self, udid, info):
        await self._call("POST", f"/api/{udid}/backup/register", json=info)

    async def manifest(self, udid):
        return (await self._call("GET", f"/api/{udid}/backup/manifest"))["files"]

    async def finished(self, udid, status, uploaded, failed):
        await self._call("POST", f"/api/{udid}/backup/finished",
                         json={"status": status, "uploaded": uploaded, "failed": failed})

    async def start(self, udid, **fields):
        return await self._call("POST", f"/api/{udid}/upload/start", json=fields)

    async def chunk(self, udid, upload_id, offset, data):
        return await self._call("PUT", f"/api/{udid}/upload/{upload_id}", params={"offset": offset},
                                content=data)

    async def finish(self, udid, upload_id):
        return await self._call("POST", f"/api/{udid}/upload/{upload_id}/finish")
