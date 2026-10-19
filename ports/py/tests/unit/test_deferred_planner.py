"""Feature 003, research R-1, R-4, R-5: the pure planner. Statuses, reuse rules and which fetches are due, for a given
document, stored records and `now`. Nothing here calls a source or reads a clock."""

from datetime import UTC, datetime, timedelta

import pytest

from aduku.behaviors.registry import BehaviorRegistry
from aduku.engine.deferred import plan
from aduku.engine.effects import fingerprint
from aduku.ion_codec import parse, to_canonical
from aduku.model import DerivedRecord
from aduku.schema.loader import load_schema

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(hours=1)


def deferred_decl(type_: str = "int", **options: str) -> str:
    merged = {
        "hook": '"fx.score@1"',
        "depends": '["/name"]',
        "refresh": "once",
        "invalidate_by": "[admin]",
        **options,
    }
    on_missing = merged.pop("on_missing", None)
    body = ", ".join(f"{k}: {v}" for k, v in merged.items())
    extra = f", on_missing: {on_missing}" if on_missing else ""
    return f"{{type: {type_}{extra}, deferred: {{{body}}}}}"


def schema_of(fields: str, top: str = ""):  # type: ignore[no-untyped-def]
    behaviors = BehaviorRegistry.with_builtins()
    for name in ("fx.score", "fx.rate", "fx.limit"):
        behaviors.register_hook(name, 1, lambda inputs, timeout: 1)
    behaviors.register_hook("fx.score", 2, lambda inputs, timeout: 1)
    text = (
        f'schema::{{id: "t", version: 1, tiers: [user, admin], {top} fields: {{"/name": {{type: string, merge: lww}}, '
        f'"/country": {{type: string, merge: lww}}, "/shout": {{type: string, computed: {{macro: upper, args: ["/name"]}}}}, '
        f"{fields}}}}}"
    )
    return load_schema(text, behaviors)


def doc(text: str = '{name: "Alice", country: "US"}'):  # type: ignore[no-untyped-def]
    return parse(text)


def stored(spec_text: str, path: str = "/score", **changes: object) -> DerivedRecord:
    """A record as a fetch of `spec_text`'s field would have left it, for the name Alice."""
    schema = schema_of(f'"{path}": {spec_text}')
    spec = schema.field_for(__import__("aduku.paths", fromlist=["x"]).parse_path(path))
    assert spec is not None and spec.deferred is not None
    base = DerivedRecord(
        doc="d1",
        field=path,
        value="712",
        has_value=True,
        as_of=NOW,
        input_fingerprint=fingerprint(spec.deferred.source, [("/name", "Alice")]),
        hook=spec.deferred.source,
        shape=spec.deferred.shape(spec.type),
        schema_version=1,
        status="ok",
        attempts=1,
        last_attempt=NOW,
    )
    from dataclasses import replace

    return replace(base, **changes)  # type: ignore[arg-type]


def one(spec_text: str, document=None, record=None, now=NOW, **kwargs):  # type: ignore[no-untyped-def]
    schema = schema_of(f'"/score": {spec_text}')
    records = {} if record is None else {record.field: record}
    result = plan(schema, document or doc(), records, now, **kwargs)
    (field,) = result.fields
    return field


# -- once -----------------------------------------------------------------------------------------------------------


def test_with_no_record_and_inputs_ready_a_once_field_is_pending_and_due_for_its_first_fetch() -> None:
    field = one(deferred_decl())
    assert (field.status, field.action, field.reason, field.waiting_for) == ("pending", "first", "not_fetched", ())
    assert field.inputs == (("/name", "Alice"),) and field.fingerprint == fingerprint(
        "fx.score@1", [("/name", "Alice")]
    )


def test_a_current_record_is_shown_and_no_call_is_due() -> None:
    field = one(deferred_decl(), record=stored(deferred_decl()))
    assert (field.status, field.action, field.shown, field.include) == ("ok", None, 712, True)


def test_a_changed_input_keeps_a_stored_once_value() -> None:
    field = one(deferred_decl(), doc('{name: "Alicia"}'), stored(deferred_decl()))
    assert (field.status, field.action, field.shown) == ("ok", None, 712)


def test_a_changed_input_fetches_again_when_the_field_opted_in() -> None:
    text = deferred_decl(on_dependency_change="recompute")
    field = one(text, doc('{name: "Alicia"}'), stored(text))
    assert (field.status, field.action, field.shown) == (
        "ok",
        "inputs",
        712,
    )  # the old value shows until the new one is in
    assert one(text, record=stored(text)).action is None  # unchanged inputs: nothing to do


@pytest.mark.parametrize(
    "changed",
    [
        {"hook": '"fx.score@2"'},  # a new source version
        {"depends": '["/name", "/country"]'},  # a new dependency list
    ],
)
def test_a_record_made_for_another_source_or_dependencies_is_not_reusable(changed: dict[str, str]) -> None:
    old = stored(deferred_decl())
    field = one(deferred_decl(**changed), record=old)
    assert (field.status, field.action, field.reason) == ("pending", "first", "not_fetched")


def test_a_record_made_for_another_type_is_not_reusable() -> None:
    old = stored(deferred_decl(type_="decimal"))
    assert one(deferred_decl(type_="int"), record=old).action == "first"


@pytest.mark.parametrize("document", ['{country: "US"}', '{name: null, country: "US"}'])
def test_a_missing_or_null_input_makes_the_field_pending_and_calls_nothing(document: str) -> None:
    field = one(deferred_decl(), doc(document), stored(deferred_decl()))
    assert (field.status, field.action, field.reason, field.waiting_for) == (
        "pending",
        None,
        "inputs_missing",
        ("/name",),
    )
    assert (field.include, field.shown) == (True, None)  # on_missing: null shows deferred::null


def test_on_missing_absent_leaves_a_pending_field_out_of_the_document() -> None:
    field = one(deferred_decl(on_missing="absent"), doc('{country: "US"}'))
    assert (field.status, field.include) == ("pending", False)


def test_a_computed_input_is_read_from_the_p4_output() -> None:
    text = deferred_decl(depends='["/shout"]')
    document = doc('{name: "Alice", shout: computed::"ALICE"}')  # computed:: is not part of what the source is given
    field = one(text, document)
    assert field.inputs == (("/shout", "ALICE"),) and field.action == "first"


def test_an_invalidated_value_is_still_shown_and_is_due_again() -> None:
    field = one(deferred_decl(), record=stored(deferred_decl(), invalidated=True))
    assert (field.status, field.action, field.shown) == ("ok", "invalidated", 712)


def test_the_scheduler_does_not_fetch_a_once_field() -> None:
    assert one(deferred_decl(), mode="scheduler").action is None
    assert one(deferred_decl(), record=stored(deferred_decl(), invalidated=True), mode="scheduler").action is None


def test_the_value_is_read_from_its_canonical_text_and_the_plan_is_a_pure_function() -> None:
    schema = schema_of(f'"/score": {deferred_decl()}')
    records = {"/score": stored(deferred_decl(), value=to_canonical("seven"))}
    first = plan(schema, doc(), records, NOW)
    assert first.fields[0].shown == "seven" and plan(schema, doc(), records, NOW) == first
    assert plan(schema, doc(), records, LATER).fields[0].action is None  # once ignores the time


def test_a_schema_without_deferred_fields_has_an_empty_plan() -> None:
    result = plan(schema_of(""), doc(), {}, NOW)
    assert result.fields == () and result.levels == ()


# -- per_fetch (FR-007, clarification 1) -----------------------------------------------------------------------------

PER_FETCH = deferred_decl(refresh="per_fetch", ttl='"5m"')


def test_a_per_fetch_field_with_no_record_is_due_for_its_first_fetch() -> None:
    assert one(PER_FETCH).action == "first"


def test_a_stored_value_is_reused_while_the_inputs_are_unchanged_and_it_is_younger_than_the_ttl() -> None:
    field = one(PER_FETCH, record=stored(PER_FETCH), now=NOW + timedelta(seconds=299))
    assert (field.status, field.action, field.shown) == ("ok", None, 712)


def test_it_is_due_again_when_the_ttl_has_passed() -> None:
    assert one(PER_FETCH, record=stored(PER_FETCH), now=NOW + timedelta(seconds=300)).action == "ttl"
    assert one(PER_FETCH, record=stored(PER_FETCH), now=NOW + timedelta(days=2)).action == "ttl"


def test_a_changed_input_makes_it_due_whatever_the_ttl() -> None:
    field = one(PER_FETCH, doc('{name: "Alicia"}'), stored(PER_FETCH), now=NOW + timedelta(seconds=1))
    assert (field.action, field.shown) == ("inputs", 712)  # the old value shows until the new one is in


def test_a_fresh_read_ignores_the_ttl_but_not_the_inputs() -> None:
    assert one(PER_FETCH, record=stored(PER_FETCH), consistency="fresh").action == "fresh"
    assert one(PER_FETCH, consistency="fresh").action == "first"
    assert one(PER_FETCH, record=stored(PER_FETCH, invalidated=True), consistency="fresh").action == "invalidated"


def test_a_failed_attempt_is_not_repeated_inside_the_ttl() -> None:
    failed = stored(PER_FETCH, status="stale", error="timeout", as_of=NOW - timedelta(hours=1), last_attempt=NOW)
    assert one(PER_FETCH, record=failed, now=NOW + timedelta(seconds=299)).action is None
    assert one(PER_FETCH, record=failed, now=NOW + timedelta(seconds=300)).action == "ttl"
    assert one(PER_FETCH, record=failed, consistency="fresh").action == "fresh"


def test_the_scheduler_does_not_fetch_a_per_fetch_field() -> None:
    assert one(PER_FETCH, mode="scheduler").action is None
    assert one(PER_FETCH, record=stored(PER_FETCH), now=LATER, mode="scheduler").action is None


def test_a_field_already_tried_by_this_request_is_not_due_again() -> None:
    schema = schema_of(f'"/score": {PER_FETCH}')
    first = plan(schema, doc(), {}, NOW, consistency="fresh")
    assert first.due()[0].action == "first"
    assert plan(schema, doc(), {}, NOW, consistency="fresh", skip=frozenset({"/score"})).due() == ()


def test_once_and_per_fetch_together_fetch_when_either_says_so() -> None:
    both = deferred_decl(refresh="[once, per_fetch]", ttl='"5m"')
    assert one(both, record=stored(both), now=NOW + timedelta(seconds=299)).action is None
    assert one(both, record=stored(both), now=NOW + timedelta(seconds=300)).action == "ttl"
    assert one(both, doc('{name: "Alicia"}'), stored(both)).action == "inputs"


# -- promises: a deferred field that needs another deferred field (FR-013, FR-014, FR-015) -----------------------------

SCORE = deferred_decl()
LIMIT = deferred_decl(hook='"fx.limit@1"', depends='["/creditScore", "/country"]')


def chain(order: tuple[str, ...] = ("score", "limit")):  # type: ignore[no-untyped-def]
    texts = {"score": f'"/creditScore": {SCORE}', "limit": f'"/creditLimit": {LIMIT}'}
    return schema_of(", ".join(texts[name] for name in order))


def made(schema, path: str, inputs: list[tuple[str, object]], value: str, **changes: object) -> DerivedRecord:  # type: ignore[no-untyped-def]
    from dataclasses import replace

    from aduku.paths import parse_path

    spec = schema.field_for(parse_path(path))
    record = DerivedRecord(
        doc="d1",
        field=path,
        value=value,
        has_value=True,
        as_of=NOW,
        input_fingerprint=fingerprint(spec.deferred.source, inputs),
        hook=spec.deferred.source,
        shape=spec.deferred.shape(spec.type),
        schema_version=1,
        status="ok",
        attempts=1,
        last_attempt=NOW,
    )
    return replace(record, **changes)  # type: ignore[arg-type]


def test_a_dependent_waits_while_the_deferred_field_it_needs_has_no_value() -> None:
    result = plan(chain(), doc(), {}, NOW)
    score, limit = result.fields
    assert (score.path, score.status, score.action) == ("/creditScore", "pending", "first")
    assert (limit.status, limit.action, limit.reason, limit.waiting_for) == (
        "pending", None, "upstream", ("/creditScore",),
    )  # fmt: skip


def test_a_dependent_is_due_once_its_upstream_has_a_value_and_is_given_that_value() -> None:
    schema = chain()
    records = {"/creditScore": made(schema, "/creditScore", [("/name", "Alice")], "712")}
    limit = plan(schema, doc(), records, NOW).fields[1]
    assert (limit.status, limit.action) == ("pending", "first")
    assert limit.inputs == (("/creditScore", 712), ("/country", "US"))
    assert limit.fingerprint == fingerprint("fx.limit@1", [("/creditScore", 712), ("/country", "US")])


def test_a_dependent_uses_a_stale_upstream_value() -> None:
    schema = schema_of(f'"/creditScore": {deferred_decl(on_failure="serve_stale")}, "/creditLimit": {LIMIT}')
    stale = made(schema, "/creditScore", [("/name", "Alice")], "700", status="stale", error="timeout")
    limit = plan(schema, doc(), {"/creditScore": stale}, NOW).fields[1]
    assert limit.action == "first" and limit.inputs[0] == ("/creditScore", 700)


def test_an_upstream_that_failed_with_nothing_to_serve_holds_its_dependents_back() -> None:
    schema = chain()
    failed = made(
        schema, "/creditScore", [("/name", "Alice")], "0", has_value=False, status="error", error="error", as_of=None
    )
    score, limit = plan(schema, doc(), {"/creditScore": failed}, NOW).fields
    assert (score.status, limit.status, limit.action, limit.reason) == ("error", "pending", None, "upstream")


def test_a_plain_input_that_is_missing_is_reported_as_missing_not_as_upstream() -> None:
    schema = chain()
    records = {"/creditScore": made(schema, "/creditScore", [("/name", "Alice")], "712")}
    limit = plan(schema, doc('{name: "Alice"}'), records, NOW).fields[1]
    assert (limit.status, limit.reason, limit.waiting_for) == ("pending", "inputs_missing", ("/country",))


def test_when_both_are_missing_the_reason_is_the_upstream() -> None:
    limit = plan(chain(), doc('{name: "Alice"}'), {}, NOW).fields[1]
    assert (limit.reason, limit.waiting_for) == ("upstream", ("/creditScore", "/country"))


def test_a_dependent_follows_its_own_policy_when_the_upstream_value_changes() -> None:
    schema = chain()
    both = {
        "/creditScore": made(schema, "/creditScore", [("/name", "Alice")], "800"),
        "/creditLimit": made(schema, "/creditLimit", [("/creditScore", 712), ("/country", "US")], "7120"),
    }
    assert plan(schema, doc(), both, NOW).fields[1].action is None  # once keeps its value
    recompute = schema_of(
        f'"/creditScore": {SCORE}, "/creditLimit": {deferred_decl(hook='"fx.limit@1"', depends='["/creditScore", "/country"]', on_dependency_change="recompute")}'
    )
    again = {
        "/creditScore": made(recompute, "/creditScore", [("/name", "Alice")], "800"),
        "/creditLimit": made(recompute, "/creditLimit", [("/creditScore", 712), ("/country", "US")], "7120"),
    }
    assert plan(recompute, doc(), again, NOW).fields[1].action == "inputs"


def test_fields_are_planned_by_level_then_path_whatever_the_declaration_order() -> None:
    forward = plan(chain(("score", "limit")), doc(), {}, NOW)
    backward = plan(chain(("limit", "score")), doc(), {}, NOW)
    assert [f.path for f in forward.fields] == [f.path for f in backward.fields] == ["/creditScore", "/creditLimit"]
    assert forward.levels == backward.levels == (("/creditScore",), ("/creditLimit",))
    assert [(f.status, f.action) for f in forward.fields] == [(f.status, f.action) for f in backward.fields]


def test_a_struct_read_whole_waits_for_the_deferred_fields_inside_it() -> None:
    text = (
        f'"/order": {{type: struct, merge: struct}}, "/order/rate": {deferred_decl(hook='"fx.rate@1"')}, '
        f'"/a_all": {deferred_decl(type_="struct", hook='"fx.limit@1"', depends='["/order"]')}'
    )
    schema = schema_of(text)
    document = doc('{name: "Alice", order: {note: "x"}}')
    result = plan(schema, document, {}, NOW)
    rate, everything = result.fields
    assert (rate.path, rate.action, everything.path, everything.status, everything.reason) == (
        "/order/rate", "first", "/a_all", "pending", "upstream",
    )  # fmt: skip


# -- scheduled (FR-009, FR-012) --------------------------------------------------------------------------------------

HOURLY = deferred_decl(refresh='scheduled::{every: "1h"}')
CRON = deferred_decl(refresh='scheduled::{cron: "0 * * * *"}')


def test_a_scheduled_only_field_is_never_fetched_by_a_read_and_waits_for_the_scheduler() -> None:
    field = one(HOURLY)
    assert (field.status, field.action, field.reason) == ("pending", None, "not_fetched")
    assert (
        one(HOURLY, record=stored(HOURLY), now=NOW + timedelta(days=3)).action is None
    )  # overdue, and a read still calls nothing
    assert one(HOURLY, record=stored(HOURLY), now=NOW + timedelta(days=3), consistency="fresh").action is None


def test_the_scheduler_makes_the_first_fetch_when_the_inputs_are_ready() -> None:
    assert one(HOURLY, mode="scheduler").action == "first"
    assert one(HOURLY, doc('{country: "US"}'), mode="scheduler").action is None  # the input is missing


def test_an_interval_comes_due_exactly_one_interval_after_the_last_attempt() -> None:
    record = stored(HOURLY)
    assert one(HOURLY, record=record, now=NOW + timedelta(seconds=3599), mode="scheduler").action is None
    assert one(HOURLY, record=record, now=NOW + timedelta(seconds=3600), mode="scheduler").action == "schedule"


def test_a_cron_expression_comes_due_at_its_next_fire_after_the_last_attempt() -> None:
    record = stored(CRON)  # fetched at 12:00 exactly
    assert one(CRON, record=record, now=NOW + timedelta(minutes=59), mode="scheduler").action is None
    assert one(CRON, record=record, now=NOW + timedelta(minutes=60), mode="scheduler").action == "schedule"
    late = stored(CRON, as_of=NOW + timedelta(minutes=30), last_attempt=NOW + timedelta(minutes=30))
    assert one(CRON, record=late, now=NOW + timedelta(minutes=59), mode="scheduler").action is None
    assert one(CRON, record=late, now=NOW + timedelta(minutes=60), mode="scheduler").action == "schedule"


def test_a_failed_attempt_counts_as_the_last_attempt_so_a_down_source_waits_for_its_next_slot() -> None:
    failed = stored(HOURLY, status="stale", error="timeout", as_of=NOW - timedelta(days=1), last_attempt=NOW)
    assert one(HOURLY, record=failed, now=NOW + timedelta(minutes=30), mode="scheduler").action is None
    assert one(HOURLY, record=failed, now=NOW + timedelta(hours=1), mode="scheduler").action == "schedule"


def test_a_changed_input_does_not_bring_a_scheduled_field_forward() -> None:
    field = one(HOURLY, doc('{name: "Alicia"}'), stored(HOURLY), now=NOW + timedelta(minutes=5), mode="scheduler")
    assert (field.action, field.shown) == (None, 712)  # the stored value stays until the schedule says so


def test_an_invalidated_scheduled_value_is_shown_to_a_read_and_recomputed_by_the_scheduler() -> None:
    record = stored(HOURLY, invalidated=True)
    assert one(HOURLY, record=record).action is None and one(HOURLY, record=record).shown == 712
    assert one(HOURLY, record=record, mode="scheduler").action == "invalidated"


def test_once_and_scheduled_together_the_read_makes_the_first_fetch_and_the_scheduler_the_refreshes() -> None:
    both = deferred_decl(refresh='[once, scheduled::{every: "1h"}]')
    assert one(both).action == "first"
    assert (
        one(both, record=stored(both), now=NOW + timedelta(hours=5)).action is None
    )  # a read leaves refreshing to the scheduler
    assert one(both, record=stored(both), now=NOW + timedelta(hours=1), mode="scheduler").action == "schedule"
    assert one(both, record=stored(both), now=NOW + timedelta(minutes=1), mode="scheduler").action is None


# -- notification (FR-011, FR-020) -----------------------------------------------------------------------------------

NOTIFIED = deferred_decl(refresh='notification::{topic: "sanctions.updated"}')


def test_a_notification_field_is_fetched_first_by_the_scheduler_not_by_a_read() -> None:
    assert one(NOTIFIED).action is None and one(NOTIFIED).reason == "not_fetched"
    assert one(NOTIFIED, mode="scheduler").action == "first"


def test_an_invalidated_value_is_shown_to_a_read_and_recomputed_by_the_scheduler() -> None:
    record = stored(NOTIFIED, invalidated=True)
    assert (one(NOTIFIED, record=record).action, one(NOTIFIED, record=record).shown) == (None, 712)
    assert one(NOTIFIED, record=record, mode="scheduler").action == "invalidated"
    assert (
        one(NOTIFIED, record=stored(NOTIFIED), now=LATER, mode="scheduler").action is None
    )  # no clock, no notification


def test_a_failed_call_keeps_the_value_invalidated_until_one_succeeds() -> None:
    failed = stored(NOTIFIED, invalidated=True, status="stale", error="timeout")
    assert one(NOTIFIED, record=failed, mode="scheduler").action == "invalidated"


# -- failure display (FR-018) ----------------------------------------------------------------------------------------


def with_policy(policy: str, refresh: str = "once", **options: str) -> str:
    return deferred_decl(refresh=refresh, on_failure=policy, **options)


STALE = {"status": "stale", "error": "timeout"}  # the last call failed; an earlier good value is kept
NOTHING = {"status": "error", "error": "timeout", "has_value": False, "value": None, "as_of": None}


def test_error_policy_shows_the_field_with_status_error_and_no_value_even_with_a_stale_one_kept() -> None:
    text = with_policy("error")
    for kept in (STALE, NOTHING):
        field = one(text, record=stored(text, **kept))
        assert (field.status, field.include, field.shown) == ("error", True, None)


def test_serve_stale_shows_the_last_good_value_as_stale_and_falls_back_to_error_without_one() -> None:
    text = with_policy("serve_stale")
    field = one(text, record=stored(text, **STALE))
    assert (field.status, field.include, field.shown) == ("stale", True, 712)
    nothing = one(text, record=stored(text, **NOTHING))
    assert (nothing.status, nothing.include, nothing.shown) == ("error", True, None)


def test_absent_policy_leaves_the_field_out_with_status_error() -> None:
    text = with_policy("absent")
    for kept in (STALE, NOTHING):
        field = one(text, record=stored(text, **kept))
        assert (field.status, field.include) == ("error", False)


def test_the_schema_default_applies_when_the_field_names_none_and_the_field_wins_otherwise() -> None:
    plain = deferred_decl()
    schema = schema_of(f'"/score": {plain}', top="deferred_on_failure: serve_stale,")
    record = {"/score": stored(plain, **STALE)}
    assert plan(schema, doc(), record, NOW).fields[0].status == "stale"
    own = with_policy("error")
    schema = schema_of(f'"/score": {own}', top="deferred_on_failure: serve_stale,")
    assert plan(schema, doc(), {"/score": stored(own, **STALE)}, NOW).fields[0].status == "error"


def test_a_failure_is_not_forgotten_but_is_not_asked_again_before_it_is_due() -> None:
    text = with_policy("serve_stale")
    held = stored(text, **STALE)
    assert one(text, record=held).action is None  # once: same inputs, held until invalidated
    assert one(text, record=stored(text, invalidated=True, **STALE)).action == "invalidated"
    assert one(text, doc('{name: "Alicia"}'), held).action is None  # a stale value for other inputs is kept under keep
    assert (
        one(text, doc('{name: "Alicia"}'), stored(text, **NOTHING)).action == "inputs"
    )  # never had a value: new inputs


def test_the_status_carries_the_kind_of_the_failure_and_when_the_value_is_from() -> None:
    from aduku.engine.deferred import statuses

    text = with_policy("serve_stale")
    schema = schema_of(f'"/score": {text}')
    shown = statuses(plan(schema, doc(), {"/score": stored(text, attempts=3, **STALE)}, NOW))[0]
    assert (shown.status, shown.error, shown.attempts, shown.as_of) == ("stale", "timeout", 3, NOW)
    bare = statuses(plan(schema, doc(), {"/score": stored(text, **NOTHING)}, NOW))[0]
    assert (bare.status, bare.error, bare.as_of) == ("error", "timeout", None)


def test_a_stale_value_is_a_value_for_the_fields_that_depend_on_it_but_an_error_is_not() -> None:
    from aduku.engine.deferred import statuses  # noqa: F401

    policy_text = with_policy("serve_stale")
    schema = schema_of(f'"/creditScore": {policy_text}, "/creditLimit": {LIMIT}')
    stale = made(schema, "/creditScore", [("/name", "Alice")], "700", status="stale", error="timeout")
    assert plan(schema, doc(), {"/creditScore": stale}, NOW).fields[1].action == "first"
    strict = schema_of(f'"/creditScore": {with_policy("error")}, "/creditLimit": {LIMIT}')
    stale2 = made(strict, "/creditScore", [("/name", "Alice")], "700", status="stale", error="timeout")
    held = plan(strict, doc(), {"/creditScore": stale2}, NOW).fields[1]
    assert (held.status, held.action, held.reason) == ("pending", None, "upstream")
