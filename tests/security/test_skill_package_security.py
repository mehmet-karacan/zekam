from __future__ import annotations

import os
import unicodedata
from pathlib import Path

import pytest

import zekam.application.skill_packages as skill_packages
from zekam.application.skill_packages import apply_projection_plan, build_projection_plan
from zekam.domain.client_integration import ClientIntegrationPolicy
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.skill_package import SkillPackage

ALL_ENABLED = ClientIntegrationPolicy(opencode=True, codex=True, claude_code=True)


def _plan(project: Path, package: SkillPackage) -> skill_packages.SkillProjectionPlan:
    return build_projection_plan(project, package, policy=ALL_ENABLED)


def _files(*, allowed_tools: bool = False) -> dict[str, bytes]:
    permission = "allowed-tools: Bash(*)\n" if allowed_tools else ""
    return {
        "SKILL.md": (
            "---\n"
            "name: zekam-arastirma-uygulama\n"
            "description: Kanıtlı araştırmayı güvenli uygulamaya dönüştürür.\n"
            f"{permission}"
            "---\n"
            "Kaynakları doğrula ve sonucu receipt ile bağla.\n"
        ).encode(),
        "references/checks.md": b"# Checks\n\nFail closed.\n",
    }


def test_agent_skill_parse_round_trip_and_authority_stripping() -> None:
    package = SkillPackage.parse("zekam-arastirma-uygulama", _files(allowed_tools=True))

    projected = package.safe_projection_files()
    reparsed = SkillPackage.parse("zekam-arastirma-uygulama", projected)

    assert package.declared_allowed_tools == "Bash(*)"
    assert b"allowed-tools" not in projected["SKILL.md"]
    assert reparsed.declared_allowed_tools is None
    assert reparsed.semantic_digest == package.semantic_digest
    assert reparsed.package_digest != package.package_digest


@pytest.mark.parametrize(
    ("files", "error"),
    [
        (
            {
                "SKILL.md": b"---\nname: x\nname: x\ndescription: d\n---\nbody\n"
            },
            ValidationFailed,
        ),
        ({"SKILL.md": b"\xff"}, ValidationFailed),
        (_files() | {"../escape.txt": b"x"}, PolicyViolation),
        (_files() | {"C:/escape.txt": b"x"}, PolicyViolation),
        (_files() | {"asset.txt:stream": b"x"}, PolicyViolation),
        (_files() | {"references/CON.txt": b"x"}, PolicyViolation),
        (_files() | {"references/report. ": b"x"}, PolicyViolation),
        (
            _files()
            | {unicodedata.normalize("NFD", "references/ölçüm.md"): b"x"},
            PolicyViolation,
        ),
        (_files() | {".zekam-managed.json": b"{}"}, PolicyViolation),
        (_files() | {".ZEKAM-MANAGED.JSON": b"{}"}, PolicyViolation),
        (_files() | {".env": b"SAFE_NAME=value"}, PolicyViolation),
        (_files() | {"notes.txt": b"password='not-a-placeholder-value'"}, PolicyViolation),
    ],
)
def test_agent_skill_rejects_malformed_metadata_and_unsafe_paths(
    files: dict[str, bytes], error: type[Exception]
) -> None:
    with pytest.raises(error):
        SkillPackage.parse("zekam-arastirma-uygulama", files)


def test_directory_import_rejects_symlink(tmp_path: Path) -> None:
    root = tmp_path / "zekam-arastirma-uygulama"
    root.mkdir()
    for relative, payload in _files().items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    link = root / "references" / "escape-link"
    try:
        link.symlink_to(tmp_path, target_is_directory=True)
    except OSError:
        pytest.skip("Current Windows token cannot create a symlink")

    with pytest.raises(PolicyViolation, match="symlink/junction"):
        SkillPackage.read_directory(root.resolve())


def test_managed_client_projection_is_shared_scoped_and_drift_safe(tmp_path: Path) -> None:
    project = (tmp_path / "project").resolve()
    project.mkdir()
    package = SkillPackage.parse("zekam-arastirma-uygulama", _files(allowed_tools=True))
    plan = _plan(project, package)

    assert [target["relative_path"] for target in plan.targets] == [
        ".opencode/skills/zekam-arastirma-uygulama",
        ".agents/skills/zekam-arastirma-uygulama",
        ".claude/skills/zekam-arastirma-uygulama",
    ]
    assert [target["clients"] for target in plan.targets] == [
        ("opencode",),
        ("codex",),
        ("claude-code",),
    ]
    with pytest.raises(PolicyViolation, match="authorization"):
        apply_projection_plan(plan, authorized_plan_digest="sha256:" + "0" * 64)

    receipt = apply_projection_plan(plan, authorized_plan_digest=plan.plan_digest)
    assert receipt["network_calls"] == 0
    assert receipt["grants_authority"] is False
    current = _plan(project, package)
    assert all(target["state"] == "current" for target in current.targets)
    for target in plan.targets:
        skill_md = project / str(target["relative_path"]) / "SKILL.md"
        assert b"allowed-tools" not in skill_md.read_bytes()

    updated_files = _files(allowed_tools=True) | {
        "SKILL.md": _files(allowed_tools=True)["SKILL.md"].replace(
            b"receipt ile bagla", b"receipt ile bagla ve tekrar dogrula"
        ),
        "references/new.md": b"# New\n",
    }
    updated_package = SkillPackage.parse("zekam-arastirma-uygulama", updated_files)
    update = _plan(project, updated_package)
    assert all(target["state"] == "managed-update" for target in update.targets)
    apply_projection_plan(update, authorized_plan_digest=update.plan_digest)
    assert all(
        target["state"] == "current"
        for target in _plan(project, updated_package).targets
    )
    changed = (
        project / ".agents" / "skills" / package.name / "references" / "checks.md"
    )
    changed.write_text("user change", encoding="utf-8")
    drifted = _plan(project, updated_package)
    assert drifted.targets[1]["state"] == "managed-drift"
    with pytest.raises(PolicyViolation, match="drifted"):
        apply_projection_plan(drifted, authorized_plan_digest=drifted.plan_digest)


def test_default_projection_is_opencode_only(tmp_path: Path) -> None:
    project = (tmp_path / "project").resolve()
    project.mkdir()
    package = SkillPackage.parse("zekam-arastirma-uygulama", _files())

    plan = build_projection_plan(project, package)

    states = {str(item["client"]): str(item["state"]) for item in plan.targets}
    assert states == {"opencode": "new", "codex": "absent", "claude-code": "absent"}
    apply_projection_plan(plan, authorized_plan_digest=plan.plan_digest)
    assert (project / ".opencode" / "skills" / package.name).is_dir()
    assert not (project / ".agents").exists()
    assert not (project / ".claude").exists()

def test_managed_update_rechecks_target_after_plan_authorization(tmp_path: Path) -> None:
    project = (tmp_path / "project").resolve()
    project.mkdir()
    original = SkillPackage.parse("zekam-arastirma-uygulama", _files())
    original_plan = _plan(project, original)
    apply_projection_plan(original_plan, authorized_plan_digest=original_plan.plan_digest)
    updated = SkillPackage.parse(
        "zekam-arastirma-uygulama",
        _files() | {"references/new.md": b"# New\n"},
    )
    update_plan = _plan(project, updated)
    user_file = project / ".agents" / "skills" / original.name / "SKILL.md"
    user_file.write_text("user edit after plan", encoding="utf-8")

    with pytest.raises(PolicyViolation, match="changed after authorization"):
        apply_projection_plan(update_plan, authorized_plan_digest=update_plan.plan_digest)

    assert user_file.read_text(encoding="utf-8") == "user edit after plan"


def test_projection_restores_user_edit_racing_with_atomic_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = (tmp_path / "project").resolve()
    project.mkdir()
    original = SkillPackage.parse("zekam-arastirma-uygulama", _files())
    original_plan = _plan(project, original)
    apply_projection_plan(original_plan, authorized_plan_digest=original_plan.plan_digest)
    updated = SkillPackage.parse(
        "zekam-arastirma-uygulama",
        _files() | {"references/new.md": b"# New\n"},
    )
    update_plan = _plan(project, updated)
    user_file = project / ".agents" / "skills" / original.name / "SKILL.md"
    real_build = skill_packages.build_projection_plan
    build_calls = 0

    def edit_after_final_check(
        root: Path, package: SkillPackage, **kwargs: object
    ) -> skill_packages.SkillProjectionPlan:
        nonlocal build_calls
        build_calls += 1
        result = real_build(root, package, **kwargs)  # type: ignore[arg-type]
        if build_calls == 2:
            user_file.write_text("concurrent user edit", encoding="utf-8")
        return result

    monkeypatch.setattr(skill_packages, "build_projection_plan", edit_after_final_check)
    with pytest.raises(PolicyViolation, match="changed during atomic swap"):
        apply_projection_plan(update_plan, authorized_plan_digest=update_plan.plan_digest)

    assert user_file.read_text(encoding="utf-8") == "concurrent user edit"
    assert not list(project.rglob("*.zekam-backup-*"))


def test_management_metadata_tamper_is_not_accepted_as_current(tmp_path: Path) -> None:
    project = (tmp_path / "project").resolve()
    project.mkdir()
    package = SkillPackage.parse("zekam-arastirma-uygulama", _files())
    plan = _plan(project, package)
    apply_projection_plan(plan, authorized_plan_digest=plan.plan_digest)
    ownership = project / ".agents" / "skills" / package.name / ".zekam-managed.json"
    body = ownership.read_text(encoding="utf-8").replace(
        package.package_digest, "sha256:" + "f" * 64
    )
    ownership.write_text(body, encoding="utf-8")

    tampered = _plan(project, package)
    assert tampered.targets[1]["state"] == "managed-metadata-invalid"


def test_projection_rolls_back_first_target_when_second_swap_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = (tmp_path / "project").resolve()
    project.mkdir()
    original = SkillPackage.parse("zekam-arastirma-uygulama", _files())
    first_plan = _plan(project, original)
    apply_projection_plan(first_plan, authorized_plan_digest=first_plan.plan_digest)
    original_bytes = {
        str(target["relative_path"]): (
            project / str(target["relative_path"]) / "SKILL.md"
        ).read_bytes()
        for target in first_plan.targets
    }
    updated = SkillPackage.parse(
        "zekam-arastirma-uygulama",
        _files()
        | {
            "references/new.md": b"# New generation\n",
        },
    )
    update_plan = _plan(project, updated)
    real_replace = os.replace
    target_swaps = 0

    def fail_second_target_swap(source: os.PathLike[str], destination: os.PathLike[str]) -> None:
        nonlocal target_swaps
        if ".zekam-stage-" in str(source):
            target_swaps += 1
            if target_swaps == 2:
                raise OSError("injected second target failure")
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_second_target_swap)
    with pytest.raises(OSError, match="injected second target failure"):
        apply_projection_plan(update_plan, authorized_plan_digest=update_plan.plan_digest)

    for relative, expected in original_bytes.items():
        assert (project / relative / "SKILL.md").read_bytes() == expected
    assert not list(project.rglob("*.zekam-stage-*"))
    assert not list(project.rglob("*.zekam-backup-*"))


def test_projection_keeps_recoverable_backup_when_rollback_restore_is_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = (tmp_path / "project").resolve()
    project.mkdir()
    original = SkillPackage.parse("zekam-arastirma-uygulama", _files())
    first_plan = _plan(project, original)
    apply_projection_plan(first_plan, authorized_plan_digest=first_plan.plan_digest)
    updated = SkillPackage.parse(
        "zekam-arastirma-uygulama",
        _files() | {"references/new.md": b"# New generation\n"},
    )
    update_plan = _plan(project, updated)
    real_replace = os.replace
    replace_calls = 0

    def fail_swap_and_one_restore(
        source: os.PathLike[str], destination: os.PathLike[str]
    ) -> None:
        nonlocal replace_calls
        replace_calls += 1
        if replace_calls in {4, 6}:
            raise OSError("injected transaction failure")
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_swap_and_one_restore)
    with pytest.raises(PolicyViolation, match="recoverable backup retained"):
        apply_projection_plan(update_plan, authorized_plan_digest=update_plan.plan_digest)

    backups = list(project.rglob("*.zekam-backup-*"))
    assert len(backups) == 1
    assert (backups[0] / "SKILL.md").read_bytes() == _files()["SKILL.md"]
