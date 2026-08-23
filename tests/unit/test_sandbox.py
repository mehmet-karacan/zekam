"""P10-T01..T04 sandbox, typed process ve patch sozlesmesi testleri."""

from __future__ import annotations

import datetime as dt

import pytest

from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.sandbox import (
    MAX_TIMEOUT_SECONDS,
    DeliveryDecision,
    DeliveryOutcome,
    NetworkPolicy,
    PatchArtifact,
    PathAllowlist,
    ProcessResult,
    ProcessSpec,
    SandboxPolicy,
    WorkspaceSpec,
    assert_no_drift,
    assert_relative_path,
)

NOW = dt.datetime(2026, 8, 20, tzinfo=dt.UTC)
PATCH = digest("patch")


# -- T01: allowlist, network, workspace ---------------------------------------


@pytest.mark.parametrize(
    "value",
    ["/etc/passwd", "C:/Windows", "..\\disari", "../disari", "src\\zekam", "src/../../disari"],
)
def test_absolute_ve_traversal_reddedilir(value: str) -> None:
    with pytest.raises(PolicyViolation):
        assert_relative_path(value)


def test_allowlist_bos_olamaz() -> None:
    with pytest.raises(PolicyViolation):
        PathAllowlist(())


def test_allowlist_alt_yollari_kapsar_disini_kapsamaz() -> None:
    allowlist = PathAllowlist(("src/zekam/domain", "docs/RAPOR.md"))
    assert allowlist.permits("src/zekam/domain/sandbox.py") is True
    assert allowlist.permits("src/zekam/domain") is True
    assert allowlist.permits("docs/RAPOR.md") is True
    assert allowlist.permits("src/zekam/application/harness.py") is False
    assert allowlist.permits("docs/BASKA.md") is False
    with pytest.raises(PolicyViolation):
        allowlist.assert_permits("src/zekam/application/harness.py")


def test_allowlist_onek_eslemesiyle_kacilamaz() -> None:
    """`docs` izinliyse `docs-gizli` izinli sayilmamalidir."""

    allowlist = PathAllowlist(("docs",))
    assert allowlist.permits("docs-gizli/rapor.md") is False


def test_network_default_deny() -> None:
    policy = NetworkPolicy()
    assert policy.is_default_deny is True
    assert policy.permits("ornek.org", "GET") is False
    with pytest.raises(PolicyViolation):
        policy.assert_permits("ornek.org", "GET")


def test_network_host_allowlist_operasyon_ister() -> None:
    with pytest.raises(PolicyViolation):
        NetworkPolicy(allowed_hosts=frozenset({"ornek.org"}))
    policy = NetworkPolicy(
        allowed_hosts=frozenset({"ornek.org"}), allowed_operations=frozenset({"GET"})
    )
    assert policy.permits("ornek.org", "GET") is True
    assert policy.permits("ornek.org", "POST") is False
    assert policy.permits("baska.org", "GET") is False


def test_direct_source_write_kapatilamaz() -> None:
    with pytest.raises(PolicyViolation):
        SandboxPolicy(allowlist=PathAllowlist(("src",)), main_tree_read_only=True)

    document = SandboxPolicy(allowlist=PathAllowlist(("src",))).as_dict()
    assert document["main_tree_read_only"] is False
    assert document["direct_source_write"] is True
    assert document["project_copy"] is False


def test_detached_worktree_reddedilir() -> None:
    with pytest.raises(PolicyViolation):
        WorkspaceSpec(
            workspace_id="w1",
            project_ref="zekam",
            work_ref="ZEKAM-P10-T01",
            source_revision="rev",
            policy=SandboxPolicy(allowlist=PathAllowlist(("src",))),
            detached=True,
        )


# -- T02: typed process -------------------------------------------------------


def test_calistirilabilir_alanda_shell_komut_satiri_reddedilir() -> None:
    """Gercek risk: argv[0] alanina gizlenmis bir kabuk komut satiri."""

    for executable in (
        "rm -rf / ; echo",
        "a && b",
        "cat file | grep x",
        "$(whoami)",
        "a`b`",
        "  python",
        "sh -c script",
    ):
        with pytest.raises(PolicyViolation):
            ProcessSpec(argv=(executable, "--version"))


def test_argumandaki_metakarakter_serbesttir() -> None:
    """`shell=False` oldugundan arguman icindeki `;` veya `$` kabuga ulasmaz."""

    spec = ProcessSpec(argv=("python", "-c", "import os; print(os.sep)"))
    assert spec.executable == "python"
    assert ProcessSpec(argv=("grep", "$HOME|x")).argv[1] == "$HOME|x"


def test_argv_satir_sonu_her_alanda_reddedilir() -> None:
    with pytest.raises(PolicyViolation):
        ProcessSpec(argv=("python", "-c", "print(1)\nimport os"))
    with pytest.raises(PolicyViolation):
        ProcessSpec(argv=("python\r", "-c", "pass"))


def test_bos_argv_ve_sinirsiz_timeout_reddedilir() -> None:
    with pytest.raises(ValidationFailed):
        ProcessSpec(argv=())
    with pytest.raises(ValidationFailed):
        ProcessSpec(argv=("pytest",), timeout_seconds=0)
    with pytest.raises(ValidationFailed):
        ProcessSpec(argv=("pytest",), timeout_seconds=MAX_TIMEOUT_SECONDS + 1)


def test_env_secret_tasiyamaz() -> None:
    with pytest.raises(PolicyViolation):
        ProcessSpec(argv=("pytest",), env=(("API_KEY", "x"),))
    with pytest.raises(PolicyViolation):
        ProcessSpec(argv=("pytest",), env=(("SAFE", "my-secret-value"),))


def test_env_adi_tekrar_edemez() -> None:
    with pytest.raises(ValidationFailed):
        ProcessSpec(argv=("pytest",), env=(("A", "1"), ("A", "2")))


def test_timeout_sonucu_basarili_olamaz() -> None:
    with pytest.raises(ValidationFailed):
        ProcessResult(
            spec_digest=digest("s"),
            exit_code=0,
            duration_ms=1,
            stdout_digest=digest(""),
            stderr_digest=digest(""),
            truncated=False,
            timed_out=True,
        )


def test_process_sonucu_ham_cikti_tasimaz() -> None:
    result = ProcessResult(
        spec_digest=digest("s"),
        exit_code=0,
        duration_ms=5,
        stdout_digest=digest("out"),
        stderr_digest=digest("err"),
        truncated=False,
        timed_out=False,
    )
    document = result.as_dict()
    assert set(document) == {
        "spec_digest",
        "exit_code",
        "duration_ms",
        "stdout_digest",
        "stderr_digest",
        "truncated",
        "timed_out",
        "succeeded",
    }
    assert result.succeeded is True


# -- T03/T04: patch, drift ve teslim karari -----------------------------------


def _artifact(paths: tuple[str, ...] = ("src/zekam/domain/sandbox.py",)) -> PatchArtifact:
    return PatchArtifact(
        artifact_id="a1",
        workspace_id="w1",
        base_revision="rev-1",
        changed_paths=paths,
        patch_digest=PATCH,
        created_at=NOW,
    )


def test_bos_yama_teslim_edilemez() -> None:
    with pytest.raises(ValidationFailed):
        _artifact(())


def test_yama_allowlist_disina_yazamaz() -> None:
    allowlist = PathAllowlist(("src/zekam/domain",))
    _artifact().assert_within(allowlist)
    with pytest.raises(PolicyViolation):
        _artifact(("src/zekam/domain/sandbox.py", "pyproject.toml")).assert_within(allowlist)


def test_revision_drift_teslimi_durdurur() -> None:
    with pytest.raises(PolicyViolation):
        assert_no_drift(
            planned_revision="rev-1",
            current_revision="rev-2",
            planned_paths=("a.py",),
            changed_paths=("a.py",),
        )


def test_plan_disi_yol_teslimi_durdurur() -> None:
    with pytest.raises(PolicyViolation):
        assert_no_drift(
            planned_revision="rev-1",
            current_revision="rev-1",
            planned_paths=("a.py",),
            changed_paths=("a.py", "b.py"),
        )


def test_verifier_builder_ile_ayni_olamaz() -> None:
    with pytest.raises(PolicyViolation):
        DeliveryDecision(
            artifact_digest=_artifact().artifact_digest,
            outcome=DeliveryOutcome.APPLIED,
            apply_check_passed=True,
            tests_passed=True,
            verifier_ref="ayni",
            builder_ref="ayni",
        )


def test_apply_check_veya_test_gecmeden_applied_olamaz() -> None:
    for apply_ok, tests_ok in ((False, True), (True, False), (False, False)):
        with pytest.raises(PolicyViolation):
            DeliveryDecision(
                artifact_digest=_artifact().artifact_digest,
                outcome=DeliveryOutcome.APPLIED,
                apply_check_passed=apply_ok,
                tests_passed=tests_ok,
                verifier_ref="v",
                builder_ref="b",
            )


def test_basarisiz_teslim_gerekce_ister() -> None:
    with pytest.raises(ValidationFailed):
        DeliveryDecision(
            artifact_digest=_artifact().artifact_digest,
            outcome=DeliveryOutcome.REJECTED,
            apply_check_passed=False,
            tests_passed=False,
            verifier_ref="v",
            builder_ref="b",
        )


def test_teslim_karari_deterministik_digest_uretir() -> None:
    decision = DeliveryDecision(
        artifact_digest=_artifact().artifact_digest,
        outcome=DeliveryOutcome.APPLIED,
        apply_check_passed=True,
        tests_passed=True,
        verifier_ref="v",
        builder_ref="b",
    )
    assert decision.decision_digest == decision.decision_digest
    assert decision.decision_digest.startswith("sha256:")
