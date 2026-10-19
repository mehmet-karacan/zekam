"""Sentezik testler: GLM pure code-to-architecture summary helper.

Gercek veri kullanilmaz; tum chunk/identity fixture'lari senteziktir. Chunk
fixture'lari gercek ChunkView/Locator sozlesmesini (Locator dataclass,
chunk_id anahtarli identity) yansitir; dict-only mock degil.
"""

from __future__ import annotations

import hashlib

from zekam.application.source_architecture_answer import (
    SCHEMA,
    architecture_explanation,
    build_architecture_summary,
    is_architecture_query,
    is_implementation_source,
    source_module,
)
from zekam.domain.knowledge import Locator

GEN_DIGEST = "sha256:" + hashlib.sha256(b"generation").hexdigest()
PROJECT_ID = "11111111-1111-4111-8111-111111111111"
REV = "rev-20261020"


def _digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _locator(path: str) -> Locator:
    return Locator(relative_path=path, line_start=1, line_end=10)


def _view(chunk_id: str, doc_id: str, text: str, path: str) -> dict:
    """Gercek ChunkView sozlesmesi: Locator dataclass locator tasiyor."""
    return {
        "chunk_id": chunk_id,
        "document_id": doc_id,
        "text": text,
        "locator": _locator(path),
        "content_digest": _digest(text),
    }


def _identity(path: str, revision: str = REV, text: str | None = None) -> dict:
    """Gercek source_identities sozlesmesi: chunk_id anahtarli, gecerli digest'li."""
    return {
        "source_id": "sha256:" + hashlib.sha256(("sid:" + path).encode()).hexdigest(),
        "source_revision": revision,
        "source_ref": path,
        "source_digest": _digest(path),
        "content_digest": _digest(text) if text is not None else _digest(path),
    }


def _meta(chunk_ids: tuple[str, ...], *, truncated: bool = False, observed: int = 0, sampled: int = 0) -> dict:
    return {"chunk_ids": chunk_ids, "truncated": truncated, "observed_module_count": observed, "sampled_module_count": sampled}


def _build(views: dict, identities: dict, meta: dict, *, revision: str = REV, generation: str = GEN_DIGEST):
    return build_architecture_summary(
        query="mimari",
        project_id=PROJECT_ID,
        generation_digest=generation,
        source_revision=revision,
        views=views,
        identities=identities,
        candidate_metadata=meta,
    )


class TestIsImplementationSource:
    def test_code_and_manifest_accepted(self):
        assert is_implementation_source("src/zekam/application/retrieval_service.py")
        assert is_implementation_source("backend/pom.xml")
        assert is_implementation_source("web/package.json")
        assert is_implementation_source("svc/build.gradle.kts")
        assert is_implementation_source("web/src/OrderList.svelte")
        assert is_implementation_source("web/src/OrderList.vue")

    def test_unsafe_paths_rejected(self):
        assert not is_implementation_source("C:/innova/projeler/x.py")
        assert not is_implementation_source("/etc/passwd")
        assert not is_implementation_source("../secret.py")
        assert not is_implementation_source("src/../../etc/passwd.py")
        assert not is_implementation_source(" src/x.py")

    def test_docs_tests_guidance_rejected(self):
        assert not is_implementation_source("docs/rehber.md")
        assert not is_implementation_source("AKTIF_GOREV.md")
        assert not is_implementation_source("tests/unit/test_x.py")
        assert not is_implementation_source("src/test_helper.py")
        assert not is_implementation_source("src/servis_test.py")
        assert not is_implementation_source("examples/demo.py")
        assert not is_implementation_source("skills/koordinator/SKILL.md")
        assert not is_implementation_source("checkpoints/cp.json")

    def test_hidden_guidance_dirs_rejected(self):
        assert not is_implementation_source(".opencode/agent/x.py")
        assert not is_implementation_source(".agents/skills/y.py")
        assert not is_implementation_source(".git/hooks/z.py")
        assert not is_implementation_source(".zekam/notes/a.py")

    def test_arbitrary_txt_json_not_code(self):
        assert not is_implementation_source("config/app.txt")
        assert not is_implementation_source("data/export.json")
        assert not is_implementation_source("notes/readme.txt")

    def test_generated_vendor_target_rejected(self):
        assert not is_implementation_source("module/target/Foo.class")
        assert not is_implementation_source("target/generated-sources/Gen.java")
        assert not is_implementation_source("web/node_modules/lib/index.js")
        assert not is_implementation_source("vendor/lib.go")
        assert not is_implementation_source("build/out.jar")


class TestSourceModule:
    def test_first_component_and_root(self):
        assert source_module("src/zekam/application/x.py") == "src"
        assert source_module("backend/pom.xml") == "backend"
        assert source_module("pom.xml") == "."
        assert source_module("x.py") == "."


class TestIsArchitectureQuery:
    def test_turkish_accented(self):
        assert is_architecture_query("Projenin mimarisini açıklar mısın?")
        assert is_architecture_query("Modüllerin sorumlulukları nelerdir?")

    def test_turkish_ascii(self):
        assert is_architecture_query("mimari yapisi nasil?")
        assert is_architecture_query("modul sorumluluklari")

    def test_english(self):
        assert is_architecture_query("Explain the architecture of this project")
        assert is_architecture_query("Which module handles authentication?")

    def test_not_simple_module_anywhere(self):
        assert not is_architecture_query("webpack module federation config hata veriyor")
        assert not is_architecture_query("bu fonksiyon nasil calisir?")
        assert not is_architecture_query("")


class TestBuildInsufficient:
    def test_docs_only_is_insufficient(self):
        views = {"c1": _view("c1", "d1", "Mimari plan ve yol haritasi", "docs/plan.md")}
        identities = {"c1": _identity("docs/plan.md")}
        result = _build(views, identities, _meta(("c1",), observed=1, sampled=0))
        assert result["schema"] == SCHEMA
        assert result["status"] == "insufficient"
        assert result["claims"] == []
        assert result["used_chunk_ids"] == []
        assert result["coverage"]["partial"] is True

    def test_code_only_chunk_is_insufficient(self):
        text = "x = 1\ny = 2\n"
        views = {"c1": _view("c1", "d1", text, "src/app/modul.py")}
        identities = {"c1": _identity("src/app/modul.py", text=text)}
        result = _build(views, identities, _meta(("c1",)))
        assert result["status"] == "insufficient"


class TestBuildSupported:
    def test_multi_module_code_beats_irrelevant_plan(self):
        api_text = 'from fastapi import APIRouter\n\n@router.get("/orders")\ndef list_orders():\n    return []\n'
        repo_text = "from sqlalchemy.orm import Session\n\nclass OrderRepository:\n    def __init__(self, session: Session) -> None:\n        self.session = session\n"
        web_text = "import React from 'react'\nexport function OrderList() {\n  return <ul>{[]}</ul>\n}\n"
        plan_text = "Yol haritasi: oncelikli moduller.\n"
        views = {
            "c-api": _view("c-api", "d-api", api_text, "backend/api/orders.py"),
            "c-repo": _view("c-repo", "d-repo", repo_text, "backend/repo/orders.py"),
            "c-web": _view("c-web", "d-web", web_text, "web/src/OrderList.tsx"),
            "c-plan": _view("c-plan", "d-plan", plan_text, "docs/plan.md"),
        }
        identities = {
            "c-api": _identity("backend/api/orders.py", text=api_text),
            "c-repo": _identity("backend/repo/orders.py", text=repo_text),
            "c-web": _identity("web/src/OrderList.tsx", text=web_text),
            "c-plan": _identity("docs/plan.md", text=plan_text),
        }
        result = _build(views, identities, _meta(("c-api", "c-repo", "c-web", "c-plan"), observed=3, sampled=3))
        assert result["status"] == "supported"
        modules = {entry["module"] for entry in result["modules"]}
        assert modules == {"backend", "web"}
        assert "docs/plan.md" not in str(result["modules"])
        assert "c-plan" not in result["used_chunk_ids"]
        roles = {claim["role"] for claim in result["claims"]}
        assert "http_api" in roles
        assert "data_access" in roles
        assert "frontend_rendering" in roles
        for claim in result["claims"]:
            assert claim["source_ref"] != "docs/plan.md"
            assert claim["evidence_chunk_ids"]

    def test_route_and_dependency_claims_carry_source_refs(self):
        api_text = '@app.get("/health")\ndef health():\n'
        views = {"c1": _view("c1", "d1", api_text, "api/routes.py")}
        identities = {"c1": _identity("api/routes.py", text=api_text)}
        result = _build(views, identities, _meta(("c1",), observed=1, sampled=1))
        assert result["status"] == "supported"
        assert any(claim["role"] == "http_api" for claim in result["claims"])
        claim = result["claims"][0]
        assert claim["source_ref"] == "api/routes.py"
        assert claim["locator"]["relative_path"] == "api/routes.py"
        assert claim["evidence_chunk_ids"] == ["c1"]

    def test_role_like_names_and_comments_no_fake_framework(self):
        text = (
            "# mimari not: Bu dosya HTTP API ve veritabani repository'sidir\n"
            '"""REST controller benzeri servis."""\n'
            "class UserServiceLikeHelper:\n"
            "    def run(self):\n"
            "        return 1\n"
        )
        views = {"c1": _view("c1", "d1", text, "src/app/helper.py")}
        identities = {"c1": _identity("src/app/helper.py", text=text)}
        result = _build(views, identities, _meta(("c1",), observed=1, sampled=1))
        assert result["status"] == "supported"
        assert all(claim["role"] == "structural" for claim in result["claims"])
        assert not any(claim["role"] in ("http_api", "data_access", "service_logic") for claim in result["claims"])

    def test_string_literal_signal_is_masked(self):
        text = 'const note = "@RestController"\nconst url = "https://api.example.com/orders"\n'
        views = {"c1": _view("c1", "d1", text, "web/src/client.ts")}
        identities = {"c1": _identity("web/src/client.ts", text=text)}
        result = _build(views, identities, _meta(("c1",), observed=1, sampled=1))
        assert result["status"] == "insufficient"

    def test_template_literal_signal_is_masked(self):
        """Backtick template literal icerigi (escape'li backtick dahil) kanit degil."""
        text = 'const example = `@RestController ...`\nconst esc = `a\\`b`\nconst url = `https://api.example.com`\n'
        views = {"c1": _view("c1", "d1", text, "web/src/client.ts")}
        identities = {"c1": _identity("web/src/client.ts", text=text)}
        result = _build(views, identities, _meta(("c1",), observed=1, sampled=1))
        assert result["status"] == "insufficient"

    def test_tsx_component_import_plus_jsx_is_rendering(self):
        """Genuine TSX bileseni: import + gercek JSX render kaniti."""
        text = "import React from 'react'\nexport function OrderList() {\n  return <ul className='list'>{[]}</ul>\n}\n"
        views = {"c1": _view("c1", "d1", text, "web/src/OrderList.tsx")}
        identities = {"c1": _identity("web/src/OrderList.tsx", text=text)}
        result = _build(views, identities, _meta(("c1",), observed=1, sampled=1))
        assert result["status"] == "supported"
        assert any(claim["role"] == "frontend_rendering" for claim in result["claims"])
        assert any("react import" in claim["text"] or "jsx" in claim["text"] for claim in result["claims"])

    def test_import_string_cue_without_jsx_is_not_rendering(self):
        """Import belirteci tek basina render kaniti degil; string ipucu da sayilmaz."""
        text = 'import React from "react"\nconst cue = "ReactDOM.render"\nexport function OrderList() { return null }\n'
        views = {"c1": _view("c1", "d1", text, "web/src/OrderList.tsx")}
        identities = {"c1": _identity("web/src/OrderList.tsx", text=text)}
        result = _build(views, identities, _meta(("c1",), observed=1, sampled=1))
        assert result["status"] == "insufficient"

    def test_fake_framework_in_template_literal_not_http_evidence(self):
        """Template literal icindeki sahte framework anotasyonu HTTP kaniti olmaz."""
        text = "const doc = `\n@RestController\nclass FakeApi {}\n`\nexport function render() { return null }\n"
        views = {"c1": _view("c1", "d1", text, "web/src/doc.ts")}
        identities = {"c1": _identity("web/src/doc.ts", text=text)}
        result = _build(views, identities, _meta(("c1",), observed=1, sampled=1))
        assert result["status"] == "insufficient"

    def test_svelte_component_template_proof(self):
        text = "<script>\n  import OrderRow from './OrderRow.svelte'\n</script>\n<template>\n  <OrderRow />\n</template>\n"
        views = {"c1": _view("c1", "d1", text, "web/src/OrderList.svelte")}
        identities = {"c1": _identity("web/src/OrderList.svelte", text=text)}
        result = _build(views, identities, _meta(("c1",), observed=1, sampled=1))
        assert result["status"] == "supported"
        assert any(claim["role"] == "frontend_rendering" for claim in result["claims"])

    def test_it_java_suffix_rejected_and_audit_java_accepted(self):
        """*IT.java konvansiyonu buyuk/kucuk harf duyarlidir; Audit.java gercek koddur."""
        assert not is_implementation_source("src/test/java/OrderIT.java")
        assert not is_implementation_source("src/it/java/OrderIT.java")
        assert is_implementation_source("src/main/java/tr/com/innova/Audit.java")
        assert is_implementation_source("src/main/java/tr/com/innova/Unit.java")

    def test_dependency_declaration_claimed(self):
        pom = (
            "<project><artifactId>orders-api</artifactId>"
            "<dependencies><dependency><artifactId>spring-boot-starter-web</artifactId></dependency></dependencies>"
            "</project>"
        )
        views = {"c1": _view("c1", "d1", pom, "backend/pom.xml")}
        identities = {"c1": _identity("backend/pom.xml", text=pom)}
        result = _build(views, identities, _meta(("c1",), observed=1, sampled=1))
        assert result["status"] == "supported"
        assert any(claim["role"] == "library_dependency" for claim in result["claims"])
        assert any("spring-boot-starter-web" in claim["text"] for claim in result["claims"])

    def test_pom_nested_plugin_dependencies_not_runtime(self):
        """Plugin icice dependencies bloklari runtime/module bagimliligi degil."""
        pom = (
            "<project><artifactId>orders-api</artifactId>"
            "<build><plugins><plugin><artifactId>maven-shade-plugin</artifactId>"
            "<dependencies><dependency><artifactId>plugin-internal-lib</artifactId></dependency></dependencies>"
            "</plugin></plugins></build>"
            "<dependencies><dependency><artifactId>real-runtime-lib</artifactId></dependency></dependencies>"
            "</project>"
        )
        views = {"c1": _view("c1", "d1", pom, "backend/pom.xml")}
        identities = {"c1": _identity("backend/pom.xml", text=pom)}
        result = _build(views, identities, _meta(("c1",), observed=1, sampled=1))
        assert result["status"] == "supported"
        dep_claims = [claim for claim in result["claims"] if claim["role"] == "library_dependency"]
        assert dep_claims
        assert any("real-runtime-lib" in claim["text"] for claim in dep_claims)
        assert not any("plugin-internal-lib" in claim["text"] for claim in result["claims"])

    def test_pom_namespaced_dependency_claimed(self):
        """Namespace'li Maven POM'da dogrudan dependencies hala okunur."""
        pom = (
            '<project xmlns="http://maven.apache.org/POM/4.0.0">'
            "<artifactId>orders-api</artifactId>"
            "<dependencies><dependency><artifactId>spring-context</artifactId></dependency></dependencies>"
            "</project>"
        )
        views = {"c1": _view("c1", "d1", pom, "backend/pom.xml")}
        identities = {"c1": _identity("backend/pom.xml", text=pom)}
        result = _build(views, identities, _meta(("c1",), observed=1, sampled=1))
        assert result["status"] == "supported"
        assert any("spring-context" in claim["text"] for claim in result["claims"])

    def test_pom_project_parent_plugin_not_dependencies(self):
        pom = (
            "<project><parent><artifactId>spring-boot-parent</artifactId></parent>"
            "<artifactId>orders-api</artifactId>"
            "<build><plugins><plugin><artifactId>maven-shade-plugin</artifactId></plugin></plugins></build>"
            "</project>"
        )
        views = {"c1": _view("c1", "d1", pom, "backend/pom.xml")}
        identities = {"c1": _identity("backend/pom.xml", text=pom)}
        result = _build(views, identities, _meta(("c1",), observed=1, sampled=1))
        assert result["status"] == "insufficient"

    def test_pom_dependency_management_not_runtime_dependency(self):
        pom = (
            "<project><dependencyManagement>"
            "<dependencies><dependency><artifactId>managed-lib</artifactId></dependency></dependencies>"
            "</dependencyManagement></project>"
        )
        views = {"c1": _view("c1", "d1", pom, "backend/pom.xml")}
        identities = {"c1": _identity("backend/pom.xml", text=pom)}
        result = _build(views, identities, _meta(("c1",), observed=1, sampled=1))
        assert result["status"] == "insufficient"

    def test_rendered_text_has_responsibilities_and_sources(self):
        api_text = 'from fastapi import APIRouter\n\n@router.get("/orders")\ndef list_orders():\n    return []\n'
        views = {"c1": _view("c1", "d1", api_text, "backend/api/orders.py")}
        identities = {"c1": _identity("backend/api/orders.py", text=api_text)}
        result = _build(views, identities, _meta(("c1",), observed=1, sampled=1))
        assert result["status"] == "supported"
        text = result["text"]
        assert "backend" in text
        assert "HTTP isteklerini karsilar" in text
        assert "backend/api/orders.py" in text
        assert "(L1-L10)" in text


class TestBuildMismatchRejection:
    def test_identity_digest_mismatch_dropped(self):
        text = 'from sqlalchemy import Column\nclass User:\n    pass\n'
        tampered = _view("c1", "d1", text, "src/app/model.py")
        tampered["content_digest"] = _digest("baska metin")
        identities = {"c1": _identity("src/app/model.py", text=text)}
        result = _build({"c1": tampered}, identities, _meta(("c1",)))
        assert result["status"] == "insufficient"

    def test_identity_content_digest_mismatch_dropped(self):
        text = 'from sqlalchemy import Column\nclass User:\n    pass\n'
        views = {"c1": _view("c1", "d1", text, "src/app/model.py")}
        identities = {"c1": _identity("src/app/model.py", text="baska icerik")}
        result = _build(views, identities, _meta(("c1",)))
        assert result["status"] == "insufficient"

    def test_empty_identity_content_digest_rejected(self):
        text = 'from sqlalchemy import Column\nclass User:\n    pass\n'
        views = {"c1": _view("c1", "d1", text, "src/app/model.py")}
        identity = _identity("src/app/model.py", text=text)
        identity["content_digest"] = ""
        result = _build(views, {"c1": identity}, _meta(("c1",)))
        assert result["status"] == "insufficient"

    def test_empty_identity_source_digest_rejected(self):
        text = 'from sqlalchemy import Column\nclass User:\n    pass\n'
        views = {"c1": _view("c1", "d1", text, "src/app/model.py")}
        identity = _identity("src/app/model.py", text=text)
        identity["source_digest"] = ""
        result = _build(views, {"c1": identity}, _meta(("c1",)))
        assert result["status"] == "insufficient"

    def test_document_id_keyed_identity_not_used(self):
        """Gercek API chunk_id anahtarli; document_id eslesmesi uydurmadir."""
        text = 'from sqlalchemy import Column\nclass User:\n    pass\n'
        views = {"c1": _view("c1", "d1", text, "src/app/model.py")}
        identities = {"d1": _identity("src/app/model.py", text=text)}
        result = _build(views, identities, _meta(("c1",)))
        assert result["status"] == "insufficient"

    def test_identity_source_ref_locator_mismatch_dropped(self):
        text = "class Api:\n    pass\n"
        view = _view("c1", "d1", text, "src/app/api.py")
        view["locator"] = _locator("src/app/other.py")
        identities = {"c1": _identity("src/app/api.py", text=text)}
        result = _build({"c1": view}, identities, _meta(("c1",)))
        assert result["status"] == "insufficient"

    def test_source_revision_mismatch_dropped(self):
        text = "class Api:\n    pass\n"
        views = {"c1": _view("c1", "d1", text, "src/app/api.py")}
        identities = {"c1": _identity("src/app/api.py", revision="rev-baska", text=text)}
        result = _build(views, identities, _meta(("c1",)))
        assert result["status"] == "insufficient"

    def test_project_generation_binding_exposed_and_validated(self):
        text = "class Api:\n    pass\n"
        views = {"c1": _view("c1", "d1", text, "src/app/api.py")}
        identities = {"c1": _identity("src/app/api.py", text=text)}
        result = _build(views, identities, _meta(("c1",), observed=1, sampled=1))
        assert result["project_id"] == PROJECT_ID
        assert result["generation_digest"] == GEN_DIGEST
        assert result["source_revision"] == REV
        bad = _build(views, identities, _meta(("c1",)), generation="sha256:gecersiz")
        assert bad["generation_digest"] != GEN_DIGEST
        assert bad["status"] == "insufficient"


class TestCoverageHonesty:
    def test_truncated_and_sampling_partial(self):
        text = "class Api:\n    pass\n"
        views = {"c1": _view("c1", "d1", text, "src/app/api.py")}
        identities = {"c1": _identity("src/app/api.py", text=text)}
        result = _build(views, identities, _meta(("c1",), truncated=True, observed=5, sampled=1))
        assert result["status"] == "supported"
        assert result["coverage"]["partial"] is True
        assert any("ornekleme sinirli" in note for note in result["coverage"]["notes"])

    def test_missing_expected_module_from_pom(self):
        pom = "<project><modules><module>orders-api</module><module>billing</module></modules></project>"
        views = {"c1": _view("c1", "d1", pom, "kok/pom.xml")}
        identities = {"c1": _identity("kok/pom.xml", text=pom)}
        result = _build(views, identities, _meta(("c1",), observed=1, sampled=1))
        assert result["status"] == "supported"
        assert result["coverage"]["partial"] is True
        assert any("billing" in note for note in result["coverage"]["notes"])
        module_names = {entry["module"] for entry in result["modules"]}
        assert "kok" in module_names

    def test_truncated_pom_reported_partial(self):
        pom = "<project><artifactId>orders-api</artifactId>"
        views = {"c1": _view("c1", "d1", pom, "backend/pom.xml")}
        identities = {"c1": _identity("backend/pom.xml", text=pom)}
        result = _build(views, identities, _meta(("c1",), observed=1, sampled=1))
        assert result["status"] == "insufficient"
        assert any("kismi" in note for note in result["coverage"]["notes"])

    def test_claim_budget_fair_per_module(self):
        texts = {f"c{i}": f"class Servis{i}:\n    pass\n" for i in range(1, 8)}
        views = {cid: _view(cid, "d-" + cid, text, f"mod{i}/servis.py") for i, (cid, text) in enumerate(texts.items(), start=1)}
        identities = {cid: _identity(f"mod{i}/servis.py", text=texts[cid]) for i, cid in enumerate(texts, start=1)}
        result = _build(views, identities, _meta(tuple(texts), observed=7, sampled=7))
        assert result["status"] == "supported"
        assert len(result["claims"]) <= 12
        claim_modules = {claim["module"] for claim in result["claims"]}
        assert claim_modules == {f"mod{i}" for i in range(1, 8)}
        evidence = {cid for claim in result["claims"] for cid in claim["evidence_chunk_ids"]}
        assert evidence == set(result["used_chunk_ids"])
        omitted = [note for note in result["coverage"]["notes"] if "aciklanmayan" in note]
        assert omitted == []

    def test_claim_budget_omission_reported(self):
        views = {}
        identities = {}
        for i in range(1, 15):
            text = f"class Api{i}:\n    pass\n"
            views[f"c{i}"] = _view(f"c{i}", f"d{i}", text, f"m{i}/api.py")
            identities[f"c{i}"] = _identity(f"m{i}/api.py", text=text)
        result = _build(views, identities, _meta(tuple(f"c{i}" for i in range(1, 15)), observed=14, sampled=14))
        assert result["status"] == "supported"
        # 14 modul > 12 kanit butcesi: bazi moduller aciklanmayan modul notuyla raporlanir.
        assert len(result["claims"]) <= 12
        evidence = {cid for claim in result["claims"] for cid in claim["evidence_chunk_ids"]}
        assert evidence == set(result["used_chunk_ids"])
        assert any("aciklanmayan" in note for note in result["coverage"]["notes"])

    def test_later_module_not_marked_missing_forever(self):
        pom = "<project><modules><module>billing</module></modules></project>"
        billing_text = "class BillingService:\n    pass\n"
        views = {
            "c-pom": _view("c-pom", "d-pom", pom, "kok/pom.xml"),
            "c-billing": _view("c-billing", "d-billing", billing_text, "billing/service.py"),
        }
        identities = {
            "c-pom": _identity("kok/pom.xml", text=pom),
            "c-billing": _identity("billing/service.py", text=billing_text),
        }
        result = _build(views, identities, _meta(("c-pom", "c-billing"), observed=2, sampled=2))
        assert result["status"] == "supported"
        missing_notes = [note for note in result["coverage"]["notes"] if "beklenen" in note]
        assert not any("billing" in note for note in missing_notes)


class TestArchitectureExplanation:
    def _rag_result(self, **overrides):
        api_text = 'from fastapi import APIRouter\n\n@router.get("/orders")\ndef list_orders():\n    return []\n'
        path = "backend/api/orders.py"
        identity = _identity(path, text=api_text)
        views = {"c1": _view("c1", "d1", api_text, path)}
        summary = _build(views, {"c1": identity}, _meta(("c1",), observed=1, sampled=1))
        citation = {
            "source_id": identity["source_id"],
            "project_scope": PROJECT_ID,
            "source_ref": path,
            "source_revision": REV,
            "source_digest": identity["source_digest"],
            "content_digest": identity["content_digest"],
            "chunk_id": "c1",
            "locator_type": "project-file",
            "locator": _locator(path).as_dict(),
            "retrieval_channels": ["exact"],
            "rank_trace": {"fused_rank": 1, "rrf_score": 1.0, "exact_match": True, "channel_ranks": {"exact": 1}},
        }
        result = {
            "schema": "zekam-embedded-rag-result/v1",
            "state": "supported",
            "project_id": PROJECT_ID,
            "generation_digest": GEN_DIGEST,
            "source_revision": REV,
            "index_freshness": "current",
            "snapshot_only": False,
            "citations": [citation],
            "architecture_summary": summary,
        }
        for key, value in overrides.items():
            if value is _DELETE:
                result.pop(key, None)
            else:
                result[key] = value
        return result

    def test_correct_native_rag_result_admitted(self):
        result = self._rag_result()
        explanation = architecture_explanation(result)
        assert explanation is not None
        assert "Kod mimarisi ozeti" in explanation
        assert "HTTP isteklerini karsilar" in explanation

    def test_standalone_summary_without_rag_envelope_rejected(self):
        summary = self._rag_result()["architecture_summary"]
        assert architecture_explanation(summary) is None

    def test_free_text_payload_rejected(self):
        assert architecture_explanation({"schema": "zekam-embedded-rag-result/v1", "text": "serbest metin"}) is None

    def test_stale_and_snapshot_rejected(self):
        assert architecture_explanation(self._rag_result(index_freshness="stale")) is None
        assert architecture_explanation(self._rag_result(snapshot_only=True)) is None
        assert architecture_explanation(self._rag_result(index_freshness="stale", snapshot_only=True)) is None

    def test_binding_mismatch_rejected(self):
        assert architecture_explanation(self._rag_result(project_id="22222222-2222-4222-8222-222222222222")) is None
        assert architecture_explanation(self._rag_result(source_revision="rev-baska")) is None
        assert architecture_explanation(self._rag_result(generation_digest="sha256:gecersiz")) is None

    def test_summary_binding_mismatch_rejected(self):
        result = self._rag_result()
        tampered_summary = dict(result["architecture_summary"])
        tampered_summary["project_id"] = "22222222-2222-4222-8222-222222222222"
        result["architecture_summary"] = tampered_summary
        assert architecture_explanation(result) is None

    def test_citation_chunk_missing_rejected(self):
        result = self._rag_result()
        result["citations"] = []
        assert architecture_explanation(result) is None

    def test_citation_digest_mismatch_rejected(self):
        result = self._rag_result()
        citation = dict(result["citations"][0])
        citation["content_digest"] = _digest("baska")
        result["citations"] = [citation]
        assert architecture_explanation(result) is None

    def test_citation_locator_mismatch_rejected(self):
        result = self._rag_result()
        citation = dict(result["citations"][0])
        citation["locator"] = _locator("backend/api/other.py").as_dict()
        result["citations"] = [citation]
        assert architecture_explanation(result) is None

    def test_citation_project_scope_mismatch_rejected(self):
        result = self._rag_result()
        citation = dict(result["citations"][0])
        citation["project_scope"] = "22222222-2222-4222-8222-222222222222"
        result["citations"] = [citation]
        assert architecture_explanation(result) is None

    def test_citation_source_revision_mismatch_rejected(self):
        result = self._rag_result()
        citation = dict(result["citations"][0])
        citation["source_revision"] = "rev-baska"
        result["citations"] = [citation]
        assert architecture_explanation(result) is None

    def test_used_ids_mismatch_rejected(self):
        result = self._rag_result()
        summary = dict(result["architecture_summary"])
        summary["used_chunk_ids"] = ["c1", "c2"]
        result["architecture_summary"] = summary
        assert architecture_explanation(result) is None

    def test_text_tamper_rejected(self):
        result = self._rag_result()
        summary = dict(result["architecture_summary"])
        summary["text"] = "serbest uydurma mimari aciklamasi"
        result["architecture_summary"] = summary
        assert architecture_explanation(result) is None

    def test_insufficient_summary_rejected(self):
        result = self._rag_result()
        summary = dict(result["architecture_summary"])
        summary["status"] = "insufficient"
        result["architecture_summary"] = summary
        assert architecture_explanation(result) is None

    def test_non_dict_and_none_rejected(self):
        assert architecture_explanation(None) is None
        assert architecture_explanation("text") is None
        assert architecture_explanation([]) is None


_DELETE = object()


def test_imported_reference_is_not_current_repository_code():
    assert not is_implementation_source(".zekam-reference/import/package.json")
    assert not is_implementation_source(".zekam-reference/import/Server.java")
    assert is_implementation_source("backend/src/Server.java")


def test_useeffect_alone_is_not_frontend_api_request():
    """useEffect tek basina HTTP/API istegi kaniti degil."""
    text = "export function Page() { useEffect(() => {}, []) }\n"
    path = "ui/src/Page.tsx"
    views = {"c1": _view("c1", "d1", text, path)}
    identities = {"c1": _identity(path, text=text)}
    result = _build(views, identities, _meta(("c1",), observed=1, sampled=1))
    roles = [claim["role"] for claim in result["claims"]]
    assert "frontend_api_requests" not in roles


def test_fake_role_prefix_annotations_do_not_match():
    """@ServiceProvider / @RestControllerAdvice substring eslesme uretmez."""
    service_text = "@ServiceProvider\nclass FakeProvider {}\n"
    path_service = "backend/src/FakeProvider.java"
    views = {"c1": _view("c1", "d1", service_text, path_service)}
    identities = {"c1": _identity(path_service, text=service_text)}
    result = _build(views, identities, _meta(("c1",), observed=1, sampled=1))
    roles = [claim["role"] for claim in result["claims"]]
    assert "service_logic" not in roles
    assert "http_api" not in roles

    advice_text = "@RestControllerAdvice\nclass FakeAdvice {}\n"
    path_advice = "backend/src/FakeAdvice.java"
    views = {"c2": _view("c2", "d2", advice_text, path_advice)}
    identities = {"c2": _identity(path_advice, text=advice_text)}
    result = _build(views, identities, _meta(("c2",), observed=1, sampled=1))
    roles = [claim["role"] for claim in result["claims"]]
    assert "http_api" not in roles
    assert "service_logic" not in roles


def test_real_service_and_restcontroller_still_match():
    """Gercek @Service sinifi ve @RestController rolu kanit olarak kalir."""
    service_text = "@Service\nclass OrderService {}\n"
    path_service = "backend/src/OrderService.java"
    views = {"c1": _view("c1", "d1", service_text, path_service)}
    identities = {"c1": _identity(path_service, text=service_text)}
    result = _build(views, identities, _meta(("c1",), observed=1, sampled=1))
    assert any(claim["role"] == "service_logic" for claim in result["claims"])

    api_text = 'from fastapi import APIRouter\n\n@router.get("/orders")\ndef list_orders():\n    return []\n'
    path_api = "backend/src/orders_api.py"
    views = {"c2": _view("c2", "d2", api_text, path_api)}
    identities = {"c2": _identity(path_api, text=api_text)}
    result = _build(views, identities, _meta(("c2",), observed=1, sampled=1))
    assert any(claim["role"] == "http_api" for claim in result["claims"])
