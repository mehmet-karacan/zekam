"""Guvenli ZIP transcript tarama, manifest uretme ve immutable CAS kaydi."""

from __future__ import annotations

import datetime as dt
import io
import stat
import unicodedata
import zipfile
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Protocol

from zekam.application.knowledge_parsers import TimestampTranscriptParser
from zekam.domain.canonical import canonical_json, digest, digest_of_bytes
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.knowledge import (
    MAX_ARCHIVE_ENTRIES,
    MAX_ARCHIVE_RATIO,
    MAX_DOCUMENT_BYTES,
    ScanLimits,
    assert_safe_relative,
    is_denied,
)
from zekam.domain.transcript_corpus import (
    StoredTranscriptCorpusImport,
    TranscriptCorpusEntry,
    TranscriptCorpusImportManifest,
)


class ContentAddressedStore(Protocol):
    """Transcript importunun ihtiyac duydugu bounded CAS yuzeyi."""

    def put(
        self,
        payload: bytes,
        *,
        media_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> StoredObject: ...

    def exists(self, object_digest: str) -> bool: ...

    def get(self, object_digest: str) -> bytes: ...


class StoredObject(Protocol):
    """CAS put sonucunun gereken tek alani."""

    @property
    def digest(self) -> str:
        """Immutable content digest exposed by any compatible CAS result."""
        ...


@dataclass(frozen=True, slots=True)
class TranscriptArchiveScan:
    """Manifest ile birlikte CAS'a yazilacak raw payload'lar."""

    manifest: TranscriptCorpusImportManifest
    archive_payload: bytes
    entry_payloads: tuple[tuple[str, bytes], ...]
    limits: ScanLimits = field(default_factory=ScanLimits)

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        """Butun provenance zincirini herhangi bir CAS etkisinden once tekrar kurar."""

        self.manifest.validate()
        _assert_hard_limits(self.limits)
        if len(self.archive_payload) != self.manifest.archive_size:
            raise ValidationFailed("scan archive size manifest ile uyusmuyor")
        if digest_of_bytes(self.archive_payload) != self.manifest.archive_digest:
            raise ValidationFailed("scan archive payload manifest digest ile uyusmuyor")
        archive_entries = _read_archive_entries(self.archive_payload, self.limits)
        if archive_entries != self.entry_payloads:
            raise ValidationFailed("scan archive entry byte zinciri payloadlarla uyusmuyor")
        if len(self.entry_payloads) != len(self.manifest.entries):
            raise ValidationFailed("scan entry payload sayisi manifest ile uyusmuyor")
        for (path, payload), entry in zip(self.entry_payloads, self.manifest.entries, strict=True):
            if path != entry.relative_path or digest_of_bytes(payload) != entry.file_digest:
                raise ValidationFailed("scan entry payload zinciri manifest ile uyusmuyor")
            normalized_text, normalized_payload = normalize_transcript_payload(payload)
            if digest_of_bytes(normalized_payload) != entry.content_digest:
                raise ValidationFailed("scan normalized content digest ile uyusmuyor")
            metadata = _metadata(normalized_text)
            expected_metadata = {
                "declared_date": entry.declared_date,
                "video_id": entry.video_id,
                "title": entry.title,
                "language": entry.language,
            }
            if {key: metadata.get(key) for key in expected_metadata} != expected_metadata:
                raise ValidationFailed("scan entry metadata manifest ile uyusmuyor")
            parser = TimestampTranscriptParser(entry_path=path, video_id=entry.video_id)
            units = parser.parse(normalized_payload)
            if (
                len(units) != entry.unit_count
                or len(normalized_text.splitlines()) != entry.line_count
            ):
                raise ValidationFailed("scan line/unit sayisi manifest ile uyusmuyor")
            if _parser_profile(parser, self.limits) != self.manifest.parser_profile:
                raise ValidationFailed("scan parser profile manifest ile uyusmuyor")


def normalize_transcript_payload(payload: bytes) -> tuple[str, bytes]:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationFailed("transcript entry UTF-8 olarak cozulemedi") from exc
    normalized = unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))
    return normalized, normalized.encode("utf-8")


def _metadata(text: str) -> dict[str, str]:
    aliases = {
        "title": "title",
        "baslik": "title",
        "video id": "video_id",
        "video-id": "video_id",
        "video_id": "video_id",
        "video kimligi": "video_id",
        "date": "declared_date",
        "tarih": "declared_date",
        "language": "language",
        "dil": "language",
    }
    found: dict[str, str] = {}
    for line in text.splitlines()[:100]:
        key, separator, value = line.partition(":")
        canonical = aliases.get(key.strip().lower())
        if separator and canonical and value.strip() and canonical not in found:
            found[canonical] = value.strip()
    return found


def _parser_profile(parser: TimestampTranscriptParser, limits: ScanLimits) -> dict[str, object]:
    return {
        "schema": "zekam-transcript-parser-profile/v1",
        "parser": parser.parser_profile,
        "content_normalization": {
            "encoding": "utf-8",
            "unicode": "NFC",
            "line_endings": "LF",
        },
        "archive_limits": limits.as_dict(),
    }


def _canonical_entry_path(value: str) -> str:
    assert_safe_relative(value, "archive entry path")
    normalized = unicodedata.normalize("NFC", value)
    canonical = str(PurePosixPath(normalized))
    assert_safe_relative(canonical, "archive entry path")
    return canonical


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0xFFFF
    return stat.S_IFMT(mode) == stat.S_IFLNK


def _assert_hard_limits(limits: ScanLimits) -> None:
    if (
        limits.max_entries > MAX_ARCHIVE_ENTRIES
        or limits.max_total_bytes > MAX_DOCUMENT_BYTES
        or limits.max_compression_ratio > MAX_ARCHIVE_RATIO
    ):
        raise PolicyViolation("transcript scan limitleri hard maximumu asamaz")


def _read_archive_entries(
    archive_payload: bytes, limits: ScanLimits
) -> tuple[tuple[str, bytes], ...]:
    """ZIP central directory ve entry byte'larini ayni bounded policy ile okur."""

    _assert_hard_limits(limits)
    if not archive_payload:
        raise ValidationFailed("transcript archive bos olamaz")
    if len(archive_payload) > limits.max_total_bytes:
        raise PolicyViolation("transcript archive raw boyut sinirini asiyor")
    try:
        archive = zipfile.ZipFile(io.BytesIO(archive_payload))
    except zipfile.BadZipFile as exc:
        raise ValidationFailed("transcript archive gecerli ZIP degil") from exc
    with archive:
        files = [info for info in archive.infolist() if not info.is_dir()]
        limits.assert_within(
            entries=len(files),
            total_bytes=sum(info.file_size for info in files),
            compressed_bytes=max(1, sum(info.compress_size for info in files)),
        )
        seen: set[str] = set()
        result: list[tuple[str, bytes]] = []
        canonical_files = [(_canonical_entry_path(info.filename), info) for info in files]
        for relative_path, info in sorted(
            canonical_files, key=lambda item: (item[0].casefold(), item[0])
        ):
            duplicate_key = relative_path.casefold()
            if duplicate_key in seen:
                raise PolicyViolation("archive yinelenen entry path tasiyamaz")
            seen.add(duplicate_key)
            if is_denied(relative_path):
                raise PolicyViolation("archive deny-list dosyasi tasiyamaz")
            if _is_symlink(info):
                raise PolicyViolation("archive symlink entry tasiyamaz")
            if info.flag_bits & 0x1:
                raise PolicyViolation("sifreli archive entry ingest edilemez")
            if PurePosixPath(relative_path).suffix.lower() != ".txt":
                raise PolicyViolation("transcript archive yalniz .txt entry kabul eder")
            if info.file_size <= 0 or info.file_size > limits.max_total_bytes:
                raise PolicyViolation("transcript entry boyutu sinir disinda")
            if (
                info.compress_size > 0
                and info.file_size / info.compress_size > limits.max_compression_ratio
            ):
                raise PolicyViolation("archive entry sikistirma orani sinirini asiyor (zip bomb)")
            try:
                with archive.open(info, "r") as stream:
                    raw = stream.read(limits.max_total_bytes + 1)
            except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
                raise ValidationFailed("transcript archive entry okunamadi") from exc
            if len(raw) != info.file_size or len(raw) > limits.max_total_bytes:
                raise PolicyViolation("transcript entry declared boyutuyla uyusmuyor")
            result.append((relative_path, raw))
    if not result:
        raise ValidationFailed("transcript archive entry icermiyor")
    return tuple(result)


def scan_transcript_archive(
    archive_payload: bytes,
    *,
    archive_name: str,
    corpus_id: str,
    source_policy_digest: str,
    imported_by: str,
    created_at: dt.datetime,
    limits: ScanLimits | None = None,
) -> TranscriptArchiveScan:
    """ZIP'i execute/extract etmeden tarar ve deterministik manifest uretir."""

    assert_safe_relative(archive_name, "archive adi")
    if PurePosixPath(archive_name).suffix.lower() != ".zip":
        raise ValidationFailed("transcript corpus archive ZIP olmali")
    parse_limits = limits or ScanLimits()
    raw_entries = _read_archive_entries(archive_payload, parse_limits)
    entries: list[TranscriptCorpusEntry] = []
    parser_profile: dict[str, object] | None = None
    parser_profile_digest: str | None = None
    parser_ref: str | None = None
    parser_version: str | None = None
    for relative_path, raw in raw_entries:
        normalized_text, normalized_payload = normalize_transcript_payload(raw)
        metadata = _metadata(normalized_text)
        parser = TimestampTranscriptParser(
            entry_path=relative_path,
            video_id=metadata.get("video_id"),
        )
        units = parser.parse(normalized_payload)
        current_profile = _parser_profile(parser, parse_limits)
        current_profile_digest = digest(current_profile)
        if parser_profile_digest is None:
            parser_profile = current_profile
            parser_profile_digest = current_profile_digest
            parser_ref = parser.parser_ref
            parser_version = parser.parser_version
        elif current_profile_digest != parser_profile_digest:
            raise ValidationFailed("archive entry parser profile drift")
        entries.append(
            TranscriptCorpusEntry(
                relative_path=relative_path,
                file_digest=digest_of_bytes(raw),
                content_digest=digest_of_bytes(normalized_payload),
                byte_size=len(raw),
                line_count=len(normalized_text.splitlines()),
                unit_count=len(units),
                declared_date=metadata.get("declared_date"),
                video_id=metadata.get("video_id"),
                title=metadata.get("title"),
                language=metadata.get("language"),
            )
        )
    if (
        not entries
        or parser_profile is None
        or parser_profile_digest is None
        or parser_ref is None
        or parser_version is None
    ):
        raise ValidationFailed("transcript archive entry icermiyor")
    manifest = TranscriptCorpusImportManifest(
        corpus_id=corpus_id,
        archive_name=archive_name,
        archive_digest=digest_of_bytes(archive_payload),
        archive_size=len(archive_payload),
        parser_ref=parser_ref,
        parser_version=parser_version,
        parser_profile_canonical=canonical_json(parser_profile),
        parser_profile_digest=parser_profile_digest,
        source_policy_digest=source_policy_digest,
        imported_by=imported_by,
        created_at=created_at,
        entries=tuple(entries),
    )
    return TranscriptArchiveScan(
        manifest=manifest,
        archive_payload=archive_payload,
        entry_payloads=tuple(raw_entries),
        limits=parse_limits,
    )


@dataclass(frozen=True, slots=True)
class TranscriptCorpusImporter:
    """Dogrulanmis transcript archive ve manifestini immutable CAS'a yazar."""

    store: ContentAddressedStore

    def persist(self, scan: TranscriptArchiveScan) -> StoredTranscriptCorpusImport:
        scan.validate()
        manifest_payload = scan.manifest.to_bytes()
        archive_digest = self._put_verified(
            scan.archive_payload,
            media_type="application/zip",
            metadata={"source_type": "external-video-transcript"},
        )
        if archive_digest != scan.manifest.archive_digest:
            raise ValidationFailed("CAS archive digest manifest ile uyusmuyor")
        entry_digests: list[str] = []
        for (path, payload), expected in zip(
            scan.entry_payloads, scan.manifest.entries, strict=True
        ):
            stored_digest = self._put_verified(
                payload,
                media_type="text/plain; charset=utf-8",
                metadata={"entry_path": path, "archive_digest": scan.manifest.archive_digest},
            )
            if stored_digest != expected.file_digest:
                raise ValidationFailed("CAS entry digest manifest ile uyusmuyor")
            entry_digests.append(stored_digest)
        manifest_object_digest = self._put_verified(
            manifest_payload,
            media_type="application/vnd.zekam.transcript-corpus+json",
            metadata={"corpus_id": scan.manifest.corpus_id},
        )
        if manifest_object_digest != digest_of_bytes(manifest_payload):
            raise ValidationFailed("CAS manifest digest payload ile uyusmuyor")
        return StoredTranscriptCorpusImport(
            manifest=scan.manifest,
            manifest_object_digest=manifest_object_digest,
            archive_object_digest=archive_digest,
            entry_object_digests=tuple(entry_digests),
        )

    def _put_verified(
        self,
        payload: bytes,
        *,
        media_type: str,
        metadata: dict[str, str],
    ) -> str:
        expected = digest_of_bytes(payload)
        info = self.store.put(payload, media_type=media_type, metadata=metadata)
        if info.digest != expected or not self.store.exists(expected):
            raise ValidationFailed("CAS durability receipt digest/exists dogrulamasi basarisiz")
        stored = self.store.get(expected)
        if stored != payload or digest_of_bytes(stored) != expected:
            raise ValidationFailed("CAS read-after-write byte integrity dogrulamasi basarisiz")
        return expected
