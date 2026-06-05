"""Tests for Pydantic API response models."""

from __future__ import annotations

from anaplan_audit.api.models import (
    Action,
    AuditEvent,
    BulkUploadChunk,
    CloudWorksIntegration,
    ImportDataSource,
    Model,
    Process,
    User,
    Workspace,
)


class TestAuditEvent:
    """Tests for AuditEvent model."""

    def test_parse_minimal(self) -> None:
        """AuditEvent parses with all defaults."""
        event = AuditEvent()
        assert event.id == ""
        assert event.userId == ""

    def test_parse_full(self) -> None:
        """AuditEvent parses a complete API payload."""
        event = AuditEvent.model_validate(
            {
                "id": "evt-001",
                "eventDate": 1705312200000,
                "userId": "user-001",
                "userName": "john@test.com",
                "eventTypeId": "CONN-1",
                "objectId": "obj-001",
                "ipAddress": "10.0.0.1",
            }
        )
        assert event.id == "evt-001"
        assert event.userId == "user-001"

    def test_extra_fields_allowed(self) -> None:
        """Unknown fields from Anaplan API are preserved."""
        event = AuditEvent.model_validate(
            {
                "id": "evt-001",
                "newFieldFromFutureApi": "value",
            }
        )
        assert event.model_extra is not None
        assert event.model_extra.get("newFieldFromFutureApi") == "value"

    def test_nested_additional_attributes_preserved(self) -> None:
        """Nested additionalAttributes dict is preserved via extra='allow'."""
        event = AuditEvent.model_validate(
            {
                "id": "evt-001",
                "additionalAttributes": {
                    "workspaceId": "ws-001",
                    "modelId": "model-001",
                },
            }
        )
        assert event.model_extra is not None
        attrs = event.model_extra["additionalAttributes"]
        assert attrs["workspaceId"] == "ws-001"


class TestUserModel:
    """Tests for User model."""

    def test_defaults(self) -> None:
        """User model has sensible defaults."""
        user = User()
        assert user.active is True
        assert user.id == ""

    def test_parse(self) -> None:
        """User model parses SCIM response payload."""
        user = User.model_validate(
            {
                "id": "user-001",
                "userName": "alice@test.com",
                "displayName": "Alice",
                "active": True,
            }
        )
        assert user.userName == "alice@test.com"


class TestWorkspaceModel:
    """Tests for Workspace model."""

    def test_parse(self) -> None:
        """Workspace model parses Integration API payload."""
        ws = Workspace.model_validate({"id": "ws-001", "name": "Finance"})
        assert ws.name == "Finance"
        assert ws.active is True


class TestModelModel:
    """Tests for Model model."""

    def test_parse(self) -> None:
        """Model model parses Integration API payload."""
        m = Model.model_validate(
            {
                "id": "model-001",
                "name": "Revenue",
                "activeState": "ACTIVE",
            }
        )
        assert m.activeState == "ACTIVE"


class TestActionModel:
    """Tests for Action model."""

    def test_parse(self) -> None:
        """Action model parses Integration API payload."""
        a = Action.model_validate({"id": "act-001", "name": "Import", "type": "IMPORT"})
        assert a.type == "IMPORT"


class TestProcessModel:
    """Tests for Process model."""

    def test_parse(self) -> None:
        """Process model parses Integration API payload."""
        p = Process.model_validate({"id": "proc-001", "name": "Nightly Run"})
        assert p.name == "Nightly Run"


class TestImportDataSource:
    """Tests for ImportDataSource model."""

    def test_parse(self) -> None:
        """ImportDataSource model parses Integration API payload."""
        ds = ImportDataSource.model_validate({"id": "file-001", "name": "audit.csv"})
        assert ds.id == "file-001"


class TestCloudWorksIntegration:
    """Tests for CloudWorksIntegration model."""

    def test_parse(self) -> None:
        """CloudWorksIntegration parses CloudWorks API payload."""
        cw = CloudWorksIntegration.model_validate(
            {
                "integrationId": "cw-001",
                "name": "Daily Sync",
                "type": "S3",
                "workspaceId": "ws-001",
                "modelId": "model-001",
            }
        )
        assert cw.integrationId == "cw-001"
        assert cw.type == "S3"


class TestBulkUploadChunk:
    """Tests for BulkUploadChunk model."""

    def test_defaults(self) -> None:
        """BulkUploadChunk has zero defaults."""
        chunk = BulkUploadChunk()
        assert chunk.chunk_index == 0
        assert chunk.total_chunks == 0
