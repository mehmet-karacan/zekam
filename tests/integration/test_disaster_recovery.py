"""P17-T03 yedek, yalitilmis geri yukleme ve proje kapsulu tatbikati.

Gercek icerik adresli depo ve gercek dosya sistemi kullanilir. Geri yukleme
**yalitilmis** bir kok altina yapilir; kapsul kaynaga bagli gelmez ve aktif lease
tasimaz.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from zekam.application.backup import (
    SchemaState,
    VerificationOutcome,
    build_manifest,
    verify_manifest,
)
from zekam.domain.canonical import digest, digest_of_bytes
from zekam.domain.errors import PolicyViolation
from zekam.domain.release import ProjectCapsule, RestoreCheck
from zekam.infrastructure.storage.local_cas import LocalContentAddressedStore

pytestmark = pytest.mark.integration

NOW = dt.datetime(2026, 8, 21, tzinfo=dt.UTC)
CONFIGURATION = {"database": {"name": "zekam"}, "home": "zekam-home"}


def _schema_state() -> SchemaState:
    return SchemaState(head=16, migrations=((16, "scheduler", "a" * 64),))


def _store(root: Path) -> LocalContentAddressedStore:
    return LocalContentAddressedStore(root).ensure()


def test_yedek_manifesti_uretilir_ve_dogrulanir(tmp_path: Path) -> None:
    store = _store(tmp_path / "kaynak-depo")
    payloads = [b"birinci belge", b"ikinci belge", b"ucuncu belge"]
    for payload in payloads:
        store.put(payload, media_type="text/plain")

    manifest = build_manifest(
        schema_state=_schema_state(), store=store, configuration=CONFIGURATION, now=NOW
    )
    assert len(manifest.artifacts) == len(payloads)
    assert manifest.total_bytes == sum(len(item) for item in payloads)

    result = verify_manifest(manifest, store)
    assert result.is_valid is True
    assert result.outcome is VerificationOutcome.VALID


def test_yalitilmis_geri_yukleme_ayni_icerigi_uretir(tmp_path: Path) -> None:
    """Tatbikat: kaynak depo -> manifest -> yeni kok altina geri yukleme."""

    source = _store(tmp_path / "kaynak-depo")
    payloads = [b"alfa", b"beta", b"gama"]
    digests = [source.put(payload, media_type="text/plain").digest for payload in payloads]
    manifest = build_manifest(
        schema_state=_schema_state(), store=source, configuration=CONFIGURATION, now=NOW
    )

    # Yalitilmis kok: uretim deposuna dokunulmaz.
    restored = _store(tmp_path / "geri-yukleme-koku")
    for value in digests:
        restored.put(source.get(value), media_type="text/plain")

    result = verify_manifest(manifest, restored)
    assert result.is_valid is True, result.detail
    assert {info.digest for info in restored.iter_objects()} == set(digests)


def test_eksik_artifact_geri_yuklemede_yakalanir(tmp_path: Path) -> None:
    source = _store(tmp_path / "kaynak-depo")
    first = source.put(b"alfa", media_type="text/plain").digest
    source.put(b"beta", media_type="text/plain")
    manifest = build_manifest(
        schema_state=_schema_state(), store=source, configuration=CONFIGURATION, now=NOW
    )

    incomplete = _store(tmp_path / "eksik-kok")
    incomplete.put(source.get(first), media_type="text/plain")

    result = verify_manifest(manifest, incomplete)
    assert result.is_valid is False
    assert result.outcome is not VerificationOutcome.VALID


def test_degistirilmis_manifest_reddedilir(tmp_path: Path) -> None:
    store = _store(tmp_path / "depo")
    store.put(b"alfa", media_type="text/plain")
    manifest = build_manifest(
        schema_state=_schema_state(), store=store, configuration=CONFIGURATION, now=NOW
    )
    tampered = type(manifest)(
        schema=manifest.schema,
        product=manifest.product,
        product_version=manifest.product_version,
        created_at=manifest.created_at,
        schema_state=SchemaState(head=99, migrations=((99, "sahte", "b" * 64),)),
        artifacts=manifest.artifacts,
        configuration_digest=manifest.configuration_digest,
        manifest_digest=manifest.manifest_digest,
    )
    result = verify_manifest(tampered, store)
    assert result.outcome is VerificationOutcome.ALTERED


def test_proje_kapsulu_absolute_path_ve_secret_tasiyamaz() -> None:
    with pytest.raises(PolicyViolation):
        ProjectCapsule(
            project_ref="zekam",
            source_revision="rev-1",
            relative_paths=("/etc/passwd",),
            content_digest=digest("k"),
        )
    with pytest.raises(PolicyViolation):
        ProjectCapsule(
            project_ref="zekam",
            source_revision="rev-1",
            relative_paths=("../disari.txt",),
            content_digest=digest("k"),
        )
    with pytest.raises(PolicyViolation):
        ProjectCapsule(
            project_ref="zekam",
            source_revision="rev-1",
            relative_paths=("config/api_key.txt",),
            content_digest=digest("k"),
        )


def test_kapsul_aktif_lease_veya_secret_ile_uretilemez() -> None:
    for override in ({"carries_active_lease": True}, {"carries_secret": True}):
        with pytest.raises(PolicyViolation):
            ProjectCapsule(
                project_ref="zekam",
                source_revision="rev-1",
                relative_paths=("docs/rapor.md",),
                content_digest=digest("k"),
                **override,
            )


def test_kapsul_disa_aktarim_ve_geri_yukleme_dogrulanir(tmp_path: Path) -> None:
    """Kapsul disa aktarilir, baska bir kok altinda geri yuklenir ve dogrulanir."""

    project = tmp_path / "proje"
    (project / "docs").mkdir(parents=True)
    (project / "docs" / "rapor.md").write_text("# rapor\n", encoding="utf-8", newline="\n")
    (project / "src").mkdir()
    (project / "src" / "modul.py").write_text("x = 1\n", encoding="utf-8", newline="\n")

    relative = ("docs/rapor.md", "src/modul.py")
    payload = b"".join((project / item).read_bytes() for item in relative)
    capsule = ProjectCapsule(
        project_ref="proje",
        source_revision="rev-1",
        relative_paths=relative,
        content_digest=digest_of_bytes(payload),
    )

    # Yalitilmis geri yukleme: yeni kok, kaynak baglantisi yok, lease yok.
    target = tmp_path / "geri-yukleme"
    for item in relative:
        destination = target / item
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((project / item).read_bytes())
    restored_payload = b"".join((target / item).read_bytes() for item in relative)

    check = RestoreCheck(
        capsule_digest=capsule.content_digest,
        restored_digest=digest_of_bytes(restored_payload),
        source_bound=False,
        active_lease_present=False,
    )
    assert check.is_valid is True
    assert check.failures() == ()
    assert not (target / ".git").exists(), "geri yukleme kaynaga bagli gelmemeli"


def test_kaynaga_bagli_veya_leaseli_geri_yukleme_gecersiz() -> None:
    content = digest("ayni")
    bound = RestoreCheck(
        capsule_digest=content,
        restored_digest=content,
        source_bound=True,
        active_lease_present=False,
    )
    assert bound.is_valid is False
    assert "kaynaga bagli" in " ".join(bound.failures())

    leased = RestoreCheck(
        capsule_digest=content,
        restored_digest=content,
        source_bound=False,
        active_lease_present=True,
    )
    assert leased.is_valid is False
    assert "lease" in " ".join(leased.failures())


def test_bozulmus_geri_yukleme_yakalanir() -> None:
    check = RestoreCheck(
        capsule_digest=digest("orijinal"),
        restored_digest=digest("bozuk"),
        source_bound=False,
        active_lease_present=False,
    )
    assert check.is_valid is False
    assert "digest" in " ".join(check.failures())
