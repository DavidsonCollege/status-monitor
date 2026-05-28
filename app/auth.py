"""Container Apps Easy Auth header parsing.

When Easy Auth is enabled, Container Apps injects the authenticated principal
as a base64-encoded JSON blob in the X-MS-CLIENT-PRINCIPAL header. We decode it,
require the identity provider to be Entra ID ("aad"), and accept any
authenticated user — the Entra app registration already restricts who can sign
in via "Assignment required = Yes".
"""

import base64
import json

from fastapi import Header, HTTPException

CLIENT_PRINCIPAL_HEADER = "x-ms-client-principal"


def _decode_principal(encoded: str) -> dict | None:
    try:
        decoded = base64.b64decode(encoded).decode("utf-8")
        return json.loads(decoded)
    except Exception:
        return None


def get_principal(x_ms_client_principal: str | None = Header(default=None)) -> dict | None:
    """Return the decoded principal dict, or None if not authenticated."""
    if not x_ms_client_principal:
        return None
    return _decode_principal(x_ms_client_principal)


def require_authenticated(
    x_ms_client_principal: str | None = Header(default=None),
) -> dict:
    """FastAPI dependency: 401 unless a valid Entra ID principal is present."""
    principal = get_principal(x_ms_client_principal)
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
