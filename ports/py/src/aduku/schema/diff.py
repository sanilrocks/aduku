"""Classification of the changes between two schema versions (feature 006, contracts/changes.md). Pure.

Two schemas in, a list out, the same list in the same order each time; nothing is read from a store. A description names what
changed and never holds a stored value (a default or an argument is named, not shown).
"""

from __future__ import annotations

from dataclasses import fields as dc_fields

from aduku.model import Change, ChangeClass, ChangeList
from aduku.schema.model import FieldSpec, Schema, _describe

__all__ = ["schema_changes"]

# FieldSpec attributes by the kind of change a difference in them is. An attribute not named here is a difference the table does
# not know: it is listed as `other` (visible, not classified) rather than missed.
_FIELD_KINDS: dict[str, str] = {
    "default": "default_changed",
    "on_delete": "default_changed",
    "merge": "merge_changed",
    "type": "type_changed",
    "validate": "validate_changed",
    "authority": "authority_changed",
    "ops": "authority_changed",
    "redact": "authority_changed",
    "computed": "computed_changed",
    "on_missing": "computed_changed",
    "impure": "impure_changed",
    "failure_mode": "impure_changed",
    "deferred": "deferred_changed",
}
_DERIVATION = {"derived", "derived_spec"}  # handled as became_derived / became_plain
# when plain turns derived (or back) these differ only because of that: the one change says it
_DROPPED_WITH_DERIVATION = {"merge", "computed", "impure", "deferred", "on_missing", "failure_mode"}
_CLASSES: dict[str, ChangeClass] = {
    "field_added": "compatible",
    "field_removed": "visible",
    "default_changed": "visible",
    "merge_changed": "breaking",
    "type_changed": "breaking",
    "validate_changed": "visible",
    "authority_changed": "visible",
    "became_derived": "breaking",
    "became_plain": "breaking",
    "computed_changed": "visible",
    "impure_changed": "visible",
    "deferred_changed": "visible",
    "tier_added_top": "compatible",
    "tier_changed": "breaking",
    "roles_added": "compatible",
    "roles_removed": "visible",
    "settings_changed": "visible",
    "audit_changed": "compatible",
    "document_validate_changed": "visible",
    "other": "visible",
}
# The reasons a change can give an entry for being left out of a document (engine/shadow.py).
_SHADOWS = {
    "became_derived": ("now_derived",),
    "type_changed": ("no_longer_conforms",),
    "merge_changed": ("cannot_be_merged",),
    "tier_changed": ("tier_not_in_schema",),
}
_WORDS = {
    "field_added": "the field is new",
    "field_removed": "the field is no longer declared: new writes to it are refused and its recorded values still show",
    "default_changed": "the default or what a delete does changed",
    "merge_changed": "the merge behavior changed",
    "type_changed": "the type changed",
    "validate_changed": "a validation rule was added, removed or changed",
    "authority_changed": "who may write the field, or how, changed",
    "became_derived": "the field is now computed or deferred, so recorded writes to it no longer count",
    "became_plain": "the field is no longer computed or deferred",
    "computed_changed": "the macro, its arguments or what happens to a missing input changed",
    "impure_changed": "the impure compute's hook, inputs or call settings changed",
    "deferred_changed": "the deferred source or its refresh settings changed",
}
_TOP_SETTINGS = {
    "lenient_tiers": "settings_changed",
    "lenient_contributors": "settings_changed",
    "purge_by": "settings_changed",
    "impure_failure_mode": "settings_changed",
    "deferred_on_failure": "settings_changed",
    "rejection_audit": "audit_changed",
    "document_validate": "document_validate_changed",
}
_HANDLED_TOP = {"version", "tiers", "fields", "roles", *_TOP_SETTINGS}


def schema_changes(old: Schema, new: Schema) -> ChangeList:
    """Every difference between two versions, each with its class; the overall class is the worst of them."""
    found: list[Change] = []
    _fields(old, new, found)
    _tiers(old, new, found)
    _roles(old, new, found)
    _settings(old, new, found)
    return ChangeList.of(old.version, new.version, found)


def _change(kind: str, path: str, description: str | None = None, *, classified: bool = True) -> Change:
    return Change(
        kind=kind,
        path=path,
        description=description or _WORDS[kind],
        change_class=_CLASSES[kind],
        classified=classified,
        detail=_SHADOWS.get(kind, ()),
    )


def _other(path: str, what: str) -> Change:
    return _change("other", path, f"{what} differs and is not in the classification table", classified=False)


def _fields(old: Schema, new: Schema, found: list[Change]) -> None:
    before = {spec.pattern: spec for spec in old.fields}
    after = {spec.pattern: spec for spec in new.fields}
    for pattern in sorted(after.keys() - before.keys()):
        found.append(_change("field_added", pattern))
    for pattern in sorted(before.keys() - after.keys()):
        found.append(_change("field_removed", pattern))
    for pattern in sorted(before.keys() & after.keys()):
        found.extend(_field(pattern, before[pattern], after[pattern]))


def _field(pattern: str, old: FieldSpec, new: FieldSpec) -> list[Change]:
    kinds: set[str] = set()
    other = False
    derivation_changed = old.derived != new.derived
    if derivation_changed:
        kinds.add("became_derived" if new.derived is not None else "became_plain")
    for item in dc_fields(FieldSpec):
        name = item.name
        if name == "pattern" or name in _DERIVATION:
            continue
        if _describe(getattr(old, name)) == _describe(getattr(new, name)):
            continue
        kind = _FIELD_KINDS.get(name)
        if kind is None:
            other = True
        elif not (derivation_changed and name in _DROPPED_WITH_DERIVATION):
            kinds.add(kind)
    result = [_change(kind, pattern) for kind in sorted(kinds)]
    if other:
        result.append(_other(pattern, "a setting of the field"))
    return result


def _tiers(old: Schema, new: Schema, found: list[Change]) -> None:
    if old.tiers == new.tiers:
        return
    common = [tier for tier in old.tiers if tier in new.tiers]
    top = max((new.tiers.index(tier) for tier in common), default=-1)
    for tier in new.tiers:
        if tier in old.tiers:
            continue
        if new.tiers.index(tier) > top:  # appended after every tier that is kept
            found.append(_change("tier_added_top", tier, "a tier was added above the others"))
        else:
            found.append(_change("tier_changed", tier, "a tier was inserted below the top"))
    for tier in old.tiers:
        if tier not in new.tiers:
            found.append(_change("tier_changed", tier, "a tier was dropped or renamed"))
    kept_in_new = [tier for tier in new.tiers if tier in old.tiers]
    if kept_in_new != common:
        for tier in common:
            if kept_in_new.index(tier) != common.index(tier):
                found.append(_change("tier_changed", tier, "a tier moved to another rank"))


def _roles(old: Schema, new: Schema, found: list[Change]) -> None:
    found.extend(
        _change("roles_added", role, "a role was declared") for role in sorted(set(new.roles) - set(old.roles))
    )
    found.extend(
        _change("roles_removed", role, "a role is no longer declared")
        for role in sorted(set(old.roles) - set(new.roles))
    )


def _settings(old: Schema, new: Schema, found: list[Change]) -> None:
    grouped: set[str] = set()
    for item in dc_fields(Schema):
        name = item.name
        if name in ("version", "tiers", "fields", "roles"):
            continue
        if _describe(getattr(old, name)) == _describe(getattr(new, name)):
            continue
        kind = _TOP_SETTINGS.get(name)
        if kind is None:
            found.append(_other("", f"the schema setting {name!r}"))
        elif kind not in grouped:
            grouped.add(kind)
            found.append(_change(kind, "", _setting_words(kind)))


def _setting_words(kind: str) -> str:
    return {
        "settings_changed": "a schema-wide setting changed (leniency, who may purge, or a failure mode)",
        "audit_changed": "the rejection audit settings changed",
        "document_validate_changed": "a document rule was added, removed or changed",
    }[kind]
