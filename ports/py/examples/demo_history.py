"""Saved layers, time travel, compaction and purge in one run (feature 005). `uv run python examples/demo_history.py`."""

from __future__ import annotations

from datetime import UTC, datetime

from aduku import Aduku, AsOf, EntryInput, Identity, PurgeScope, SchemaRegistry, load_schema
from aduku.storage.memory import MemoryStorage

SCHEMA = """schema::{id: "customer", version: 1, tiers: [backfill, user, admin], roles: [dpo], purge_by: [dpo],
  fields: {"/name": {type: string, merge: lww}, "/age": {type: int, merge: max}, "/email": {type: string, merge: lww}}}"""
USER = Identity("alice", ("user",))
DPO = Identity("officer", ("admin",), ("dpo",))


def main() -> dict[str, object]:
    registry = SchemaRegistry()
    registry.put(load_schema(SCHEMA))
    registry.activate(1)
    db = Aduku(MemoryStorage(), registry)
    for number, name in enumerate(["Ann", "Anna", "Annie"], start=1):
        db.append(
            "acme",
            [
                EntryInput("user", "/name", "set", name, valid_time=datetime(2024, number, 1, tzinfo=UTC)),
                EntryInput("user", "/age", "set", 30 + number),
            ],
            contributor=USER,
        )
    db.append("acme", [EntryInput("user", "/email", "set", "ann@example.com")], contributor=USER)

    found: dict[str, object] = {}
    found["now"] = db.get("acme").document["name"]
    found["as of seq 2"] = db.get("acme", as_of=AsOf(seq=2)).document["name"]
    found["as of 15 Feb 2024"] = db.get("acme", as_of=AsOf(valid_time=datetime(2024, 2, 15, tzinfo=UTC))).document[
        "name"
    ]

    report = db.compact("acme", through_seq=db.last_seq("acme"))
    found["entries after compaction"] = len(db.list_entries("acme"))
    found["checked paths"] = report.checked_paths
    found["as of seq 2 (archive kept)"] = db.get("acme", as_of=AsOf(seq=2)).document["name"]

    db.purge("acme", PurgeScope(path="/email"), reason="ticket 4711", caller=DPO)
    found["email after purge"] = db.get("acme").document.get("email")
    found["purge log"] = [(r.reason, r.actor) for r in db.purge_log("acme")]
    found["rebuild identical"] = db.rebuild().identical
    return found


if __name__ == "__main__":
    for key, value in main().items():
        print(f"{key}: {value}")
