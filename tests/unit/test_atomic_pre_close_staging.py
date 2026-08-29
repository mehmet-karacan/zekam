from __future__ import annotations

from pathlib import Path

import pytest

from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation
from zekam.infrastructure.postgres.client_lifecycle_repository import (
    _terminal_delivery_digest,
)

ROOT = Path(__file__).resolve().parents[2]


def test_pre_close_does_not_finalize_lifecycle_outbox_with_hook_digest() -> None:
    source = (ROOT / "src/zekam/application/client_lifecycle_continuity.py").read_text(
        encoding="utf-8"
    )

    assert 'if entry.internal_event_type != "pre_close":' in source
    assert "finalize_lifecycle_delivery(" in source


def test_terminal_lookup_accepts_only_open_pre_close_or_terminal_other_event() -> None:
    source = (
        ROOT / "src/zekam/infrastructure/postgres/client_lifecycle_repository.py"
    ).read_text(encoding="utf-8")

    assert source.count("event_type='pre_close'") >= 2
    assert source.count("outbox.terminal_receipt_digest is null") >= 2
    assert source.count("event_type<>'pre_close'") >= 2


def test_pre_close_terminal_binding_uses_hook_receipt_while_outbox_is_open() -> None:
    hook_digest = digest("pre-close-hook")

    assert (
        _terminal_delivery_digest(
            event_type="pre_close",
            outbox_terminal_digest=None,
            hook_output_digest=hook_digest,
        )
        == hook_digest
    )

    assert (
        _terminal_delivery_digest(
            event_type="pre_close",
            outbox_terminal_digest=digest("atomic-close"),
            hook_output_digest=hook_digest,
            pre_close_terminal_bound=True,
        )
        == hook_digest
    )
    with pytest.raises(PolicyViolation, match="binding drift"):
        _terminal_delivery_digest(
            event_type="pre_close",
            outbox_terminal_digest=digest("foreign-close"),
            hook_output_digest=hook_digest,
            pre_close_terminal_bound=False,
        )


def test_non_pre_close_requires_exact_terminal_outbox_digest() -> None:
    hook_digest = digest("ordinary-hook")

    assert (
        _terminal_delivery_digest(
            event_type="post_task",
            outbox_terminal_digest=hook_digest,
            hook_output_digest=hook_digest,
        )
        == hook_digest
    )
    with pytest.raises(PolicyViolation, match="eksik"):
        _terminal_delivery_digest(
            event_type="post_task",
            outbox_terminal_digest=None,
            hook_output_digest=hook_digest,
        )
    with pytest.raises(PolicyViolation, match="drift"):
        _terminal_delivery_digest(
            event_type="post_task",
            outbox_terminal_digest=digest("other"),
            hook_output_digest=hook_digest,
        )


def test_migration_73_is_exactly_reversible_and_refuses_live_staged_down() -> None:
    up = (ROOT / "migrations/0073_atomic_pre_close_staging.sql").read_text(
        encoding="utf-8"
    )
    down = (ROOT / "migrations/0073_atomic_pre_close_staging.down.sql").read_text(
        encoding="utf-8"
    )

    assert "pg_get_functiondef" in up and "baseline drift" in up
    assert "legacy terminal pre-close lacks close receipt" in up
    assert "close_receipt.close_status='closed'" in up
    assert "completion.operation='projection-aware-close'" in up
    assert "staged pre-close outbox exists" in down
    assert "terminal_receipt_digest is null" in up and "completed_at is null" in up
