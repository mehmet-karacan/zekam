"""Worker handler registry fail-closed kurallari."""

from __future__ import annotations

from uuid import uuid4

import pytest

from zekam.application.worker import WorkerSettings, build_worker, noop_handler, resolve_handlers
from zekam.domain.errors import PolicyViolation
from zekam.domain.runtime import JobKind

pytestmark = pytest.mark.unit


def test_unknown_handler_is_rejected_at_registry_resolution() -> None:
    with pytest.raises(PolicyViolation, match="handler tanimsiz"):
        resolve_handlers([str(JobKind.READ_ONLY)])


def test_explicit_test_handler_can_be_resolved() -> None:
    name = str(JobKind.READ_ONLY)
    assert resolve_handlers([name], registry={name: noop_handler}) == {name: noop_handler}


def test_unrequested_registry_entries_are_not_exposed() -> None:
    requested = str(JobKind.READ_ONLY)
    extra = str(JobKind.MUTATION)
    resolved = resolve_handlers(
        [requested], registry={requested: noop_handler, extra: noop_handler}
    )
    assert set(resolved) == {requested}


def test_worker_cannot_start_without_an_explicit_handler() -> None:
    settings = WorkerSettings(worker_label="test", capabilities=("read",))
    with pytest.raises(PolicyViolation, match="explicit handler"):
        build_worker(object(), uuid4(), settings=settings, handlers={})
