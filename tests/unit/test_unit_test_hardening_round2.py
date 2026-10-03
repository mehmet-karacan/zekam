"""W05 verifier tur 2: Windows 8.3/cihaz adlari ve deger-tabanli secret taramasi regresyonlari.

Yol testleri saf-string (8.3 acik olmayan ortamda da calisir); gercek dosya testi 8.3 kisa
adi uretilebiliyorsa calisir, yoksa atlanir.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from tests.unit.unit_test_loop_support import (
    SRC,
    TEST_DIR,
    Harness,
    default_builder,
)

from zekam.application.unit_test_agents import (
    AgentDispatchFailure,
    AgentSpecialty,
    AgentTask,
)
from zekam.application.unit_test_patch import (
    FileChange,
    GuardViolation,
    TestOnlyPolicy,
    TestPatch,
)
from zekam.application.unit_test_secret_scan import find_secret_value
from zekam.domain.canonical import digest, digest_of_bytes
from zekam.domain.errors import PolicyViolation
from zekam.domain.unit_test_engineering import UnitTestStopReason
from zekam.infrastructure.unit_test_runner.patch_workspace import FileSystemTestPatchWorkspace


def broad_policy() -> TestOnlyPolicy:
    return TestOnlyPolicy(frozenset({SRC}), allowed_test_paths=("src", "mod", "build", "mvn~1"))


# =========================================================== 1) 8.3 kisa adlari ve cihaz adlari


@pytest.mark.parametrize(
    "path",
    [
        "mod/SETTIN~1.XML",
        "mod/TOOLCH~1.XML",
        "mod/toolch~1.xml",
        "BUILD~1.GRA",
        "mod/JVM~1.CON",
        "SRC~1/MAIN~1/java/p/X.java",
        "src/MAIN~1/java/p/X.java",
        "MVN~1/x",
        "src/test/java/p/ABCDEF~12.JAVA",
    ],
)
def test_short_8_3_names_are_rejected_as_strings(path: str) -> None:
    with pytest.raises(GuardViolation) as raised:
        broad_policy().check_path(path, modifies_existing=False)
    assert raised.value.reason == "windows-short-name"


@pytest.mark.parametrize(
    "path",
    [
        "src/test/java/p/CON.java",
        "src/test/java/p/con",
        "src/test/java/NUL",
        "src/test/java/p/aux.java",
        "src/test/java/p/Aux.JAVA",
        "src/test/java/p/PRN.txt.java",
        "src/test/java/p/COM1",
        "src/test/java/p/com9.java",
        "src/test/java/p/LPT1.java",
        "src/test/java/p/CONIN$",
        "src/test/java/CONOUT$.java",
        "src/test/java/p/NUL .java",
    ],
)
def test_windows_device_names_are_rejected(path: str) -> None:
    with pytest.raises(GuardViolation) as raised:
        broad_policy().check_path(path, modifies_existing=False)
    assert raised.value.reason in {"windows-device-name", "invalid-path-component", "invalid-path"}


@pytest.mark.parametrize(
    "path",
    [
        "src/test/java/p/ConnectionTest.java",
        "src/test/java/p/Console.java",
        "src/test/java/p/Auxiliary.java",
        "src/test/java/p/Com10.java",
        "src/test/java/p/Tilde~Test.java",
        "src/test/java/p/Nulls.java",
    ],
)
def test_ordinary_names_are_not_false_positives(path: str) -> None:
    broad_policy().check_path(path, modifies_existing=False)


def _short_alias(path: Path) -> str | None:
    if sys.platform != "win32":
        return None
    import ctypes

    buffer = ctypes.create_unicode_buffer(1024)
    size = ctypes.windll.kernel32.GetShortPathNameW(str(path), buffer, 1024)
    return buffer.value if size else None


def test_real_file_alias_cannot_overwrite_protected_toolchains_via_apply(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    (root / "mod").mkdir(parents=True)
    target = root / "mod" / "toolchains.xml"
    target.write_text("<toolchains/>", encoding="utf-8")
    alias = _short_alias(target)
    if alias is None or Path(alias).name.casefold() == "toolchains.xml":
        pytest.skip("bu makinede 8.3 kisa adi uretilmiyor; saf-string testleri gecerli")
    relative = f"mod/{Path(alias).name}"
    workspace = FileSystemTestPatchWorkspace(root)
    patch = TestPatch((FileChange(relative, "pwn", digest_of_bytes(b"<toolchains/>")),))
    with pytest.raises((GuardViolation, PolicyViolation)):
        workspace.apply(patch)
    with pytest.raises((GuardViolation, PolicyViolation)):
        workspace.apply(patch, policy=broad_policy())
    assert target.read_text(encoding="utf-8") == "<toolchains/>"


def test_apply_revalidates_resolved_real_path_against_the_protected_set(tmp_path: Path) -> None:
    """TOCTOU: apply aninda hedefin gercek yolu korumali kumeye dusuyorsa yazilmaz."""

    root = tmp_path / "proj"
    (root / "src" / "test" / "java").mkdir(parents=True)
    real_dir = root / "src" / "main" / "java"
    real_dir.mkdir(parents=True)
    link = root / "src" / "test" / "java" / "link"
    try:
        link.symlink_to(real_dir, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink kurulamiyor")
    workspace = FileSystemTestPatchWorkspace(root)
    policy = TestOnlyPolicy(frozenset({SRC}), allowed_test_paths=("src/test/java",))
    patch = TestPatch((FileChange("src/test/java/link/Evil.java", "x", None),))
    with pytest.raises((GuardViolation, PolicyViolation)):
        workspace.apply(patch, policy=policy)
    assert not (real_dir / "Evil.java").exists()


def test_apply_with_policy_rechecks_even_if_pre_validation_was_skipped(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    (root / "src" / "test" / "java").mkdir(parents=True)
    workspace = FileSystemTestPatchWorkspace(root)
    policy = TestOnlyPolicy(frozenset({SRC}), allowed_test_paths=("src/test/java",))
    patch = TestPatch((FileChange("src/test/java/pom.xml", "x", None),))
    with pytest.raises(GuardViolation):
        workspace.apply(patch, policy=policy)
    ok = TestPatch((FileChange("src/test/java/FooTest.java", "x", None),))
    workspace.apply(ok, policy=policy)
    assert (root / "src/test/java/FooTest.java").exists()


def test_loop_rejects_short_name_proposal_and_writes_nothing(tmp_path: Path) -> None:
    def evil(context: Mapping[str, Any], call: int) -> Mapping[str, Any]:
        base = dict(default_builder(context, call))
        base["patch"] = {
            "files": [
                {"path": "src/test/java/p/TOOLCH~1.JAVA", "content": "x", "preimage_digest": None}
            ]
        }
        return base

    h = Harness(tmp_path, handlers={AgentSpecialty.BUILDER: evil})
    outcome = h.run()
    assert outcome.terminal.stop_reason is UnitTestStopReason.STAGNATION_REVIEW
    assert "windows-short-name" in outcome.detail
    assert h.tree() == {f"{TEST_DIR}/ExistingTest.java"}


# =========================================================== 2) deger-tabanli secret taramasi

POSITIVE_TEXTS = [
    'String password = "hunter2";',
    'password = "Abcdef1234567890"',
    'password="Abc!defghijklmnopqrstuvwxyz123"',
    "jdbc:postgresql://admin:S3cret!pw@db.internal:5432/app",
    "jdbc:postgresql://admin:S3cret!pw@db.example.org/app",
    "Authorization: Basic dXNlcjpwYXNzd29yZDEyMw==",
    "aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    "PASSWORD=hunter2",
    "jdbc:mysql://h/db?user=a&password=hunter2&ssl=true",
    "Zk3Jd9Qx7LmN2pVb8TyC4rWs6HfGa1UeOi5XnKj0Rq",
    "client_secret: s3cr3tvalue",
    'String dbPassword = "x";',
]

POSITIVE_DOCS: list[Any] = [
    {"password": "S3cretValueThatIsLong1234"},
    {"db": {"password": "x"}},
    {"api_key": "abc"},
    {"items": [{"client_secret": "hunter2"}]},
    {"AWS_SECRET_ACCESS_KEY": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"},
]

NEGATIVE: list[Any] = [
    "PasswordValidator rejects expired token",
    "// password must contain a digit\nclass PasswordValidatorTest {}",
    "@Test void rejectsShortPassword() { assertFalse(validator.isValid(input)); }",
    'String passwordRule = "min-8-chars";',
    "validator.validate(password);",
    "String token = nextToken();",
    {"token_count": 5, "password_policy": "strict", "note": "secret sauce recipe discussion"},
    {"password": ""},
    {"password": "<redacted>"},
    {"password": "${DB_PASSWORD}"},
    ["sha256:" + "a" * 64, digest("x"), digest_of_bytes(b"y")],
    "src/test/java/p/VeryLongTestClassNameForPasswordValidatorBehaviourSpecification.java",
]


@pytest.mark.parametrize("text", POSITIVE_TEXTS)
def test_secret_values_in_text_are_found_and_never_echoed(text: str) -> None:
    found = find_secret_value(text)
    assert found is not None, text
    for secret in ("hunter2", "S3cret", "wJalr", "dXNlcjpw", "Zk3Jd9"):
        assert secret not in found


@pytest.mark.parametrize("doc", POSITIVE_DOCS)
def test_structured_key_value_secrets_are_found_even_when_key_and_value_are_separate(
    doc: Any,
) -> None:
    assert find_secret_value(doc) is not None


@pytest.mark.parametrize("doc", NEGATIVE)
def test_plain_words_names_comments_and_digests_are_not_secrets(doc: Any) -> None:
    assert find_secret_value(doc) is None, doc


def _task(**context: Any) -> AgentTask:
    return AgentTask(AgentSpecialty.BUILDER, digest("r"), 2, 0, {"scenarios": [], **context})


@pytest.mark.parametrize("text", POSITIVE_TEXTS[:8])
def test_outgoing_context_scan_uses_the_strengthened_detector(tmp_path: Path, text: str) -> None:
    h = Harness(tmp_path)
    with pytest.raises(PolicyViolation, match="secret"):
        h.gateway.invoke(_task(note=text))
    assert all(a.calls == 0 for a in h.adapters.values())


def test_outgoing_context_scan_covers_structured_dicts(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    with pytest.raises(PolicyViolation, match="secret"):
        h.gateway.invoke(_task(config={"password": "hunter2"}))


@pytest.mark.parametrize("text", POSITIVE_TEXTS[:8])
def test_artifact_result_scan_uses_the_strengthened_detector(tmp_path: Path, text: str) -> None:
    def builder(context: Mapping[str, Any], call: int) -> Mapping[str, Any]:
        base = dict(default_builder(context, call))
        base["review_note"] = text
        return base

    h = Harness(tmp_path, artifact_bodies=True, handlers={AgentSpecialty.BUILDER: builder})
    with pytest.raises(AgentDispatchFailure) as raised:
        h.gateway.invoke(_task(scenarios=[{"scenario_id": "S1"}]))
    assert raised.value.category == "result-contains-secret-value"


def test_artifact_structured_secret_is_rejected(tmp_path: Path) -> None:
    def builder(context: Mapping[str, Any], call: int) -> Mapping[str, Any]:
        base = dict(default_builder(context, call))
        base["config"] = {"password": "hunter2"}
        return base

    h = Harness(tmp_path, artifact_bodies=True, handlers={AgentSpecialty.BUILDER: builder})
    with pytest.raises(AgentDispatchFailure) as raised:
        h.gateway.invoke(_task(scenarios=[{"scenario_id": "S1"}]))
    assert raised.value.category == "result-contains-secret-value"
