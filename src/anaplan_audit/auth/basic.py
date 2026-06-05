"""Basic (username/password) authentication flow."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta

import httpx
import structlog

from anaplan_audit.auth.models import AuthToken
from anaplan_audit.config import AnaplanUris
from anaplan_audit.exceptions import BasicAuthError

logger: structlog.stdlib.BoundLogger = structlog.get_logger()


def authenticate_basic(
    username: str,
    password: str,
    uris: AnaplanUris,
) -> AuthToken:
    """Authenticate via Anaplan basic auth (username + password).

    Args:
        username: Anaplan account email.
        password: Anaplan account password.
        uris: API base URIs.

    Returns:
        A valid :class:`AuthToken`.

    Raises:
        BasicAuthError: If authentication fails.
    """
    encoded = base64.b64encode(f"{username}:{password}".encode()).decode()
    headers = {"Authorization": f"Basic {encoded}"}

    try:
        with httpx.Client(http2=True, timeout=60.0) as client:
            resp = client.post(uris.authServiceUri, headers=headers)
            resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise BasicAuthError(
            f"Basic auth failed: HTTP {exc.response.status_code}",
            context={"status_code": exc.response.status_code},
        ) from exc
    except httpx.HTTPError as exc:
        raise BasicAuthError(
            f"Basic auth request error: {exc}",
            context={"error": str(exc)},
        ) from exc

    data = resp.json()
    token_info = data.get("tokenInfo", data)
    logger.info("basic_auth_success", user=username)

    return AuthToken(
        access_token=token_info["tokenValue"],
        expires_at=datetime.now(tz=UTC) + timedelta(minutes=35),
    )
