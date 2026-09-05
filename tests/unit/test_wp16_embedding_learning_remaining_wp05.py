from __future__ import annotations

import datetime as dt
import io
import json
import sqlite3
import struct
import subprocess
import zipfile
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from uuid import uuid4
from xml.etree import ElementTree

import pytest

from zekam.application import opencode_embedding as embedding
from zekam.application.memory_service import ReviewDecision
from zekam.application.model_registry import load_inventory
from zekam.application.projection_closure import ProjectionAwareClosureService
from zekam.domain.canonical import digest
from zekam.domain.errors import (
    ConfigurationError,
    PolicyViolation,
    ValidationFailed,
)
from zekam.domain.knowledge import SourceFormat
from zekam.domain.learning import SkillEvaluation, SkillFixture
from zekam.domain.model_inventory import HealthState, InventorySnapshot, Modality
from zekam.domain.security import SecretBackend, SecretRef
from zekam.infrastructure.knowledge import document_parsers as parsers
from zekam.infrastructure.sqlite import local_learning as learning
from zekam.infrastructure.sqlite import local_model_registry as registry

NOW = dt.datetime(2026, 9, 4, 12, tzinfo=dt.UTC)
MODEL = "openai/BAAI/bge-m3"


def _config(**provider_changes: object) -> dict[str, object]:
    provider: dict[str, object] = {
        "npm": "@ai-sdk/openai-compatible",
        "name": "AIHub",
        "options": {
            "baseURL": f"https://{embedding.AIHUB_PROVIDER_HOST}/v1",
            "apiKey": "{env:OPENCODE_KEY}",
            "timeout": 10,
        },
        "models": {MODEL: {"name": "BGE"}},
    }
    provider.update(provider_changes)
    return {"enabled_providers": ["litellm"], "provider": {"litellm": provider}}


def _write(path: Path, value: object) -> Path:
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "provider_id,change",
    [
        (" bad ", {}),
        ("litellm", {"npm": 3}),
        ("litellm", {"name": ""}),
        ("litellm", {"options": {"baseURL": 3, "apiKey": []}}),
        ("litellm", {"options": {"baseURL": "https://x", "apiKey": "literal"}}),
        ("litellm", {"models": {}}),
        ("litellm", {"models": {" ": {}}}),
        ("litellm", {"models": {MODEL: {"name": []}}}),
    ],
)
def test_opencode_catalog_remaining_guards(
    tmp_path: Path, provider_id: str, change: dict[str, object]
) -> None:
    with pytest.raises((ConfigurationError, PolicyViolation)):
        embedding.load_opencode_aihub_catalog(
            _write(tmp_path / "opencode.json", _config(**change)), provider_id=provider_id
        )


def test_opencode_secure_read_parent_and_size_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _write(tmp_path / "opencode.json", _config())
    original = embedding._is_link_or_reparse
    monkeypatch.setattr(
        embedding,
        "_is_link_or_reparse",
        lambda path: path == source.parent or original(path),
    )
    with pytest.raises(ConfigurationError, match="parent"):
        embedding._secure_json_document(source, max_bytes=100_000)
    monkeypatch.setattr(embedding, "_is_link_or_reparse", original)
    original_read = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda self: original_read(self) + b" ")
    with pytest.raises(ConfigurationError, match="degisti"):
        embedding._secure_json_document(source, max_bytes=100_000)


def test_opencode_binding_store_resolver_and_manifest_guards(tmp_path: Path) -> None:
    inventory = load_inventory()
    source = _write(tmp_path / "opencode.json", _config())
    config = embedding.load_opencode_embedding_configuration(
        source, provider_id="litellm", selected_model_id=MODEL, inventory=inventory
    )
    for provider, selected in ((" bad ", MODEL), ("litellm", " bad ")):
        with pytest.raises(ConfigurationError):
            embedding.load_opencode_embedding_configuration(
                source, provider_id=provider, selected_model_id=selected, inventory=inventory
            )
    backend = embedding.OpenCodeCredentialStore("litellm", "OPENCODE_KEY", {})
    wrong = SecretRef.create(
        realm_id=uuid4(),
        name="wrong",
        provider="litellm",
        purpose="embedding",
        allowed_operations=("embeddings",),
        store_backend=SecretBackend.OS_KEYCHAIN,
        store_locator="OPENCODE_KEY",
    )
    with pytest.raises(PolicyViolation):
        backend.resolve(wrong)
    resolver = embedding.OpenCodeEndpointResolver("litellm", "ref", config.embedding_endpoint)
    with pytest.raises(PolicyViolation):
        resolver.resolve("bad", "embeddings")
    with pytest.raises(PolicyViolation):
        resolver.resolve("ref", "chat")
    with pytest.raises(ValidationFailed):
        embedding.build_opencode_embedding_probe_manifest(())
    with pytest.raises(ValidationFailed):
        embedding.build_opencode_embedding_probe_manifest(
            (config, replace(config, provider_id="other"))
        )


def test_opencode_health_and_response_remaining_branches(tmp_path: Path) -> None:
    base = load_inventory()
    source = _write(tmp_path / "opencode.json", _config())
    config = embedding.load_opencode_embedding_configuration(
        source, provider_id="litellm", selected_model_id=MODEL, inventory=base
    )
    with pytest.raises(PolicyViolation):
        embedding.evaluate_opencode_aihub_models(
            embedding.OpenCodeModelCatalog("x", (), digest("x"), "other"), base
        )
    records = tuple(
        replace(
            record,
            enabled=False,
            health_state=HealthState.QUARANTINED,
        )
        for record in base.records
    )
    changed = InventorySnapshot(base.schema, base.inventory_date, records)
    result = embedding.evaluate_embedding_candidates((), changed)
    assert any(len(item.reasons) >= 3 for item in result)
    response: dict[str, object] = {
        "data": [{"embedding": [1.0, 0.0]} for _ in embedding.SYNTHETIC_EMBEDDING_FIXTURE]
    }
    with pytest.raises(ValidationFailed, match="latency"):
        embedding.evaluate_opencode_embedding_response(config, response, latency_ms=-1)
    response["data"] = [{"embedding": []} for _ in embedding.SYNTHETIC_EMBEDDING_FIXTURE]
    with pytest.raises(ValidationFailed):
        embedding.evaluate_opencode_embedding_response(config, response, latency_ms=1)


@pytest.mark.parametrize("enabled", [1, [], [""], ["other"]])
def test_opencode_enabled_provider_and_configuration_model_guards(
    tmp_path: Path, enabled: object
) -> None:
    document = _config()
    document["enabled_providers"] = enabled
    source = _write(tmp_path / "opencode.json", document)
    with pytest.raises(ConfigurationError, match="enabled"):
        embedding.load_opencode_aihub_catalog(source, provider_id="litellm")
    with pytest.raises(ConfigurationError, match="enabled"):
        embedding.load_opencode_embedding_configuration(
            source,
            provider_id="litellm",
            selected_model_id=MODEL,
            inventory=load_inventory(),
        )


def test_opencode_valid_resolver_and_inconsistent_vector_dimensions(tmp_path: Path) -> None:
    source = _write(tmp_path / "opencode.json", _config())
    config = embedding.load_opencode_embedding_configuration(
        source,
        provider_id="litellm",
        selected_model_id=MODEL,
        inventory=load_inventory(),
    )
    resolver = embedding.OpenCodeEndpointResolver("litellm", "ref", config.embedding_endpoint)
    assert resolver.resolve("ref", "embeddings") == config.embedding_endpoint
    vectors = [[1.0, 0.0] for _ in embedding.SYNTHETIC_EMBEDDING_FIXTURE]
    vectors[-1] = [1.0]
    with pytest.raises(ValidationFailed):
        embedding.evaluate_opencode_embedding_response(
            config,
            {"data": [{"embedding": item} for item in vectors]},
            latency_ms=1,
        )
    response: dict[str, object] = {
        "data": [{"embedding": [float("nan"), 0.0]} for _ in embedding.SYNTHETIC_EMBEDDING_FIXTURE]
    }
    with pytest.raises(ValidationFailed, match="sonlu"):
        embedding.evaluate_opencode_embedding_response(config, response, latency_ms=1)


@pytest.mark.parametrize(
    "change",
    [
        {"npm": []},
        {"name": []},
        {"options": {"baseURL": 3, "apiKey": []}},
        {"models": {}},
        {"models": {" ": {}}},
        {"models": {MODEL: {"name": []}}},
    ],
)
def test_opencode_configuration_remaining_provider_fields(
    tmp_path: Path, change: dict[str, object]
) -> None:
    with pytest.raises(ConfigurationError):
        embedding.load_opencode_embedding_configuration(
            _write(tmp_path / "opencode.json", _config(**change)),
            provider_id="litellm",
            selected_model_id=MODEL,
            inventory=load_inventory(),
        )


def test_opencode_missing_selection_and_conflicting_unhealthy_candidate(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="bulunamadi"):
        embedding.load_opencode_embedding_configuration(
            _write(
                tmp_path / "opencode.json",
                _config(models={"openai/other": {"name": "Other"}}),
            ),
            provider_id="litellm",
            selected_model_id=MODEL,
            inventory=load_inventory(),
        )
    base = load_inventory()
    records = tuple(
        replace(record, declared_mode="chat", health_state=HealthState.UNTESTED)
        if record.modality is Modality.EMBEDDING
        else record
        for record in base.records
    )
    results = embedding.evaluate_embedding_candidates(
        (MODEL,), InventorySnapshot(base.schema, base.inventory_date, records)
    )
    candidate = next(item for item in results if item.access_name == MODEL)
    assert "canonical-modality-conflict" in candidate.reasons
    assert "canonical-health-not-eligible" in candidate.reasons


def _docx(document: str, *, styles: str | None = None) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", document)
        if styles is not None:
            archive.writestr("word/styles.xml", styles)
    return stream.getvalue()


def test_document_parser_akilli_shaped_ooxml_and_image_boundaries() -> None:
    ns = parsers._WORD_NS
    document = (
        f'<w:document xmlns:w="{ns}"><w:body>'
        '<w:p><w:pPr><w:pStyle w:val="deep"/></w:pPr><w:r><w:t>Akilli Kasa</w:t>'
        "<w:tab/><w:t>Durum</w:t><w:br/></w:r></w:p>"
        "<w:p/>"
        "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Alan</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>Deger</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
        "</w:body></w:document>"
    )
    styles = (
        f'<w:styles xmlns:w="{ns}"><w:style w:styleId="missing"/>'
        '<w:style w:styleId="deep"><w:name w:val="Heading 3"/></w:style></w:styles>'
    )
    units = parsers.DocxParser().parse(_docx(document, styles=styles))
    assert len(units) == 2 and "\t" in units[0].text and "Alan | Deger" in units[1].text
    with pytest.raises(ValidationFailed, match="body"):
        parsers.DocxParser().parse(_docx(f'<w:document xmlns:w="{ns}"/>'))
    with pytest.raises(ValidationFailed, match="icerik"):
        parsers.DocxParser().parse(
            _docx(f'<w:document xmlns:w="{ns}"><w:body><w:p/></w:body></w:document>')
        )
    with pytest.raises(ValidationFailed):
        parsers._png_dimensions(b"bad")
    with pytest.raises(ValidationFailed):
        parsers._jpeg_dimensions(b"\xff\xd8\xff\xe0\x00\x01")
    assert parsers._tiff_value(b"", "<", 3, 2, b"\0" * 4) is None
    with pytest.raises(ValidationFailed, match="header"):
        parsers._tiff_dimensions(b"II\x01\x00\x00\x00\x00\x00")
    with pytest.raises(ValidationFailed, match="gorsel format"):
        parsers._dimensions(cast(Any, object()), b"")


def test_document_parser_tiff_chain_and_tool_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Little-endian single-page TIFF derived from a tiny scanned-note fixture.
    payload = b"II*\x00\x08\x00\x00\x00" + struct.pack("<H", 0) + struct.pack("<I", 8)
    with pytest.raises(ValidationFailed):
        parsers._tiff_dimensions(payload)

    def timeout(*args: object, **kwargs: object) -> Any:
        del args, kwargs
        raise subprocess.TimeoutExpired("x", 1)

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(ConfigurationError):
        parsers._tesseract_version("tesseract")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: cast(Any, type("R", (), {"stdout": "unexpected"})()),
    )
    with pytest.raises(ConfigurationError, match="surum"):
        parsers._tesseract_version("tesseract")
    with pytest.raises(ConfigurationError, match="konumu"):
        parsers._tessdata_digests("tesseract", ("tur",))
    with pytest.raises(ValidationFailed, match="media type"):
        parsers.media_type_for(cast(Any, object()))


def test_document_parser_archive_and_raster_remaining_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ns = parsers._WORD_NS
    minimal = _docx(
        f'<w:document xmlns:w="{ns}"><w:body><w:p><w:r>'
        "<w:t>Akilli</w:t></w:r></w:p></w:body></w:document>"
    )
    for name, value, message in (
        ("MAX_DOCX_ENTRIES", 1, "girdi"),
        ("MAX_DOCX_EXPANDED_BYTES", 1, "acilmis"),
        ("MAX_DOCX_COMPRESSION_RATIO", 0, "sikistirma"),
    ):
        with monkeypatch.context() as scoped:
            scoped.setattr(parsers, name, value)
            with pytest.raises(PolicyViolation, match=message):
                parsers.DocxParser().parse(minimal)
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
    with pytest.raises(ValidationFailed, match="zorunlu"):
        parsers.DocxParser().parse(stream.getvalue())
    table_only_empty = _docx(
        f'<w:document xmlns:w="{ns}"><w:body><w:tbl>'
        "<w:tr><w:tc/></w:tr></w:tbl></w:body></w:document>"
    )
    with pytest.raises(ValidationFailed, match="icerik"):
        parsers.DocxParser().parse(table_only_empty)
    with pytest.raises(ValidationFailed, match="boyut bilgisi"):
        parsers._jpeg_dimensions(b"\xff\xd8\x00\x00\xff\xd9")
    with pytest.raises(ValidationFailed, match="segment"):
        parsers._jpeg_dimensions(b"\xff\xd8\xff\xe0\x00\xff")
    with monkeypatch.context() as scoped:
        scoped.setattr(parsers, "_png_dimensions", lambda payload: ((0, 1),))
        with pytest.raises(ValidationFailed, match="boyutu"):
            parsers._dimensions(SourceFormat.PNG, b"x")
    with monkeypatch.context() as scoped:
        scoped.setattr(parsers, "_png_dimensions", lambda payload: ((parsers.MAX_IMAGE_PIXELS, 2),))
        with pytest.raises(PolicyViolation, match="pixel"):
            parsers._dimensions(SourceFormat.PNG, b"x")


def test_document_parser_ocr_and_pdf_fail_closed_branches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValidationFailed, match="payload"):
        parsers.TesseractOcrParser(SourceFormat.PNG).parse(b"")

    class Info:
        flags = ("V8",)
        origin = "local"

    monkeypatch.setitem(
        __import__("sys").modules,
        "pypdfium2_raw.version",
        cast(Any, type("M", (), {"PDFIUM_INFO": Info})()),
    )
    with pytest.raises(PolicyViolation, match="V8"):
        parsers._pdfium_build_profile(object())
    with pytest.raises(ValidationFailed, match="imzasi"):
        parsers.PdfParser().parse(b"bad")


def test_document_parser_tiff_structural_failures() -> None:
    header = b"II*\x00\x08\x00\x00\x00"
    with pytest.raises(ValidationFailed, match="girdisi"):
        parsers._tiff_dimensions(header + struct.pack("<H", 1))
    entry_width = struct.pack("<HHI", 256, 4, 1) + struct.pack("<I", 1)
    entry_height = struct.pack("<HHI", 257, 4, 1) + struct.pack("<I", 1)
    with pytest.raises(ValidationFailed, match="sonraki"):
        parsers._tiff_dimensions(header + struct.pack("<H", 2) + entry_width + entry_height)


def test_document_parser_remaining_style_marker_cycle_and_empty_pdf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = ElementTree.fromstring(f'<w:styles xmlns:w="{parsers._WORD_NS}"><w:style/></w:styles>')
    assert parsers.DocxParser._heading_styles(root) == {}
    with pytest.raises(ValidationFailed, match="boyut bilgisi"):
        parsers._jpeg_dimensions(b"\xff\xd8\xff\xd8")
    with pytest.raises(ValidationFailed, match="segment"):
        parsers._jpeg_dimensions(b"\xff\xd8\xff\xd8\xff\xe0\x00\x01")
    header = b"II*\x00\x08\x00\x00\x00"
    width = struct.pack("<HHI", 256, 4, 1) + struct.pack("<I", 1)
    height = struct.pack("<HHI", 257, 4, 1) + struct.pack("<I", 1)
    cycle = header + struct.pack("<H", 2) + width + height + struct.pack("<I", 8)
    with pytest.raises(ValidationFailed, match="zinciri"):
        parsers._tiff_dimensions(cycle)

    class EmptyDocument:
        closed = False

        def __len__(self) -> int:
            return 0

        def close(self) -> None:
            self.closed = True

    document = EmptyDocument()
    module = type("PdfModule", (), {"PdfDocument": staticmethod(lambda payload: document)})()
    monkeypatch.setattr(parsers.PdfParser, "_module", lambda self: module)
    monkeypatch.setattr(parsers, "_pdfium_build_profile", lambda value: {})
    with pytest.raises(PolicyViolation, match="sayfa"):
        parsers.PdfParser().parse(b"%PDF-1.7")


def _private(path: Path) -> Path:
    path.mkdir(parents=True, mode=0o700)
    return path


def _registry(tmp_path: Path) -> registry.SQLiteLocalModelRegistry:
    store = registry.SQLiteLocalModelRegistry(
        (_private(tmp_path / "registry") / "models.db").resolve()
    )
    store.bootstrap()
    return store


def _snapshot(*models: registry.LocalModelIdentity) -> registry.LocalDiscoverySnapshot:
    return registry.LocalDiscoverySnapshot(
        "device", "opencode", "1", digest("client"), True, models, NOW, NOW + dt.timedelta(hours=1)
    )


def test_registry_discovery_and_remaining_routing_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "opencode"
    executable.write_bytes(b"binary")
    executable.chmod(0o700)
    private = _private(tmp_path / "home")
    expected = "sha256:" + __import__("hashlib").sha256(b"binary").hexdigest()
    with pytest.raises(ValidationFailed):
        registry.discover_installed_client(
            executable,
            client_id="other",
            device_id="d",
            private_root=private,
            expected_artifact_digest=expected,
            now=NOW,
        )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: cast(Any, type("R", (), {"returncode": 1, "stdout": b"", "stderr": b""})()),
    )
    with pytest.raises(PolicyViolation, match="version"):
        registry.discover_installed_client(
            executable,
            client_id="codex",
            device_id="d",
            private_root=private,
            expected_artifact_digest=expected,
            now=NOW,
        )
    store = _registry(tmp_path)
    model = registry.LocalModelIdentity("openai", "gpt-5", "1")
    snapshot = _snapshot(model)
    store.reconcile(snapshot)
    with pytest.raises(ValidationFailed):
        store.reconcile(cast(Any, object()))
    with pytest.raises(ValidationFailed):
        store.routable(device_id="d", client_id="other", now=NOW)
    assert store.routable(device_id="device", client_id="codex", now=NOW) == ()


def test_registry_discovery_identity_digest_and_success_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "opencode"
    executable.write_bytes(b"binary")
    executable.chmod(0o700)
    private = _private(tmp_path / "private")
    with pytest.raises(PolicyViolation, match="digest"):
        registry.discover_installed_client(
            executable,
            client_id="codex",
            device_id="d",
            private_root=private,
            expected_artifact_digest=digest("wrong"),
            now=NOW,
        )


def test_registry_remaining_discovery_path_size_model_failure_and_codex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    private = _private(tmp_path / "private")
    relative = Path("opencode")
    with pytest.raises(ValidationFailed, match="path"):
        registry.discover_installed_client(
            relative,
            client_id="codex",
            device_id="d",
            private_root=private,
            expected_artifact_digest=digest("x"),
            now=NOW,
        )
    empty = tmp_path / "empty"
    empty.touch(mode=0o700)
    with pytest.raises(PolicyViolation, match="size"):
        registry.discover_installed_client(
            empty,
            client_id="codex",
            device_id="d",
            private_root=private,
            expected_artifact_digest="sha256:" + "0" * 64,
            now=NOW,
        )
    executable = tmp_path / "client"
    executable.write_bytes(b"x")
    executable.chmod(0o700)
    expected = "sha256:" + __import__("hashlib").sha256(b"x").hexdigest()
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: cast(
            Any,
            type("R", (), {"returncode": 0, "stdout": b"1\n", "stderr": b""})(),
        ),
    )
    codex = registry.discover_installed_client(
        executable,
        client_id="codex",
        device_id="d",
        private_root=private,
        expected_artifact_digest=expected,
        now=NOW,
    )
    assert not codex.listing_supported and not codex.models
    calls = 0

    def failed_models(*args: object, **kwargs: object) -> Any:
        nonlocal calls
        del args, kwargs
        calls += 1
        return type(
            "R",
            (),
            {"returncode": 0 if calls == 1 else 1, "stdout": b"1\n", "stderr": b""},
        )()

    monkeypatch.setattr(subprocess, "run", failed_models)
    with pytest.raises(PolicyViolation, match="model discovery"):
        registry.discover_installed_client(
            executable,
            client_id="opencode",
            device_id="d",
            private_root=private,
            expected_artifact_digest=expected,
            now=NOW,
        )
    responses = iter(
        (
            type("R", (), {"returncode": 0, "stdout": b"1.0\n", "stderr": b""})(),
            type("R", (), {"returncode": 0, "stdout": b"openai/gpt-5\n", "stderr": b""})(),
        )
    )
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: next(responses))
    observed = registry.discover_installed_client(
        executable,
        client_id="opencode",
        device_id="d",
        private_root=private,
        expected_artifact_digest=expected,
        now=NOW,
    )
    assert observed.listing_supported and observed.models[0].exact_id == "openai/gpt-5"
    executable.chmod(0o722)
    with pytest.raises(PolicyViolation, match="identity"):
        registry.discover_installed_client(
            executable,
            client_id="codex",
            device_id="d",
            private_root=private,
            expected_artifact_digest=expected,
            now=NOW,
        )


def test_registry_reconcile_ambiguous_removed_and_health_history(tmp_path: Path) -> None:
    store = _registry(tmp_path)
    model = registry.LocalModelIdentity("openai", "gpt-5", "1")
    first = _snapshot(model)
    assert store.reconcile(first)["new"] == 1
    second = replace(
        first,
        models=(),
        observed_at=NOW + dt.timedelta(minutes=1),
        expires_at=NOW + dt.timedelta(hours=2),
    )
    assert store.reconcile(second)["removed"] == 1
    assert (
        store.routable(device_id="device", client_id="opencode", now=NOW + dt.timedelta(minutes=2))
        == ()
    )
    other = _registry(tmp_path / "other")
    ambiguous = _snapshot(model, replace(model))
    assert other.reconcile(ambiguous)["ambiguous"] == 1
    with pytest.raises(PolicyViolation, match="timestamp"):
        registry._parse_instant("2026-09-04T12:00:00")


def test_learning_remaining_exact_input_guards(tmp_path: Path) -> None:
    operational = (tmp_path / "operational.db").resolve()
    sqlite3.connect(operational).close()
    store = learning.SQLiteLocalLearning(
        (tmp_path / "learning.db").resolve(), operational_path=operational
    )
    store.bootstrap()
    with pytest.raises(ValidationFailed):
        store.review_skill(digest("m"), digest("e"), cast(Any, object()), now=NOW)
    decision = cast(Any, type("D", (), {"approved": 1})())
    with pytest.raises(ValidationFailed):
        store.review_skill(digest("m"), digest("e"), decision, now=NOW)
    for outcome in (None, True, "unknown"):
        with pytest.raises(ValidationFailed):
            store.record_skill_outcome(
                digest("activation"),
                run_ref="run",
                usage_digest=digest("usage"),
                outcome=cast(Any, outcome),
                verifier_ref="verifier",
                now=NOW,
            )
    for finding in (None, True, "unknown"):
        with pytest.raises(ValidationFailed):
            store.propose_hygiene(digest("subject"), cast(Any, finding), now=NOW)


def test_learning_remaining_precondition_and_identity_matrix(tmp_path: Path) -> None:
    with pytest.raises(PolicyViolation):
        learning._parse_time(None)
    operational = (tmp_path / "operational.db").resolve()
    sqlite3.connect(operational).close()
    store = learning.SQLiteLocalLearning(
        (tmp_path / "learning.db").resolve(), operational_path=operational
    )
    store.bootstrap()
    with pytest.raises(ValidationFailed):
        store.review_memory(digest("candidate"), cast(Any, object()), now=NOW)
    decision = ReviewDecision(True, "reviewer", "approved")
    object.__setattr__(decision, "approved", 1)
    with pytest.raises(ValidationFailed, match="bool"):
        store.review_memory(digest("candidate"), decision, now=NOW)
    with pytest.raises(PolicyViolation, match="approved"):
        store.activate_memory(digest("candidate"), digest("review"), now=NOW)
    with pytest.raises(ValidationFailed):
        store.observe_failure(cast(Any, object()))
    with pytest.raises(ValidationFailed):
        store.create_failure_card(digest("signature"), cast(Any, object()), now=NOW)
    with pytest.raises(PolicyViolation, match="Lesson"):
        store.extract_lesson(digest("card"), "lesson", author_ref="author", now=NOW)
    with pytest.raises(ValidationFailed):
        store.propose_skill(cast(Any, object()), digest("lesson"), now=NOW)
    with pytest.raises(ValidationFailed):
        store.evaluate_skill(digest("manifest"), cast(Any, object()), now=NOW)
    evaluation = SkillEvaluation(
        "skill",
        (SkillFixture("fixture", "1", digest("input")),),
        5,
        5,
        "evaluator",
        "verifier",
        0.5,
    )
    object.__setattr__(evaluation, "trials", True)
    with pytest.raises(ValidationFailed, match="numeric"):
        store.evaluate_skill(digest("manifest"), evaluation, now=NOW)


def test_learning_private_parent_and_store_file_identity(tmp_path: Path) -> None:
    parent = tmp_path / "shared"
    parent.mkdir(mode=0o777)
    parent.chmod(0o777)
    store = learning.SQLiteLocalLearning(
        (parent / "learning.db").resolve(),
        operational_path=(tmp_path / "operational.db").resolve(),
    )
    with pytest.raises(PolicyViolation, match="private parent"):
        store.bootstrap()
    parent.chmod(0o700)
    sqlite3.connect(store.operational_path).close()
    store.bootstrap()
    store.path.chmod(0o644)
    with pytest.raises(PolicyViolation, match="identity"):
        store.audit()


def test_learning_operational_and_missing_skill_evidence_guards(tmp_path: Path) -> None:
    operational = (tmp_path / "operational.db").resolve()
    sqlite3.connect(operational).close()
    operational.chmod(0o644)
    store = learning.SQLiteLocalLearning(
        (tmp_path / "learning.db").resolve(), operational_path=operational
    )
    store.bootstrap()
    with pytest.raises(PolicyViolation, match="operational evidence identity"):
        store._operational()
    draft = learning.SkillManifestDraft(
        "skill",
        1,
        "purpose",
        ("trigger",),
        ("input",),
        ("output",),
        (),
        ("step",),
        ("check",),
        ("risk",),
        "read-only",
        ("receipt",),
        "rollback",
        "deprecate",
        "author",
    )
    with pytest.raises(PolicyViolation, match="durable lesson"):
        store.propose_skill(draft, digest("lesson"), now=NOW)
    decision = ReviewDecision(True, "reviewer", "ok")
    object.__setattr__(decision, "approved", 1)
    with pytest.raises(ValidationFailed, match="bool"):
        store.review_skill(digest("manifest"), digest("evaluation"), decision, now=NOW)


def test_projection_remaining_snapshot_and_service_guards() -> None:
    namespace: dict[str, Any] = {}
    source = Path("tests/unit/test_projection_closure.py").read_text(encoding="utf-8")
    exec(compile(source, "projection_fixture", "exec"), namespace)
    fixture_now = cast(dt.datetime, namespace["NOW"])
    receipt, snapshot = namespace["_fixture"]()
    for changed in (
        replace(snapshot, other_open_job_count=1),
        replace(snapshot, other_receiptless_claim_count=1),
        replace(snapshot, lease_expires_at=fixture_now),
    ):
        with pytest.raises(PolicyViolation):
            changed.assert_ready(now=fixture_now)
    service = ProjectionAwareClosureService(
        namespace["Repository"](snapshot), namespace["Authorizations"]()
    )
    object.__setattr__(receipt, "pending_steps", (namespace["_ref"]("pending"),))
    with pytest.raises(PolicyViolation):
        service.prepare(receipt, idempotency_key="key", now=fixture_now)
    receipt, snapshot = namespace["_fixture"]()
    service = ProjectionAwareClosureService(
        namespace["Repository"](snapshot), namespace["Authorizations"]()
    )
    plan = service.prepare(receipt, idempotency_key="projection-close:remaining", now=fixture_now)
    with pytest.raises(ValidationFailed):
        service.replay_completed(
            receipt,
            idempotency_key="bad key",
            plan_digest=plan.plan_digest,
            authorization_id=uuid4(),
            claim_id=uuid4(),
        )


def test_projection_snapshot_plan_and_prepare_defensive_matrix() -> None:
    namespace: dict[str, Any] = {}
    exec(
        compile(
            Path("tests/unit/test_projection_closure.py").read_text(encoding="utf-8"),
            "projection_fixture",
            "exec",
        ),
        namespace,
    )
    fixture_now = cast(dt.datetime, namespace["NOW"])
    receipt, snapshot = namespace["_fixture"]()
    bad_release = replace(snapshot.release)
    object.__setattr__(bad_release, "work_revision", snapshot.release.work_revision + 1)
    invalid_snapshots = (
        {"release": bad_release},
        {"run_id": cast(Any, type(snapshot.run_id)(int=0))},
        {"lease_worker_label": " "},
        {"lease_expires_at": fixture_now.replace(tzinfo=None)},
        {"pre_close_previous_digest": None},
        {"pre_close_outbox_payload_digest": digest("wrong")},
        {"other_open_job_count": -1},
        {"grants_authority": True},
    )
    for changes in invalid_snapshots:
        with pytest.raises((PolicyViolation, ValidationFailed)):
            replace(snapshot, **cast(Any, changes))
    with pytest.raises(ValidationFailed):
        snapshot.assert_ready(now=fixture_now.replace(tzinfo=None))
    active_snapshot = replace(snapshot)
    object.__setattr__(
        active_snapshot,
        "work_item",
        snapshot.work_item.with_state(namespace["WorkState"].ACTIVE),
    )
    with pytest.raises(PolicyViolation):
        active_snapshot.assert_ready(now=fixture_now)

    service = ProjectionAwareClosureService(
        namespace["Repository"](snapshot), namespace["Authorizations"]()
    )
    plan = service.prepare(receipt, idempotency_key="projection-close:defensive", now=fixture_now)
    for changes in (
        {"grants_authority": True},
        {"pre_close_previous_digest": None},
        {"result_digest": digest("wrong")},
    ):
        with pytest.raises(PolicyViolation):
            replace(plan, **cast(Any, changes)).assert_integrity()
    with pytest.raises(ValidationFailed):
        namespace["ProjectionClosurePlan"].create(
            receipt=receipt,
            completed_work=plan.completed_work,
            projection_receipt=plan.projection_receipt,
            idempotency_key="bad key",
            snapshot=snapshot,
        )
    with pytest.raises(ValidationFailed):
        service.apply(
            plan,
            authorization_id=uuid4(),
            claim_id=uuid4(),
            now=fixture_now.replace(tzinfo=None),
        )

    bad_receipt, bad_snapshot = namespace["_fixture"]()
    object.__setattr__(bad_receipt, "status", namespace["CloseStatus"].RECOVERY_REQUIRED)
    with pytest.raises(PolicyViolation):
        ProjectionAwareClosureService(
            namespace["Repository"](bad_snapshot), namespace["Authorizations"]()
        ).prepare(bad_receipt, idempotency_key="projection-close:status", now=fixture_now)
    empty_receipt, empty_snapshot = namespace["_fixture"]()
    object.__setattr__(empty_receipt, "verified_outcomes", ())
    with pytest.raises(PolicyViolation):
        ProjectionAwareClosureService(
            namespace["Repository"](empty_snapshot), namespace["Authorizations"]()
        ).prepare(empty_receipt, idempotency_key="projection-close:evidence", now=fixture_now)
    late_receipt, late_snapshot = namespace["_fixture"]()
    object.__setattr__(late_receipt, "closed_at", fixture_now + dt.timedelta(minutes=1))
    with pytest.raises(PolicyViolation):
        ProjectionAwareClosureService(
            namespace["Repository"](late_snapshot), namespace["Authorizations"]()
        ).prepare(late_receipt, idempotency_key="projection-close:late", now=fixture_now)
