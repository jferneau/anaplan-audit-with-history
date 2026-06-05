"""Pydantic response models for Anaplan API payloads.

All models use ``extra="allow"`` so new fields from Anaplan don't break
existing runs — but known fields are typed.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class AuditEvent(BaseModel):
    """A single audit event from the Anaplan Audit API."""

    model_config = ConfigDict(extra="allow")

    id: str = ""
    date: str = ""
    userId: str = ""
    userName: str = ""
    eventTypeId: str = ""
    eventType: str = ""
    objectId: str = ""
    objectName: str = ""
    objectType: str = ""
    ipAddress: str = ""
    workspaceId: str = ""
    workspaceName: str = ""
    modelId: str = ""
    modelName: str = ""


class User(BaseModel):
    """An Anaplan user from the SCIM API."""

    model_config = ConfigDict(extra="allow")

    id: str = ""
    userName: str = ""
    displayName: str = ""
    active: bool = True


class Workspace(BaseModel):
    """An Anaplan workspace from the Integration API."""

    model_config = ConfigDict(extra="allow")

    id: str = ""
    name: str = ""
    active: bool = True


class Model(BaseModel):
    """An Anaplan model from the Integration API."""

    model_config = ConfigDict(extra="allow")

    id: str = ""
    name: str = ""
    activeState: str = ""


class Action(BaseModel):
    """An Anaplan action from the Integration API."""

    model_config = ConfigDict(extra="allow")

    id: str = ""
    name: str = ""
    type: str = ""


class Process(BaseModel):
    """An Anaplan process from the Integration API."""

    model_config = ConfigDict(extra="allow")

    id: str = ""
    name: str = ""


class ImportDataSource(BaseModel):
    """An Anaplan import data source from the Integration API."""

    model_config = ConfigDict(extra="allow")

    id: str = ""
    name: str = ""


class CloudWorksIntegration(BaseModel):
    """A CloudWorks integration."""

    model_config = ConfigDict(extra="allow")

    integrationId: str = ""
    name: str = ""
    type: str = ""
    workspaceId: str = ""
    modelId: str = ""


class BulkUploadChunk(BaseModel):
    """Metadata for a bulk upload chunk."""

    model_config = ConfigDict(extra="allow")

    chunk_index: int = 0
    total_chunks: int = 0
    data: str = ""


class Export(BaseModel):
    """An Anaplan export action from the Integration API."""

    model_config = ConfigDict(extra="allow")

    id: str = ""
    name: str = ""


class ExportTask(BaseModel):
    """Status of an Anaplan export task.

    Attributes:
        taskId: Unique task identifier.
        taskState: One of NOT_STARTED, IN_PROGRESS, COMPLETE, FAILED, CANCELLED.
    """

    model_config = ConfigDict(extra="allow")

    taskId: str = ""
    taskState: str = ""
