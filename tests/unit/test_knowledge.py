"""P11-T01..T06 knowledge ingestion sozlesmesi testleri."""

from __future__ import annotations

import datetime as dt

import pytest

from zekam.application.knowledge_ingestion import (
    IngestionService,
    PlSqlObjectExtractor,
    PythonSymbolExtractor,
    database_units,
    pending_version,
)
from zekam.application.knowledge_parsers import (
    MarkdownParser,
    OcrParser,
    PlainTextParser,
    StructuredDocumentParser,
    default_router,
)
from zekam.domain.canonical import digest, digest_of_bytes
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.knowledge import (
    Artifact,
    ContentUnit,
    DatabaseObject,
    IngestionJob,
    IngestionStage,
    Locator,
    NormalizedDocument,
    ScanLimits,
    SourceFormat,
    SourceVersion,
    UnitKind,
    VersionState,
    is_denied,
)

NOW = dt.datetime(2026, 8, 21, tzinfo=dt.UTC)
CONTENT = digest("content")


def _artifact(name: str = "rapor.md", payload: bytes = b"# baslik\n\nmetin\n") -> Artifact:
    return Artifact(
        artifact_id="a1",
        content_digest=digest_of_bytes(payload),
        byte_size=len(payload),
        media_type="text/markdown",
        original_name=name,
        stored_at=NOW,
    )


# -- T01: artifact ve surum ---------------------------------------------------


def test_artifact_absolute_path_ve_deny_listeyi_reddeder() -> None:
    with pytest.raises(PolicyViolation):
        _artifact(name="/etc/passwd")
    with pytest.raises(PolicyViolation):
        _artifact(name="../disari/rapor.md")
    with pytest.raises(PolicyViolation):
        _artifact(name=".env")
    with pytest.raises(PolicyViolation):
        _artifact(name="keys/id_rsa")


@pytest.mark.parametrize(
    "path", ["gizli/.env", "config/credentials.json", "deploy/server.pem", ".npmrc"]
)
def test_deny_list_secret_dosyalarini_yakalar(path: str) -> None:
    assert is_denied(path) is True


def test_deny_list_mesru_dosyayi_engellemez() -> None:
    assert is_denied("docs/environment.md") is False
    assert is_denied("src/zekam/domain/knowledge.py") is False


def _job(stages: tuple[IngestionStage, ...] = ()) -> IngestionJob:
    return IngestionJob(
        job_id="j1",
        source_id="s1",
        artifact_digest=_artifact().artifact_digest,
        idempotency_key="key-1",
        completed_stages=stages,
    )


def _version(state: VersionState = VersionState.PENDING) -> SourceVersion:
    return SourceVersion(
        version_id="v1",
        source_id="s1",
        revision=1,
        artifact_digest=_artifact().artifact_digest,
        content_digest=CONTENT,
        state=state,
        created_at=NOW,
    )


def test_ingestion_asamasi_atlanamaz() -> None:
    job = _job()
    assert job.next_stage is IngestionStage.VALIDATED
    with pytest.raises(ValidationFailed):
        job.advance(IngestionStage.PARSED)
    advanced = job.advance(IngestionStage.VALIDATED)
    assert advanced.next_stage is IngestionStage.STORED


def test_basarisiz_ingestion_sessizce_devam_edemez() -> None:
    failed = _job().advance(IngestionStage.VALIDATED).fail("parser cokti")
    assert failed.next_stage is None
    assert failed.is_complete is False
    with pytest.raises(PolicyViolation):
        failed.advance(IngestionStage.STORED)


def test_tamamlanmamis_ingestion_aktif_surum_uretemez() -> None:
    partial = _job().advance(IngestionStage.VALIDATED).advance(IngestionStage.STORED)
    with pytest.raises(PolicyViolation):
        _version().activate(partial)


def test_tamamlanan_ingestion_atomik_aktivasyon_yapar() -> None:
    job = _job()
    for stage in (
        IngestionStage.VALIDATED,
        IngestionStage.STORED,
        IngestionStage.PARSED,
        IngestionStage.NORMALIZED,
        IngestionStage.INDEXED,
        IngestionStage.ACTIVATED,
    ):
        job = job.advance(stage)
    assert job.is_complete is True
    active = _version().activate(job)
    assert active.state is VersionState.ACTIVE


def test_baska_artifacta_ait_ingestion_aktive_edemez() -> None:
    job = _job()
    for stage in IngestionStage:
        job = job.advance(stage)
    other = SourceVersion(
        version_id="v2",
        source_id="s1",
        revision=2,
        artifact_digest=digest("baska"),
        content_digest=CONTENT,
        state=VersionState.PENDING,
        created_at=NOW,
    )
    with pytest.raises(ValidationFailed):
        other.activate(job)


def test_supersede_yalniz_aktif_surumden_olur() -> None:
    with pytest.raises(PolicyViolation):
        _version().supersede("v2")
    active = _version(VersionState.ACTIVE)
    superseded = active.supersede("v2")
    assert superseded.state is VersionState.SUPERSEDED
    assert superseded.superseded_by == "v2"


# -- T03: normalize icerik ve parser router -----------------------------------


def test_locatorsuz_birim_kabul_edilmez() -> None:
    with pytest.raises(ValidationFailed):
        ContentUnit(unit_id="u", kind=UnitKind.PARAGRAPH, text="metin", locator=Locator(), order=0)


def test_ocr_birimi_confidence_ister() -> None:
    with pytest.raises(ValidationFailed):
        ContentUnit(
            unit_id="u",
            kind=UnitKind.OCR_BLOCK,
            text="metin",
            locator=Locator(page=1, bbox=(0.0, 0.0, 1.0, 1.0)),
            order=0,
        )


def test_bbox_sayfa_olmadan_anlamsizdir() -> None:
    with pytest.raises(ValidationFailed):
        Locator(bbox=(0.0, 0.0, 1.0, 1.0))


def test_duz_metin_parser_paragraf_uretir() -> None:
    units = PlainTextParser().parse(b"birinci paragraf\n\nikinci paragraf\n")
    assert [unit.kind for unit in units] == [UnitKind.PARAGRAPH, UnitKind.PARAGRAPH]
    assert units[1].locator.block_index == 1


def test_markdown_parser_baslik_yolunu_korur() -> None:
    source = b"""# Ust baslik

Giris metni.

## Alt baslik

- birinci
- ikinci

```python
x = 1
```
"""
    units = MarkdownParser().parse(source)
    kinds = [unit.kind for unit in units]
    assert UnitKind.HEADING in kinds
    assert UnitKind.CODE in kinds
    assert UnitKind.LIST in kinds
    code = next(unit for unit in units if unit.kind is UnitKind.CODE)
    assert code.locator.heading_path == ("Ust baslik", "Alt baslik")
    assert code.text.strip() == "x = 1"


def test_parser_router_cevrimdisi_formatlari_tanir_bilinmeyeni_metin_saymaz() -> None:
    router = default_router()
    assert {
        SourceFormat.MARKDOWN,
        SourceFormat.DOCX,
        SourceFormat.PDF,
        SourceFormat.PNG,
        SourceFormat.JPEG,
        SourceFormat.TIFF,
    }.issubset(router.supported())
    with pytest.raises(PolicyViolation):
        router.parse(SourceFormat.ARCHIVE, b"payload")


def test_docx_icin_sayfa_numarasi_uydurulamaz() -> None:
    parser = StructuredDocumentParser(
        provider=lambda _: ({"text": "metin", "kind": "paragraph", "page": 3},),
        source_format=SourceFormat.DOCX,
    )
    with pytest.raises(PolicyViolation):
        parser.parse(b"docx")


def test_pdf_sayfa_locatoru_korunur() -> None:
    parser = StructuredDocumentParser(
        provider=lambda _: ({"text": "metin", "kind": "paragraph", "page": 3},),
        source_format=SourceFormat.PDF,
    )
    units = parser.parse(b"pdf")
    assert units[0].locator.page == 3


def test_yapili_blok_locatorsuz_kabul_edilmez() -> None:
    parser = StructuredDocumentParser(
        provider=lambda _: ({"text": "metin", "kind": "paragraph"},),
        source_format=SourceFormat.PDF,
    )
    with pytest.raises(ValidationFailed):
        parser.parse(b"pdf")


# -- T04: OCR -----------------------------------------------------------------


def test_ocr_page_bbox_confidence_uretir() -> None:
    parser = OcrParser(
        provider=lambda _: (
            {"text": "Turkce metin", "page": 1, "bbox": (0.1, 0.2, 0.5, 0.6), "confidence": 0.93},
        )
    )
    units = parser.parse(b"png")
    assert units[0].locator.page == 1
    assert units[0].locator.bbox == (0.1, 0.2, 0.5, 0.6)
    assert units[0].confidence == pytest.approx(0.93)


def test_ocr_eksik_alan_uydurulmaz() -> None:
    parser = OcrParser(provider=lambda _: ({"text": "metin", "page": 1},))
    with pytest.raises(ValidationFailed):
        parser.parse(b"png")


def test_ocr_esik_alti_bloklar_elenir() -> None:
    parser = OcrParser(
        provider=lambda _: (
            {"text": "iyi", "page": 1, "bbox": (0, 0, 1, 1), "confidence": 0.9},
            {"text": "kotu", "page": 1, "bbox": (0, 0, 1, 1), "confidence": 0.2},
        ),
        minimum_confidence=0.5,
    )
    units = parser.parse(b"png")
    assert [unit.text for unit in units] == ["iyi"]


# -- T05: tarama sinirlari ----------------------------------------------------


def test_zip_bomb_orani_reddedilir() -> None:
    limits = ScanLimits(max_compression_ratio=10)
    limits.assert_within(entries=1, total_bytes=1000, compressed_bytes=200)
    with pytest.raises(PolicyViolation):
        limits.assert_within(entries=1, total_bytes=1_000_000, compressed_bytes=100)


def test_girdi_ve_boyut_sinirlari_uygulanir() -> None:
    limits = ScanLimits(max_entries=2, max_total_bytes=100)
    with pytest.raises(PolicyViolation):
        limits.assert_within(entries=3, total_bytes=10, compressed_bytes=10)
    with pytest.raises(PolicyViolation):
        limits.assert_within(entries=1, total_bytes=1000, compressed_bytes=1000)


# -- T06: kod ve DB adapter ---------------------------------------------------


def test_python_sembolleri_satir_araligiyla_cikarilir() -> None:
    source = "import os\n\n\nclass Ornek:\n    def calis(self) -> None:\n        return None\n"
    symbols = PythonSymbolExtractor().extract(
        source, relative_path="src/ornek.py", revision="rev-1"
    )
    names = {item.name: item for item in symbols}
    assert names["Ornek"].kind == "class"
    assert names["Ornek"].line_start == 4
    assert names["calis"].kind == "function"
    assert "os" in names["Ornek"].dependencies
    assert names["calis"].to_locator().symbol == "calis"


def test_kod_calistirilmaz_yalniz_ayristirilir() -> None:
    """Ayristirma sirasinda modul import edilmez; yan etki olusmaz."""

    source = "raise SystemExit('bu calisirsa test coker')\n\ndef guvenli() -> int:\n    return 1\n"
    symbols = PythonSymbolExtractor().extract(source, relative_path="a.py", revision="r")
    assert [item.name for item in symbols] == ["guvenli"]


def test_bozuk_kaynak_gorunur_hata_verir() -> None:
    with pytest.raises(ValidationFailed):
        PythonSymbolExtractor().extract("def (", relative_path="a.py", revision="r")


def test_plsql_nesneleri_metadata_olarak_cikarilir() -> None:
    source = """
    create or replace package body app.siparis_paketi as ...
    CREATE PROCEDURE hesapla AS BEGIN NULL; END;
    """
    objects = PlSqlObjectExtractor().extract(source, revision="rev-1")
    names = {item.qualified_name for item in objects}
    assert "app.siparis_paketi" in names
    assert "public.hesapla" in names
    assert all(item.row_data_included is False for item in objects)


def test_satir_verisi_varsayilan_olarak_alinmaz() -> None:
    with pytest.raises(PolicyViolation):
        DatabaseObject(
            schema_name="app",
            object_name="musteri",
            object_kind="table",
            revision="r",
            row_data_included=True,
        )


def test_db_metadata_locator_tasir() -> None:
    units = database_units(
        (
            DatabaseObject(
                schema_name="app",
                object_name="musteri",
                object_kind="table",
                revision="r",
                columns=("id", "ad"),
            ),
        )
    )
    assert units[0].kind is UnitKind.DB_OBJECT
    assert units[0].locator.object_name == "app.musteri"


# -- servis akisi -------------------------------------------------------------


def test_ingestion_servisi_tam_akisi_yurutur() -> None:
    service = IngestionService(default_router())
    artifact = _artifact()
    job = service.start(job_id="j", source_id="s", artifact=artifact, idempotency_key="k")
    job = service.store(job)
    job, document = service.parse(
        job,
        document_id="d1",
        source_format=SourceFormat.MARKDOWN,
        payload=b"# baslik\n\nmetin\n",
    )
    job = service.index(job)
    version = pending_version(
        version_id="v1",
        source_id="s",
        revision=1,
        artifact=artifact,
        content_digest=document.content_digest,
        now=NOW,
    )
    job, active = service.activate(job, version)
    assert job.is_complete is True
    assert active.state is VersionState.ACTIVE
    assert document.unit_count >= 2


def test_normalize_belge_sirasi_tekrarsiz_ve_artan_olmali() -> None:
    unit = ContentUnit(
        unit_id="u1",
        kind=UnitKind.PARAGRAPH,
        text="metin",
        locator=Locator(block_index=0),
        order=0,
    )
    with pytest.raises(ValidationFailed):
        NormalizedDocument(
            document_id="d",
            artifact_digest=_artifact().artifact_digest,
            source_format=SourceFormat.TXT,
            units=(unit, unit),
            parser_ref="p",
            parser_version="1",
        )
