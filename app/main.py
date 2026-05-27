"""FastAPI HTTP server for the status monitor.

Serves the public dashboard, the Entra-gated admin dashboard, and the JSON API.
Easy Auth (configured on the Container App) gates /admin and /api/admin/* by
injecting the X-MS-CLIENT-PRINCIPAL header; see app/auth.py.

Stores are instantiated lazily inside handlers (not at import) so the module
imports cleanly without Azure environment variables — needed for local import
checks and unit testing.
"""

from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .auth import get_principal, require_authenticated
from .config_store import ConfigStore
from .email_notify import send_change_request_email
from .feed_store import FeedStore
from .keyvault import load_secrets
from .state_store import ChangeStore

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="Status Monitor")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR), check_dir=False), name="static")


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


# ── Pages ────────────────────────────────────────────────────────────────────

@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/admin", dependencies=[Depends(require_authenticated)])
def admin():
    return FileResponse(STATIC_DIR / "admin.html")


# ── Public API ───────────────────────────────────────────────────────────────

@app.get("/api/config")
def get_public_config():
    return ConfigStore().read()


@app.get("/api/feeds/{team_id}")
def get_feed(team_id: str):
    return FeedStore().read_feed(team_id)


@app.get("/api/feeds/{team_id}/status")
def get_feed_status(team_id: str):
    return FeedStore().read_summary(team_id)


@app.post("/api/changes")
async def submit_change(request: Request):
    payload = await request.json()
    result = ChangeStore().create(payload)
    try:
        send_change_request_email(payload, load_secrets())
    except Exception as exc:  # noqa: BLE001 - email failure shouldn't drop the request
        print(f"  change-request email error: {exc}")
    return JSONResponse(result, status_code=201)


# ── Admin API (Easy Auth gated) ──────────────────────────────────────────────

@app.get("/api/admin/me")
def whoami(principal: dict = Depends(require_authenticated)):
    return {
        "name": principal.get("userDetails") or principal.get("name"),
        "provider": principal.get("identityProvider") or principal.get("auth_typ"),
    }


@app.get("/api/admin/config", dependencies=[Depends(require_authenticated)])
def get_admin_config():
    return ConfigStore().read_with_etag()


@app.put("/api/admin/config", dependencies=[Depends(require_authenticated)])
async def save_admin_config(request: Request):
    body = await request.json()
    etag = request.headers.get("if-match")
    config = body.get("config", body)
    return ConfigStore().write(config, etag=etag)


@app.get("/api/admin/changes", dependencies=[Depends(require_authenticated)])
def list_changes(status: str = "pending"):
    return ChangeStore().list(status=status or None)


@app.post("/api/admin/changes/{change_id}/approve", dependencies=[Depends(require_authenticated)])
def approve_change(change_id: str):
    try:
        return ChangeStore().set_status(change_id, "approved")
    except KeyError:
        raise HTTPException(status_code=404, detail="Change request not found")


@app.post("/api/admin/changes/{change_id}/reject", dependencies=[Depends(require_authenticated)])
def reject_change(change_id: str):
    try:
        return ChangeStore().set_status(change_id, "rejected")
    except KeyError:
        raise HTTPException(status_code=404, detail="Change request not found")
