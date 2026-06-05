"""Shared test fixtures and respx mocks."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import respx

FIXTURES_DIR = Path(__file__).parent / "fixtures"


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
