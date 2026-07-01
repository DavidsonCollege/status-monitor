"""Container Apps Easy Auth header parsing.

When Easy Auth is enabled, Container Apps injects the authenticated principal
as a base64-encoded JSON blob in the X-MS-CLIENT-PRINCIPAL header. We decode it,
require the identity provider to be Entra ID ("aad"), and accept any
authenticated user — the Entra app registration already restricts who can sign
in via "Assignment required = Yes".

Server-to-server bypass
-----------------------
Backend services (e.g. dc-adminapi) that cannot receive the Easy Auth header
may instead present the X-Service-Api-Key header with the value of the
SERVICE_API_KEY environment variable.  An empty or unset SERVICE_API_KEY
disables this bypass entirely.
"""

from __future__ import annotations

import base64
import json
import os
import secrets

from fastapi import Header, HTTPException

CLIENT_PRINCIPAL_HEADER = "x-ms-client-principal"

# Pre-shared key for server-to-server calls.  Empty string = disabled.
_SERVICE_API_KEY: str = os.environ.get("SERVICE_API_KEY", "").strip()


def _decode_principal(encoded: str) -> dict | None:
    try:
        decoded = base64.b64decode(encoded).decode("utf-8")
        return json.loads(decoded)
    except Exception:
        return None


def _service_principal() -> dict:
    """Synthetic principal for service-account callers."""
    return {
        "identityProvider": "aad",
        "auth_typ": "aad",
        "userDetails": "service-account@davidson.edu",
        "name": "Service Account (dc-adminapi)",
    }


def get_principal(
    x_ms_client_principal: str | None = Header(default=None),
    x_service_api_key: str | None = Header(default=None),
) -> dict | None:
    """Return the decoded principal dict, or None if not authenticated."""
    # Service-key bypass (server-to-server only)
    if (
        x_service_api_key
        and _SERVICE_API_KEY
        and secrets.compare_digest(x_service_api_key, _SERVICE_API_KEY)
    ):
        return _service_principal()
    if not x_ms_client_principal:
        return None
    return _decode_principal(x_ms_client_principal)


def require_authenticated(
    x_ms_client_principal: str | None = Header(default=None),
    x_service_api_key: str | None = Header(default=None),
) -> dict:
    """FastAPI dependency: 401 unless a valid Entra ID principal is present."""
    principal = get_principal(x_ms_client_principal, x_service_api_key)
    if not principal:
        raise HTTPException(status_code=401, detail="Authentication required")

    provider = (
        principal.get("identityProvider")
        or principal.get("auth_typ")
        or ""
    )
    if provider not in ("aad", "AAD", "azureactivedirectory"):
        raise HTTPException(status_code=401, detail="Entra ID authentication required")

    return principal
