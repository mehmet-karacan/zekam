"""Authority-free control-plane port for observations after a frozen session."""

from __future__ import annotations

from typing import Any, Protocol

from zekam.application.local_continuity import ContinuityBinding


class LocalContinuityControl(Protocol):
    def is_frozen(self, binding: ContinuityBinding) -> bool: ...

    def drain(self, binding: ContinuityBinding) -> int:
        """Account the real contiguous spool, committing rejection before raising."""
        ...

    def inspect(self, binding: ContinuityBinding) -> dict[str, Any]: ...
