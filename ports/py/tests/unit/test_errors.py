"""Error codes and exception types (SPEC 12.5; feature 002 adds COMPUTE_FAULT)."""

from aduku.errors import AdukuError, ComputeFault, ErrorCode, RejectedError


def test_the_compute_codes_exist() -> None:
    assert ErrorCode.COMPUTE_FAULT.value == "COMPUTE_FAULT"
    assert ErrorCode.IMPURE_COMPUTE_FAILED.value == "IMPURE_COMPUTE_FAILED"


def test_a_compute_fault_names_the_macro_and_the_reason_and_is_an_aduku_error() -> None:
    fault = ComputeFault("acme.label", "step budget exceeded")
    assert isinstance(fault, AdukuError) and fault.macro == "acme.label" and fault.reason == "step budget exceeded"
    assert "acme.label" in str(fault) and "step budget exceeded" in str(fault)
    assert fault.code is ErrorCode.COMPUTE_FAULT


def test_a_compute_fault_can_be_a_rejection() -> None:
    rejection = RejectedError(ErrorCode.COMPUTE_FAULT, message=str(ComputeFault("m", "x")))
    assert rejection.code is ErrorCode.COMPUTE_FAULT
