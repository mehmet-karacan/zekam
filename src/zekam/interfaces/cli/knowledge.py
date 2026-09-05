"""`zekam knowledge` komutlari.

`scan` ve `inspect` salt okunurdur; hicbir kod calistirmaz. `ingest` yalniz
`--uygula` ile kanonik store'a yazar.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from typing import Annotated
from uuid import NAMESPACE_URL, uuid5

import typer
from rich.console import Console
from rich.table import Table

from zekam.application.composition import build_context
from zekam.application.knowledge_file_plane import note_content_digest
from zekam.application.knowledge_ingestion import (
    ArchiveInspector,
    DirectoryScanner,
    IngestionService,
    ScanReport,
    pending_version,
)
from zekam.application.knowledge_parsers import default_router
from zekam.domain.canonical import digest, digest_of_bytes
from zekam.domain.errors import ValidationFailed, ZekamError
from zekam.domain.identifiers import validate_slug
from zekam.domain.knowledge import SourceFormat
from zekam.domain.realm import DEFAULT_REALM_SLUG
from zekam.infrastructure.knowledge.document_parsers import media_type_for
from zekam.infrastructure.storage.local_cas import LocalContentAddressedStore
from zekam.interfaces.cli.session import (
    HOME_HELP,
    REALM_HELP,
    fail,
    fail_from,
    sqlite_operational_store,
    sqlite_repository,
)

app = typer.Typer(name="knowledge", help="Knowledge Plane islemleri", no_args_is_help=True)
console = Console()
_LOCAL_REALM_ID = str(uuid5(NAMESPACE_URL, "zekam://realm/yerel"))

#: Uzantidan format cikarimi; bilinmeyen uzanti tahmin edilmez.
_SUFFIXES: dict[str, SourceFormat] = {
    ".md": SourceFormat.MARKDOWN,
    ".markdown": SourceFormat.MARKDOWN,
    ".txt": SourceFormat.TXT,
    ".docx": SourceFormat.DOCX,
    ".pdf": SourceFormat.PDF,
    ".png": SourceFormat.PNG,
    ".jpg": SourceFormat.JPEG,
    ".jpeg": SourceFormat.JPEG,
    ".tif": SourceFormat.TIFF,
    ".tiff": SourceFormat.TIFF,
}

_TEXT_MEDIA_TYPES: dict[SourceFormat, str] = {
    SourceFormat.TXT: "text/plain",
    SourceFormat.MARKDOWN: "text/markdown",
}


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _render(report: ScanReport) -> None:
    table = Table(title="Tarama sonucu")
    table.add_column("Yol")
    table.add_column("Durum")
    table.add_column("Gerekce")
    for decision in report.decisions:
        table.add_row(
            decision.path,
            "[green]alindi[/green]" if decision.included else "[yellow]atlandi[/yellow]",
            decision.reason,
        )
    console.print(table)
    console.print(f"toplam bayt: {report.total_bytes}")


def _parse_vector(value: str) -> tuple[float, ...]:
    try:
        parsed = json.loads(value)
        if not isinstance(parsed, list):
            raise TypeError
        return tuple(float(item) for item in parsed)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValidationFailed("Vector JSON sonlu sayi dizisi olmali") from exc


def _materialize_once(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
            raise ValidationFailed("Knowledge materialization replay drift")
        return
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise ValidationFailed("Knowledge materialization yazilamadi") from exc


@app.command("vector-index")
def vector_index_command(
    project: Annotated[str, typer.Argument(help="Proje slug veya kimlik")],
    source_ref: Annotated[str, typer.Option("--source-ref", help="Portable kaynak ref")],
    body: Annotated[str, typer.Option("--body", help="Chunk metni")],
    model_ref: Annotated[str, typer.Option("--model-ref", help="Embedding model ref")],
    vector_json: Annotated[str, typer.Option("--vector-json", help="JSON sayi dizisi")],
    apply: Annotated[bool, typer.Option("--uygula", help="Gercekten indeksler")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """SQLite minimum profilinde hazir yerel embedding'i indeksler."""
    if not apply:
        console.print("[yellow]Dry-run. Indekslemek icin --uygula verin.[/yellow]")
        return
    try:
        if sqlite_operational_store(home, realm) is not None:
            raise ValidationFailed(
                "Knowledge index operational DB'den ayridir; local adapter henuz aktif degil"
            )
        repository = sqlite_repository(home, realm)
        if repository is None:
            raise ValidationFailed("vector-index yalniz SQLite minimum profilinde kullanilir")
        project_row = repository.get_project(project)
        chunk_id = repository.index_chunk(
            project_id=project_row.id,
            source_ref=source_ref,
            body=body,
            metadata={},
            model_ref=model_ref,
            vector=_parse_vector(vector_json),
        )
    except ZekamError as exc:
        raise fail_from(exc) from exc
    console.print_json(json.dumps({"chunk_id": chunk_id, "indexed": True}))


@app.command("vector-search")
def vector_search_command(
    project: Annotated[str, typer.Argument(help="Proje slug veya kimlik")],
    model_ref: Annotated[str, typer.Option("--model-ref", help="Embedding model ref")],
    vector_json: Annotated[str, typer.Option("--vector-json", help="JSON sayi dizisi")],
    limit: Annotated[int, typer.Option("--limit", min=1)] = 10,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """SQLite JSON-vector cosine aramasini model uzayina bagli calistirir."""
    try:
        if sqlite_operational_store(home, realm) is not None:
            raise ValidationFailed(
                "Knowledge index operational DB'den ayridir; local adapter henuz aktif degil"
            )
        repository = sqlite_repository(home, realm)
        if repository is None:
            raise ValidationFailed("vector-search yalniz SQLite minimum profilinde kullanilir")
        project_row = repository.get_project(project)
        hits = repository.search(
            project_id=project_row.id,
            model_ref=model_ref,
            query_vector=_parse_vector(vector_json),
            limit=limit,
        )
    except ZekamError as exc:
        raise fail_from(exc) from exc
    console.print_json(
        json.dumps(
            [
                {
                    "chunk_id": hit.chunk_id,
                    "source_ref": hit.source_ref,
                    "body": hit.body,
                    "score": hit.score,
                }
                for hit in hits
            ],
            ensure_ascii=False,
        )
    )


@app.command("scan")
def scan_command(
    directory: Annotated[Path, typer.Argument(help="Izinli kok dizin")],
    ignore: Annotated[
        list[str] | None, typer.Option("--yoksay", help="Dosya adi (tekrarlanabilir)")
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="JSON cikti")] = False,
) -> None:
    """Dizini salt okunur tarar. Hicbir kod, hook veya build calistirilmaz."""
    try:
        report = DirectoryScanner(directory, ignore_names=frozenset(ignore or ())).scan()
    except ZekamError as exc:
        raise fail_from(exc) from exc
    if as_json:
        console.print_json(json.dumps(report.as_dict(), ensure_ascii=False))
    else:
        _render(report)


@app.command("inspect")
def inspect_command(
    archive: Annotated[Path, typer.Argument(help="Arsiv dosyasi")],
    as_json: Annotated[bool, typer.Option("--json", help="JSON cikti")] = False,
) -> None:
    """Arsivi acmadan inceler; zip bomb ve traversal fail-closed reddedilir."""
    try:
        report = ArchiveInspector().inspect(archive)
    except ZekamError as exc:
        raise fail_from(exc) from exc
    if as_json:
        console.print_json(json.dumps(report.as_dict(), ensure_ascii=False))
    else:
        _render(report)


@app.command("ingest")
def ingest_command(
    document: Annotated[Path, typer.Argument(help="Ingest edilecek dosya")],
    slug: Annotated[str, typer.Option("--slug", help="Kaynak kimligi")],
    apply: Annotated[bool, typer.Option("--uygula", help="Kanonik store'a yazar")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="JSON cikti")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Belgeyi normalize eder ve tamamlanirsa atomik olarak aktive eder."""
    if not document.is_file():
        raise fail(f"Dosya bulunamadi: {document.name}")
    source_format = _SUFFIXES.get(document.suffix.lower())
    if source_format is None:
        raise fail(f"Uzanti icin parser tanimli degil: {document.suffix}")

    payload = document.read_bytes()
    service = IngestionService(default_router())
    now = _now()
    try:
        media_type = _TEXT_MEDIA_TYPES.get(source_format) or media_type_for(source_format)
        artifact = service.artifact_for(payload, name=document.name, media_type=media_type, now=now)
        parser = service.router.resolve(source_format)
        idempotency_key = digest(
            {
                "source_slug": slug,
                "content_digest": digest_of_bytes(payload),
                "parser_profile": parser.parser_profile,
            }
        )
        job = service.start(
            job_id=slug,
            source_id=slug,
            artifact=artifact,
            idempotency_key=idempotency_key,
        )
        job = service.store(job)
        job, normalized = service.parse(
            job, document_id=slug, source_format=source_format, payload=payload
        )
        job = service.index(job)
        version = pending_version(
            version_id=f"{slug}-pending",
            source_id=slug,
            revision=1,
            artifact=artifact,
            content_digest=normalized.content_digest,
            now=now,
        )
        job, active = service.activate(job, version)
    except ZekamError as exc:
        raise fail_from(exc) from exc

    summary = {
        "slug": slug,
        "source_format": str(source_format),
        "unit_count": normalized.unit_count,
        "content_digest": normalized.content_digest,
        "artifact_content_digest": artifact.content_digest,
        "artifact_digest": artifact.artifact_digest,
        "parser_profile_digest": digest(normalized.parser_profile),
        "state": str(active.state),
        "lexical_index_state": "ready",
        "embedding_state": "pending",
        "applied": apply,
    }

    if not apply:
        summary["applied"] = False
        if as_json:
            console.print_json(json.dumps(summary, ensure_ascii=False))
        else:
            console.print(f"normalize birim sayisi: {normalized.unit_count}")
            console.print("[yellow]Dry-run. Yazmak icin --uygula verin.[/yellow]")
        return

    try:
        context = build_context(home=home)
        operational = sqlite_operational_store(home, realm)
        assert operational is not None
        safe_slug = validate_slug(slug)
        content_digest = note_content_digest(payload)
        cas = LocalContentAddressedStore(
            context.home / context.settings.object_store_relative
        ).ensure()
        stored = cas.put(payload, media_type=media_type, metadata={"source_slug": safe_slug})
        portable_ref = f"inbox/user/global/{safe_slug}.md"
        target = context.home / portable_ref
        with operational.unit_of_work() as uow:
            uow.register_artifact(
                artifact_digest=stored.digest,
                media_type=media_type,
                size_bytes=stored.size_bytes,
                classification="internal",
            )
            note = uow.register_knowledge_note(
                realm_id=_LOCAL_REALM_ID,
                project_id=None,
                owner_scope="global-user",
                portable_ref=portable_ref,
                note_kind="reference",
                authorship="user",
                classification="internal",
                content_digest=content_digest,
            )
            _materialize_once(target, payload)
            note = uow.confirm_knowledge_note(
                note_id=note.id,
                expected_content_digest=content_digest,
                evidence_digest=digest(
                    {"artifact_digest": stored.digest, "portable_ref": portable_ref}
                ),
            )
            uow.commit()
    except ZekamError as exc:
        raise fail_from(exc) from exc
    summary.update(
        {
            "applied": True,
            "note_id": note.id,
            "portable_ref": portable_ref,
            "artifact_digest": stored.digest,
        }
    )
    if as_json:
        console.print_json(json.dumps(summary, ensure_ascii=False))
    else:
        console.print(f"[green]Ingest edildi:[/green] {portable_ref}")
