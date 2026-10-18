"""Structured log events for admission decisions and rebuilds (constitution, Development Workflow)."""

from __future__ import annotations

import json
import logging

LOGGER = logging.getLogger("aduku")


def emit(event: str, **fields: object) -> None:
    """Write one JSON log line, e.g. emit("admission", doc="d1", decision="rejected", codes=[...])."""
    if not LOGGER.isEnabledFor(logging.INFO):
        return
    LOGGER.info(json.dumps({"event": event, **fields}, sort_keys=True, default=str))
