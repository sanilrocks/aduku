"""The request hash used to recognize retries (SPEC 4.2 item 4). Pure."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

from aduku.ion_codec import FrozenStruct, or_placeholder, to_canonical, to_utc
from aduku.model import EntryInput

__all__ = ["request_hash"]


def request_hash(doc: str, contributor_id: str, lenient: bool, entries: Sequence[EntryInput]) -> str:
    """SHA-256 over the canonical form of (doc, contributor, flag, entries); `expected_seq` is not part of it."""
    placeholders = [
        value
        for entry in entries
        for value in (entry.value if entry.op != "delete" else None, entry.meta)
        if or_placeholder(value) is not value
    ]
    body = FrozenStruct(
        {
            # a request holding a value with no Ion form is marked, so that it can never equal a stored request
            **({"unstorable": len(placeholders)} if placeholders else {}),
            "doc": doc,
            "contributor": contributor_id,
            "lenient": lenient,
            "entries": tuple(
                FrozenStruct(
                    {
                        "tier": entry.tier,
                        "path": entry.path,
                        "op": entry.op,
                        "value": or_placeholder(entry.value) if entry.op != "delete" else None,
                        "idem_key": entry.idem_key,
                        "meta": or_placeholder(entry.meta),
                        # only when given, so that the hash of a request without it is what it always was (feature 005)
                        **({"valid_time": _instant(entry.valid_time)} if entry.valid_time is not None else {}),
                    }
                )
                for entry in entries
            ),
        }
    )
    return hashlib.sha256(to_canonical(body).encode("utf-8")).hexdigest()


def _instant(moment: object) -> str:
    """The instant in UTC as fixed-width text, so that the same moment in any offset hashes alike."""
    try:
        return to_utc(moment).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    except (AttributeError, TypeError, ValueError, OverflowError):
        return "invalid"  # a request with a bad valid_time is rejected before its hash matters
