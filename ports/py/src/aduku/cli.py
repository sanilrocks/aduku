"""Thin command line over the library (contracts/cli.md). Exit codes: 0 ok, 1 rejected request (or a rebuild that found a problem),
2 usage or schema error."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from typing import Any

from aduku.audit import AuditRecord
from aduku.errors import AdukuError, RejectedError, SchemaError
from aduku.ion_codec import FrozenStruct, IonError, parse, to_canonical
from aduku.model import Entry, EntryInput, Identity, ModelError
from aduku.schema.loader import load_schema
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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aduku", description="aduku-py command line")
    parser.add_argument("--db", default=os.environ.get("ADUKU_DB", "aduku.db"))
    parser.add_argument("--schema", default=os.environ.get("ADUKU_SCHEMA"))
    parser.add_argument("--format", choices=("json", "ion"), default="json")
    sub = parser.add_subparsers(dest="command", required=True)

    append = sub.add_parser("append", help="submit one atomic request")
    append.add_argument("doc")
    append.add_argument("--contributor", required=True)
    append.add_argument("--tier", required=True)
    append.add_argument("--role", action="append", default=[], help="a role the contributor holds (repeatable)")
    append.add_argument("--lenient", action="store_true")
    append.add_argument("--request-key")
    append.add_argument("--idem-key")
    append.add_argument("--set", dest="ops", action=_OpAction, metavar="PATH=VALUE")
    append.add_argument("--delete", dest="ops", action=_OpAction, metavar="PATH")

    get = sub.add_parser("get", help="read the merged document")
    get.add_argument("doc")

    rebuild = sub.add_parser("rebuild", help="re-derive from the ledger and compare with the cached views")
    rebuild.add_argument("doc", nargs="?")

    explain = sub.add_parser("explain", help="why a field has its value")
    explain.add_argument("doc")
    explain.add_argument("path")

    entries = sub.add_parser("entries", help="list ledger entries")
    entries.add_argument("doc")
    entries.add_argument("--since", type=int, default=0)

    schema = sub.add_parser("schema", help="schema tools")
    schema_sub = schema.add_subparsers(dest="schema_command", required=True)
    schema_sub.add_parser("check", help="load and validate a schema file").add_argument("file")

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
        if args.command == "schema":
            return _check_schema(args)
        service = _service(args)
        return _run(args, service)
    except _UsageError as error:
        print(f"aduku: {error}", file=sys.stderr)
        return 2
    except SchemaError as error:
        print("aduku: schema error: " + "; ".join(error.problems), file=sys.stderr)
        return 2
    except AdukuError as error:
        print(f"aduku: {error}", file=sys.stderr)
        return 2


def _check_schema(args: argparse.Namespace) -> int:
    """Exit 0 with a summary if the schema loads; exit 2 with every problem (and its stable code) if not."""
    try:
        with open(args.file, encoding="utf-8") as handle:
            text = handle.read()
    except OSError as error:
        raise _UsageError(f"cannot read schema: {error}") from error
    try:
        schema = load_schema(text)
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
            "derived": [spec.pattern for spec in schema.fields if spec.derived],
        },
    )
    return 0


def _service(args: argparse.Namespace) -> Aduku:
    if not args.schema:
        raise _UsageError("no schema: pass --schema PATH or set ADUKU_SCHEMA")
    try:
        with open(args.schema, encoding="utf-8") as handle:
            schema = load_schema(handle.read())
    except OSError as error:
        raise _UsageError(f"cannot read schema: {error}") from error
    registry = SchemaRegistry()
    registry.put(schema)
    registry.activate(schema.version)
    return Aduku(SqliteStorage(args.db), registry)


def _run(args: argparse.Namespace, service: Aduku) -> int:
    try:
        if args.command == "append":
            return _append(args, service)
        if args.command == "get":
            result = service.get(args.doc)
            _emit(
                args,
                {
                    "document": to_canonical(result.document),
                    "model_errors": [_model_error(e) for e in result.model_errors],
                    "skipped": list(result.skipped),
                    "conflicts": list(result.conflicts),
                    "status": result.status,
                },
            )
            return 0
        if args.command == "explain":
            why = service.explain(args.doc, args.path)
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
                    "model_errors": [_model_error(e) for e in why.model_errors],
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
                },
            )
            return 0 if report.identical else 1
        if args.command == "entries":
            _emit(args, {"entries": [_entry(e) for e in service.list_entries(args.doc, since_seq=args.since)]})
            return 0
        return _audit(args, service)
    finally:
        service.storage.close()


def _append(args: argparse.Namespace, service: Aduku) -> int:
    ops = args.ops or []
    if not ops:
        raise _UsageError("append needs at least one --set PATH=VALUE or --delete PATH")
    if args.idem_key and len(ops) != 1:
        raise _UsageError("--idem-key applies to a single entry; submit one --set or --delete")
    entries: list[EntryInput] = []
    for kind, text in ops:
        if kind == "delete":
            entries.append(EntryInput(tier=args.tier, path=text, op="delete", idem_key=args.idem_key))
        else:
            path, value = _split_assignment(text)
            entries.append(EntryInput(tier=args.tier, path=path, op="set", value=value, idem_key=args.idem_key))
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
