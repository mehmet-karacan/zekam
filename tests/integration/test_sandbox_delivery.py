"""P10 bagli gercek source rootu ve typed process kabul testleri.

Bu testler gercek source repository ve gercek alt surec kullanir; mock degildir.
"""

from __future__ import annotations

import datetime as dt
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from zekam.application.sandbox_delivery import SandboxDeliveryService, default_policy
from zekam.domain.checkpoint_v2 import SandboxDisposition
from zekam.domain.errors import PolicyViolation
from zekam.domain.sandbox import ProcessSpec, WorkspaceSpec
from zekam.infrastructure.git.worktree import WorktreeManager, fingerprint
from zekam.infrastructure.process import runner

pytestmark = pytest.mark.integration

NOW = dt.datetime(2026, 8, 20, tzinfo=dt.UTC)


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def source_repo(tmp_path: Path) -> Path:
    root = tmp_path / "kaynak"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "test@ornek.local")
    _git(root, "config", "user.name", "Test")
    (root / "src").mkdir()
    (root / "src" / "modul.py").write_text("DEGER = 1\n", encoding="utf-8", newline="\n")
    (root / "README.md").write_text("# kaynak\n", encoding="utf-8", newline="\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "ilk")
    return root


@pytest.fixture
def service(source_repo: Path, tmp_path: Path) -> SandboxDeliveryService:
    return SandboxDeliveryService(
        WorktreeManager(source_root=source_repo, workspaces_root=tmp_path / "worktrees"),
        resolve_bound_source=lambda project_ref: (
            source_repo if project_ref == "kaynak" else tmp_path / "kayitsiz"
        ),
    )


def _spec(source_repo: Path, paths: tuple[str, ...] = ("src",)) -> WorkspaceSpec:
    head = fingerprint(source_repo).head
    return WorkspaceSpec(
        workspace_id="w-1",
        project_ref="kaynak",
        work_ref="ZEKAM-P10-T01",
        source_revision=head,
        policy=default_policy(paths),
    )


def test_workspace_bagli_gercek_source_rootunu_kullanir(
    service: SandboxDeliveryService, source_repo: Path
) -> None:
    before = fingerprint(source_repo)
    workspace = service.prepare(_spec(source_repo))
    assert workspace.worktree.exists is True
    assert workspace.worktree.revision == before.head
    assert workspace.worktree.path == source_repo.resolve()

    (workspace.worktree.path / "src" / "modul.py").write_text(
        "DEGER = 2\n", encoding="utf-8", newline="\n"
    )
    after = fingerprint(source_repo)
    assert not after.matches(before), "direct-source yazimi gercek tree'yi degistirmeli"
    assert (source_repo / "src" / "modul.py").read_text(encoding="utf-8") == "DEGER = 2\n"

    service.discard(workspace)
    assert not fingerprint(source_repo).matches(before)


def test_project_ref_binding_uyusmazligi_reddedilir(
    service: SandboxDeliveryService, source_repo: Path
) -> None:
    spec = WorkspaceSpec(
        workspace_id="w-mismatch",
        project_ref="baska-proje",
        work_ref="ZEKAM-P10-T01",
        source_revision=fingerprint(source_repo).head,
        policy=default_policy(("src",)),
    )
    with pytest.raises(PolicyViolation):
        service.prepare(spec)


def test_prepare_proje_kopyasi_ve_worktree_dizini_uretmez(
    source_repo: Path, tmp_path: Path
) -> None:
    workspaces_root = tmp_path / "uretilmemeli"
    service = SandboxDeliveryService(
        WorktreeManager(source_root=source_repo, workspaces_root=workspaces_root),
        resolve_bound_source=lambda project_ref: source_repo,
    )
    workspace = service.prepare(_spec(source_repo))
    assert workspace.worktree.path == source_repo.resolve()
    assert not workspaces_root.exists()


def test_allowlist_disina_yazma_reddedilir(
    service: SandboxDeliveryService, source_repo: Path
) -> None:
    workspace = service.prepare(_spec(source_repo, ("src",)))
    try:
        assert workspace.resolve_write("src/modul.py").name == "modul.py"
        with pytest.raises(PolicyViolation):
            workspace.resolve_write("README.md")
        with pytest.raises(PolicyViolation):
            workspace.resolve_write("../kacis.py")
    finally:
        service.discard(workspace)


def test_symlink_kacisi_reddedilir(service: SandboxDeliveryService, source_repo: Path) -> None:
    workspace = service.prepare(_spec(source_repo, ("src",)))
    try:
        link = workspace.worktree.path / "src" / "disari"
        try:
            link.symlink_to(source_repo.parent, target_is_directory=True)
        except OSError:
            pytest.skip("Windows'ta symlink olusturmak yonetici yetkisi ister")
        with pytest.raises(PolicyViolation):
            workspace.resolve_write("src/disari/gizli.txt")
    finally:
        service.discard(workspace)


def test_source_icindeki_symlink_bile_yazma_hedefi_olamaz(
    service: SandboxDeliveryService, source_repo: Path
) -> None:
    workspace = service.prepare(_spec(source_repo, ("src",)))
    try:
        (source_repo / "hedef").mkdir()
        link = source_repo / "src" / "ic-link"
        try:
            link.symlink_to(source_repo / "hedef", target_is_directory=True)
        except OSError:
            pytest.skip("Windows'ta symlink olusturmak yonetici yetkisi ister")
        with pytest.raises(PolicyViolation, match="symlink veya reparse"):
            workspace.resolve_write("src/ic-link/dosya.txt")
    finally:
        service.discard(workspace)


def test_tracked_degisiklik_symlink_kapisini_atlayamaz(
    service: SandboxDeliveryService, source_repo: Path
) -> None:
    workspace = service.prepare(_spec(source_repo, ("src",)))
    try:
        (source_repo / "hedef").mkdir()
        link = source_repo / "src" / "tracked-link"
        try:
            link.symlink_to(source_repo / "hedef", target_is_directory=True)
        except OSError:
            pytest.skip("Windows'ta symlink olusturmak yonetici yetkisi ister")
        _git(source_repo, "add", "src/tracked-link")
        with pytest.raises(PolicyViolation, match="symlink veya reparse"):
            service.build_artifact(workspace, artifact_id="tracked-link", now=NOW)
    finally:
        service.discard(workspace)


@pytest.mark.skipif(os.name != "nt", reason="Windows junction/reparse testi")
def test_windows_junction_reparse_yazma_hedefi_olamaz(
    service: SandboxDeliveryService, source_repo: Path
) -> None:
    workspace = service.prepare(_spec(source_repo, ("src",)))
    try:
        target = source_repo / "junction-hedef"
        target.mkdir()
        link = source_repo / "src" / "junction"
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            pytest.skip("Windows junction olusturulamadi")
        with pytest.raises(PolicyViolation, match="symlink veya reparse"):
            workspace.resolve_write("src/junction/dosya.txt")
    finally:
        service.discard(workspace)


def test_ayni_source_rootunda_paralel_builder_lease_reddedilir(
    service: SandboxDeliveryService, source_repo: Path
) -> None:
    first = service.prepare(_spec(source_repo))
    try:
        with pytest.raises(PolicyViolation, match="baska builder"):
            service.prepare(replace(_spec(source_repo), workspace_id="w-2"))
    finally:
        service.discard(first)

    second = service.prepare(replace(_spec(source_repo), workspace_id="w-2"))
    service.discard(second)


def test_stale_revision_ile_workspace_hazirlanmaz(
    service: SandboxDeliveryService, source_repo: Path
) -> None:
    spec = WorkspaceSpec(
        workspace_id="w-stale",
        project_ref="kaynak",
        work_ref="ZEKAM-P10-T01",
        source_revision="0" * 40,
        policy=default_policy(("src",)),
    )
    with pytest.raises(PolicyViolation):
        service.prepare(spec)


def test_typed_runner_shell_kullanmaz(service: SandboxDeliveryService, source_repo: Path) -> None:
    workspace = service.prepare(_spec(source_repo))
    try:
        output = runner.run(
            ProcessSpec(argv=(sys.executable, "-c", "print('merhaba')"), timeout_seconds=30),
            cwd=workspace.worktree.path,
        )
        assert output.result.succeeded is True
        assert output.stdout.strip() == b"merhaba"
        assert output.result.stdout_digest.startswith("sha256:")
    finally:
        service.discard(workspace)


def test_timeout_bounded_calisir(service: SandboxDeliveryService, source_repo: Path) -> None:
    workspace = service.prepare(_spec(source_repo))
    try:
        output = runner.run(
            ProcessSpec(
                argv=(sys.executable, "-c", "import time; time.sleep(30)"), timeout_seconds=1
            ),
            cwd=workspace.worktree.path,
        )
        assert output.result.timed_out is True
        assert output.result.succeeded is False
    finally:
        service.discard(workspace)


def test_env_allowlist_disi_degisken_sizmaz(
    service: SandboxDeliveryService, source_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ZEKAM_SIZINTI_TESTI", "gizli-deger")
    workspace = service.prepare(_spec(source_repo))
    try:
        output = runner.run(
            ProcessSpec(
                argv=(
                    sys.executable,
                    "-c",
                    "import os; print(os.environ.get('ZEKAM_SIZINTI_TESTI', 'yok'))",
                ),
                timeout_seconds=30,
            ),
            cwd=workspace.worktree.path,
        )
        assert output.stdout.strip() == b"yok"
    finally:
        service.discard(workspace)


def test_cikti_siniri_kirpar(service: SandboxDeliveryService, source_repo: Path) -> None:
    workspace = service.prepare(_spec(source_repo))
    try:
        output = runner.run(
            ProcessSpec(
                argv=(sys.executable, "-c", "print('a' * 10000)"),
                timeout_seconds=30,
                max_output_bytes=100,
            ),
            cwd=workspace.worktree.path,
        )
        assert output.result.truncated is True
        assert len(output.stdout) == 100
    finally:
        service.discard(workspace)


def test_yama_uretimi_apply_check_test_ve_receipt_akisi(
    service: SandboxDeliveryService, source_repo: Path
) -> None:
    spec = _spec(source_repo, ("src",))
    workspace = service.prepare(spec)
    try:
        (workspace.worktree.path / "src" / "modul.py").write_text(
            "DEGER = 2\n", encoding="utf-8", newline="\n"
        )
        artifact, patch = service.build_artifact(workspace, artifact_id="a-1", now=NOW)
        assert artifact.changed_paths == ("src/modul.py",)
        assert patch.strip()

        results = service.run_tests(
            workspace,
            (ProcessSpec(argv=(sys.executable, "-c", "assert 1 == 1"), timeout_seconds=30),),
        )
        report = service.deliver(
            workspace,
            artifact=artifact,
            patch=patch,
            planned_paths=("src/modul.py",),
            test_results=results,
            builder_ref="builder-1",
            verifier_ref="verifier-1",
        )
        assert report.decision.outcome.value == "applied"
        assert report.decision.apply_check_passed is True
        assert report.receipt_eligible is True
        assert not report.main_tree_before.matches(report.main_tree_after)
        assert (source_repo / "src" / "modul.py").read_text(encoding="utf-8") == "DEGER = 2\n"
    finally:
        service.discard(workspace)


def test_checkpoint_binding_canli_patch_base_ve_dirty_statei_dogrular(
    service: SandboxDeliveryService, source_repo: Path
) -> None:
    workspace = service.prepare(_spec(source_repo, ("src",)))
    clean = service.checkpoint_binding(workspace)
    assert clean.disposition is SandboxDisposition.CLEAN
    service.assert_checkpoint_binding(clean)

    (workspace.worktree.path / "src" / "modul.py").write_text(
        "DEGER = 20\n", encoding="utf-8", newline="\n"
    )
    artifact, _ = service.build_artifact(workspace, artifact_id="checkpoint-patch", now=NOW)
    dirty = service.checkpoint_binding(workspace, artifact=artifact)
    assert dirty.disposition is SandboxDisposition.DIRTY
    assert dirty.base_revision == workspace.worktree.revision
    assert dirty.patch_digest == artifact.patch_digest
    assert dirty.dirty_state_digest is not None
    service.assert_checkpoint_binding(dirty)

    with pytest.raises(PolicyViolation, match="patch provenance drift"):
        service.checkpoint_binding(
            workspace, artifact=replace(artifact, patch_digest="sha256:" + "0" * 64)
        )

    (workspace.worktree.path / "src" / "modul.py").write_text(
        "DEGER = 21\n", encoding="utf-8", newline="\n"
    )
    with pytest.raises(PolicyViolation, match="patch veya dirty state drift"):
        service.assert_checkpoint_binding(dirty)


def test_untracked_icerik_patch_ve_dirty_state_digestine_dahildir(
    service: SandboxDeliveryService, source_repo: Path
) -> None:
    workspace = service.prepare(_spec(source_repo, ("src",)))
    new_file = workspace.worktree.path / "src" / "yeni.py"
    new_file.write_text("DEGER = 1\n", encoding="utf-8", newline="\n")
    artifact, patch = service.build_artifact(workspace, artifact_id="untracked", now=NOW)
    assert artifact.changed_paths == ("src/yeni.py",)
    assert patch == ""
    binding = service.checkpoint_binding(workspace, artifact=artifact)
    service.assert_checkpoint_binding(binding)

    new_file.write_text("DEGER = 2\n", encoding="utf-8", newline="\n")
    with pytest.raises(PolicyViolation, match="patch veya dirty state drift"):
        service.assert_checkpoint_binding(binding)


def test_untracked_allowlist_disina_sizamaz(
    service: SandboxDeliveryService, source_repo: Path
) -> None:
    workspace = service.prepare(_spec(source_repo, ("src",)))
    (workspace.worktree.path / "yasak.txt").write_text("yasak\n", encoding="utf-8")
    with pytest.raises(PolicyViolation, match="allowlist"):
        service.build_artifact(workspace, artifact_id="untracked-outside", now=NOW)


def test_basarisiz_test_teslimi_reddeder(
    service: SandboxDeliveryService, source_repo: Path
) -> None:
    workspace = service.prepare(_spec(source_repo, ("src",)))
    try:
        (workspace.worktree.path / "src" / "modul.py").write_text(
            "DEGER = 3\n", encoding="utf-8", newline="\n"
        )
        artifact, patch = service.build_artifact(workspace, artifact_id="a-2", now=NOW)
        results = service.run_tests(
            workspace,
            (ProcessSpec(argv=(sys.executable, "-c", "raise SystemExit(1)"), timeout_seconds=30),),
        )
        report = service.deliver(
            workspace,
            artifact=artifact,
            patch=patch,
            planned_paths=("src/modul.py",),
            test_results=results,
            builder_ref="builder-1",
            verifier_ref="verifier-1",
        )
        assert report.decision.outcome.value == "rejected"
        assert report.receipt_eligible is False
        assert "test" in report.decision.detail
    finally:
        service.discard(workspace)


def test_plan_disi_yol_degisikligi_drift_uretir(
    service: SandboxDeliveryService, source_repo: Path
) -> None:
    workspace = service.prepare(_spec(source_repo, ("src", "README.md")))
    try:
        (workspace.worktree.path / "src" / "modul.py").write_text(
            "DEGER = 4\n", encoding="utf-8", newline="\n"
        )
        (workspace.worktree.path / "README.md").write_text(
            "# degisti\n", encoding="utf-8", newline="\n"
        )
        artifact, patch = service.build_artifact(workspace, artifact_id="a-3", now=NOW)
        report = service.deliver(
            workspace,
            artifact=artifact,
            patch=patch,
            planned_paths=("src/modul.py",),
            test_results=(),
            builder_ref="builder-1",
            verifier_ref="verifier-1",
        )
        assert report.decision.outcome.value == "drifted"
        assert report.receipt_eligible is False
    finally:
        service.discard(workspace)


def test_allowlist_disi_yama_artifact_uretemez(
    service: SandboxDeliveryService, source_repo: Path
) -> None:
    workspace = service.prepare(_spec(source_repo, ("src",)))
    try:
        (workspace.worktree.path / "README.md").write_text(
            "# izinsiz\n", encoding="utf-8", newline="\n"
        )
        with pytest.raises(PolicyViolation):
            service.build_artifact(workspace, artifact_id="a-4", now=NOW)
    finally:
        service.discard(workspace)
