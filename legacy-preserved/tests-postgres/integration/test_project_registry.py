"""Proje kayit defteri, alias cozumleme, binding ve entegrasyon yasam dongusu."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from zekam.application.project_integration import ProjectIntegrationService, locator_digest_for
from zekam.domain.errors import NotFound, PolicyViolation
from zekam.domain.project import (
    BindingStatus,
    IntegrationStage,
    ProjectAlias,
    ResolutionKind,
    SourceBindingKind,
)
from zekam.domain.realm import Realm
from zekam.infrastructure.postgres.project_repository import (
    ProjectRepository,
    ProjectResolver,
    SourceBindingRepository,
)

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


def _write(root: Path, relative: str, body: str) -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8", newline="\n")


@pytest.fixture
def sample_source(tmp_path: Path) -> Path:
    root = tmp_path / "gpu-fusion"
    _write(root, "pyproject.toml", '[project]\nname = "gpu"\ndependencies = ["fastapi"]\n')
    _write(root, "src/gpu/__init__.py", "")
    _write(root, "tests/test_gpu.py", "def test_gpu(): pass\n")
    return root


@pytest.fixture
def service(realm_session: tuple[Realm, Any]) -> ProjectIntegrationService:
    realm, connection = realm_session
    return ProjectIntegrationService(connection, realm)


# -- kayit ve listeleme ---------------------------------------------------------


def test_register_creates_project_binding_and_state(
    service: ProjectIntegrationService, sample_source: Path
) -> None:
    project = service.register(source_path=sample_source)
    assert project.slug == "gpu-fusion"

    bindings = service.bindings.for_project(project.id)
    assert len(bindings) == 1
    assert bindings[0].kind is SourceBindingKind.DIRECTORY
    assert bindings[0].access_mode == "read-only"
    assert bindings[0].status is BindingStatus.BOUND

    stage, _, _ = service.states.get(project.id)
    assert stage is IntegrationStage.BOUND


def test_register_does_not_write_to_source(
    service: ProjectIntegrationService, sample_source: Path
) -> None:
    before = sorted(path.relative_to(sample_source).as_posix() for path in sample_source.rglob("*"))
    service.register(source_path=sample_source)
    after = sorted(path.relative_to(sample_source).as_posix() for path in sample_source.rglob("*"))
    assert before == after


def test_binding_record_carries_no_absolute_path(
    service: ProjectIntegrationService, sample_source: Path
) -> None:
    project = service.register(source_path=sample_source)
    binding = service.bindings.for_project(project.id)[0]
    rendered = repr(binding.as_dict())
    assert str(sample_source) not in rendered
    assert binding.root_label == "gpu-fusion"


def test_local_path_is_available_only_through_local_table(
    service: ProjectIntegrationService, sample_source: Path
) -> None:
    project = service.register(source_path=sample_source)
    binding = service.bindings.for_project(project.id)[0]
    assert service.bindings.local_path(binding.id) == sample_source.resolve()


def test_explicit_slug_and_display_name_are_used(
    service: ProjectIntegrationService, sample_source: Path
) -> None:
    project = service.register(source_path=sample_source, slug="gpu", display_name="GPU Fusion")
    assert project.slug == "gpu"
    assert project.display_name == "GPU Fusion"


def test_register_requires_existing_directory(
    service: ProjectIntegrationService, tmp_path: Path
) -> None:
    with pytest.raises(PolicyViolation):
        service.register(source_path=tmp_path / "yok")


def test_duplicate_slug_is_rejected(
    service: ProjectIntegrationService, sample_source: Path, tmp_path: Path
) -> None:
    service.register(source_path=sample_source, slug="gpu")
    other = tmp_path / "baska"
    other.mkdir()
    with pytest.raises(Exception, match="project_slug_unique_per_realm"):
        service.register(source_path=other, slug="gpu")


# -- cozumleme ------------------------------------------------------------------


def _resolver(service: ProjectIntegrationService) -> ProjectResolver:
    return ProjectResolver(service.connection, service.realm.id)


def test_resolve_by_identifier(service: ProjectIntegrationService, sample_source: Path) -> None:
    project = service.register(source_path=sample_source, slug="gpu")
    resolution = _resolver(service).resolve(str(project.id))
    assert resolution.kind is ResolutionKind.EXACT_ID
    assert resolution.resolved is not None
    assert resolution.resolved.project_id == project.id


def test_resolve_by_slug(service: ProjectIntegrationService, sample_source: Path) -> None:
    service.register(source_path=sample_source, slug="gpu")
    resolution = _resolver(service).resolve("gpu")
    assert resolution.kind is ResolutionKind.EXACT_SLUG
    assert resolution.is_resolved


def test_resolve_by_alias(service: ProjectIntegrationService, sample_source: Path) -> None:
    service.register(source_path=sample_source, slug="gpu", aliases=("GPU Projesi",))
    resolution = _resolver(service).resolve("gpu projesi")
    assert resolution.kind is ResolutionKind.EXACT_ALIAS
    assert resolution.resolved is not None
    assert resolution.resolved.matched_on.startswith("alias:")


def test_resolve_is_case_and_separator_insensitive(
    service: ProjectIntegrationService, sample_source: Path
) -> None:
    service.register(source_path=sample_source, slug="gpu", aliases=("GPU Fusion",))
    assert _resolver(service).resolve("  GPU   Fusion  ").is_resolved


def test_unknown_query_is_not_found(service: ProjectIntegrationService) -> None:
    resolution = _resolver(service).resolve("hicbir-zaman-var-olmayan-proje")
    assert resolution.kind is ResolutionKind.NOT_FOUND
    assert not resolution.is_resolved


def test_ambiguous_query_returns_candidates_without_resolving(
    service: ProjectIntegrationService, tmp_path: Path
) -> None:
    for slug in ("veri-servisi", "veri-servisleri"):
        root = tmp_path / slug
        root.mkdir()
        service.register(source_path=root, slug=slug)

    resolution = _resolver(service).resolve("veri servis")
    assert resolution.kind is ResolutionKind.AMBIGUOUS
    assert resolution.resolved is None
    assert resolution.requires_user_choice
    assert len(resolution.candidates) >= 2


def test_close_typo_resolves_when_single_strong_candidate(
    service: ProjectIntegrationService, sample_source: Path
) -> None:
    service.register(source_path=sample_source, slug="gpu-fusion")
    resolution = _resolver(service).resolve("gpu-fusio")
    assert resolution.kind is ResolutionKind.FUZZY
    assert resolution.is_resolved


def test_alias_is_unique_per_realm(
    service: ProjectIntegrationService, sample_source: Path, tmp_path: Path
) -> None:
    first = service.register(source_path=sample_source, slug="gpu", aliases=("ortak",))
    other = tmp_path / "digeri"
    other.mkdir()
    second = service.register(source_path=other, slug="digeri")
    with pytest.raises(Exception, match="alias_normalized_unique_per_realm"):
        service.projects.add_alias(ProjectAlias.create(project=second, alias="ortak"))
    assert first.id != second.id


# -- binding ve rebind ----------------------------------------------------------


def test_rebind_updates_local_path_and_locator(
    service: ProjectIntegrationService, sample_source: Path, tmp_path: Path
) -> None:
    project = service.register(source_path=sample_source)
    binding_before = service.bindings.for_project(project.id)[0]

    moved = tmp_path / "tasinmis"
    shutil.copytree(sample_source, moved)
    binding_after = service.rebind(project.id, source_path=moved)

    assert binding_after.id == binding_before.id
    assert binding_after.locator_digest == locator_digest_for(moved)
    assert binding_after.locator_digest != binding_before.locator_digest
    assert service.bindings.local_path(binding_after.id) == moved.resolve()


def test_mark_unbound_removes_local_path(
    service: ProjectIntegrationService, sample_source: Path
) -> None:
    project = service.register(source_path=sample_source)
    binding = service.bindings.for_project(project.id)[0]
    service.bindings.mark_unbound(binding.id)
    assert service.bindings.get(binding.id).status is BindingStatus.UNBOUND
    assert service.bindings.local_path(binding.id) is None


def test_missing_binding_raises_not_found(service: ProjectIntegrationService) -> None:
    with pytest.raises(NotFound):
        service.bindings.get(uuid4())


# -- tarama ve staleness --------------------------------------------------------


def test_scan_records_revision_and_profile(
    service: ProjectIntegrationService, sample_source: Path
) -> None:
    project = service.register(source_path=sample_source)
    result = service.scan(project.id)

    assert result.stage is IntegrationStage.CURRENT
    assert result.changed is True
    assert result.discovery.file_count == 3
    assert "fastapi" in [item.identifier for item in result.profile.frameworks]

    stored = service.profiles.latest_for_project(project.id)
    assert stored is not None
    assert stored[0] == result.profile.digest


def test_second_scan_without_change_is_not_marked_changed(
    service: ProjectIntegrationService, sample_source: Path
) -> None:
    project = service.register(source_path=sample_source)
    service.scan(project.id)
    assert service.scan(project.id).changed is False


def test_scan_after_source_change_is_marked_changed(
    service: ProjectIntegrationService, sample_source: Path
) -> None:
    project = service.register(source_path=sample_source)
    service.scan(project.id)
    _write(sample_source, "src/gpu/yeni.py", "x = 1\n")
    assert service.scan(project.id).changed is True


def test_revision_history_is_append_only(
    service: ProjectIntegrationService, sample_source: Path
) -> None:
    project = service.register(source_path=sample_source)
    service.scan(project.id)
    _write(sample_source, "src/gpu/yeni.py", "x = 1\n")
    service.scan(project.id)
    binding = service.bindings.for_project(project.id)[0]
    assert len(service.bindings.revision_history(binding.id)) == 2


def test_profile_is_idempotent_for_same_revision(
    service: ProjectIntegrationService, sample_source: Path
) -> None:
    project = service.register(source_path=sample_source)
    first = service.scan(project.id)
    second = service.scan(project.id)
    assert first.profile.digest == second.profile.digest


# -- entegrasyon raporu ---------------------------------------------------------


def test_evaluate_reports_bound_before_scan(
    service: ProjectIntegrationService, sample_source: Path
) -> None:
    project = service.register(source_path=sample_source)
    report = service.evaluate(project.id)
    assert report.stage is IntegrationStage.BOUND
    assert "scan" in report.next_action
    assert not report.is_current


def test_evaluate_reports_current_after_scan(
    service: ProjectIntegrationService, sample_source: Path
) -> None:
    project = service.register(source_path=sample_source)
    service.scan(project.id)
    report = service.evaluate(project.id)
    assert report.stage is IntegrationStage.CURRENT
    assert report.is_current
    assert report.blockers == ()
    assert report.profile_digest is not None


def test_evaluate_detects_same_head_dirty_content_drift(
    service: ProjectIntegrationService, sample_source: Path
) -> None:
    project = service.register(source_path=sample_source)
    service.scan(project.id)
    source = sample_source / "src" / "gpu" / "__init__.py"
    source.write_text("VALUE = 2\n", encoding="utf-8")

    report = service.evaluate(project.id)

    assert report.stage is IntegrationStage.STALE
    assert report.is_stale
    assert report.current_revision is not None
    assert report.observed_revision is not None
    assert report.current_revision.tree_digest != report.observed_revision.tree_digest


def test_evaluate_detects_non_git_tree_drift(
    service: ProjectIntegrationService, sample_source: Path
) -> None:
    project = service.register(source_path=sample_source)
    service.scan(project.id)
    _write(sample_source, "src/gpu/new.py", "VALUE = 3\n")

    report = service.evaluate(project.id)

    assert report.is_stale
    assert report.observed_revision is not None
    assert report.observed_revision.revision == report.observed_revision.tree_digest


def test_evaluate_reports_source_moved(
    service: ProjectIntegrationService, sample_source: Path, tmp_path: Path
) -> None:
    project = service.register(source_path=sample_source)
    service.scan(project.id)

    moved = tmp_path / "yeni-konum"
    shutil.move(str(sample_source), str(moved))
    binding = service.bindings.for_project(project.id)[0]
    # Yerel kayit eski yolu gosteriyor; once yeni yola isaret edelim ki tasima gorulsun.
    with service.connection.cursor() as cursor:
        cursor.execute(
            "update projects.source_binding_local set absolute_path = %s where binding_id = %s",
            (str(moved), binding.id),
        )

    report = service.evaluate(project.id)
    assert "source-moved" in report.blockers
    assert report.is_stale
    assert "rebind" in report.next_action


def test_evaluate_reports_unbound_when_local_path_is_gone(
    service: ProjectIntegrationService, sample_source: Path
) -> None:
    project = service.register(source_path=sample_source)
    service.scan(project.id)
    shutil.rmtree(sample_source)
    report = service.evaluate(project.id)
    assert report.stage is IntegrationStage.UNBOUND
    assert "local-path-unavailable" in report.blockers
    assert "rebind" in report.next_action


def test_project_listing_excludes_other_realms(
    service: ProjectIntegrationService, sample_source: Path
) -> None:
    service.register(source_path=sample_source)
    repository = ProjectRepository(service.connection, service.realm.id)
    assert [project.slug for project in repository.list_all()] == ["gpu-fusion"]


def test_binding_repository_rejects_cross_realm(
    service: ProjectIntegrationService, sample_source: Path
) -> None:
    project = service.register(source_path=sample_source)
    binding = service.bindings.for_project(project.id)[0]
    foreign = SourceBindingRepository(service.connection, uuid4())
    with pytest.raises(PolicyViolation):
        foreign.bind(binding, absolute_path=sample_source)
