"""Local tracked source fixtures; test discovery must never import their code."""

from __future__ import annotations

import io
import json
import subprocess
import zipfile

import pytest

from tests.unit.test_active_task_contract import _task
from zekam.application import architecture_export as subject
from zekam.domain.canonical import digest, digest_of_bytes
from zekam.domain.errors import PolicyViolation, ValidationFailed


def _git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


@pytest.fixture
def checkout(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    _git(root, "init")
    files = {
        "AKTIF_GOREV.md": _task()
        + (
            "\n### ZKM-REQ-001 — Fixture requirement\n"
            "| CAP-01 | Fixture capability | `partial` | WP-02 | Native acceptance |\n"
            "### WP-02 — Fixture package\n"
            "**Girdi/kapsam:** REQ-001; CAP-01.\n"
            "**Test source:** `tests/test_example.py`, `tests/test_missing.py`.\n"
            "- [ ] **WP-02.01** — Fixture source work\n"
        ),
        "pyproject.toml": '[project]\nname="fixture"\n',
        "src/zekam/interfaces/cli/example.py": (
            'raise RuntimeError("NEVER IMPORT")\n@app.command("hello")\ndef hello(): pass\n'
        ),
        "src/zekam/infrastructure/sqlite/example.py": 'MIGRATION_NAME="fixture-v1"\nDDL="create table example(id text);"\n',
        "tests/test_example.py": 'raise RuntimeError("NEVER IMPORT TESTS")\ndef test_example(): pass\n',
    }
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _git(root, "add", *files)
    _git(
        root,
        "-c",
        "user.name=fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "-c",
        "core.hooksPath=NUL",
        "commit",
        "-m",
        "fixture",
    )
    return root


def test_catalog_reads_source_declarations_without_import_or_test_execution(checkout):
    untracked = checkout / "src/zekam/private.py"
    untracked.write_text("this file must never be read", encoding="utf-8")
    catalog = subject.build_architecture_catalog(checkout)
    sources = {item["path"]: item for item in catalog["sources"]}
    assert "src/zekam/private.py" not in sources
    assert sources["src/zekam/interfaces/cli/example.py"]["interfaces"][0]["name"] == "hello"
    assert sources["src/zekam/infrastructure/sqlite/example.py"]["tables"][0]["name"] == "example"
    assert (
        sources["src/zekam/infrastructure/sqlite/example.py"]["migrations"][0]["value"]
        == "fixture-v1"
    )
    test = sources["tests/test_example.py"]["tests"][0]
    assert test["name"] == "test_example" and test["run"] is None and test["evidence"] is None
    assert catalog["acceptance_verified"] is False and catalog["grants_authority"] is False
    assert catalog["task"]["capabilities"][0]["work_packages"] == ["WP-02"]
    trace = catalog["source_traceability"]
    assert trace["missing_test_paths"] == ["tests/test_missing.py"]
    assert trace["release_chain_complete"] is False
    assert subject.build_architecture_catalog(checkout) == catalog


def test_all_views_and_deterministic_zip_share_one_catalog_and_hashes(checkout):
    catalog = subject.build_architecture_catalog(checkout)
    files = subject.render_architecture_bundle(catalog)
    assert subject.render_architecture_bundle(catalog) == files
    manifest = json.loads(files["manifest.json"])
    assert manifest["catalog_digest"] == catalog["catalog_digest"]
    for name, expected in manifest["files"].items():
        assert digest_of_bytes(files[name]) == expected
    with zipfile.ZipFile(io.BytesIO(files["architecture.zip"])) as bundle:
        assert set(bundle.namelist()) == set(files) - {"architecture.zip"}
        assert all(bundle.read(name) == files[name] for name in bundle.namelist())
    assert b"WP_02 --> CAP_01" in files["architecture.mmd"]
    assert catalog["catalog_digest"].encode() in files["architecture.md"]
    assert catalog["catalog_digest"].encode() in files["architecture.html"]
    assert b"miro-plan.json" in files["architecture.md"]
    assert b"gitbook-plan.json" in files["architecture.html"]
    miro = json.loads(files["miro-plan.json"])
    gitbook = json.loads(files["gitbook-plan.json"])
    assert miro["schema"] == "zekam-miro-local-export-plan/v1"
    assert gitbook["schema"] == "zekam-gitbook-local-export-plan/v1"
    assert miro["tool_usage"]["remote_calls"] == 0
    assert gitbook["published"] is False


def test_digest_and_source_changes_cannot_silently_reuse_catalog(checkout):
    original = subject.build_architecture_catalog(checkout)
    bad = original | {"acceptance_verified": True}
    with pytest.raises(ValidationFailed):
        subject.render_architecture_bundle(bad)
    path = checkout / "tests/test_example.py"
    path.write_text("def test_changed(): pass\n", encoding="utf-8")
    changed = subject.build_architecture_catalog(checkout)
    assert changed["catalog_digest"] != original["catalog_digest"]
    assert (
        changed["source_revision"] == original["source_revision"]
    )  # working tree basis is explicit


def test_local_export_is_new_target_only_and_cannot_write_inside_checkout(checkout, tmp_path):
    with pytest.raises(PolicyViolation):
        subject.export_architecture(checkout, checkout / "generated")
    destination = tmp_path / "export"
    report = subject.export_architecture(checkout, destination)
    assert report["published"] is False and report["grants_authority"] is False
    assert set(path.name for path in destination.iterdir()) == set(report["files"])
    with pytest.raises(PolicyViolation):
        subject.export_architecture(checkout, destination)


def test_duplicate_task_ids_and_unresolved_cap_links_are_rejected(checkout):
    task = checkout / "AKTIF_GOREV.md"
    raw = task.read_text(encoding="utf-8")
    task.write_text(raw + "\n- [x] **WP-02.01** — duplicate\n", encoding="utf-8")
    with pytest.raises(ValidationFailed, match="duplicate"):
        subject.build_architecture_catalog(checkout)
    task.write_text(raw.replace("| WP-02 |", "| WP-99 |"), encoding="utf-8")
    with pytest.raises(ValidationFailed, match="unknown Work"):
        subject.build_architecture_catalog(checkout)


def test_html_labels_are_escaped_and_not_executable(checkout):
    catalog = subject.build_architecture_catalog(checkout)
    catalog["task"]["capabilities"][0]["title"] = "<script>alert('x')</script>"
    catalog["catalog_digest"] = digest(
        {key: value for key, value in catalog.items() if key != "catalog_digest"}
    )
    page = subject.render_architecture_bundle(catalog)["architecture.html"]
    assert b"<script>" not in page and b"&lt;script&gt;" in page


def test_historical_task_appendix_never_becomes_current_capability_authority(checkout):
    task = checkout / "AKTIF_GOREV.md"
    with task.open("a", encoding="utf-8") as stream:
        stream.write("\n| D-01 | Pending decision | Actual evidence required |\n")
        stream.write("\n## Ek A — Historical\n### ZKM-REQ-001 — old duplicate\n")
        stream.write("| CAP-01 | Old capability | ready | accepted |\n")
    catalog = subject.build_architecture_catalog(checkout)
    assert len(catalog["task"]["capabilities"]) == len(catalog["task"]["requirements"]) == 1
    assert catalog["task"]["capabilities"][0]["declared_status"] == "partial"
    assert catalog["task"]["decision_gates"][0]["resolution_verified"] is False


def test_source_mutation_during_inventory_is_rejected(checkout, monkeypatch):
    original = subject._python_records

    def mutate(path, raw):
        records = original(path, raw)
        if path == "tests/test_example.py":
            (checkout / path).write_text("def test_raced(): pass\n", encoding="utf-8")
        return records

    monkeypatch.setattr(subject, "_python_records", mutate)
    with pytest.raises(PolicyViolation, match="content changed"):
        subject.build_architecture_catalog(checkout)


def test_persisted_readback_is_source_bound_and_never_runtime_acceptance(checkout, tmp_path):
    destination = tmp_path / "verified-export"
    written = subject.export_architecture(checkout, destination)
    before = {path.name: path.read_bytes() for path in destination.iterdir()}
    report = subject.verify_architecture_export(checkout, destination)
    assert report["file_readback_verified"] is True
    assert report["catalog_digest"] == written["catalog_digest"]
    assert report["files"] == written["files"]
    assert report["runtime_verified"] is False and report["acceptance_verified"] is False
    assert report["published"] is False and report["grants_authority"] is False
    assert {path.name: path.read_bytes() for path in destination.iterdir()} == before


@pytest.mark.parametrize(
    "name",
    [
        "catalog.json",
        "manifest.json",
        "architecture.md",
        "architecture.html",
        "architecture.mmd",
        "architecture.zip",
        "traceability.json",
        "miro-plan.json",
        "gitbook-plan.json",
    ],
)
def test_persisted_readback_rejects_every_changed_or_rehashed_bundle_member(
    checkout, tmp_path, name
):
    destination = tmp_path / "tampered-export"
    subject.export_architecture(checkout, destination)
    path = destination / name
    path.write_bytes(path.read_bytes() + b" ")
    if name not in {"manifest.json", "architecture.zip"}:
        manifest_path = destination / "manifest.json"
        manifest = json.loads(manifest_path.read_bytes())
        manifest["files"][name] = digest_of_bytes(path.read_bytes())
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises((PolicyViolation, ValidationFailed, OSError)):
        subject.verify_architecture_export(checkout, destination)


@pytest.mark.parametrize("change", ["missing", "extra", "directory", "source"])
def test_persisted_readback_rejects_incomplete_extra_and_stale_source(checkout, tmp_path, change):
    destination = tmp_path / "incomplete-export"
    subject.export_architecture(checkout, destination)
    if change == "missing":
        (destination / "architecture.html").unlink()
    elif change == "extra":
        (destination / "unexpected.json").write_text("{}", encoding="utf-8")
    elif change == "directory":
        (destination / "architecture.html").unlink()
        (destination / "architecture.html").mkdir()
    else:
        (checkout / "tests/test_example.py").write_text(
            "def test_new_revision(): pass\n", encoding="utf-8"
        )
    with pytest.raises((PolicyViolation, ValidationFailed, OSError)):
        subject.verify_architecture_export(checkout, destination)


def test_export_cannot_report_success_without_its_own_disk_readback(
    checkout, tmp_path, monkeypatch
):
    def reject(*_args):
        raise ValidationFailed("readback rejected")

    monkeypatch.setattr(subject, "_verify_export_files", reject)
    with pytest.raises(ValidationFailed, match="readback rejected"):
        subject.export_architecture(checkout, tmp_path / "failed-export")
