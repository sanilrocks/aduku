"""Feature 006: the new stable error codes, and the SchemaError code of a changed served version."""

from aduku.errors import ErrorCode, SchemaError


def test_new_codes_exist_with_their_names() -> None:
    for name in ("BREAKING_CHANGE_NOT_ACKNOWLEDGED", "SCHEMA_STATE_CHANGED", "NOT_PINNED"):
        assert ErrorCode(name).value == name


def test_a_changed_served_version_is_a_schema_error_with_its_code() -> None:
    error = SchemaError("SCHEMA_VERSION_CHANGED: version 7 differs from the one served: /age, /name")
    assert error.codes == ("SCHEMA_VERSION_CHANGED",)
