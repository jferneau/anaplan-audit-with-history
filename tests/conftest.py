"""Shared test fixtures and respx mocks."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import respx

from anaplan_audit.api.client import APIClient
from anaplan_audit.auth.models import AuthToken

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def make_token() -> AuthToken:
    """A pre-authenticated test token, valid for one hour."""
    return AuthToken(
        access_token="test-token",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


def make_client(token: AuthToken | None = None) -> APIClient:
    """An APIClient wired with a fresh test token.

    Importable helper (not a fixture) so tests can construct clients
    inside ``with respx.mock`` blocks at the exact point they need them.
    """
    return APIClient(token or make_token())


@pytest.fixture()
def audit_response_data() -> dict[str, Any]:
    """Load the audit API fixture data."""
    return json.loads((FIXTURES_DIR / "audit_response.json").read_text())  # type: ignore[no-any-return]


@pytest.fixture()
def scim_response_data() -> dict[str, Any]:
    """Load the SCIM API fixture data."""
    return json.loads((FIXTURES_DIR / "scim_response.json").read_text())  # type: ignore[no-any-return]


@pytest.fixture()
def cloudworks_response_data() -> dict[str, Any]:
    """Load the CloudWorks API fixture data."""
    return json.loads((FIXTURES_DIR / "cloudworks_response.json").read_text())  # type: ignore[no-any-return]


@pytest.fixture()
def mock_api() -> respx.MockRouter:
    """Provide a started respx mock router."""
    with respx.mock(assert_all_called=False) as router:
        yield router
