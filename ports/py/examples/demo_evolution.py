"""Schema versions in one run (feature 006): diff, dry run, a breaking activation, rollback, pin and migrate.
`uv run python examples/demo_evolution.py`."""

from __future__ import annotations

from aduku import Aduku, EntryInput, Identity, RejectedError, SchemaRegistry, load_schema, schema_changes
from aduku.storage.memory import MemoryStorage

FIELDS = '"/name": {type: string, merge: lww}, "/age": {type: int, merge: %s}'
SCHEMAS = [
    f'schema::{{id: "customer", version: 1, tiers: [backfill, user, admin], fields: {{{FIELDS % "max"}}}}}',
    f'schema::{{id: "customer", version: 2, tiers: [backfill, user, admin], fields: {{{FIELDS % "min"}}}}}',
]
USER = Identity("alice", ("user",))


def main() -> dict[str, object]:
    registry = SchemaRegistry()
    for text in SCHEMAS:
        registry.put(load_schema(text))
    registry.activate(1)
    db = Aduku(MemoryStorage(), registry)
    for doc in ("acme", "globex"):
        db.append(doc, [EntryInput("user", "/name", "set", doc)], contributor=USER)
    db.append("acme", [EntryInput("user", "/age", "set", 30), EntryInput("user", "/age", "set", 50)], contributor=USER)

    found: dict[str, object] = {}
    found["age now"] = db.get("acme").document["age"]

    changes = schema_changes(registry.get(1), registry.get(2))  # two schemas in, a list out; no store
    found["overall class"] = changes.overall
    found["changes"] = [(c.kind, c.path, c.change_class) for c in changes.changes]

    plan = db.plan_schema(2)  # what activating version 2 would do; nothing is written
    found["dry run"] = (plan.examined, plan.changed, plan.unchanged, [c.doc for c in plan.documents])

    try:
        db.activate_schema(2, operator="alice")
    except RejectedError as refusal:
        found["refused"] = refusal.code.value
    db.pin_document("acme", 1, operator="alice")  # hold one document on its version
    db.activate_schema(2, operator="alice", acknowledge_breaking=True)
    found["age under 2 (acme is pinned)"] = db.get("acme").document["age"]
    found["age of the other document"] = db.get("globex").document.get("age")

    report = db.migrate(["acme"], operator="alice")  # move it when ready
    found["migrated"] = report.moved
    found["age after the migration"] = db.get("acme").document["age"]

    db.activate_schema(1, operator="alice")  # a rollback is activating the older version again
    found["age after the rollback"] = db.get("acme").document["age"]
    found["history"] = [(r.action, r.operator, r.doc) for r in db.schema_history()]
    return found


if __name__ == "__main__":
    for key, value in main().items():
        print(f"{key}: {value}")
