"""P3: apply tombstones. Pure.

During P1 and P2 tombstones stay in the layers so that they can win. Here they are removed: a tombstoned path becomes
absent, and a re-created value loses its reset marker.
"""

from __future__ import annotations

from typing import Any

from aduku.behaviors.merge import unwrap
from aduku.ion_codec import TOMBSTONE
from aduku.paths import Path

__all__ = ["apply_tombstones"]


def apply_tombstones(merged: dict[Path, Any], fallbacks: dict[Path, Any] | None = None) -> dict[Path, Any]:
    """The plain value of every path that still has one.

    `fallbacks` are the defaults of fields that say `on_delete: default`. Such a path takes its default when it, or
    an ancestor, ends up tombstoned and nothing was written to the path afterwards, so deleting a struct falls back
    child by child, each by its own setting.
    """
    values = {path: unwrap(state) for path, state in merged.items()}
    present = {path: value for path, value in values.items() if value is not TOMBSTONE}
    if fallbacks:
        tombstoned = {path for path, value in values.items() if value is TOMBSTONE}
        for path, value in fallbacks.items():
            if path not in present and any(path[:length] in tombstoned for length in range(len(path) + 1)):
                present[path] = value
    return present
