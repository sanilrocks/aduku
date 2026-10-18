"""Values and metadata with no Ion form must be rejected cleanly, never crash a request or split the backends."""

import re
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from aduku.behaviors.registry import BehaviorRegistry
from aduku.engine.admission import check_request as _check_request
from aduku.engine.request_hash import request_hash
from aduku.errors import ErrorCode, RejectedError
from aduku.ion_codec import FrozenStruct, or_placeholder, parse, to_canonical
from aduku.model import EntryInput
from aduku.schema.loader import load_schema
from aduku.schema.model import conforms
from aduku.storage.base import RequestRecord, Storage
from aduku.storage.sqlite import SqliteStorage
from tests.helpers import make_entry
from tests.service_helpers import ALICE, make_service, put, schema_text

AUDIT = "rejection_audit: {enabled: true, mode: full, retention: {max_entries: 50}},"
NON_FINITE = [Decimal("NaN"), Decimal("sNaN"), Decimal("Infinity"), Decimal("-Infinity")]


def check_request(schema, entries, who, lenient):  # type: ignore[no-untyped-def]
    return _check_request(schema, entries, who, lenient, BehaviorRegistry.with_builtins())


@pytest.mark.parametrize("number", NON_FINITE)
def test_non_finite_decimals_never_conform(number: Decimal) -> None:
    assert not conforms("decimal", number)
    assert not conforms("any", number)
    assert not conforms("list", (number,))
    assert not conforms("struct", FrozenStruct({"a": number}))
    assert conforms("decimal", Decimal("1.50"))


@pytest.mark.parametrize("value", [b"raw", object(), *NON_FINITE])
def test_the_placeholder_covers_every_value_without_an_ion_form(value: object) -> None:
    placeholder = or_placeholder(value)
    assert isinstance(placeholder, str) and placeholder.startswith("<") and "no Ion form" in placeholder


def test_meta_must_be_storable() -> None:
    schema = load_schema(schema_text())
    for bad in (b"raw", Decimal("NaN"), FrozenStruct({"k": b"raw"})):
        reasons = check_request(schema, [put(meta=bad)], ALICE, False)
        assert [r.code for r in reasons] == [ErrorCode.TYPE_MISMATCH] and "meta" in reasons[0].message
    assert check_request(schema, [put(meta=FrozenStruct({"ticket": "T-1", "n": (1, 2)}))], ALICE, False) == []


@pytest.mark.parametrize("request_key", [None, "K"])
@pytest.mark.parametrize("bad", ["value", "meta"])
def test_bad_input_is_a_clean_rejection_on_every_backend(backend: Storage, request_key: str | None, bad: str) -> None:
    service = make_service(backend, audit=AUDIT)
    for poison in NON_FINITE + [b"raw"]:
        entry = put("/age", poison) if bad == "value" else put("/age", 1, meta=poison)
        with pytest.raises(RejectedError) as caught:
            service.append("d1", [entry], contributor=ALICE, request_key=request_key)
        assert caught.value.code is ErrorCode.TYPE_MISMATCH
    assert service.list_entries("d1") == []
    assert len(service.list_rejections("d1")) == len(NON_FINITE) + 1  # every rejection was audited, with placeholders


# -- timestamps keep all their fractional digits -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-01-01T10:00:00.123456789Z", "2026-01-01T10:00:00.123456789Z"),
        ("2026-01-01T10:00:00.123456789+01:00", "2026-01-01T09:00:00.123456789Z"),
        ("2026-01-01T10:00:00.5Z", "2026-01-01T10:00:00.5Z"),
        ("2026-01-01T10:00:00.000Z", "2026-01-01T10:00:00.000Z"),
        ("2026-01-01T10:00:00Z", "2026-01-01T10:00:00Z"),
    ],
)
def test_fractional_seconds_are_not_truncated(text: str, expected: str) -> None:
    assert to_canonical(parse(text)) == expected
    assert to_canonical(parse(expected)) == expected


def test_requests_differing_only_beyond_microseconds_have_different_hashes() -> None:
    a = parse("2026-01-01T10:00:00.123456789Z")
    b = parse("2026-01-01T10:00:00.123456788Z")
    one = request_hash("d1", "alice", False, [EntryInput("user", "/when", "set", a)])
    two = request_hash("d1", "alice", False, [EntryInput("user", "/when", "set", b)])
    assert one != two


def test_nanosecond_timestamps_survive_storage(backend: Storage) -> None:
    value = parse("2026-01-01T10:00:00.123456789Z")
    with backend.transaction() as tx:
        tx.append_request(RequestRecord("r1", "d1", "alice"), [make_entry(value=value)])
    with backend.read() as tx:
        stored = tx.entries("d1")[0].value
    assert to_canonical(stored) == "2026-01-01T10:00:00.123456789Z"


# -- the ledger's tx_time column is fixed width, so that text order is time order ---------------------------------------


def test_tx_time_is_fixed_width_utc_text(tmp_path) -> None:  # type: ignore[no-untyped-def]
    storage = SqliteStorage(str(tmp_path / "t.db"))
    moments = [
        datetime(2026, 10, 18, 12, 0, 0, tzinfo=UTC),  # no microseconds
        datetime(2026, 10, 18, 12, 0, 0, 500000, tzinfo=UTC),
        datetime(
            2026, 10, 18, 17, 30, 1, 250000, tzinfo=timezone(timedelta(hours=5, minutes=30))
        ),  # same instant + 0.25s
    ]
    entries = []
    for seq, moment in enumerate(moments, start=1):
        entry = make_entry(seq=seq, request_id="r1")
        entries.append(
            type(entry)(**{**entry.__dict__, "tx_time": moment, "id": f"e{seq}"})  # a copy with another time
        )
    with storage.transaction() as tx:
        tx.append_request(RequestRecord("r1", "d1", "alice"), entries)
    stored = [row[0] for row in storage.connection.execute("SELECT tx_time FROM entries ORDER BY seq")]
    assert all(re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z", text) for text in stored), stored
    assert stored == sorted(stored)  # text order is time order
    with storage.read() as tx:
        assert [e.tx_time for e in tx.entries("d1")] == moments  # same instants back
    storage.close()


# -- symbol ids are not names ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["$10", "$0", "$99999"])
def test_a_name_that_looks_like_a_symbol_id_is_quoted_and_round_trips(name: str) -> None:
    from aduku.ion_codec import Symbol, normalize

    assert normalize(Symbol(name)) == Symbol(name)
    assert normalize(FrozenStruct({name: 1})) == FrozenStruct({name: 1})
    assert to_canonical(FrozenStruct({name: 1})) == f"{{'{name}':1}}"


def test_a_value_the_codec_cannot_write_back_is_rejected_not_a_crash(backend: Storage, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import aduku.service as service_module
    from aduku.ion_codec import IonError

    real = service_module.normalize

    def failing(value):  # type: ignore[no-untyped-def]
        if value == "boom":
            raise IonError("no canonical form")
        return real(value)

    monkeypatch.setattr(service_module, "normalize", failing)
    service = make_service(backend)
    with pytest.raises(RejectedError) as caught:
        service.append("d1", [put("/name", "boom")], contributor=ALICE)
    assert caught.value.code.value == "TYPE_MISMATCH" and service.list_entries("d1") == []
