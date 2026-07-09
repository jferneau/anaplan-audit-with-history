"""Pydantic response models for Anaplan API payloads.

All models use ``extra="allow"`` so new fields from Anaplan don't break
existing runs — but known fields are typed.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict


def _to_str(value: object) -> str:
    """Coerce any scalar (or None) to a string.

    The Anaplan Audit API is loosely typed: fields we treat as strings can
    come back as integers (e.g. the event ``id`` is a number like
    ``2529918698``) or ``null``. We store everything as text in SQLite and
    resolve/join on it downstream, so normalise to ``str`` at the edge
    rather than let a numeric ``id`` raise a ValidationError mid-fetch.
    """
    return "" if value is None else str(value)


# A string field that tolerates ints / None from the API.
StrCoerce = Annotated[str, BeforeValidator(_to_str)]


class AuditEvent(BaseModel):
    """A single audit event from the Anaplan Audit API.

    The declared fields are exactly the top-level attributes that
    ``audit_query.sql`` references (``e.<field>``), so those columns are
    guaranteed to exist in the events table regardless of what any given
    batch contains.  Everything else — including the nested
    ``additionalAttributes`` dict — flows through ``extra="allow"`` and is
    flattened into dotted column names by ``pd.json_normalize``.

    String fields use :data:`StrCoerce` because the Audit API returns some
    of them (notably ``id``) as integers or ``null``.
    """

    model_config = ConfigDict(extra="allow")

    id: StrCoerce = ""
    eventDate: int = 0
    index: int = 0
    eventTimeZone: StrCoerce = ""
    createdDate: int = 0
    createdTimeZone: StrCoerce = ""
    eventTypeId: StrCoerce = ""
    userId: StrCoerce = ""
    tenantId: StrCoerce = ""
    objectId: StrCoerce = ""
    objectTypeId: StrCoerce = ""
    objectTenantId: StrCoerce = ""
    message: StrCoerce = ""
    success: bool = True
    errorNumber: StrCoerce | None = None
    ipAddress: StrCoerce = ""
    userAgent: StrCoerce = ""
    sessionId: StrCoerce = ""
    hostName: StrCoerce = ""
    serviceVersion: StrCoerce = ""
    checksum: StrCoerce = ""


class User(BaseModel):
    """An Anaplan user from the SCIM API."""

    model_config = ConfigDict(extra="allow")

    id: str = ""
    userName: str = ""
    displayName: str = ""
    active: bool = True


class Workspace(BaseModel):
    """An Anaplan workspace from the Integration API.

    ``sizeAllowance`` and ``currentSize`` are populated only when the
    caller passes ``?tenantDetails=true`` (see :func:`list_workspaces`);
    otherwise they default to ``0``.
    """

    model_config = ConfigDict(extra="allow")

    id: str = ""
    name: str = ""
    active: bool = True
    sizeAllowance: int = 0
    currentSize: int = 0


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


class ImportAction(BaseModel):
    """An Anaplan import action from the Integration API."""

    model_config = ConfigDict(extra="allow")

    id: str = ""
    name: str = ""


class AnaplanList(BaseModel):
    """An Anaplan list from the Transactional API."""

    model_config = ConfigDict(extra="allow")

    id: str = ""
    name: str = ""


class Module(BaseModel):
    """An Anaplan module from the Transactional API."""

    model_config = ConfigDict(extra="allow")

    id: str = ""
    name: str = ""


class LineItem(BaseModel):
    """A line item inside an Anaplan module."""

    model_config = ConfigDict(extra="allow")

    id: str = ""
    name: str = ""
    moduleId: str = ""


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
