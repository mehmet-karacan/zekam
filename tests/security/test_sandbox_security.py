"""P10 sandbox ve adapter guvenlik sinirlari.

Sandbox'in amaci "iyi niyetli builder'a yardim etmek" degil, kotu veya hatali
builder'i **durdurmaktir**. Bu testler kacis yollarini dener.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from zekam.domain.canonical import digest
from zekam.domain.clients import (
    CanonicalDispatchPermit,
    ClientDescriptor,
    ClientKind,
    DispatchRequest,
    _issue_canonical_dispatch_permit,
)
from zekam.domain.errors import PolicyViolation
from zekam.domain.sandbox import (
    NetworkPolicy,
    PatchArtifact,
    PathAllowlist,
    ProcessSpec,
    SandboxPolicy,
    WorkspaceSpec,
)
from zekam.infrastructure.clients.adapters import (
    ClientRegistry,
    SubprocessClientAdapter,
    claude_code_adapter,
    codex_adapter,
    opencode_adapter,
)
from zekam.infrastructure.process import runner

pytestmark = pytest.mark.security

NOW = dt.datetime(2026, 8, 20, tzinfo=dt.UTC)


@pytest.mark.parametrize(
    "path",
    [
        "../../../etc/passwd",
        "/etc/shadow",
        "C:/Windows/System32/config/SAM",
        "src/../../disari.py",
        "src\\zekam\\gizli.py",
    ],
)
def test_allowlist_kacis_denemeleri_reddedilir(path: str) -> None:
    allowlist = PathAllowlist(("src/zekam",))
    with pytest.raises(PolicyViolation):
        allowlist.assert_permits(path)


def test_network_izin_verilmeden_acilmaz() -> None:
    policy = SandboxPolicy(allowlist=PathAllowlist(("src",)))
    assert policy.network.is_default_deny is True
    document = policy.as_dict()
    assert document["network"]["allowed_hosts"] == []
    assert document["main_tree_read_only"] is False
    assert document["direct_source_write"] is True
    assert document["project_copy"] is False


def test_workspace_policy_digest_degisikligi_yakalar() -> None:
    first = WorkspaceSpec(
        workspace_id="w",
        project_ref="p",
        work_ref="w1",
        source_revision="rev",
        policy=SandboxPolicy(allowlist=PathAllowlist(("src",))),
    )
    second = WorkspaceSpec(
        workspace_id="w",
        project_ref="p",
        work_ref="w1",
        source_revision="rev",
        policy=SandboxPolicy(
            allowlist=PathAllowlist(("src",)),
            network=NetworkPolicy(
                allowed_hosts=frozenset({"ornek.org"}), allowed_operations=frozenset({"GET"})
            ),
        ),
    )
    assert first.spec_digest != second.spec_digest


def test_yama_izinsiz_dosyayi_gizleyemez() -> None:
    artifact = PatchArtifact(
        artifact_id="a",
        workspace_id="w",
        base_revision="rev",
        changed_paths=("src/zekam/ok.py", ".github/workflows/deploy.yml"),
        patch_digest=digest("patch"),
        created_at=NOW,
    )
    with pytest.raises(PolicyViolation):
        artifact.assert_within(PathAllowlist(("src/zekam",)))


def test_process_calisma_dizini_yoksa_calismaz(tmp_path: Path) -> None:
    with pytest.raises(PolicyViolation):
        runner.run(
            ProcessSpec(argv=(sys.executable, "-c", "pass")),
            cwd=tmp_path / "olmayan",
        )


def test_process_ortami_devralinmaz(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Cagiran surecin butun ortami alt surece gecmemelidir."""

    monkeypatch.setenv("ZEKAM_GIZLI_ORNEK", "sizmamali")
    environment = runner.build_env(ProcessSpec(argv=(sys.executable,)))
    assert "ZEKAM_GIZLI_ORNEK" not in environment
    assert set(environment) <= set(runner.INHERITED_ENV_NAMES)


def test_calistirilabilir_alan_komut_satiri_olamaz() -> None:
    for executable in ("sh -c 'rm -rf /'", "python; rm", "  python", "cmd | tee"):
        with pytest.raises(PolicyViolation):
            ProcessSpec(argv=(executable, "--version"))


def test_argv_satir_sonu_tasiyamaz() -> None:
    with pytest.raises(PolicyViolation):
        ProcessSpec(argv=("python", "-c", "print(1)\nimport os"))


def test_kayitli_olmayan_istemci_turetilmez() -> None:
    registry = ClientRegistry((codex_adapter("codex.exe"),))
    assert registry.get("codex").descriptor.kind is ClientKind.CODEX
    with pytest.raises(PolicyViolation):
        registry.get("bilinmeyen-istemci")


def test_yetenek_sorgusu_gercek_beyana_dayanir() -> None:
    registry = ClientRegistry(
        (
            codex_adapter("codex.exe"),
            claude_code_adapter("claude.exe"),
            opencode_adapter("opencode.exe"),
        )
    )
    parallel = registry.with_capability("parallel-dispatch")
    assert [item.descriptor.client_id for item in parallel] == ["claude-code", "opencode"]
    selection = registry.with_capability("model-selection")
    assert [item.descriptor.client_id for item in selection] == ["opencode"]


def test_structured_result_beyani_olmadan_dispatch_reddedilir(tmp_path: Path) -> None:
    adapter = SubprocessClientAdapter(
        ClientDescriptor(
            kind=ClientKind.INTERNAL,
            client_id="kurum-ici",
            executable="model.exe",
            capabilities=frozenset({"chat"}),
        )
    )
    request = DispatchRequest(
        assignment_id=uuid4(),
        invocation_id=uuid4(),
        client_id="kurum-ici",
        role="researcher",
        instruction_digest=digest("i"),
        context_manifest_digest=digest("m"),
        timeout_seconds=10,
    )
    with pytest.raises(PolicyViolation):
        adapter.dispatch(
            request,
            cwd=tmp_path,
            permit=_issue_canonical_dispatch_permit(request.assignment_id, request.invocation_id),
        )


def test_adapter_komut_satiri_talimat_metni_tasimaz() -> None:
    adapter = codex_adapter("codex.exe")
    request = DispatchRequest(
        assignment_id=uuid4(),
        invocation_id=uuid4(),
        client_id="codex",
        role="researcher",
        instruction_digest=digest("gizli talimat"),
        context_manifest_digest=digest("manifest"),
        timeout_seconds=30,
    )
    argv = adapter.build_spec(request).argv
    assert "gizli talimat" not in " ".join(argv)
    assert request.instruction_digest in argv
    assert str(request.assignment_id) in argv
    assert str(request.invocation_id) in argv


def test_adapter_forged_canonical_dispatch_permit_reddeder(tmp_path: Path) -> None:
    adapter = codex_adapter("codex.exe")
    request = DispatchRequest(
        assignment_id=uuid4(),
        invocation_id=uuid4(),
        client_id="codex",
        role="researcher",
        instruction_digest=digest("instruction"),
        context_manifest_digest=digest("manifest"),
        timeout_seconds=30,
    )
    forged = CanonicalDispatchPermit(request.assignment_id, request.invocation_id, object())
    with pytest.raises(PolicyViolation, match="permit gecersiz"):
        adapter.dispatch(request, cwd=tmp_path, permit=forged)


def test_adapter_calistirilabilir_secret_tasiyamaz() -> None:
    with pytest.raises(PolicyViolation):
        ClientDescriptor(
            kind=ClientKind.INTERNAL,
            client_id="x",
            executable="/opt/run?api_key=AKIA",
            capabilities=frozenset({"chat"}),
        )
