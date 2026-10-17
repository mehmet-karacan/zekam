"""WP-03.02-B: real Work and bound source file readback without mutation."""

from __future__ import annotations

import os
import sqlite3
import stat
from pathlib import Path

import pytest

from zekam.application.home import HomeLayout
from zekam.application.natural_request_evidence import read_work_project, verify_session_file
from zekam.application.natural_request_scope import ContextSignal, resolve_natural_request_scope
from zekam.application.project_rag_runtime import bind_project_source
from zekam.application.request_routing import (
    RegisteredProject,
    load_project_families,
    route_request,
)
from zekam.domain.canonical import digest_of_bytes
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.infrastructure.sqlite.operational_schema import bootstrap
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore


@pytest.fixture
def bound_context(tmp_path: Path) -> tuple[Path, Path, str]:
    home = tmp_path / "home"
    HomeLayout(home).ensure()
    database = home / "state" / "operational.db"
    bootstrap(database)
    store = SQLiteOperationalStore(database)
    with store.unit_of_work() as uow:
        sky = uow.create_project(slug="sky-spring-ui", display_name="Sky UI")
        uow.create_project(slug="gpu-fusion", display_name="GPU")
        work = uow.create_work(
            project_id=sky.id,
            kind="task",
            title="Onceki dosya",
            state="active",
        )
        uow.commit()
    source = tmp_path / "sky-source"
    source.mkdir()
    (source / "src").mkdir()
    (source / "src" / "Panel.java").write_text("class Panel {}\n", encoding="utf-8")
    bind_project_source(home, "sky-spring-ui", source)
    return home, source, work.id


def _file_digest(source: Path) -> str:
    return digest_of_bytes((source / "src" / "Panel.java").read_bytes())


def test_work_and_file_readback_bind_real_previous_scope(
    bound_context: tuple[Path, Path, str],
) -> None:
    home, source, work_id = bound_context
    work = read_work_project(home, work_id)
    file = verify_session_file(home, "sky-spring-ui", "src/Panel.java", _file_digest(source))
    assert work.project_slug == file.project_slug == "sky-spring-ui"
    assert file.relative_path == "src/Panel.java"
    assert file.digest == _file_digest(source)
    assert file.as_dict()["content_included"] is False
    assert work.as_dict()["grants_authority"] is False

    route = route_request(
        "Bunu da GPU icin ekle",
        catalog=load_project_families(),
        registered_projects=(RegisteredProject("gpu-fusion"), RegisteredProject("sky-spring-ui")),
    )
    decision = resolve_natural_request_scope(
        route,
        registered_project_refs=frozenset({"gpu-fusion", "sky-spring-ui"}),
        explicit_project_ref="gpu-fusion",
        session=ContextSignal(file.project_slug, True),
    )
    assert decision.status == "clarification-required"
    assert decision.project_refs == ()
    assert decision.as_dict()["grants_authority"] is False


def test_missing_work_fails_closed(bound_context: tuple[Path, Path, str]) -> None:
    home, _source, _work_id = bound_context
    with pytest.raises(PolicyViolation, match="work-readback-missing"):
        read_work_project(home, "missing-work")


def test_work_readback_does_not_take_a_write_lock(bound_context: tuple[Path, Path, str]) -> None:
    home, _source, work_id = bound_context
    connection = sqlite3.connect(home / "state" / "operational.db")
    try:
        connection.execute("begin immediate")
        evidence = read_work_project(home, work_id)
        assert evidence.work_id == work_id
        assert evidence.project_slug == "sky-spring-ui"
        assert evidence.as_dict()["grants_authority"] is False
    finally:
        connection.rollback()
        connection.close()


def test_exclusively_locked_work_readback_reports_unavailable(
    bound_context: tuple[Path, Path, str],
) -> None:
    home, _source, work_id = bound_context
    connection = sqlite3.connect(home / "state" / "operational.db")
    try:
        connection.execute("begin exclusive")
        with pytest.raises(PolicyViolation, match="work-readback-unavailable"):
            read_work_project(home, work_id)
    finally:
        connection.rollback()
        connection.close()


@pytest.mark.parametrize("invalid_database", [False, True])
def test_unavailable_work_store_never_creates_or_repairs_data(
    tmp_path: Path, invalid_database: bool,
) -> None:
    home = tmp_path / "unavailable-home"
    database = home / "state" / "operational.db"
    payload = b"invalid SQLite fixture"
    if invalid_database:
        database.parent.mkdir(parents=True)
        database.write_bytes(payload)
    with pytest.raises(PolicyViolation, match="work-readback-unavailable"):
        read_work_project(home, "unknown-work")
    if invalid_database:
        assert database.read_bytes() == payload
    else:
        assert not home.exists()


def test_closed_work_cannot_be_session_authority(bound_context: tuple[Path, Path, str]) -> None:
    home, _source, _work_id = bound_context
    store = SQLiteOperationalStore(home / "state" / "operational.db")
    with store.unit_of_work() as uow:
        project = uow.resolve_project("sky-spring-ui")
        closed = uow.create_work(
            project_id=project.id,
            kind="task",
            title="Eski is",
            state="archived",
        )
        uow.commit()
    with pytest.raises(PolicyViolation, match="work-not-open"):
        read_work_project(home, closed.id)


def test_archived_project_reports_archived_reason(bound_context: tuple[Path, Path, str]) -> None:
    home, _source, work_id = bound_context
    connection = sqlite3.connect(home / "state" / "operational.db")
    try:
        connection.execute("update project set status = 'archived' where slug = 'sky-spring-ui'")
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(PolicyViolation, match="work-project-archived"):
        read_work_project(home, work_id)


def test_windows_normalized_source_name_is_rejected(bound_context: tuple[Path, Path, str]) -> None:
    home, source, _work_id = bound_context
    with pytest.raises(ValidationFailed, match="session-file-path-traversal"):
        verify_session_file(home, "sky-spring-ui", "src/Panel.java.", _file_digest(source))


def test_wrong_project_source_binding_fails_closed(bound_context: tuple[Path, Path, str]) -> None:
    home, source, _work_id = bound_context
    with pytest.raises((ValidationFailed, PolicyViolation)):
        verify_session_file(home, "gpu-fusion", "src/Panel.java", _file_digest(source))


def test_stale_file_digest_fails_closed(bound_context: tuple[Path, Path, str]) -> None:
    home, source, _work_id = bound_context
    expected = _file_digest(source)
    (source / "src" / "Panel.java").write_text("class Panel { int x; }\n", encoding="utf-8")
    with pytest.raises(PolicyViolation, match="digest-mismatch"):
        verify_session_file(home, "sky-spring-ui", "src/Panel.java", expected)


@pytest.mark.parametrize(
    "relative", ["../outside.java", "/tmp/file.java", "src//Panel.java", "C:/x.java"]
)
def test_path_escape_fails_closed(bound_context: tuple[Path, Path, str], relative: str) -> None:
    home, source, _work_id = bound_context
    with pytest.raises(ValidationFailed):
        verify_session_file(home, "sky-spring-ui", relative, _file_digest(source))


def test_secret_config_and_binary_are_rejected(bound_context: tuple[Path, Path, str]) -> None:
    home, source, _work_id = bound_context
    secret = source / ".env"
    secret.write_text("TOKEN=example\n", encoding="utf-8")
    with pytest.raises(PolicyViolation, match="secret-config"):
        verify_session_file(home, "sky-spring-ui", ".env", digest_of_bytes(secret.read_bytes()))
    upper = source / ".ENV"
    upper.write_text("TOKEN=example\n", encoding="utf-8")
    with pytest.raises(PolicyViolation, match="secret-config"):
        verify_session_file(home, "sky-spring-ui", ".ENV", digest_of_bytes(upper.read_bytes()))
    binary = source / "src" / "blob.dat"
    binary.write_bytes(b"x\x00y")
    with pytest.raises(ValidationFailed, match="binary"):
        verify_session_file(
            home, "sky-spring-ui", "src/blob.dat", digest_of_bytes(binary.read_bytes())
        )
    disguised = source / "src" / "ascii.bin"
    disguised.write_bytes(b"plain text")
    with pytest.raises(PolicyViolation, match="binary-type"):
        verify_session_file(
            home, "sky-spring-ui", "src/ascii.bin", digest_of_bytes(disguised.read_bytes())
        )


def test_symlink_file_is_rejected_without_platform_privilege(
    bound_context: tuple[Path, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    home, source, _work_id = bound_context
    target = source / "src" / "link.java"
    try:
        target.symlink_to(source / "src" / "Panel.java")
    except (OSError, NotImplementedError) as exc:
        # Windows developer-mode/symlink privilege is not required to exercise
        # the fail-closed validator.  Simulate the lstat metadata returned by
        # a symlink so this negative case remains covered in restricted CI.
        target.write_text("link placeholder\n", encoding="utf-8")
        original_lstat = Path.lstat

        def mocked_lstat(path: Path) -> os.stat_result:
            if path == target:
                return os.stat_result((stat.S_IFLNK, 0, 0, 1, 0, 0, 0, 0, 0, 0))
            return original_lstat(path)

        monkeypatch.setattr(Path, "lstat", mocked_lstat)
    with pytest.raises(PolicyViolation, match="symlink"):
        verify_session_file(home, "sky-spring-ui", "src/link.java", _file_digest(source))


def test_hardlink_to_outside_file_cannot_be_verified(
    bound_context: tuple[Path, Path, str],
) -> None:
    home, source, _work_id = bound_context
    outside = source.parent / "outside-secret.txt"
    outside.write_text("private source outside registered root\n", encoding="utf-8")
    link = source / "src" / "Linked.java"
    try:
        os.link(outside, link)
    except OSError as exc:
        pytest.skip(f"hardlink olusturulamadi: {exc}")
    with pytest.raises(PolicyViolation, match="hardlink"):
        verify_session_file(
            home, "sky-spring-ui", "src/Linked.java", digest_of_bytes(outside.read_bytes())
        )
