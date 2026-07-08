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

    Notes:
        The Anaplan Audit API contract (verified against the tenant):

        - ``POST {audit_uri}/events/search?limit=N`` with a JSON body
          ``{"from": <epoch milliseconds>}``.
        - Events are returned under the ``"response"`` key.
        - Pages are followed via ``meta.paging.nextUrl`` (POST to it with
          the same body) until that key is absent.

        ``since_epoch`` is stored in **seconds** (the ``lastRun`` setting),
        but the API's ``from`` filter is **milliseconds**, so it is
        converted here. ``from = 0`` (first run) returns everything within
        Anaplan's ~30-day retention window.
    """
    total = 0
    from_ms = since_epoch * 1000
    body = {"from": from_ms}

    # First page carries the limit; subsequent pages come from nextUrl,
    # which embeds its own paging state.
    url: str | None = f"{audit_uri}/events/search?limit={batch_size}"

    while url:
        resp = client.post(url, json=body)
        payload = resp.json()
        events = payload.get("response") or []

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

        # Stop if this page was empty (guards against a misbehaving API that
        # keeps returning a nextUrl with no data).
        if not events:
            break

        url = payload.get("meta", {}).get("paging", {}).get("nextUrl")

    logger.info("audit_events_fetched", total_count=total, since_epoch=since_epoch)
