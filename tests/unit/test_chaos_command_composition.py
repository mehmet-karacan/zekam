from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from zekam.application.chaos_command_composition import (
    ChaosCommandDriver,
    canonical_zekam_source_root,
    compose_command_chaos_handler,
)
from zekam.domain.errors import PolicyViolation


def test_command_driver_shellsiz_exact_operation_calistirir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run(argv, **kwargs):  # type: ignore[no-untyped-def]
        captured.update({"argv": argv, **kwargs})
        return subprocess.CompletedProcess(argv, 0, stdout=b'{"current":true}', stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = ChaosCommandDriver(("driver.exe", "--isolated")).call(
        "authorize-verify", {"authorization": {"id": "a1"}}
    )
    assert result == {"current": True}
    assert captured["argv"] == ("driver.exe", "--isolated", "authorize-verify")
    assert captured["shell"] is False
    assert captured["check"] is False


def test_artifact_root_canonical_source_altinda_olamaz(tmp_path: Path) -> None:
    source_root = canonical_zekam_source_root()
    config = tmp_path / "chaos.json"
    config.write_text(
        json.dumps(
            {
                "driver_argv": ["injector.exe"],
                "verifier_argv": ["verifier.exe"],
                "artifact_root": str(source_root / "forbidden-chaos-output"),
                "realm_id": "chaos-realm",
                "source_revision": "abc123",
                "verifier_identity": "verifier/v1",
                "campaign_id": "campaign-1",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(PolicyViolation, match="source repository"):
        compose_command_chaos_handler(config)
    assert not (source_root / "forbidden-chaos-output").exists()


def test_cli_env_configured_handleri_scheduler_registrye_baglar(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from zekam.interfaces.cli import worker as cli_worker

    config = tmp_path / "chaos.json"
    config.write_text("{}", encoding="utf-8")
    marker = lambda now: "chaos-ok"  # noqa: E731
    seen: list[Path] = []
    monkeypatch.setattr(cli_worker, "compose_diagnostic_trace_purge_handler", lambda **_: None)

    def fake_compose(path: Path):
        seen.append(path)
        return marker

    monkeypatch.setattr(cli_worker, "compose_command_chaos_handler", fake_compose)
    monkeypatch.setenv("ZEKAM_CHAOS_DRIVER_CONFIG", str(config))
    context = SimpleNamespace(connection=None, realm_id="realm-1")
    handlers = cli_worker._scheduled_handlers(context, str(tmp_path))
    assert handlers == {"chaos-campaign": marker}
    assert seen == [config]
