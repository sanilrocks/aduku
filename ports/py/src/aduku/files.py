"""Reading schema files: the one place the schema code reaches the filesystem, kept outside `schema/` (Principle III)."""

from __future__ import annotations

from pathlib import PurePath

from aduku.errors import SchemaError

__all__ = ["read_schema_text"]


def read_schema_text(path: str | PurePath) -> str:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError as error:
        raise SchemaError(f"cannot read schema: {error}") from error
