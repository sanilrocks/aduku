"""contracts/cli.md: append, entries and audit commands (exit 1 for rejected requests)."""

import json
import pathlib

import pytest

from aduku.cli import main

SCHEMA = """schema::{id: "t", version: 1, tiers: [backfill, user, admin],
  rejection_audit: {enabled: true, mode: full, retention: {max_entries: 10}},
  fields: {"/name": {type: string, merge: lww}, "/age": {type: int, merge: max}}}"""


@pytest.fixture
def env(tmp_path: pathlib.Path) -> list[str]:
    schema = tmp_path / "t.schema.ion"
    schema.write_text(SCHEMA)
    return ["--db", str(tmp_path / "cli.db"), "--schema", str(schema)]


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict, str]:  # type: ignore[type-arg]
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, (json.loads(captured.out) if captured.out.strip() else {}), captured.err


def test_append_and_list_entries(env: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(
        capsys,
        *env,
        "append",
        "d1",
        "--contributor",
        "alice",
        "--tier",
        "user",
        "--set",
        '/name="Alice"',
        "--set",
        "/age=30",
    )
    assert code == 0 and [e["seq"] for e in out["entries"]] == [1, 2] and out["duplicate"] is False
    code, out, _ = run(capsys, *env, "entries", "d1")
    assert code == 0
    assert [(e["seq"], e["path"], e["op"]) for e in out["entries"]] == [(1, "/name", "set"), (2, "/age", "set")]
    assert out["entries"][0]["value"] == '"Alice"'
    code, out, _ = run(capsys, *env, "entries", "d1", "--since", "1")
    assert [e["seq"] for e in out["entries"]] == [2]


def test_delete_and_request_key(env: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    args = [*env, "append", "d1", "--contributor", "alice", "--tier", "user", "--request-key", "K", "--delete", "/name"]
    code, first, _ = run(capsys, *args)
    code2, again, _ = run(capsys, *args)
    assert code == code2 == 0 and first["duplicate"] is False and again["duplicate"] is True
    assert first["entries"] == again["entries"]
    _, out, _ = run(capsys, *env, "entries", "d1")
    assert [e["op"] for e in out["entries"]] == ["delete"]


def test_rejected_request_exits_1_with_every_reason(env: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = run(
        capsys,
        *env,
        "append",
        "d1",
        "--contributor",
        "alice",
        "--tier",
        "user",
        "--set",
        "/nope=1",
        "--set",
        '/age="x"',
    )
    assert code == 1
    assert out["rejected"]["code"] == "UNKNOWN_PATH"
    assert [r["code"] for r in out["rejected"]["reasons"]] == ["UNKNOWN_PATH", "TYPE_MISMATCH"]
    assert "UNKNOWN_PATH" in err
    _, entries, _ = run(capsys, *env, "entries", "d1")
    assert entries["entries"] == []


def test_audit_commands(env: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    run(capsys, *env, "append", "d1", "--contributor", "alice", "--tier", "user", "--set", "/nope=1")
    code, out, _ = run(capsys, *env, "audit", "list", "d1")
    assert code == 0 and len(out["records"]) == 1 and out["records"][0]["reasons"][0]["code"] == "UNKNOWN_PATH"
    code, out, _ = run(capsys, *env, "audit", "prune", "d1")
    assert code == 0 and out["pruned"] == 0
    code, out, _ = run(capsys, *env, "audit", "set", "d1", "disabled")
    assert code == 0
    run(capsys, *env, "append", "d1", "--contributor", "alice", "--tier", "user", "--set", "/nope=2")
    _, out, _ = run(capsys, *env, "audit", "list", "d1")
    assert len(out["records"]) == 1


def test_environment_fallbacks(
    env: list[str], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ADUKU_DB", env[1])
    monkeypatch.setenv("ADUKU_SCHEMA", env[3])
    code, out, _ = run(capsys, "append", "d1", "--contributor", "alice", "--tier", "user", "--set", '/name="A"')
    assert code == 0 and out["entries"][0]["seq"] == 1


def test_usage_errors_exit_2(env: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = run(capsys, *env, "append", "d1", "--contributor", "alice", "--tier", "user")  # nothing to do
    assert code == 2 and err
    code, _, _ = run(capsys, "--db", env[1], "entries", "d1")  # no schema anywhere
    assert code == 2


def test_schema_check_ok(capsys: pytest.CaptureFixture[str]) -> None:
    customer = str(pathlib.Path(__file__).resolve().parents[4] / "conformance" / "customer.schema.ion")
    code, out, _ = run(capsys, "schema", "check", customer)
    assert code == 0 and out["ok"] is True and out["id"] == "customer" and out["version"] == 7
    assert out["merge_rules"]["lww"] >= 5 and out["derived"] == ["/displayName"]


def test_schema_check_reports_every_problem_with_codes(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "bad.schema.ion"
    bad.write_text(
        'schema::{id: "t", version: 1, tiers: [a], fields: {"/x": {type: bool, merge: max}, "/y": {merge: nope}}}'
    )
    code, out, err = run(capsys, "schema", "check", str(bad))
    assert code == 2 and out["ok"] is False
    assert set(out["codes"]) >= {"MERGE_TYPE_UNSUITABLE", "UNKNOWN_BEHAVIOR"} and "MERGE_TYPE_UNSUITABLE" in err
    code, _, _ = run(capsys, "schema", "check", str(tmp_path / "missing.ion"))
    assert code == 2


@pytest.mark.parametrize(
    ("text", "path", "value"),
    [
        ("/name=1", "/name", 1),
        ('/name="a=b"', "/name", "a=b"),
        ('/items[sku="A1"]/qty=5', '/items[sku="A1"]/qty', 5),
        ('/items[name="a]b"]/qty=5', '/items[name="a]b"]/qty', 5),
        ("/items[name='x=y']/qty=5", "/items[name='x=y']/qty", 5),
        ('/items[name="a[b"]/qty=5', '/items[name="a[b"]/qty', 5),
    ],
)
def test_assignments_split_at_the_first_equals_outside_a_key(text: str, path: str, value: object) -> None:
    from aduku.cli import _split_assignment

    assert _split_assignment(text) == (path, value)


def test_roles_can_be_passed_for_role_based_authority(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    schema = tmp_path / "roles.schema.ion"
    schema.write_text(
        'schema::{id: "t", version: 1, tiers: [user], roles: [ops],'
        ' fields: {"/limit": {type: int, merge: lww, authority: [ops]}}}'
    )
    base = [
        "--db",
        str(tmp_path / "r.db"),
        "--schema",
        str(schema),
        "append",
        "d1",
        "--contributor",
        "svc",
        "--tier",
        "user",
    ]
    code, out, _ = run(capsys, *base, "--set", "/limit=5")
    assert code == 1 and out["rejected"]["code"] == "NOT_AUTHORIZED"
    code, out, _ = run(capsys, *base, "--role", "ops", "--set", "/limit=5")
    assert code == 0 and out["entries"][0]["seq"] == 1


def test_a_ledger_with_a_tier_the_schema_lacks_is_refused_at_startup(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str], env: list[str]
) -> None:
    from aduku.storage.base import RequestRecord
    from aduku.storage.sqlite import SqliteStorage
    from tests.helpers import make_entry

    storage = SqliteStorage(env[1])
    with storage.transaction() as tx:  # written under an older schema that had a tier this one lacks
        tx.append_request(
            RequestRecord("old", "d1", "x"),
            [make_entry(seq=1, request_id="old", tier="legacy", path="/name", value="old")],
        )
    storage.close()
    code, _, err = run(capsys, *env, "get", "d1")
    assert code == 2 and "TIER_IN_USE" in err  # FR-009: a schema that drops a used tier cannot be the active one


def test_rebuild_reports_identical_and_exits_zero(env: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    for doc in ("d1", "d2"):
        run(capsys, *env, "append", doc, "--contributor", "alice", "--tier", "user", "--set", '/name="A"')
    code, out, _ = run(capsys, *env, "rebuild")
    assert code == 0 and out["identical"] is True and out["checked"] == ["d1", "d2"] and out["differing"] == []
    code, out, _ = run(capsys, *env, "rebuild", "d1")
    assert code == 0 and out["checked"] == ["d1"]


def test_explain_names_the_winner_and_the_rule(env: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    for age in (10, 30, 20):
        run(capsys, *env, "append", "d1", "--contributor", "alice", "--tier", "user", "--set", f"/age={age}")
    code, out, _ = run(capsys, *env, "explain", "d1", "/age")
    assert code == 0 and (out["value"], out["rule"], out["winner_seq"]) == ("30", "max@1", 2)
    assert len(out["shadowed"]) == 2 and out["inputs"] == [out["winner"]]
    code, _, err = run(capsys, *env, "explain", "d1", "/nope")
    assert code == 2 and "does not declare" in err  # not a rejected request: a usage error


# -- feature 002: computed fields, impure computes, effects ---------------------------------------------------------

DEMO = str(pathlib.Path(__file__).resolve().parents[2] / "examples")
IMPURE_SCHEMA = """schema::{id: "t", version: 1, tiers: [backfill, user, admin],
  fields: {
    "/country": {type: string, merge: lww}, "/name": {type: string, merge: lww}, "/city": {type: string, merge: lww},
    "/label": {type: string, computed: {macro: "acme.label@1", args: ["/name", "/city"]}},
    "/taxId": {type: string, computed: {impure: {hook: "fx.echo@1", depends: ["/country"], reason: "tax id from the registry",
              timeout: "1s", retry: {max: 1, backoff: "10ms"}, deadline: "3s", recompute_by: [admin]}}}}}"""


@pytest.fixture
def impure_env(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    monkeypatch.syspath_prepend(DEMO)
    schema = tmp_path / "impure.schema.ion"
    schema.write_text(IMPURE_SCHEMA)
    return ["--db", str(tmp_path / "cli.db"), "--schema", str(schema), "--behaviors", "demo_hooks"]


def test_schema_check_reports_the_computed_fields_and_the_impure_inventory(
    impure_env: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = run(capsys, *impure_env, "schema", "check", impure_env[3])
    assert code == 0 and out["ok"] is True
    assert [c["field"] for c in out["computed"]] == ["/label"] and out["computed"][0]["macro"] == "acme.label@1"
    (inventory,) = out["impure"]
    assert inventory["field"] == "/taxId" and inventory["hook"] == "fx.echo@1"
    assert inventory["reason"] == "tax id from the registry" and inventory["recompute_by"] == ["admin"]
    assert (inventory["timeout"], inventory["retry"], inventory["deadline"]) == ("1s", 1, "3s") or inventory[
        "timeout"
    ] == 1.0


def test_a_schema_that_needs_behaviors_is_refused_without_them(
    impure_env: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    without = [impure_env[0], impure_env[1], impure_env[2], impure_env[3]]
    code, _, err = run(capsys, *without, "get", "d1")
    assert code == 2 and "UNKNOWN_BEHAVIOR" in err


def test_a_behaviors_module_that_cannot_be_imported_is_a_usage_error(
    impure_env: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    broken = [*impure_env[:4], "--behaviors", "no_such_module_anywhere"]
    code, _, err = run(capsys, *broken, "get", "d1")
    assert code == 2 and "no_such_module_anywhere" in err


def test_computed_values_the_hook_call_the_effect_log_and_explain(
    impure_env: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = run(
        capsys, *impure_env, "append", "d1", "--contributor", "pat", "--tier", "user",
        "--set", '/country="US"', "--set", '/name="Alice S."', "--set", '/city="Paris"',
    )  # fmt: skip
    assert code == 0
    code, out, _ = run(capsys, *impure_env, "get", "d1")
    assert 'label:computed::"Alice S. (Paris)"' in out["document"] and 'taxId:computed::"tax-US"' in out["document"]
    code, out, _ = run(capsys, *impure_env, "effects", "list", "d1")
    (record,) = out["effects"]
    assert (record["field"], record["hook"], record["outcome"], record["attempts"]) == (
        "/taxId",
        "fx.echo@1",
        "value",
        1,
    )
    assert "value" not in record and "error" not in record  # metadata only
    code, out, _ = run(capsys, *impure_env, "explain", "d1", "/taxId")
    assert out["winner"] == "computed" and out["rule"] == "fx.echo@1" and out["effect_seq"] == record["effect_seq"]
    assert out["dependencies"] == [{"path": "/country", "value": '"US"'}]
    code, out, _ = run(capsys, *impure_env, "effects", "prune", "d1")
    assert code == 0 and out["pruned"] == 0
    code, out, _ = run(capsys, *impure_env, "rebuild", "d1")
    assert code == 0 and out["identical"] is True
    code, out, _ = run(capsys, *impure_env, "effects", "list", "d1")
    assert len(out["effects"]) == 1  # reading and rebuilding made no further call


def test_recompute_through_the_cli(impure_env: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    run(capsys, *impure_env, "append", "d1", "--contributor", "pat", "--tier", "user", "--set", '/country="US"')
    code, out, _ = run(capsys, *impure_env, "recompute", "d1", "/taxId", "--contributor", "root", "--tier", "admin")
    assert code == 0 and out["recomputed"] == ["/taxId"] and out["failed"] == [] and out["skipped"] == []
    assert [s["field"] for s in out["effect_seqs"]] == ["/taxId"] and out["effect_seqs"][0]["effect_seq"] == 2
    code, out, _ = run(capsys, *impure_env, "effects", "list", "d1")
    assert [e["outcome"] for e in out["effects"]] == ["value", "value"]  # the recompute added a record


def test_recompute_by_someone_the_schema_does_not_list_exits_1(
    impure_env: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    run(capsys, *impure_env, "append", "d1", "--contributor", "pat", "--tier", "user", "--set", '/country="US"')
    code, out, err = run(capsys, *impure_env, "recompute", "d1", "/taxId", "--contributor", "pat", "--tier", "user")
    assert code == 1 and out["rejected"]["code"] == "NOT_AUTHORIZED" and "NOT_AUTHORIZED" in err
    code, out, _ = run(capsys, *impure_env, "effects", "list", "d1")
    assert len(out["effects"]) == 1


def test_recompute_of_a_field_that_cannot_be_called_exits_1(
    impure_env: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = run(capsys, *impure_env, "recompute", "d1", "/taxId", "--contributor", "root", "--tier", "admin")
    assert code == 1 and out["failed"] == [{"field": "/taxId", "kind": "missing_input"}]
