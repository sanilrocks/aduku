"""Thin command line over the library (contracts/cli.md). Exit codes: 0 ok, 1 rejected request (or a rebuild that found a problem),
2 usage or schema error."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from aduku.audit import AuditRecord
from aduku.behaviors.registry import BehaviorRegistry
from aduku.errors import AdukuError, HistoryCompactedError, RejectedError, SchemaError
from aduku.ion_codec import ABSENT, FrozenStruct, IonError, parse, to_canonical
from aduku.model import (
    AsOf,
    AsOfInfo,
    ChangeList,
    DocumentChange,
    Entry,
    EntryInput,
    Identity,
    ModelError,
    PurgeScope,
    Shadowed,
)
from aduku.schema.diff import schema_changes
from aduku.schema.loader import load_schema
from aduku.schema.model import Schema
from aduku.schema.registry import SchemaRegistry
from aduku.service import Aduku
from aduku.storage.sqlite import SqliteStorage

__all__ = ["main"]


class _UsageError(Exception):
    pass


class _OpAction(argparse.Action):
    """Collect --set and --delete into one list, keeping the order they were typed in."""

    def __call__(self, parser, namespace, values, option_string=None):  # type: ignore[no-untyped-def]
        items = list(getattr(namespace, self.dest, None) or [])
        items.append(("set" if option_string == "--set" else "delete", values))
        setattr(namespace, self.dest, items)


def _caller_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--contributor", required=True)
    parser.add_argument("--tier", required=True)
    parser.add_argument("--role", action="append", default=[], help="a role the caller holds (repeatable)")


def _operator_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--operator", required=True, help="who is doing this; recorded in the schema history")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aduku", description="aduku-py command line")
    parser.add_argument("--db", default=os.environ.get("ADUKU_DB", "aduku.db"))
    parser.add_argument(
        "--schema",
        action="append",
        default=None,
        metavar="PATH",
        help="a schema file or a directory of *.ion files, one version each; repeatable (default: $ADUKU_SCHEMA)",
    )
    parser.add_argument("--format", choices=("json", "ion"), default="json")
    parser.add_argument(
        "--behaviors",
        default=os.environ.get("ADUKU_BEHAVIORS"),
        metavar="MODULE",
        help="a module with a register(behaviors) function that registers custom macros and hooks",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    append = sub.add_parser("append", help="submit one atomic request")
    append.add_argument("doc")
    append.add_argument("--contributor", required=True)
    append.add_argument("--tier", required=True)
    append.add_argument("--role", action="append", default=[], help="a role the contributor holds (repeatable)")
    append.add_argument("--lenient", action="store_true")
    append.add_argument("--request-key")
    append.add_argument("--idem-key")
    append.add_argument("--valid-time", metavar="ISO8601", help="when the facts became true (a moment with an offset)")
    append.add_argument("--set", dest="ops", action=_OpAction, metavar="PATH=VALUE")
    append.add_argument("--delete", dest="ops", action=_OpAction, metavar="PATH")

    get = sub.add_parser("get", help="read the merged document")
    get.add_argument("doc")
    get.add_argument("--fresh", action="store_true", help="ignore the ttl of per_fetch deferred fields")
    get.add_argument(
        "--include", action="append", default=[], choices=("pending",), help="say what pending fields wait for"
    )
    get.add_argument("--as-of", action="append", default=[], metavar="KIND=VALUE", help="seq=N, tx=ISO or valid=ISO")

    rebuild = sub.add_parser("rebuild", help="re-derive from the ledger and compare with the cached views")
    rebuild.add_argument("doc", nargs="?")

    recompute = sub.add_parser("recompute", help="call impure hooks again with the current inputs")
    recompute.add_argument("doc")
    recompute.add_argument("field", nargs="?")
    recompute.add_argument("--contributor", required=True)
    recompute.add_argument("--tier", required=True)
    recompute.add_argument("--role", action="append", default=[], help="a role the caller holds (repeatable)")

    invalidate = sub.add_parser("invalidate", help="mark a deferred value out of date (DOC * for every document)")
    invalidate.add_argument("doc")
    invalidate.add_argument("field")
    _caller_options(invalidate)

    scheduler = sub.add_parser("scheduler", help="the deferred scheduler")
    scheduler_sub = scheduler.add_subparsers(dest="scheduler_command", required=True)
    scheduler_run = scheduler_sub.add_parser("run", help="refresh what is due for a source (name@version)")
    scheduler_run.add_argument("hook")
    _caller_options(scheduler_run)

    notify = sub.add_parser("notify", help="mark the values linked to a topic out of date")
    notify.add_argument("topic")
    notify.add_argument("docs", nargs="*")

    deferred = sub.add_parser("deferred", help="the stored state of deferred fields")
    deferred_sub = deferred.add_subparsers(dest="deferred_command", required=True)
    deferred_sub.add_parser("list").add_argument("doc")

    effects = sub.add_parser("effects", help="the outcomes of impure computes")
    effects_sub = effects.add_subparsers(dest="effects_command", required=True)
    effects_list = effects_sub.add_parser("list")
    effects_list.add_argument("doc")
    effects_list.add_argument("field", nargs="?")
    effects_sub.add_parser("prune").add_argument("doc", nargs="?")

    explain = sub.add_parser("explain", help="why a field has its value")
    explain.add_argument("doc")
    explain.add_argument("path")
    explain.add_argument(
        "--as-of", action="append", default=[], metavar="KIND=VALUE", help="seq=N, tx=ISO or valid=ISO"
    )

    compact = sub.add_parser(
        "compact", help="replace old entries by one synthetic entry per tier (the originals are archived)"
    )
    compact.add_argument("doc")
    compact.add_argument("--through-seq", type=int)
    compact.add_argument("--older-than", metavar="DURATION", help="for example 90d, 12h, 30m")

    archive = sub.add_parser("archive", help="the archive of compacted originals")
    archive_sub = archive.add_subparsers(dest="archive_command", required=True)
    archive_sub.add_parser("verify", help="derive from the originals and compare").add_argument("doc")
    expire = archive_sub.add_parser("expire", help="delete archived originals up to a seq (recorded; cannot be undone)")
    expire.add_argument("doc")
    expire.add_argument("--through-seq", type=int, required=True)

    purge = sub.add_parser("purge", help="erase contributions for good (legal erasure); the schema says who may")
    purge.add_argument("doc")
    purge.add_argument("--path", help="every contribution at or below this path")
    purge.add_argument("--scope-tier", help="with --path: only the contributions of this tier")
    purge.add_argument("--entry", action="append", default=[], metavar="ID", help="an entry to erase (repeatable)")
    purge.add_argument("--reason", required=True, help="the reason or ticket, recorded in the purge log")
    _caller_options(purge)
    sub.add_parser("purge-log", help="what was purged, by whom and why (never a value)").add_argument("doc")

    entries = sub.add_parser("entries", help="list ledger entries")
    entries.add_argument("doc")
    entries.add_argument("--since", type=int, default=0)

    schema = sub.add_parser("schema", help="schema tools")
    schema_sub = schema.add_subparsers(dest="schema_command", required=True)
    schema_sub.add_parser("check", help="load and validate a schema file").add_argument("file")
    diff = schema_sub.add_parser("diff", help="the changes between two schema files and how risky they are (no store)")
    diff.add_argument("old")
    diff.add_argument("new")
    plan = schema_sub.add_parser("plan", help="the dry run: what activating a version would change (writes nothing)")
    plan.add_argument("--target", type=int, required=True, metavar="V")
    plan.add_argument("--limit", type=int, help="examine only the first N documents (the same N each time)")
    plan.add_argument("--doc", action="append", default=[], help="examine only this document (repeatable)")
    activate = schema_sub.add_parser("activate", help="make a version the active one for every process on the store")
    activate.add_argument("version", type=int)
    _operator_option(activate)
    activate.add_argument("--acknowledge-breaking", action="store_true", help="accept a breaking change")
    activate.add_argument("--rebuild", action="store_true", help="refresh saved layers and views of unpinned documents")
    history = schema_sub.add_parser(
        "history", help="who changed the active version or a pin, when, and how risky it was"
    )
    history.add_argument("--limit", type=int)
    history.add_argument("--doc")

    pin = sub.add_parser("pin", help="serve one document from a version whatever the active version is")
    pin.add_argument("doc")
    pin.add_argument("version", type=int)
    _operator_option(pin)
    unpin = sub.add_parser("unpin", help="serve a pinned document from the active version again")
    unpin.add_argument("doc")
    _operator_option(unpin)
    migrate = sub.add_parser("migrate", help="move pinned documents to a version (the active one by default)")
    migrate.add_argument("docs", nargs="*", help="documents to move (default: every pinned document)")
    migrate.add_argument("--to", type=int, metavar="V")
    _operator_option(migrate)
    migrate.add_argument("--dry-run", action="store_true", help="show what would change and move nothing")
    migrate.add_argument("--batch", type=int, default=100, help="documents per batch")

    audit = sub.add_parser("audit", help="rejection audit log")
    audit_sub = audit.add_subparsers(dest="audit_command", required=True)
    audit_sub.add_parser("list").add_argument("doc")
    prune = audit_sub.add_parser("prune")
    prune.add_argument("doc", nargs="?")
    setting = audit_sub.add_parser("set")
    setting.add_argument("doc")
    setting.add_argument("setting", choices=("enabled", "disabled", "inherit"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as stop:
        return int(stop.code or 0) if isinstance(stop.code, int) else 2
    try:
        if args.command == "schema" and args.schema_command == "check":
            return _check_schema(args)
        if args.command == "schema" and args.schema_command == "diff":
            return _schema_diff(args)
        service = _service(args)
        return _run(args, service)
    except ValueError as error:  # an empty operator name, a negative limit
        print(f"aduku: {error}", file=sys.stderr)
        return 2
    except _UsageError as error:
        print(f"aduku: {error}", file=sys.stderr)
        return 2
    except SchemaError as error:
        print("aduku: schema error: " + "; ".join(error.problems), file=sys.stderr)
        return 2
    except AdukuError as error:
        print(f"aduku: {error}", file=sys.stderr)
        return 2


def _behaviors(args: argparse.Namespace) -> BehaviorRegistry:
    """The built-in behaviors, plus those registered by the module named with --behaviors."""
    registry = BehaviorRegistry.with_builtins()
    if args.behaviors:
        try:
            module = importlib.import_module(args.behaviors)
        except ImportError as error:
            raise _UsageError(f"cannot import the behaviors module {args.behaviors!r}: {error}") from error
        register = getattr(module, "register", None)
        if not callable(register):
            raise _UsageError(f"the behaviors module {args.behaviors!r} has no register(behaviors) function")
        register(registry)
    return registry


def _inventory(schema: Schema) -> dict[str, Any]:
    """What `schema check` says about computed fields and, above all, about impure computes (SPEC 9.3.1 item 7)."""
    computed = [
        {
            "field": spec.pattern,
            "macro": f"{spec.computed.macro}@{spec.computed.version}",
            "args": [arg.path if arg.path is not None else to_canonical(arg.literal) for arg in spec.computed.args],
            "on_missing": spec.on_missing,
        }
        for spec in schema.fields
        if spec.computed is not None
    ]
    impure = [
        {
            "field": spec.pattern,
            "hook": f"{spec.impure.hook}@{spec.impure.version}",
            "depends": list(spec.impure.depends),
            "reason": spec.impure.reason,
            "timeout": spec.impure.timeout,
            "retry": {"max": spec.impure.retry_max, "backoff": spec.impure.backoff},
            "deadline": spec.impure.deadline,
            "recompute_by": list(spec.impure.recompute_by),
            "failure_mode": spec.impure.failure_mode or spec.failure_mode or schema.impure_failure_mode,
        }
        for spec in schema.fields
        if spec.impure is not None
    ]
    deferred = [
        {
            "field": spec.pattern,
            "hook": spec.deferred.source,
            "depends": list(spec.deferred.depends),
            "refresh": [p.kind for p in spec.deferred.refresh],
            "ttl": spec.deferred.ttl,
            "on_failure": schema.on_failure_of(spec),
            "invalidate_by": list(spec.deferred.invalidate_by),
        }
        for spec in schema.fields
        if spec.deferred is not None
    ]
    field_rules = []
    for spec in schema.fields:
        declared = schema.authority_of(spec)
        field_rules.append(
            {
                "field": spec.pattern,
                "authority": list(declared[0]) if declared else None,
                "authority_from": declared[1] if declared else None,
                "rules": [_rule(ref) for ref in spec.validate],
            }
        )
    return {
        "computed": computed,
        "impure": impure,
        "deferred": deferred,
        "field_rules": field_rules,
        "document_rules": [_rule(ref) for ref in schema.document_validate],
    }


def _rule(ref: Any) -> dict[str, Any]:
    """A use of a validation rule, as the schema check lists it."""
    return {"rule": f"{ref.name}@{ref.version}", "args": to_canonical(ref.args)}


def _check_schema(args: argparse.Namespace) -> int:
    """Exit 0 with a summary if the schema loads; exit 2 with every problem (and its stable code) if not."""
    try:
        with open(args.file, encoding="utf-8") as handle:
            text = handle.read()
    except OSError as error:
        raise _UsageError(f"cannot read schema: {error}") from error
    try:
        schema = load_schema(text, _behaviors(args))
    except SchemaError as error:
        _emit(args, {"ok": False, "problems": list(error.problems), "codes": list(error.codes)})
        for problem in error.problems:
            print(problem, file=sys.stderr)
        return 2
    rules: dict[str, int] = {}
    for spec in schema.fields:
        if spec.merge is not None:
            rules[spec.merge.name] = rules.get(spec.merge.name, 0) + 1
    _emit(
        args,
        {
            "ok": True,
            "id": schema.id,
            "version": schema.version,
            "tiers": list(schema.tiers),
            "fields": len(schema.fields),
            "merge_rules": rules,
            "purge_by": list(schema.purge_by),
            "derived": [spec.pattern for spec in schema.fields if spec.derived],
            **_inventory(schema),
        },
    )
    return 0


def _schema_files(args: argparse.Namespace) -> list[str]:
    """The schema files named by --schema (a directory stands for its *.ion files), in name order."""
    given = args.schema or ([os.environ["ADUKU_SCHEMA"]] if os.environ.get("ADUKU_SCHEMA") else [])
    files: list[str] = []
    for entry in given:
        if os.path.isdir(entry):
            files.extend(sorted(os.path.join(entry, name) for name in os.listdir(entry) if name.endswith(".ion")))
        else:
            files.append(entry)
    if not files:
        raise _UsageError("no schema: pass --schema PATH (a file or a directory of *.ion files) or set ADUKU_SCHEMA")
    return files


def _service(args: argparse.Namespace) -> Aduku:
    files = _schema_files(args)
    behaviors = _behaviors(args)
    registry = SchemaRegistry()
    versions: list[int] = []
    for name in files:
        try:
            with open(name, encoding="utf-8") as handle:
                schema = load_schema(handle.read(), behaviors)
        except OSError as error:
            raise _UsageError(f"cannot read schema: {error}") from error
        registry.put(schema)
        versions.append(schema.version)
    # the highest version is the one a store with no state adopts; a store that has state serves what it was told to
    registry.activate(max(versions))
    return Aduku(SqliteStorage(args.db), registry, behaviors=behaviors)


def _change_rows(changes: ChangeList) -> list[dict[str, Any]]:
    return [
        {
            "kind": c.kind,
            "path": c.path,
            "description": c.description,
            "class": c.change_class,
            "classified": c.classified,
        }
        for c in changes.changes
    ]


def _schema_diff(args: argparse.Namespace) -> int:
    """The changes between two schema files; needs no store (exit 0 whatever the class)."""
    behaviors = _behaviors(args)
    loaded = []
    for name in (args.old, args.new):
        try:
            with open(name, encoding="utf-8") as handle:
                loaded.append(load_schema(handle.read(), behaviors))
        except OSError as error:
            raise _UsageError(f"cannot read schema: {error}") from error
    changes = schema_changes(loaded[0], loaded[1])
    _emit(
        args,
        {
            "old": changes.old_version,
            "new": changes.new_version,
            "overall": changes.overall,
            "changes": _change_rows(changes),
        },
    )
    return 0


def _refused_text(args: argparse.Namespace, code: str, message: str) -> int:
    _emit(args, {"rejected": {"code": code, "reasons": [{"code": code, "message": message}]}})
    print(f"{code}: {message}", file=sys.stderr)
    return 1


def _schema_command(args: argparse.Namespace, service: Aduku) -> int:
    try:
        if args.schema_command == "plan":
            report = service.plan_schema(args.target, docs=args.doc or None, limit=args.limit)
            _emit(
                args,
                {
                    "target": report.target,
                    "generation": report.generation,
                    "examined": report.examined,
                    "left_out": report.left_out,
                    "changed": report.changed,
                    "unchanged": report.unchanged,
                    "failed": report.failed,
                    "would_call_hook": report.would_call_hook,
                    "overall": report.change_list.overall,
                    "changes": _change_rows(report.change_list),
                    "documents": [_document_change(c) for c in report.documents],
                },
            )
            return 0
        if args.schema_command == "activate":
            done = service.activate_schema(
                args.version,
                operator=args.operator,
                acknowledge_breaking=args.acknowledge_breaking,
                rebuild=args.rebuild,
            )
            payload: dict[str, Any] = {
                "version": done.version,
                "previous": done.previous,
                "class": done.change_class,
                "rollback": done.rollback,
                "changed": list(done.changed),
            }
            if done.rebuild is not None:
                payload["rebuild"] = {
                    "checked": list(done.rebuild.checked),
                    "differing": list(done.rebuild.differing),
                    "refreshed": list(done.rebuild.refreshed),
                    "failed": [{"doc": doc, "error": error} for doc, error in done.rebuild.failed],
                }
            _emit(args, payload)
            return 0
        _emit(
            args,
            {
                "history": [
                    {
                        "id": r.id,
                        "at": r.at.isoformat(),
                        "operator": r.operator,
                        "action": r.action,
                        "doc": r.doc,
                        "from": r.from_version,
                        "to": r.to_version,
                        "class": r.change_class,
                        "note": r.note,
                    }
                    for r in service.schema_history(limit=args.limit, doc=args.doc)
                ],
                "served": {str(version): fp for version, fp in service.served_versions().items()},
            },
        )
        return 0
    except RejectedError as rejection:
        return _refused(args, rejection)
    except SchemaError as refusal:  # TIER_IN_USE, HISTORY_COMPACTED, SCHEMA_STATE_CHANGED
        return _refused_text(args, refusal.codes[0] if refusal.codes else "SCHEMA_ERROR", "; ".join(refusal.problems))


def _pin_command(args: argparse.Namespace, service: Aduku) -> int:
    try:
        if args.command == "pin":
            service.pin_document(args.doc, args.version, operator=args.operator)
            _emit(args, {"pinned": args.doc, "version": args.version})
        elif args.command == "unpin":
            service.unpin_document(args.doc, operator=args.operator)
            _emit(args, {"unpinned": args.doc})
        elif args.dry_run:
            if not args.operator.strip():  # the same rule as a real run, though nothing is recorded
                raise ValueError("an operator name is required to change which schema version serves")
            names = args.docs or sorted(service.schemas.pins())
            changes = [service.plan_migration(name, args.to) for name in names]
            target = args.to if args.to is not None else service.schemas.active().version
            _emit(args, {"target": target, "documents": [_document_change(c) for c in changes]})
        else:
            report = service.migrate(args.docs or None, to=args.to, operator=args.operator, batch=args.batch)
            _emit(
                args,
                {
                    "moved": list(report.moved),
                    "already_moved": list(report.already_moved),
                    "not_pinned": list(report.not_pinned),
                    "failed": [{"doc": doc, "code": code} for doc, code in report.failed],
                    "batches": report.batches,
                },
            )
        return 0
    except RejectedError as rejection:
        return _refused(args, rejection)
    except SchemaError as refusal:
        return _refused_text(args, refusal.codes[0] if refusal.codes else "SCHEMA_ERROR", "; ".join(refusal.problems))


def _document_change(change: DocumentChange) -> dict[str, Any]:
    """What differs in one document: whether, never what (no document text, no value)."""
    return {
        "doc": change.doc,
        "document_changed": change.document_changed,
        "errors_added": list(change.errors_added),
        "errors_resolved": list(change.errors_resolved),
        "skipped_before": list(change.skipped_before),
        "skipped_after": list(change.skipped_after),
        "shadowed_by_reason": dict(change.shadowed_by_reason),
        "conflicts_changed": change.conflicts_changed,
        "would_call_hook": change.would_call_hook,
        "failure_code": change.failure_code,
    }


def _shadowed(item: Shadowed) -> dict[str, Any]:
    return {
        "seq": item.seq,
        "entry": item.entry_id,
        "tier": item.tier,
        "path": item.path,
        "reason": item.reason,
        "detail": item.detail,
    }


def _run(args: argparse.Namespace, service: Aduku) -> int:
    try:
        if args.command == "append":
            return _append(args, service)
        if args.command == "get":
            try:
                result = service.get(
                    args.doc,
                    consistency="fresh" if args.fresh else "cached",
                    include=tuple(args.include),
                    as_of=_as_of_arg(args.as_of),
                )
            except HistoryCompactedError as refusal:
                return _refused(args, refusal)
            _emit(
                args,
                {
                    **({"as_of": _as_of_info(result.as_of)} if result.as_of is not None else {}),
                    "document": to_canonical(result.document),
                    "deferred": [_deferred_status(d) for d in result.deferred],
                    "model_errors": [_model_error(e) for e in result.model_errors],
                    "skipped": list(result.skipped),
                    "shadowed": [_shadowed(item) for item in result.shadowed],
                    "conflicts": list(result.conflicts),
                    "status": result.status,
                },
            )
            return 0
        if args.command == "schema":
            return _schema_command(args, service)
        if args.command in ("pin", "unpin", "migrate"):
            return _pin_command(args, service)
        if args.command == "recompute":
            return _recompute(args, service)
        if args.command in ("invalidate", "scheduler"):
            return _deferred_admin(args, service)
        if args.command == "notify":
            _emit(args, {"marked": service.notify(args.topic, args.docs or None)})
            return 0
        if args.command == "deferred":
            _emit(
                args,
                {
                    "deferred": [
                        {
                            "field": i.field,
                            "status": i.status,
                            "as_of": i.as_of.isoformat() if i.as_of else None,
                            "attempts": i.attempts,
                            "invalidated": i.invalidated,
                        }
                        for i in service.list_deferred(args.doc)
                    ]
                },
            )
            return 0
        if args.command == "effects":
            if args.effects_command == "list":
                found = service.list_effects(args.doc, args.field)
                _emit(
                    args,
                    {
                        "effects": [
                            {
                                "effect_seq": e.effect_seq,
                                "field": e.field,
                                "hook": e.hook,
                                "outcome": e.outcome,
                                "attempts": e.attempts,
                                "recorded_at": e.recorded_at.isoformat(),
                            }
                            for e in found
                        ]
                    },
                )
            else:
                _emit(args, {"pruned": service.prune_effects(args.doc)})
            return 0
        if args.command == "explain":
            try:
                why = service.explain(args.doc, args.path, as_of=_as_of_arg(args.as_of))
            except HistoryCompactedError as refusal:
                return _refused(args, refusal)
            _emit(
                args,
                {
                    "path": why.path,
                    "value": to_canonical(why.value),
                    "winner": why.winner,
                    "winner_seq": why.winner_seq,
                    "rule": why.rule,
                    "inputs": list(why.inputs),
                    "shadowed": list(why.shadowed),
                    "shadowed_by_schema": [_shadowed(item) for item in why.shadowed_by_schema],
                    "model_errors": [_model_error(e) for e in why.model_errors],
                    "dependencies": [
                        {"path": path, "value": None if value is ABSENT else to_canonical(value)}
                        for path, value in why.dependencies
                    ],
                    "effect_seq": why.effect_seq,
                    "status": why.status,
                    "authority": list(why.authority) if why.authority is not None else None,
                    "authority_from": why.authority_from,
                    "as_of": why.as_of.isoformat() if why.as_of else None,
                    "originals_kept": None
                    if why.originals_kept is None
                    else ("kept" if why.originals_kept else "expired"),
                    "sources": list(why.sources),
                    "purged": [
                        {"entry": m.entry_id, "seq": m.seq, "at": m.purged_at.isoformat(), "reason": m.reason}
                        for m in why.purged
                    ],
                },
            )
            return 0
        if args.command == "rebuild":
            report = service.rebuild(args.doc)
            _emit(
                args,
                {
                    "identical": report.identical,
                    "checked": list(report.checked),
                    "differing": list(report.differing),
                    "refreshed": list(report.refreshed),
                    "failed": [{"doc": name, "why": why} for name, why in report.failed],
                    "layers_differing": [{"doc": d, "tier": t} for d, t in report.layers_differing],
                    "layers_refreshed": [{"doc": d, "tier": t} for d, t in report.layers_refreshed],
                },
            )
            return 0 if report.identical else 1
        if args.command == "compact":
            return _compact(args, service)
        if args.command == "purge":
            return _purge(args, service)
        if args.command == "purge-log":
            _emit(
                args,
                {
                    "purges": [
                        {
                            "purge": r.id,
                            "scope": r.scope,
                            "reason": r.reason,
                            "actor": r.actor,
                            "at": r.at.isoformat(),
                            "counts": r.counts,
                        }
                        for r in service.purge_log(args.doc)
                    ]
                },
            )
            return 0
        if args.command == "archive":
            if args.archive_command == "verify":
                verified = service.verify_archive(args.doc)
                _emit(
                    args, {"doc": verified.doc, "checked": verified.checked, "differences": list(verified.differences)}
                )
                return 0 if not verified.differences else 1
            gone = service.expire_archive(args.doc, through_seq=args.through_seq)
            _emit(args, {"doc": gone.doc, "through_seq": gone.through_seq, "expired": gone.expired})
            return 0
        if args.command == "entries":
            _emit(args, {"entries": [_entry(e) for e in service.list_entries(args.doc, since_seq=args.since)]})
            return 0
        return _audit(args, service)
    finally:
        service.storage.close()


def _deferred_status(status: Any) -> dict[str, Any]:
    return {
        "path": status.path,
        "status": status.status,
        "as_of": status.as_of.isoformat() if status.as_of else None,
        "error": status.error,
        "attempts": status.attempts,
        "waiting_for": list(status.waiting_for),
        "reason": status.reason,
    }


def _deferred_admin(args: argparse.Namespace, service: Aduku) -> int:
    caller = Identity(args.contributor, (args.tier,), tuple(args.role))
    try:
        if args.command == "invalidate":
            marked = service.invalidate(None if args.doc == "*" else args.doc, args.field, caller=caller)
            _emit(args, {"marked": marked})
            return 0
        report = service.scheduler_run(args.hook, caller=caller)
    except RejectedError as rejection:
        _emit(args, {"rejected": {"code": rejection.code.value, "reasons": [_reason(r) for r in rejection.reasons]}})
        for reason in rejection.reasons:
            print(f"{reason.code.value}: {reason.message}", file=sys.stderr)
        return 1
    _emit(
        args,
        {
            "run_id": report.run_id,
            "attempted": report.attempted,
            "succeeded": report.succeeded,
            "failed": report.failed,
            "skipped": report.skipped,
            "calls": report.calls,
            "reason": report.reason,
        },
    )
    return 0


def _recompute(args: argparse.Namespace, service: Aduku) -> int:
    caller = Identity(args.contributor, (args.tier,), tuple(args.role))
    try:
        report = service.recompute(args.doc, args.field, caller=caller)
    except RejectedError as rejection:
        _emit(args, {"rejected": {"code": rejection.code.value, "reasons": [_reason(r) for r in rejection.reasons]}})
        for reason in rejection.reasons:
            print(f"{reason.code.value}: {reason.message}", file=sys.stderr)
        return 1
    _emit(
        args,
        {
            "recomputed": list(report.recomputed),
            "skipped": list(report.skipped),
            "failed": [{"field": name, "kind": kind} for name, kind in report.failed],
            "effect_seqs": [{"field": name, "effect_seq": number} for name, number in report.effect_seqs],
        },
    )
    return 1 if report.failed else 0


_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def _duration(text: str, option: str) -> timedelta:
    if len(text) < 2 or text[-1] not in _UNITS or not text[:-1].isdigit():
        raise _UsageError(f"{option}: {text!r} is not a duration such as 90d, 12h or 30m")
    return timedelta(seconds=int(text[:-1]) * _UNITS[text[-1]])


def _refused(args: argparse.Namespace, rejection: RejectedError) -> int:
    """Print a refusal the way a rejected request is printed; exit 1."""
    _emit(args, {"rejected": {"code": rejection.code.value, "reasons": [_reason(r) for r in rejection.reasons]}})
    for reason in rejection.reasons:
        print(f"{reason.code.value}: {reason.message}", file=sys.stderr)
    return 1


def _purge(args: argparse.Namespace, service: Aduku) -> int:
    if bool(args.path) == bool(args.entry):
        raise _UsageError("purge needs exactly one of --path PATH and --entry ID (repeatable)")
    if args.scope_tier and not args.path:
        raise _UsageError("--scope-tier narrows a --path scope")
    scope = PurgeScope(path=args.path, tier=args.scope_tier, entries=tuple(args.entry))
    try:
        report = service.purge(
            args.doc,
            scope,
            reason=args.reason,
            caller=Identity(args.contributor, (args.tier,), tuple(args.role)),
        )
    except RejectedError as rejection:
        return _refused(args, rejection)
    _emit(
        args,
        {
            "purge": report.purge_id,
            "doc": report.doc,
            "entries_erased": report.entries_erased,
            "entries_rewritten": report.entries_rewritten,
            "archive_rows": report.archive_erased,
            "effects_erased": report.effects_erased,
            "derived_erased": report.derived_erased,
            "audit_scrubbed": report.audit_scrubbed,
            "expired_sources": report.expired_sources,
        },
    )
    return 0


def _compact(args: argparse.Namespace, service: Aduku) -> int:
    if (args.through_seq is None) == (args.older_than is None):
        raise _UsageError("compact needs exactly one of --through-seq N and --older-than DURATION")
    older = _duration(args.older_than, "--older-than") if args.older_than else None
    try:
        report = service.compact(args.doc, through_seq=args.through_seq, older_than=older)
    except RejectedError as rejection:
        return _refused(args, rejection)
    if report.noop:
        _emit(args, {"noop": True, "message": "nothing to compact"})
        return 0
    _emit(
        args,
        {
            "noop": False,
            "compaction": report.compaction_id,
            "watermark": report.watermark_seq,
            "replaced": report.replaced,
            "synthetic": report.synthetic,
            "checked_paths": report.checked_paths,
        },
    )
    return 0


def _moment(text: str, option: str) -> datetime:
    try:
        return datetime.fromisoformat(text)
    except ValueError as error:
        raise _UsageError(f"{option}: {text!r} is not an ISO 8601 moment") from error


def _as_of_arg(items: Sequence[str]) -> AsOf | None:
    """`--as-of seq=N | tx=ISO | valid=ISO`, exactly once; anything else is AS_OF_AMBIGUOUS or a usage error."""
    if not items:
        return None
    if len(items) != 1:
        raise _UsageError("AS_OF_AMBIGUOUS: --as-of takes exactly one point: seq=N, tx=ISO8601 or valid=ISO8601")
    kind, _, text = items[0].partition("=")
    if kind == "seq":
        if not text.lstrip("-").isdigit():
            raise _UsageError(f"--as-of seq={text}: the seq is a whole number")
        return AsOf(seq=int(text))
    if kind == "tx":
        return AsOf(tx_time=_moment(text, "--as-of tx"))
    if kind == "valid":
        return AsOf(valid_time=_moment(text, "--as-of valid"))
    raise _UsageError(f"--as-of {items[0]!r}: use seq=N, tx=ISO8601 or valid=ISO8601")


def _as_of_info(info: AsOfInfo) -> dict[str, Any]:
    point = info.point.isoformat() if isinstance(info.point, datetime) else str(info.point)
    return {
        "kind": info.kind,
        "point": point,
        "resolved_seq": info.resolved_seq,
        "included": info.included,
        "inside_request": info.inside_request,
    }


def _append(args: argparse.Namespace, service: Aduku) -> int:
    ops = args.ops or []
    if not ops:
        raise _UsageError("append needs at least one --set PATH=VALUE or --delete PATH")
    if args.idem_key and len(ops) != 1:
        raise _UsageError("--idem-key applies to a single entry; submit one --set or --delete")
    valid_time = _moment(args.valid_time, "--valid-time") if args.valid_time else None
    entries: list[EntryInput] = []
    for kind, text in ops:
        if kind == "delete":
            entries.append(
                EntryInput(tier=args.tier, path=text, op="delete", idem_key=args.idem_key, valid_time=valid_time)
            )
        else:
            path, value = _split_assignment(text)
            entries.append(
                EntryInput(
                    tier=args.tier, path=path, op="set", value=value, idem_key=args.idem_key, valid_time=valid_time
                )
            )
    try:
        result = service.append(
            args.doc,
            entries,
            contributor=Identity(args.contributor, (args.tier,), tuple(args.role)),
            lenient=args.lenient,
            request_key=args.request_key,
        )
    except RejectedError as rejection:
        reasons = [_reason(r) for r in rejection.reasons]
        _emit(args, {"rejected": {"code": rejection.code.value, "reasons": reasons}})
        for reason in rejection.reasons:
            print(f"{reason.code.value}: {reason.message}", file=sys.stderr)
        return 1
    _emit(
        args,
        {
            "request_id": result.request_id,
            "duplicate": result.duplicate,
            "entries": [{"id": entry_id, "seq": seq} for entry_id, seq in result.entries],
            "model_errors_added": [_model_error(e) for e in result.model_errors_added],
            "model_errors_resolved": [_model_error(e) for e in result.model_errors_resolved],
        },
    )
    return 0


def _model_error(error: ModelError) -> dict[str, Any]:
    found: dict[str, Any] = {"rule": error.rule_id, "path": error.path_scope, "message": error.message}
    if error.introduced_by_request is not None:
        found["introduced_by_request"] = error.introduced_by_request
    if error.introduced_by_schema_version is not None:
        found["introduced_by_schema_version"] = error.introduced_by_schema_version
    return found


def _audit(args: argparse.Namespace, service: Aduku) -> int:
    if args.audit_command == "list":
        _emit(args, {"records": [_record(r) for r in service.list_rejections(args.doc)]})
    elif args.audit_command == "prune":
        _emit(args, {"pruned": service.prune_rejections(args.doc)})
    else:
        service.set_rejection_audit(args.doc, args.setting)
        _emit(args, {"doc": args.doc, "setting": args.setting})
    return 0


def _split_assignment(text: str) -> tuple[str, Any]:
    """PATH=VALUE: split at the first '=' that is not inside a [key] predicate; VALUE is Ion text."""
    depth = 0
    quote: str | None = None
    for position, char in enumerate(text):
        if quote is not None:  # inside a quoted key: brackets and '=' are ordinary characters
            if char == quote:
                quote = None
        elif depth > 0 and char in "\"'":
            quote = char
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
        elif char == "=" and depth == 0:
            path, raw = text[:position], text[position + 1 :]
            try:
                return path, parse(raw)
            except IonError as error:
                raise _UsageError(f"the value in {text!r} is not valid Ion: {error}") from error
    raise _UsageError(f"expected PATH=VALUE, got {text!r}")


# -- output ------------------------------------------------------------------------------------------------------


def _reason(reason: Any) -> dict[str, Any]:
    full = {
        "code": reason.code.value,
        "message": reason.message,
        "entry_index": reason.entry_index,
        "path": reason.path,
        "rule": reason.rule,
    }
    return {key: value for key, value in full.items() if value is not None}


def _entry(entry: Entry) -> dict[str, Any]:
    full = {
        "id": entry.id,
        "seq": entry.seq,
        "request_id": entry.request_id,
        "tier": entry.tier,
        "path": entry.path,
        "op": entry.op,
        "value": to_canonical(entry.value),
        "contributor": entry.contributor,
        "idem_key": entry.idem_key,
        "request_key": entry.request_key,
        "lenient": entry.lenient,
        "schema_version": entry.schema_version,
        "tx_time": entry.tx_time.isoformat(),
        "valid_time": entry.valid_time.isoformat() if entry.valid_time is not None else None,
        "synthetic": entry.synthetic,
        "purged": True if entry.purged else None,
    }
    return {key: value for key, value in full.items() if value is not None}


def _record(record: AuditRecord) -> dict[str, Any]:
    return {
        "doc": record.doc,
        "contributor": record.contributor,
        "tiers": list(record.tiers),
        "lenient": record.lenient,
        "attempted": [
            {key: to_canonical(value) if key in ("value", "meta") else value for key, value in a.items()}
            for a in record.attempted
        ],
        "reasons": list(record.reasons),
        "schema_version": record.schema_version,
        "recorded_at": record.recorded_at.isoformat(),
    }


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return FrozenStruct({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze(item) for item in value)
    return value


def _emit(args: argparse.Namespace, payload: dict[str, Any]) -> None:
    if args.format == "ion":
        print(to_canonical(_freeze(payload)))
    else:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
