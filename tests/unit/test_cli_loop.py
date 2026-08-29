from __future__ import annotations

from collections.abc import Callable
from uuid import UUID

import pytest
from typer.testing import CliRunner

from zekam.domain.canonical import digest
from zekam.interfaces.cli import loop as loop_commands

LOOP_ID = UUID("00000000-0000-0000-0000-000000000001")
WORK_ID = UUID("00000000-0000-0000-0000-000000000002")
runner = CliRunner()


class FakeService:
    def plan(self, loop_id: UUID) -> dict[str, object]:
        return {"operation": "plan", "loop_id": str(loop_id)}

    def status(self, loop_id: UUID, *, limit: int) -> dict[str, object]:
        return {"operation": "status", "loop_id": str(loop_id), "limit": limit}

    def attempts(self, loop_id: UUID, *, limit: int) -> dict[str, object]:
        return {"operation": "attempts", "loop_id": str(loop_id), "limit": limit}

    def progress(self, loop_id: UUID, *, limit: int) -> dict[str, object]:
        return {"operation": "progress", "loop_id": str(loop_id), "limit": limit}

    def assess(self, work_item_id: UUID, *, limit: int) -> dict[str, object]:
        return {"operation": "assess", "work_item_id": str(work_item_id), "limit": limit}

    def graph(self, work_item_id: UUID, *, limit: int) -> dict[str, object]:
        return {"operation": "graph", "work_item_id": str(work_item_id), "limit": limit}

    def tournament(self, work_item_id: UUID, *, limit: int) -> dict[str, object]:
        return {"operation": "tournament", "work_item_id": str(work_item_id), "limit": limit}

    def ablation(self, work_item_id: UUID, *, limit: int) -> dict[str, object]:
        return {"operation": "ablation", "work_item_id": str(work_item_id), "limit": limit}


def test_all_loop_cli_commands_are_read_only_and_dispatch_without_apply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    documents: list[dict[str, object]] = []

    def fake_read(
        operation: Callable[[FakeService], dict[str, object]],
        *,
        home: str | None,
        realm: str,
        as_json: bool,
    ) -> None:
        assert home is None
        assert realm == "yerel"
        assert as_json is True
        documents.append(operation(FakeService()))

    monkeypatch.setattr(loop_commands, "_read", fake_read)

    loop_commands.plan(LOOP_ID, True, "yerel", None)
    loop_commands.status(LOOP_ID, 7, True, "yerel", None)
    loop_commands.attempts(LOOP_ID, 8, True, "yerel", None)
    loop_commands.progress(LOOP_ID, 9, True, "yerel", None)
    loop_commands.assess(WORK_ID, 10, True, "yerel", None)
    loop_commands.topology(WORK_ID, 11, True, "yerel", None)
    loop_commands.graph(WORK_ID, 12, True, "yerel", None)
    loop_commands.tournament(WORK_ID, 13, True, "yerel", None)
    loop_commands.ablation(WORK_ID, 14, True, "yerel", None)

    assert [item["operation"] for item in documents] == [
        "plan",
        "status",
        "attempts",
        "progress",
        "assess",
        "assess",
        "graph",
        "tournament",
        "ablation",
    ]
    assert all("apply" not in item for item in documents)


def test_loop_cli_read_surfaces_do_not_advertise_mutation_switch() -> None:
    for command in (
        "assess",
        "plan",
        "status",
        "attempts",
        "progress",
        "topology",
        "graph",
        "tournament",
        "ablation",
    ):
        result = runner.invoke(loop_commands.app, [command, "--help"])
        assert result.exit_code == 0
        assert "--uygula" not in result.stdout
        assert "--apply" not in result.stdout


def test_loop_control_is_the_only_explicit_mutation_surface() -> None:
    result = runner.invoke(loop_commands.app, ["control", "--help"])

    assert result.exit_code == 0
    assert "--uygula" in result.stdout
    assert "--authorization-id" in result.stdout
    assert "--reason-digest" in result.stdout


def test_loop_control_apply_without_exact_authorization_fails_before_database_access() -> None:
    result = runner.invoke(
        loop_commands.app,
        [
            "control",
            str(LOOP_ID),
            "--state",
            "paused",
            "--reason-digest",
            digest("reviewed-pause"),
            "--uygula",
            "--json",
        ],
    )

    assert result.exit_code == 64
    assert "exact --authorization-id ister" in result.stderr


def test_loop_control_rejects_unknown_state_and_non_digest_reason() -> None:
    unknown = runner.invoke(
        loop_commands.app,
        [
            "control",
            str(LOOP_ID),
            "--state",
            "running",
            "--reason-digest",
            digest("reason"),
        ],
    )
    assert unknown.exit_code == 2

    invalid_digest = runner.invoke(
        loop_commands.app,
        [
            "control",
            str(LOOP_ID),
            "--state",
            "paused",
            "--reason-digest",
            "plain-text-reason",
        ],
    )
    assert invalid_digest.exit_code != 0
