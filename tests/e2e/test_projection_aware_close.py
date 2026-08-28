from __future__ import annotations

from types import SimpleNamespace

from typer.testing import CliRunner

from zekam.domain.canonical import digest
from zekam.interfaces.cli import close as close_commands
from zekam.interfaces.cli.main import app


def test_root_close_plan_is_authority_free_and_apply_is_separate(
    monkeypatch, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    input_file = tmp_path / "close.json"
    input_file.write_text("{}", encoding="utf-8")
    body = {
        "schema": "zekam-projection-aware-close-plan/v1",
        "grants_authority": False,
        "requires_authorization": True,
    }
    fake = SimpleNamespace(
        body=lambda: body,
        plan_digest=digest("plan"),
        resource="work:project:work:projection-close:run",
        effect_digest=digest("effect"),
    )
    monkeypatch.setattr(close_commands, "_plan", lambda **_: fake)

    result = CliRunner().invoke(
        app,
        [
            "close",
            "plan",
            "--girdi",
            str(input_file),
            "--idempotency-key",
            "projection-close:e2e",
        ],
    )

    assert result.exit_code == 0
    assert '"grants_authority": false' in result.stdout
    assert '"one_shot": true' in result.stdout


def test_root_close_apply_requires_explicit_apply_before_database(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    input_file = tmp_path / "close.json"
    input_file.write_text("{}", encoding="utf-8")
    result = CliRunner().invoke(
        app,
        [
            "close",
            "apply",
            "--girdi",
            str(input_file),
            "--idempotency-key",
            "projection-close:e2e",
            "--plan-digest",
            digest("plan"),
            "--authorization-id",
            "00000000-0000-0000-0000-000000000001",
            "--claim-id",
            "00000000-0000-0000-0000-000000000002",
        ],
    )

    assert result.exit_code == 64
    assert "--uygula" in result.stdout


def test_root_close_apply_replays_terminal_chain_before_prepare(
    monkeypatch, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    input_file = tmp_path / "close.json"
    input_file.write_text("{}", encoding="utf-8")
    calls: list[str] = []
    replay = SimpleNamespace(
        as_dict=lambda: {
            "schema": "zekam-projection-aware-close-apply-receipt/v1",
            "replayed": True,
            "next_safe_action": None,
        }
    )

    class Session:
        def __init__(self, *_args, **_kwargs) -> None:  # type: ignore[no-untyped-def]
            pass

        def __enter__(self):  # type: ignore[no-untyped-def]
            return object()

        def __exit__(self, *_args):  # type: ignore[no-untyped-def]
            return None

    class Service:
        def replay_completed(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            calls.append("replay")
            return replay

        def prepare(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            calls.append("prepare")
            raise AssertionError("terminal replay prepare yapmamali")

    monkeypatch.setattr(close_commands, "RealmSession", Session)
    monkeypatch.setattr(close_commands, "_session_close_receipt", lambda _path: object())
    monkeypatch.setattr(close_commands, "_service", lambda _context: Service())

    result = CliRunner().invoke(
        app,
        [
            "close",
            "apply",
            "--girdi",
            str(input_file),
            "--idempotency-key",
            "projection-close:e2e-replay",
            "--plan-digest",
            digest("plan"),
            "--authorization-id",
            "00000000-0000-0000-0000-000000000001",
            "--claim-id",
            "00000000-0000-0000-0000-000000000002",
            "--uygula",
        ],
    )

    assert result.exit_code == 0
    assert '"replayed": true' in result.stdout
    assert calls == ["replay"]
