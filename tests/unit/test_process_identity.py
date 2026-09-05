from __future__ import annotations

import os

import pytest

from zekam.domain.errors import ValidationFailed
from zekam.infrastructure.process_identity import process_incarnation_token


def test_current_process_token_is_stable_and_dead_pid_is_absent() -> None:
    first = process_incarnation_token(os.getpid())
    assert first is not None and first.startswith("psutil-create-time-micros:")
    assert process_incarnation_token(os.getpid()) == first
    assert process_incarnation_token(2_147_483_647) is None


@pytest.mark.parametrize("value", [None, True, "1", 0, -1, 2_147_483_648])
def test_process_token_rejects_wrong_pid_types_and_bounds(value: object) -> None:
    with pytest.raises(ValidationFailed):
        process_incarnation_token(value)  # type: ignore[arg-type]
