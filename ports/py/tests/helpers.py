"""Shared test builders."""

from datetime import UTC, datetime

from aduku.ion_codec import Value
from aduku.model import Entry

T0 = datetime(2026, 10, 18, 12, 0, 0, tzinfo=UTC)


def make_entry(
    doc: str = "d1",
    seq: int = 1,
    *,
    request_id: str = "r1",
    tier: str = "user",
    path: str = "/name",
    op: str = "set",
    value: Value = "Alice",
    contributor: str = "alice",
    idem_key: str | None = None,
    request_key: str | None = None,
    request_hash: str | None = None,
    lenient: bool = False,
    schema_version: int = 1,
    meta: Value = None,
    tx_time: datetime = T0,
) -> Entry:
    return Entry(
        id=f"e-{doc}-{seq}",
        doc=doc,
        seq=seq,
        request_id=request_id,
        tier=tier,
        path=path,
        op=op,  # type: ignore[arg-type]
        value=value,
        contributor=contributor,
        tx_time=tx_time,
        schema_version=schema_version,
        idem_key=idem_key,
        request_key=request_key,
        request_hash=request_hash,
        lenient=lenient,
        meta=meta,
    )
