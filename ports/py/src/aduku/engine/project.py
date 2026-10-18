"""P7 (minimal): the response for a document. Pure. Redaction and provenance arrive with later features."""

from __future__ import annotations

from collections.abc import Sequence

from aduku.ion_codec import FrozenStruct
from aduku.model import DocumentResult, ModelError

__all__ = ["project"]


def project(
    document: FrozenStruct,
    model_errors: Sequence[ModelError] = (),
    skipped: Sequence[int] = (),
    conflicts: Sequence[str] = (),
) -> DocumentResult:
    return DocumentResult(
        document=document,
        model_errors=tuple(model_errors),
        status="ok",
        skipped=tuple(skipped),
        conflicts=tuple(conflicts),
    )
