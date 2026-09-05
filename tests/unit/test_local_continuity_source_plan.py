"""Independent actual-source capture tests; no checkout copies or source mutations."""

from __future__ import annotations

import errno
import json
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from tests.unit.test_local_continuity_startup import ROOT, SOURCE_REF
from tests.unit.test_local_continuity_startup import startup as startup

from zekam.application.local_continuity_source_plan import (
    MAX_SOURCE_BYTES,
    CapturedSourceFile,
    ContinuitySourceRecipe,
)
from zekam.domain.canonical import canonical_json, digest, digest_of_bytes
from zekam.domain.errors import LayoutError, PolicyViolation, ValidationFailed
from zekam.infrastructure.knowledge_files import KnowledgeFileStore
from zekam.infrastructure.local_continuity_source_plan import (
    BoundedContinuitySource,
    _bounded_git_process,
)
from zekam.infrastructure.sqlite.operational_backup import logical_database_digest


@pytest.fixture
def source(startup: dict[str, Any]) -> dict[str, Any]:
    b = startup["binding"]
    recipe = ContinuitySourceRecipe(
        b.project_id,
        b.realm_id,
        startup["source_binding"],
        (SOURCE_REF,),
        b.task_digest,
        b.policy_digest,
    )
    adapter = BoundedContinuitySource(ROOT, recipe)
    return startup | {"recipe": recipe, "adapter": adapter, "plan": adapter.capture()}


def _counts(value: dict[str, Any]) -> dict[str, int]:
    with sqlite3.connect(value["path"]) as db:
        return {
            table: db.execute(f"select count(*) from {table}").fetchone()[0]
            for table in (
                "source_snapshot",
                "source_binding",
                "project",
                "work_item",
                "run",
                "session",
                "hydration_receipt",
                "local_job",
                "local_outbox",
            )
        }


def _apply(value: dict[str, Any], plan: Any = None) -> Any:
    selected = value["plan"] if plan is None else plan
    return value["adapter"].apply(
        value["operational"], selected, expected_plan_digest=selected.content_digest
    )


def test_real_source_capture_is_exact_portable_bounded_and_read_only(
    source: dict[str, Any],
) -> None:
    before = logical_database_digest(source["path"])
    payload = (ROOT / SOURCE_REF).read_bytes()
    plan = source["plan"]
    assert plan == source["adapter"].capture()
    assert plan.revision_ref == source["revision"]
    assert len(plan.files) == 1
    assert plan.files[0].content_digest == digest_of_bytes(payload)
    assert plan.files[0].size_bytes == len(payload)
    assert plan.content_digest == digest(plan.body())
    encoded = canonical_json(plan.body())
    assert str(ROOT) not in encoded and payload.decode() not in encoded
    assert plan.body()["atomic_filesystem_snapshot"] is False
    assert plan.recipe.body()["tree_scope"] == "bounded-files-not-whole-repository"
    assert plan.body()["grants_authority"] is False
    assert logical_database_digest(source["path"]) == before
    assert (ROOT / SOURCE_REF).read_bytes() == payload


def test_apply_only_creates_fresh_snapshot_and_exact_replay(source: dict[str, Any]) -> None:
    before = _counts(source)
    snapshot = _apply(source)
    assert _apply(source) == snapshot
    after = _counts(source)
    assert after == before | {"source_snapshot": before["source_snapshot"] + 1}
    assert source["adapter"].assert_snapshot(source["operational"], snapshot.id) == source["plan"]
    binding = replace(source["binding"], source_snapshot_id=snapshot.id)
    assert source["adapter"].probe(source["operational"], binding) == source["plan"].content_digest


def test_existing_arbitrary_snapshot_is_not_silently_converted(source: dict[str, Any]) -> None:
    before = logical_database_digest(source["path"])
    with pytest.raises(PolicyViolation, match=r"unknown recipe|different capture"):
        source["adapter"].assert_snapshot(
            source["operational"], source["binding"].source_snapshot_id
        )
    assert logical_database_digest(source["path"]) == before


def test_actual_source_recipe_survives_real_process_restart(source: dict[str, Any]) -> None:
    snapshot = _apply(source)
    script = """
import json, socket, sys
from pathlib import Path
from zekam.application.local_continuity_source_plan import ContinuitySourceRecipe
from zekam.infrastructure.local_continuity_source_plan import BoundedContinuitySource
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore
def forbidden(*args, **kwargs): raise AssertionError('No network/provider')
socket.socket.connect = forbidden
socket.create_connection = forbidden
r = json.load(sys.stdin)
r['recipe']['allowed_paths'] = tuple(r['recipe']['allowed_paths'])
source = BoundedContinuitySource(Path(r['root']), ContinuitySourceRecipe(**r['recipe']))
print(source.assert_snapshot(SQLiteOperationalStore(Path(r['db'])), r['snapshot']).content_digest)
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        text=True,
        capture_output=True,
        check=True,
        timeout=25,
        input=json.dumps(
            {
                "root": str(ROOT),
                "db": str(source["path"]),
                "snapshot": snapshot.id,
                "recipe": asdict(source["recipe"]),
            }
        ),
    )
    assert result.stdout.strip() == source["plan"].content_digest


@pytest.mark.parametrize(
    "field,value",
    [
        ("allowed_paths", None),
        ("allowed_paths", []),
        ("allowed_paths", ()),
        ("allowed_paths", (SOURCE_REF, SOURCE_REF)),
        ("allowed_paths", ("a.py", "A.py")),
        ("allowed_paths", ("z.py", "a.py")),
        ("allowed_paths", ("../escape.py",)),
        ("allowed_paths", ("/absolute.py",)),
        ("allowed_paths", ("a/" * 17 + "x.py",)),
        ("allowed_paths", ("source.py#L1-L2",)),
        ("allowed_paths", (None,)),
        ("allowed_paths", tuple(f"{i}.py" for i in range(9))),
        ("project_id", None),
        ("realm_id", "not-uuid"),
        ("source_binding_id", True),
        ("task_digest", None),
        ("policy_digest", "broken"),
    ],
)
def test_recipe_wrong_types_paths_and_bounds_fail(
    source: dict[str, Any], field: str, value: Any
) -> None:
    with pytest.raises((ValidationFailed, PolicyViolation)):
        replace(source["recipe"], **{field: value})


@pytest.mark.parametrize(
    "path",
    [
        ".git/config",
        ".git/info/exclude",
        ".env",
        ".env.local",
        "veriler/input.csv",
        "node_modules/file.py",
        "secrets/key.txt",
        "file.sqlite3",
        "input.zip",
    ],
)
def test_forbidden_selected_material_rejected_without_read(
    source: dict[str, Any], path: str
) -> None:
    with pytest.raises(PolicyViolation, match="forbidden"):
        BoundedContinuitySource(ROOT, replace(source["recipe"], allowed_paths=(path,)))


@pytest.mark.parametrize("value", [None, True, 0, -1, MAX_SOURCE_BYTES + 1])
def test_file_size_contract_is_strict(source: dict[str, Any], value: Any) -> None:
    with pytest.raises(ValidationFailed):
        replace(source["plan"].files[0], size_bytes=value)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("revision_ref", None),
        ("revision_ref", "A" * 40),
        ("files", None),
        ("files", []),
        ("files", (object(),)),
        ("files", ()),
        ("ignore_digests", None),
        ("ignore_digests", tuple((f"{index}/.gitignore", None) for index in range(257))),
        ("ignore_digests", ((".gitignore",),)),
        ("ignore_digests", (("README.md", None),)),
        ("ignore_digests", ((".gitignore", "broken"),)),
        ("ignore_digests", (("z/.gitignore", None), ("a/.gitignore", None))),
        ("ignore_digests", ((".gitignore", None), (".gitignore", None))),
        ("secret_policy_digest", None),
    ],
)
def test_source_plan_rejects_noncanonical_nested_contracts(
    source: dict[str, Any], field: str, value: Any
) -> None:
    with pytest.raises(ValidationFailed):
        replace(source["plan"], **{field: value})


def test_source_plan_rejects_total_bytes_above_aggregate_bound(
    source: dict[str, Any],
) -> None:
    paths = tuple(f"selected-{index}.txt" for index in range(5))
    recipe = replace(source["recipe"], allowed_paths=paths)
    files = tuple(CapturedSourceFile(path, digest(path), MAX_SOURCE_BYTES) for path in paths)
    with pytest.raises(ValidationFailed, match="total byte bound"):
        replace(source["plan"], recipe=recipe, files=files)


@pytest.mark.parametrize(
    "mode", ["expected-digest", "file-digest", "recipe", "missing-file", "ignore"]
)
def test_forged_reviewed_plan_cannot_admit_snapshot(source: dict[str, Any], mode: str) -> None:
    plan = source["plan"]
    before = _counts(source)
    with pytest.raises((PolicyViolation, ValidationFailed)):
        if mode == "expected-digest":
            source["adapter"].apply(
                source["operational"], plan, expected_plan_digest=digest("wrong")
            )
        else:
            if mode == "file-digest":
                plan = replace(
                    plan, files=(replace(plan.files[0], content_digest=digest("forged")),)
                )
            elif mode == "recipe":
                plan = replace(plan, recipe=replace(plan.recipe, realm_id=str(uuid4())))
            elif mode == "missing-file":
                plan = replace(plan, files=())
            else:
                plan = replace(plan, ignore_digests=())
            _apply(source, plan)
    assert _counts(source) == before


@pytest.mark.parametrize("call", [1, 2])
def test_drift_before_insert_or_before_commit_rolls_back_snapshot(
    source: dict[str, Any], monkeypatch: pytest.MonkeyPatch, call: int
) -> None:
    before = logical_database_digest(source["path"])
    actual = source["adapter"].capture
    count = 0

    def changed() -> Any:
        nonlocal count
        count += 1
        result = actual()
        return replace(result, revision_ref="1" * 40) if count == call else result

    monkeypatch.setattr(source["adapter"], "capture", changed)
    with pytest.raises(PolicyViolation, match="Source changed"):
        _apply(source)
    assert logical_database_digest(source["path"]) == before


def test_interrupted_after_insert_leaves_no_half_snapshot_and_retries_cleanly(
    source: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    before = _counts(source)
    actual = source["adapter"].capture
    count = 0

    def interrupted() -> Any:
        nonlocal count
        count += 1
        if count == 2:
            raise KeyboardInterrupt("Interrupted after snapshot insert")
        return actual()

    with monkeypatch.context() as patch:
        patch.setattr(source["adapter"], "capture", interrupted)
        with pytest.raises(KeyboardInterrupt):
            _apply(source)
    assert _counts(source) == before
    assert _apply(source).content_digest == source["plan"].content_digest


def test_superseded_matching_tuple_cannot_be_reported_as_fresh_applied_snapshot(
    source: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _apply(source)
    with monkeypatch.context() as patch:
        patch.setattr(source["adapter"], "_head", lambda: "1" * 40)
        next_plan = source["adapter"].capture()
        second = _apply(source, next_plan)
    assert first.id != second.id
    before = _counts(source)
    # Return-to-old-content must not return a successful snapshot that the same
    # production verifier immediately rejects as non-current.
    with pytest.raises(PolicyViolation):
        _apply(source)
    assert _counts(source) == before


@pytest.mark.parametrize(
    "field", ["project_id", "realm_id", "source_binding_id", "task_digest", "policy_digest"]
)
def test_wrong_database_scope_or_policy_rejects_before_capture(
    source: dict[str, Any], monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    replacement = digest("wrong") if field.endswith("digest") else str(uuid4())
    adapter = BoundedContinuitySource(ROOT, replace(source["recipe"], **{field: replacement}))
    plan = replace(source["plan"], recipe=adapter.recipe)
    monkeypatch.setattr(adapter, "capture", lambda: pytest.fail("Invalid DB admission read source"))
    before = _counts(source)
    with pytest.raises(PolicyViolation, match="admission drift"):
        adapter.apply(source["operational"], plan, expected_plan_digest=plan.content_digest)
    assert _counts(source) == before


@pytest.mark.parametrize(
    "variant",
    [
        "binary",
        "invalid-utf8",
        "secret",
        "too-large",
        "missing",
        "leaf-symlink",
        "parent-symlink",
        "fifo",
    ],
)
def test_bounded_fd_reader_rejects_unsafe_standalone_fixture(
    source: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    variant: str,
) -> None:
    files = KnowledgeFileStore(tmp_path)
    monkeypatch.setattr(source["adapter"], "_files", files)
    path = tmp_path / "fixture.py"
    payload = (ROOT / SOURCE_REF).read_bytes()
    limit = MAX_SOURCE_BYTES
    ref = path.name
    if variant == "binary":
        path.write_bytes(payload + b"\x00")
    elif variant == "invalid-utf8":
        path.write_bytes(payload + b"\xff")
    elif variant == "secret":
        path.write_bytes(payload + b"\nGITHUB_TOKEN=ghp_" + b"A" * 36)
    elif variant == "too-large":
        path.write_bytes(payload)
        limit = len(payload) - 1
    elif variant == "leaf-symlink":
        path.symlink_to(ROOT / SOURCE_REF)
    elif variant == "parent-symlink":
        path.symlink_to(ROOT, target_is_directory=True)
        ref += "/" + SOURCE_REF
    elif variant == "fifo":
        os.mkfifo(path)
    with pytest.raises((PolicyViolation, LayoutError, ValidationFailed)):
        source["adapter"]._read(ref, limit)
    assert (ROOT / SOURCE_REF).read_bytes() == payload


@pytest.mark.parametrize("variant", ["in-place", "replace-leaf"])
def test_fd_reader_detects_mutation_during_single_capture(
    source: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    variant: str,
) -> None:
    path = tmp_path / "fixture.py"
    payload = (ROOT / SOURCE_REF).read_bytes()
    path.write_bytes(payload)
    monkeypatch.setattr(source["adapter"], "_files", KnowledgeFileStore(tmp_path))
    original = os.read
    changed = False

    def read(fd: int, maximum: int) -> bytes:
        nonlocal changed
        result = original(fd, maximum)
        if not changed:
            changed = True
            if variant == "replace-leaf":
                path.unlink()
            path.write_bytes(payload if variant == "replace-leaf" else payload + b"\n")
        return result

    monkeypatch.setattr(os, "read", read)
    with pytest.raises(PolicyViolation, match="changed during"):
        source["adapter"]._read(path.name, MAX_SOURCE_BYTES)
    assert changed


def test_standalone_root_symlink_never_becomes_project_binding(
    source: dict[str, Any], tmp_path: Path
) -> None:
    link = tmp_path / "root-link"
    link.symlink_to(ROOT, target_is_directory=True)
    with pytest.raises(PolicyViolation, match="symlink"):
        BoundedContinuitySource(link, source["recipe"])


@pytest.mark.parametrize("variant", ["missing", "gitdir-pointer", "worktree-pointer", "symlink"])
def test_unsupported_git_layout_is_typed_and_does_not_run_git(
    source: dict[str, Any], monkeypatch: pytest.MonkeyPatch, variant: str
) -> None:
    # Inject only the directory-open syscall result; never manufacture a checkout.
    original = os.open
    error = {
        "missing": errno.ENOENT,
        "gitdir-pointer": errno.ENOTDIR,
        "worktree-pointer": errno.ENOTDIR,
        "symlink": errno.ELOOP,
    }[variant]

    def opened(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if path == ".git":
            raise OSError(error, "Injected unsupported Git layout")
        return original(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", opened)
    monkeypatch.setattr(
        BoundedContinuitySource, "_git", lambda *_args, **_kwargs: pytest.fail("Git was invoked")
    )
    with pytest.raises(PolicyViolation, match="Git layout unsupported"):
        BoundedContinuitySource(ROOT, source["recipe"])


def test_foreign_git_metadata_owner_is_rejected(
    source: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    original = os.fstat
    git_inode = (ROOT / ".git").stat().st_ino

    def observed(fd: int) -> os.stat_result:
        info = original(fd)
        if info.st_ino == git_inode:
            fields = list(info)
            fields[4] = os.geteuid() + 1
            return os.stat_result(fields)
        return info

    monkeypatch.setattr(os, "fstat", observed)
    with pytest.raises(PolicyViolation, match="ownership unsupported"):
        BoundedContinuitySource(ROOT, source["recipe"])


@pytest.mark.parametrize("operation", ["_head", "_ignore_capture"])
def test_git_metadata_identity_change_rejected(
    source: dict[str, Any], monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    identity = source["adapter"]._git_identity
    monkeypatch.setattr(source["adapter"], "_git_layout", lambda: (identity[0], -1, *identity[2:]))
    with pytest.raises(PolicyViolation, match="identity changed"):
        getattr(source["adapter"], operation)()


def test_git_metadata_identity_change_during_head_rejected(
    source: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = source["adapter"]._git_identity
    calls = 0

    def layout() -> tuple[int, ...]:
        nonlocal calls
        calls += 1
        return tuple(identity) if calls == 1 else (identity[0], -1, *identity[2:])

    monkeypatch.setattr(source["adapter"], "_git_layout", layout)
    with pytest.raises(PolicyViolation, match="identity changed during"):
        source["adapter"]._head()


def test_git_exclude_policy_fingerprint_missing_and_stale_are_explicit(
    source: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    actual = ROOT / ".git/info/exclude"
    expected = digest_of_bytes(actual.read_bytes()) if actual.exists() else None
    assert dict(source["plan"].ignore_digests)[".git/info/exclude"] == expected
    old_snapshot = _apply(source)
    before = _counts(source)
    original = source["adapter"]._read
    metadata: bytes | None = None

    def read(relative: str, maximum: int, **kwargs: Any) -> Any:
        if relative == ".git/info/exclude":
            return metadata
        return original(relative, maximum, **kwargs)

    monkeypatch.setattr(source["adapter"], "_read", read)
    missing = source["adapter"].capture()
    assert dict(missing.ignore_digests)[".git/info/exclude"] is None
    metadata = b"# Independent metadata-only policy fingerprint\n"
    changed = source["adapter"].capture()
    assert dict(changed.ignore_digests)[".git/info/exclude"] == digest_of_bytes(metadata)
    assert changed.config_digest != missing.config_digest
    assert changed.tree_digest == missing.tree_digest == source["plan"].tree_digest
    with pytest.raises(PolicyViolation):
        _apply(source)
    with pytest.raises(PolicyViolation):
        source["adapter"].assert_snapshot(source["operational"], old_snapshot.id)
    assert _counts(source) == before


def test_optional_git_exclude_missing_parent_returns_explicit_absence(
    source: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(source["adapter"], "_files", KnowledgeFileStore(tmp_path))
    assert source["adapter"]._read(".git/info/exclude", 65536, optional=True) is None
    with pytest.raises(PolicyViolation):
        source["adapter"]._read(".git/info/exclude", 65536)


def test_capture_does_not_scan_unselected_files_or_run_other_git_commands(
    source: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    reads: list[str] = []
    commands: list[tuple[str, ...]] = []
    original_read, original_git = source["adapter"]._read, source["adapter"]._git

    def read(relative: str, maximum: int, **kwargs: Any) -> Any:
        reads.append(relative)
        assert relative == SOURCE_REF or relative.endswith(
            (".gitignore", ".zekamignore", ".git/info/exclude")
        )
        return original_read(relative, maximum, **kwargs)

    def git(arguments: tuple[str, ...], **kwargs: Any) -> bytes:
        commands.append(arguments)
        assert arguments[0] in {"rev-parse", "check-ignore", "ls-files"}
        result = original_git(arguments, **kwargs)
        assert isinstance(result, bytes)
        return result

    monkeypatch.setattr(source["adapter"], "_read", read)
    monkeypatch.setattr(source["adapter"], "_git", git)
    assert source["adapter"].capture() == source["plan"]
    assert reads and commands


@pytest.mark.parametrize(
    "variant", ["gitignore", "custom-ignore", "custom-parent", "unsupported-custom"]
)
def test_selected_tracked_file_respects_ignore_policy(
    source: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    variant: str,
) -> None:
    original_read, original_git = source["adapter"]._read, source["adapter"]._git

    def read(relative: str, maximum: int, **kwargs: Any) -> Any:
        if relative == ".zekamignore" and variant != "gitignore":
            return {
                "custom-ignore": SOURCE_REF.encode(),
                "custom-parent": b"src/\n",
                "unsupported-custom": b"[ab]*.py\n",
            }[variant]
        return original_read(relative, maximum, **kwargs)

    def git(arguments: tuple[str, ...], **kwargs: Any) -> bytes:
        if arguments[0] == "check-ignore" and variant == "gitignore":
            assert "--no-index" in arguments
            return SOURCE_REF.encode() + b"\x00"
        result = original_git(arguments, **kwargs)
        assert isinstance(result, bytes)
        return result

    monkeypatch.setattr(source["adapter"], "_read", read)
    monkeypatch.setattr(source["adapter"], "_git", git)
    with pytest.raises(PolicyViolation, match=r"ignored|syntax"):
        source["adapter"].capture()


def test_ancestor_ignore_file_is_part_of_config_provenance(
    source: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    original = source["adapter"]._read

    def read(relative: str, maximum: int, **kwargs: Any) -> Any:
        if relative == "src/akilli_kasa/.zekamignore":
            return b"# reviewed ancestor policy\n"
        return original(relative, maximum, **kwargs)

    monkeypatch.setattr(source["adapter"], "_read", read)
    changed = source["adapter"].capture()
    assert changed.config_digest != source["plan"].config_digest
    assert changed.tree_digest == source["plan"].tree_digest
    assert dict(changed.ignore_digests)["src/akilli_kasa/.zekamignore"] == digest_of_bytes(
        b"# reviewed ancestor policy\n"
    )


@pytest.mark.parametrize("variant", ["head", "root", "git-timeout", "file-set"])
def test_capture_drift_is_visible_not_a_partial_plan(
    source: dict[str, Any], monkeypatch: pytest.MonkeyPatch, variant: str
) -> None:
    if variant == "head":
        heads = iter((source["revision"], "1" * 40))
        monkeypatch.setattr(source["adapter"], "_head", lambda: next(heads))
    elif variant == "root":
        monkeypatch.setattr(source["adapter"], "_root", lambda: ())
    elif variant == "git-timeout":

        def failure(*args: Any, **kwargs: Any) -> bytes:
            raise PolicyViolation("Source fixed Git observation timed out")

        monkeypatch.setattr(source["adapter"], "_git", failure)
    else:
        original = source["adapter"]._capture_once
        count = 0

        def capture_once() -> Any:
            nonlocal count
            count += 1
            plan = original()
            return (
                replace(plan, files=(replace(plan.files[0], content_digest=digest("changed")),))
                if count == 2
                else plan
            )

        monkeypatch.setattr(source["adapter"], "_capture_once", capture_once)
    with pytest.raises(PolicyViolation):
        source["adapter"].capture()


def test_git_environment_cannot_redirect_real_source_to_caller_controlled_root(
    source: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_SYSTEM",
    ):
        monkeypatch.setenv(name, "/nonexistent/untrusted-location")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.worktree")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "/nonexistent/untrusted-location")
    assert source["adapter"].capture() == source["plan"]


def test_parent_directory_replacement_cannot_preserve_stale_open_file_as_current(
    source: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    payload = (ROOT / SOURCE_REF).read_bytes()
    (parent / "fixture.py").write_bytes(payload)
    monkeypatch.setattr(source["adapter"], "_files", KnowledgeFileStore(tmp_path))
    original = os.read
    changed = False

    def read(fd: int, maximum: int) -> bytes:
        nonlocal changed
        value = original(fd, maximum)
        if not changed:
            changed = True
            parent.rename(tmp_path / "previous-parent")
            parent.mkdir()
            (parent / "fixture.py").write_bytes(payload)
        return value

    monkeypatch.setattr(os, "read", read)
    with pytest.raises(PolicyViolation, match=r"directory unavailable|parent path changed"):
        source["adapter"]._read("parent/fixture.py", MAX_SOURCE_BYTES)


def test_concurrent_same_plan_apply_has_one_snapshot_identity(source: dict[str, Any]) -> None:
    before = _counts(source)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: _apply(source), range(2)))
    assert results[0] == results[1]
    assert _counts(source)["source_snapshot"] == before["source_snapshot"] + 1


def test_admission_stays_locked_during_source_capture(
    source: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    original = source["adapter"].capture
    lock_observations = 0

    def captured() -> Any:
        nonlocal lock_observations
        with (
            sqlite3.connect(source["path"], timeout=0) as db,
            pytest.raises(sqlite3.OperationalError, match="locked"),
        ):
            db.execute("update source_binding set active=0")
        lock_observations += 1
        return original()

    monkeypatch.setattr(source["adapter"], "capture", captured)
    _apply(source)
    assert lock_observations == 2


@pytest.mark.parametrize(
    "arguments",
    [
        None,
        [],
        ("status",),
        ("config", "user.name", "unsafe"),
        ("ls-files", "--error-unmatch", "--", "foreign.py"),
    ],
)
def test_git_private_entry_still_rejects_unreviewed_command_shapes(
    source: dict[str, Any], arguments: Any
) -> None:
    with pytest.raises(ValidationFailed):
        source["adapter"]._git(arguments)


@pytest.mark.parametrize(
    "size,stderr,success", [(16384, 0, True), (16385, 0, False), (8192, 8193, False)]
)
def test_bounded_process_enforces_combined_output_cap(
    size: int, stderr: int, success: bool
) -> None:
    script = (
        f"import sys;sys.stdout.buffer.write(b'x'*{size});sys.stderr.buffer.write(b'y'*{stderr})"
    )
    command = [sys.executable, "-c", script]
    if success:
        code, out, err = _bounded_git_process(command, {"PATH": os.defpath}, None)
        assert code == 0 and len(out) == size and len(err) == stderr
    else:
        with pytest.raises(PolicyViolation, match="output exceeded"):
            _bounded_git_process(command, {"PATH": os.defpath}, None)


def test_bounded_process_timeout_reaps_child(monkeypatch: pytest.MonkeyPatch) -> None:
    original = subprocess.Popen
    children: list[Any] = []

    def spawn(*args: Any, **kwargs: Any) -> Any:
        process = original(*args, **kwargs)
        children.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", spawn)
    with pytest.raises(PolicyViolation, match="timed out"):
        _bounded_git_process(
            [sys.executable, "-c", "import time; time.sleep(30)"], {"PATH": os.defpath}, None
        )
    assert len(children) == 1
    assert children[0].poll() is not None


def test_bounded_process_cleanup_permission_race_preserves_original_finding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = subprocess.Popen
    children: list[Any] = []

    def spawn(*args: Any, **kwargs: Any) -> Any:
        process = original(*args, **kwargs)
        children.append(process)
        return process

    def permission_race(*_args: Any, **_kwargs: Any) -> None:
        raise PermissionError(1, "process group already unavailable")

    monkeypatch.setattr(subprocess, "Popen", spawn)
    monkeypatch.setattr(os, "killpg", permission_race)
    with pytest.raises(PolicyViolation, match="output exceeded"):
        _bounded_git_process(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdout.buffer.write(b'x' * 16385)",
            ],
            {"PATH": os.defpath},
            None,
        )
    assert len(children) == 1
    assert children[0].poll() is not None
