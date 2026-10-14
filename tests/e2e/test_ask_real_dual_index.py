"""WP-03.03: ``zekam ask`` karsilastirma fan-out'unun GERCEK cerceve kabul testi.

``tests/integration/test_natural_request_cli_fanout.py`` ve
``tests/e2e/test_cli_project_rag.py`` fan-out zincirini mock RAG seam'iyle kanitlar
ve gercek coklu indeks fixture'i olmadigini acikca belirtir. Bu suite o boslugu
doldurur:

* Iki kayitli proje icin GERCEK, okunabilir yerel RAG indeksi
  (``SQLiteKnowledgeIndex``: FTS5 + sqlite-vec, 1024 boyut) kurulur.
* ``zekam ask "<karsilastirma>"`` CLI'i, hicbir saglayici/model cagrisi ve
  indeksleme baslatilmadan, iki AYRI scoped sonuc dondurur.
* Her sonucun ``project_id``'si ve citation'i dogru, proje-baglidir; iki indeks
  arasi karisim (cross-project leakage) olmaz.
* ``project_rag_status``/``query_registered_project`` (``project_rag_runtime``)
  alt katmani mock EDILMEZ; gercek yol calisir.

Neden lexical-only (degraded)? Varsayilan ``remote`` embedding route'i
yetkilendirilmeden (``--authorize-remote-query`` verilmeden) calisir ve local BGE
route'u yalniz macOS'ta desteklenir. Bu nedenle gercek yol, saglayicisiz
``exact`` + ``lexical`` (FTS5) kanallariyla, acikca ``lexical-only-degraded``
etiketli kanit dondurur; dense/remote/local saglayici hic cagrilmaz. Bu, gercek
indeksin salt okunur okundugunun ve sorgunun indekslemeden once/sonra indeks
icerigini degistirmediginin kanididir.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from zekam.application import project_rag_runtime as runtime
from zekam.application.config import load_settings
from zekam.application.home import HomeLayout
from zekam.application.knowledge_index import KnowledgeIndexRecord
from zekam.application.project_rag_runtime import project_rag_status
from zekam.domain.canonical import digest, digest_of_bytes
from zekam.domain.knowledge import Locator
from zekam.infrastructure.local_file_security import restrict_private_tree
from zekam.infrastructure.sqlite.knowledge_index import (
    KNOWLEDGE_VECTOR_DIMENSION,
    SQLiteKnowledgeIndex,
)
from zekam.infrastructure.sqlite.operational_schema import bootstrap
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore
from zekam.interfaces.cli import main as cli

pytestmark = pytest.mark.e2e

REGISTERED = ("sky-spring-ui", "sky-microservis")

#: Acik iki kayitli proje karsilastirma sorusu: ``sky-tile`` aile alias'i iki
#: kayitli uye (sky-spring-ui, sky-microservis) paralel RAG stratejisiyle secilir;
#: ``karsilastir`` acik karsilastirma ifadesidir (``route-fanout`` kapsami).
COMPARISON_QUESTION = "sky-tile mimarisini karsilastir"

#: Her projenin gercek kaynak dosyasi ve icerigi. Iki icerik de sorgu sozcuklerini
#: (``sky-tile``, ``mimarisini``, ``karsilastir``) tasiyarak FTS5 lexical kanalda
#: projeye ozgu kanit uretir; ancak her projenin KENDI nesnesi farklidir
#: (``SaglikYaniti`` vs ``VeriServisi``) boylece citation karisim yoklugu dogrulanir.
SOURCE_FILES: dict[str, str] = {
    "sky-spring-ui": "belgeler/saglik-servisi.md",
    "sky-microservis": "belgeler/veri-servisi.md",
}
SOURCE_CONTENT: dict[str, str] = {
    "sky-spring-ui": (
        "sky-tile mimarisini karsilastir: sky-spring-ui SaglikYaniti saglik kontrol "
        "endpointi sunar. SaglikYaniti status ve versiyon alanlari icerir.\n"
    ),
    "sky-microservis": (
        "sky-tile mimarisini karsilastir: sky-microservis VeriServisi veri erisim "
        "katmani tanimlar. VeriServisi tablo okuma ve yazma islemlerini yonetir.\n"
    ),
}


def _normalized_vector() -> tuple[float, ...]:
    """Deterministik, normalize, 1024-boyutlu vektor (sqlite-vec girdisi).

    dense kanali bu testte devre disidir (saglayicisiz), ancak indeksin
    ``chunk_vector`` tablosuna gectigi vektor ``validate_vector`` sozlesmesini
    (finite float + normalize) karsilamali. Sonuc salt kanit (citation) uretir;
    vektorun semantik degeri burada onemli degildir, sozlesme dogrugu onemlidir.
    """

    a = 0.001
    b = (1.0 - (KNOWLEDGE_VECTOR_DIMENSION - 1) * a * a) ** 0.5
    return (a,) * (KNOWLEDGE_VECTOR_DIMENSION - 1) + (b,)


def _project_ids(home: Path) -> dict[str, str]:
    store = SQLiteOperationalStore(home / "state" / "operational.db")
    with store.unit_of_work() as uow:
        ids = {project.slug: project.id for project in uow.list_projects(include_archived=False)}
        uow.commit()
    return ids


def _build_one_project(home: Path, slug: str, project_id: str, binding_digest: str) -> None:
    """Bir proje icin gercek kaynak dosyasi + gercek RAG indeksi + rag-state yazar."""

    content = SOURCE_CONTENT[slug]
    source_path = SOURCE_FILES[slug]
    source_root = home / "projeler" / slug / "kaynak"
    source_file = source_root / source_path
    source_file.parent.mkdir(parents=True, exist_ok=True)
    source_file.write_text(content, encoding="utf-8")

    _, status_digest, revision = runtime._git_source_state(source_root)
    tree_digest = digest(f"tree-{project_id}")
    index_root = home / "knowledge-index" / "vector" / "opencode-bge-m3" / slug
    index_path = index_root / "knowledge.sqlite3"
    index_root.mkdir(parents=True, exist_ok=True)
    (home / "knowledge-index" / "manifests" / slug).mkdir(parents=True, exist_ok=True)

    record = KnowledgeIndexRecord(
        chunk_id="chunk-1",
        project_id=project_id,
        source_revision=f"directory:{tree_digest}",
        source_path=source_path,
        source_digest=digest_of_bytes(content.encode("utf-8")),
        locator=Locator(relative_path=source_path, line_start=1, line_end=1),
        text=content,
        content_digest=digest_of_bytes(content.encode("utf-8")),
        chunk_order=0,
        vector=_normalized_vector(),
    )
    with SQLiteKnowledgeIndex(index_path, create=True) as index:
        generation = index.build_generation(
            (record,),
            project_id=project_id,
            source_revision=record.source_revision,
            tree_digest=tree_digest,
            source_manifest_digest=digest(f"manifest-{project_id}"),
            embedding_profile_digest=digest("embedding-profile"),
            provider_profile_digest=digest("provider-profile"),
            created_at="2026-01-01T00:00:00Z",
        )

    state: dict[str, Any] = {
        "schema": "zekam-project-rag-index/v1",
        "project_id": project_id,
        "project_slug": slug,
        "state": "ready",
        "generation_digest": generation.generation_digest,
        "source_revision": generation.source_revision,
        "repository_source_revision": revision,
        "repository_tree_digest": "sha256:" + status_digest,
        "tree_digest": generation.tree_digest,
        "source_chunk_count": generation.chunk_count,
        "database_access": "disabled",
        "knowledge_binding_digest": binding_digest,
    }
    runtime._write_private(
        home / "projeler" / slug / "runtime" / "rag-state.json",
        (json.dumps(state, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"),
    )


def _build_dual_home(tmp_path: Path) -> Path:
    """Iki kayitli proje + iki gercek okunabilir RAG indeksi tasiyan ZEKAM_HOME."""

    home = tmp_path / "zekam-home"
    layout = HomeLayout(home).ensure()
    for slug in REGISTERED:
        layout.ensure_project(slug)

    database = home / "state" / "operational.db"
    bootstrap(database)
    store = SQLiteOperationalStore(database)
    project_ids: dict[str, str] = {}
    with store.unit_of_work() as uow:
        project_ids["sky-spring-ui"] = uow.create_project(
            slug="sky-spring-ui", display_name="Sky Spring UI"
        ).id
        project_ids["sky-microservis"] = uow.create_project(
            slug="sky-microservis", display_name="Sky Microservis"
        ).id
        uow.commit()

    knowledge = load_settings(home=home).knowledge
    binding_digest = runtime._knowledge_binding_digest(knowledge)
    for slug in REGISTERED:
        _build_one_project(home, slug, project_ids[slug], binding_digest)

    # Scoped runtime yollarinin private ACL sozlesmesini her platformda garantiye al
    # (Windows'ta ``ensure()`` agaci private ACL'lerle kurdugu icin idempotent).
    for slug in REGISTERED:
        paths = runtime._existing_runtime_paths(home, slug)
        for name in ("project_root", "index_root", "manifest_root"):
            restrict_private_tree(paths[name])

    return home


def _block_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Her saglayici/model ve ag cagrisini fail-closed bozucuyla degistirir.

    Gercek offline gercek-indeks yolu ``remote`` route'unda yetkilendirilmedigi
    icin dense/remote/local saglayiciyi ASLA kullanmaz; bu cagrilari bozucu
    yapariz ki gercek yolun salt okunur FTS5/lexical kanit dondurdugu
    (indekslemeden/saglayicidan bagimsiz) kanitlansin.
    """

    def _forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("gercek offline indeks sorgusu saglayici/ag cagrisi yapamaz")

    monkeypatch.setattr(socket.socket, "connect", _forbidden)
    monkeypatch.setattr(socket, "create_connection", _forbidden)
    monkeypatch.setattr(runtime, "_provider", _forbidden)
    monkeypatch.setattr(runtime, "_local_query_binding", _forbidden)


@pytest.fixture()
def dual_home(tmp_path: Path) -> Path:
    return _build_dual_home(tmp_path)


def test_real_dual_index_fixture_is_readable_and_scoped(dual_home: Path) -> None:
    """Gercek indeksler ``ready``/okunabilir ve proje-kapsamli (ACL drift yok)."""

    for slug in REGISTERED:
        status = project_rag_status(dual_home, slug)
        assert status["state"] == "ready", status
        assert status["index_readable"] is True, status
        assert status["state"] != "blocked", status
        assert status["generation_digest"].startswith("sha256:")
        assert status["chunk_count"] == 1


def test_ask_real_dual_index_fans_out_two_scoped_results(
    dual_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acik karsilastirma sorusu GERCEK iki indeks uzerinden iki AYRI scoped sonuc
    dondurur; her sonucun project_id/citation'i dogru, cross-project karisim yok,
    saglayici/model cagrisi ve indeksleme yok."""

    home = dual_home
    _block_providers(monkeypatch)
    result = CliRunner().invoke(
        cli.app, ["ask", COMPARISON_QUESTION, "--home", str(home), "--json"]
    )
    assert result.exit_code == 0, result.output

    document = json.loads(result.output)
    assert document["schema"] == "zekam-ask-multi-result/v1"
    assert document["route"] == "natural-request-retrieval"
    assert document["answer"]["schema"] == "zekam-natural-request-answer/v1"
    assert document["answer"]["proposed_explanation_accepted"] is True
    assert document["answer"]["effect_claimed"] is False
    retrieval = document["retrieval"]
    assert retrieval["status"] == "answered"
    assert retrieval["project_refs"] == ["sky-spring-ui", "sky-microservis"]
    assert retrieval["query_count"] == 2
    assert retrieval["index_triggered"] is False
    assert retrieval["grants_authority"] is False

    project_ids = _project_ids(home)
    by_ref = {item["project_ref"]: item for item in retrieval["results"]}
    assert set(by_ref) == set(REGISTERED)
    # Deterministik scope sirasi (aile ureye).
    assert [item["project_ref"] for item in retrieval["results"]] == list(REGISTERED)

    cited_sources: set[str] = set()
    for slug in REGISTERED:
        entry = by_ref[slug]
        answer = entry["result"]
        assert entry["status"] == "answered", entry
        # Scope kararindaki project_id ile gercek index'teki proje baglantisi uyumlu.
        assert entry["project_id"] == project_ids[slug]
        assert answer["project_id"] == project_ids[slug]
        # Saglayicisiz, salt okunur kanit: remote hic kullanilmadi.
        assert answer["state"] == "lexical-only-degraded"
        assert answer["remote_provider_used"] is False
        assert answer["embedding_route"] == "remote"
        assert answer["degraded_reason"] == "remote-query-not-authorized"
        assert answer["searched_channels"] == ["exact", "lexical"]
        # Gercek FTS5 citation: projeye ozgu, dogru scope'ta.
        citations = answer["citations"]
        assert citations, slug
        assert citations[0]["source_ref"] == SOURCE_FILES[slug]
        assert citations[0]["project_scope"] == project_ids[slug]
        assert citations[0]["chunk_id"] == "chunk-1"
        # Cevap kesitinin gercek kaynaktan geldigi (uydurma degil).
        first_line = SOURCE_CONTENT[slug].splitlines()[0]
        assert answer["answer_excerpt"] and first_line in answer["answer_excerpt"]
        cited_sources.add(citations[0]["source_ref"])

    # Iki proje farkli kaynak dosyalarindan citelendi: cross-project karisim yok.
    assert cited_sources == {SOURCE_FILES["sky-spring-ui"], SOURCE_FILES["sky-microservis"]}


def test_ask_real_dual_index_starts_no_indexing_and_no_provider(
    dual_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fan-out sorgusu gercek indeksleri DEGISTIRMEZ: byte-byte ayni kalir, yeni
    generation/chunk/indis dosyasi yok, saglayici/model cagrisi yok."""

    home = dual_home
    _block_providers(monkeypatch)

    before_index_bytes: dict[str, bytes] = {}
    before_state_bytes: dict[str, bytes] = {}
    before_generation: dict[str, str] = {}
    for slug in REGISTERED:
        paths = runtime._existing_runtime_paths(home, slug)
        before_index_bytes[slug] = paths["index"].read_bytes()
        before_state_bytes[slug] = paths["state"].read_bytes()
        before_generation[slug] = project_rag_status(home, slug)["generation_digest"]
    index_tree_before = {
        p.relative_to(home).as_posix() for p in (home / "knowledge-index").rglob("*")
    }

    result = CliRunner().invoke(
        cli.app, ["ask", COMPARISON_QUESTION, "--home", str(home), "--json"]
    )
    assert result.exit_code == 0, result.output
    retrieval = json.loads(result.output)["retrieval"]
    assert retrieval["status"] == "answered"
    assert retrieval["query_count"] == 2
    assert retrieval["index_triggered"] is False

    for slug in REGISTERED:
        paths = runtime._existing_runtime_paths(home, slug)
        # Gercek indeks dosyasi byte-byte degismedi (salt okunur, indeksleme yok).
        assert paths["index"].read_bytes() == before_index_bytes[slug]
        # rag-state.json degismedi (query_verified_at yalnizca dogrulanmis sorguda yazilir).
        assert paths["state"].read_bytes() == before_state_bytes[slug]
        # Generation ilerlemedi (yeni chunk/generation uretilmedi).
        assert project_rag_status(home, slug)["generation_digest"] == before_generation[slug]
    # knowledge-index agacinda yeni dosya yok (reindex/rebuild artifact'i yok).
    index_tree_after = {
        p.relative_to(home).as_posix() for p in (home / "knowledge-index").rglob("*")
    }
    assert index_tree_after == index_tree_before


def test_real_single_project_query_keeps_legacy_contract_with_real_index(
    dual_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--project`` ile tek proje sorgusu GERCEK indeks uzerinden mevcut
    ``zekam-ask-result/v1`` semantik ve gercek citation'i korur (saglayicisiz)."""

    home = dual_home
    _block_providers(monkeypatch)
    result = CliRunner().invoke(
        cli.app,
        ["ask", "SaglikYaniti nedir?", "--project", "sky-spring-ui", "--home", str(home), "--json"],
    )
    assert result.exit_code == 0, result.output
    document = json.loads(result.output)
    assert document["schema"] == "zekam-ask-result/v1"
    assert "route" not in document
    assert document["project_ref"] == "sky-spring-ui"
    retrieval = document["retrieval"]
    assert retrieval["remote_provider_used"] is False
    assert retrieval["searched_channels"] == ["exact", "lexical"]
    citations = retrieval["citations"]
    assert citations, "gercek FTS5 kanit beklenir"
    assert citations[0]["source_ref"] == SOURCE_FILES["sky-spring-ui"]
    assert citations[0]["project_scope"] == _project_ids(home)["sky-spring-ui"]
