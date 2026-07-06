"""Anaplan Audit API client."""

from __future__ import annotations

from collections.abc import Iterator

import structlog

from anaplan_audit.api.client import APIClient
from anaplan_audit.api.models import AuditEvent

logger: structlog.stdlib.BoundLogger = structlog.get_logger()


def fetch_audit_events(
    client: APIClient,
    audit_uri: str,
    *,
    since_epoch: int,
    batch_size: int,
    max_events: int | None = None,
) -> Iterator[AuditEvent]:
    """Fetch audit events from the Anaplan Audit API.

    Handles pagination automatically and yields individual events.

    Args:
        client: An authenticated :class:`APIClient`.
        audit_uri: Base URI for the Audit API.
        since_epoch: Fetch events since this Unix epoch (seconds).
        batch_size: Number of events to request per page.
        max_events: Stop after yielding this many events.  ``None`` (the
            default) fetches everything.  Used by ``run --limit`` so
            first-time customers can pull a bounded sample.

    Yields:
        Individual :class:`AuditEvent` instances.
    """
    total = 0
    offset = 0

    while True:
        resp = client.get(
            f"{audit_uri}/events",
            params={
                "since": since_epoch,
                "limit": batch_size,
                "offset": offset,
            },
        )
        payload = resp.json()
        events = payload.get("events", payload.get("audits", []))

        if not events:
            break

        for raw in events:
            yield AuditEvent.model_validate(raw)
            total += 1
            if max_events is not None and total >= max_events:
                logger.info(
                    "audit_events_fetch_capped",
                    total_count=total,
                    max_events=max_events,
                )
                return

        if len(events) < batch_size:
            break

        offset += len(events)

    logger.info("audit_events_fetched", total_count=total, since_epoch=since_epoch)
