"""WP-03.05: citation'a bagli aciklama kapisi sozlesmesi.

Kimlik modeli: retrieval entry ``project_ref`` slug'dir; status readback
``entry.project_id``, alt RAG sonucu ``project_id`` ve citation
``project_scope`` proje UUID'sidir.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from zekam.application import natural_request_retrieval as retrieval_subject
from zekam.application.natural_request_answer import (
    CLARIFICATION_ANSWER,
    FAILURE_ANSWER,
    MAX_EXPLANATION_BYTES,
    UNCERTAINTY_ANSWER,
    answer_natural_request,
    render_natural_request_answer,
)
from zekam.application.natural_request_retrieval import retrieve_natural_request
from zekam.application.natural_request_scope import NaturalRequestScope
from zekam.domain.canonical import digest
from zekam.domain.errors import ValidationFailed

VALID_DIGEST_A = digest({"fixture": "source-a"})
VALID_DIGEST_B = digest({"fixture": "source-b"})

GPU_PROJECT_ID = "11111111-1111-4111-8111-111111111111"
SKY_UI_PROJECT_ID = "22222222-2222-4222-8222-222222222222"
SKY_MS_PROJECT_ID = "33333333-3333-4333-8333-333333333333"
PROJECT_IDS = {
    "gpu-fusion": GPU_PROJECT_ID,
    "sky-spring-ui": SKY_UI_PROJECT_ID,
    "sky-microservis": SKY_MS_PROJECT_ID,
}


def _citation(
    project_scope: str = GPU_PROJECT_ID,
    *,
    source_ref: str = "src/service/audit_service.py",
    source_digest: str = VALID_DIGEST_A,
    locator: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "source_id": f"src:{source_ref}",
        "project_scope": project_scope,
        "source_ref": source_ref,
        "source_revision": "rev-1",
        "source_digest": source_digest,
        "content_digest": digest({"fixture": "chunk-content"}),
        "chunk_id": "chunk-1",
        "locator_type": "project-file",
        "locator": locator if locator is not None else {"relative_path": source_ref},
    }


def _rag_result(
    project_id: str,
    citations: list[dict[str, Any]],
    *,
    state: str = "answered",
) -> dict[str, Any]:
    return {
        "schema": "zekam-embedded-rag-result/v1",
        "state": state,
        "project_id": project_id,
        "citations": citations,
        "evidence_found": bool(citations),
    }


def _entry(
    ref: str,
    status: str,
    result: dict[str, Any] | None,
    *,
    unavailable_reason: str | None = None,
) -> dict[str, Any]:
    return {
        "project_ref": ref,
        "project_id": PROJECT_IDS[ref],
        "selected_by": "route",
        "query_attempted": status == "answered",
        "status": status,
        "unavailable_reason": unavailable_reason,
        "result": result,
    }


def _retrieval(
    status: str,
    results: list[dict[str, Any]],
    reason_codes: tuple[str, ...] = (),
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "schema": "zekam-natural-request-retrieval/v1",
        "status": status,
        "question_digest": digest({"question": "GPU servisleri nasil calisiyor?"}),
        "project_refs": [item["project_ref"] for item in results],
        "reason_codes": list(reason_codes),
        "results": results,
        "query_count": sum(1 for item in results if item["query_attempted"]),
        "source_access": "read-only",
        "grants_authority": False,
    }
    return body | {"retrieval_digest": digest(body)}


def _valid_single_retrieval() -> dict[str, Any]:
    return _retrieval(
        "answered",
        [_entry("gpu-fusion", "answered", _rag_result(GPU_PROJECT_ID, [_citation()]))],
    )


def test_registered_scope_identity_flows_through_retrieval_to_answer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    scope = NaturalRequestScope(
        status="selected",
        project_refs=("gpu-fusion",),
        reason_codes=(),
        jira_issue_key=None,
        selected_by="route",
        route_digest=digest({"route": "fixture"}),
    )
    monkeypatch.setattr(
        retrieval_subject,
        "project_rag_status",
        lambda _home, _slug: {
            "state": "ready",
            "index_readable": True,
            "project_id": GPU_PROJECT_ID,
        },
    )
    monkeypatch.setattr(
        retrieval_subject,
        "query_registered_project",
        lambda _home, _slug, _question, **_kwargs: _rag_result(GPU_PROJECT_ID, [_citation()]),
    )
    retrieval = retrieve_natural_request(
        scope, tmp_path, "GPU servisleri", registered_project_refs=frozenset({"gpu-fusion"})
    )
    outcome = answer_natural_request(retrieval, "AuditService kaynagi bulundu.")
    assert retrieval["results"][0]["project_id"] == GPU_PROJECT_ID
    assert outcome["proposed_explanation_accepted"] is True


def test_single_project_with_valid_locator_keeps_proposed_explanation() -> None:
    retrieval = _valid_single_retrieval()
    proposed = "Audit servisleri AuditService uzerinden calisiyor."

    outcome = answer_natural_request(retrieval, proposed)

    assert outcome["answer"] == proposed
    assert outcome["proposed_explanation_accepted"] is True
    assert outcome["reason_codes"] == []
    assert outcome["schema"] == "zekam-natural-request-answer/v1"
    assert outcome["effect_claimed"] is False
    assert outcome["grants_authority"] is False


def test_slug_entry_with_uuid_project_id_is_accepted_without_equating_them() -> None:
    # Entry project_ref slug, RAG result project_id UUID; bu ikisinin esit
    # sayilmasi gerektirilmemelidir (guvenilir dis baglama retrieval'dadir).
    retrieval = _valid_single_retrieval()
    assert retrieval["results"][0]["project_ref"] != retrieval["results"][0]["result"]["project_id"]

    outcome = answer_natural_request(retrieval, "Servisler AuditService uzerinden calisiyor.")

    assert outcome["proposed_explanation_accepted"] is True


def test_retrieval_output_preserved_separately_and_unchanged() -> None:
    retrieval = _valid_single_retrieval()
    snapshot = dict(retrieval)

    outcome = answer_natural_request(retrieval, "Oneri")

    assert outcome["retrieval"] is retrieval
    assert dict(outcome["retrieval"]) == snapshot
    assert outcome["answer"] is not outcome["retrieval"]
    assert outcome["retrieval"]["retrieval_digest"].startswith("sha256:")


def test_multi_project_partial_with_unavailable_project_rejects_global_claim() -> None:
    proposed = "Iki proje ayni musteri akisini kullaniyor."
    retrieval = _retrieval(
        "answered",
        [
            _entry(
                "sky-spring-ui",
                "answered",
                _rag_result(
                    SKY_UI_PROJECT_ID,
                    [_citation(project_scope=SKY_UI_PROJECT_ID, source_ref="ui/app.tsx")],
                ),
            ),
            _entry(
                "sky-microservis",
                "unavailable",
                None,
                unavailable_reason="index-unavailable",
            ),
        ],
        ("index-unavailable",),
    )

    outcome = answer_natural_request(retrieval, proposed)

    assert outcome["answer"] == FAILURE_ANSWER
    assert outcome["answer"] != proposed
    assert outcome["proposed_explanation_accepted"] is False
    assert outcome["reason_codes"] == ["project-unavailable"]


def test_multi_project_partial_with_missing_citation_rejects_global_claim() -> None:
    proposed = "Iki proje ayni musteri akisini kullaniyor."
    retrieval = _retrieval(
        "answered",
        [
            _entry(
                "sky-spring-ui",
                "answered",
                _rag_result(SKY_UI_PROJECT_ID, []),
            ),
            _entry(
                "sky-microservis",
                "answered",
                _rag_result(
                    SKY_MS_PROJECT_ID,
                    [
                        _citation(
                            project_scope=SKY_MS_PROJECT_ID,
                            source_ref="api/customer_api.py",
                            source_digest=VALID_DIGEST_B,
                        )
                    ],
                ),
            ),
        ],
    )

    outcome = answer_natural_request(retrieval, proposed)

    assert outcome["answer"] == UNCERTAINTY_ANSWER
    assert outcome["answer"] != proposed
    assert outcome["proposed_explanation_accepted"] is False
    assert outcome["reason_codes"] == ["citation-invalid"]


def test_multi_project_all_valid_keeps_comparison_claim() -> None:
    proposed = "Iki proje ayni musteri akisini kullaniyor."
    retrieval = _retrieval(
        "answered",
        [
            _entry(
                "sky-spring-ui",
                "answered",
                _rag_result(
                    SKY_UI_PROJECT_ID,
                    [_citation(project_scope=SKY_UI_PROJECT_ID, source_ref="ui/app.tsx")],
                ),
            ),
            _entry(
                "sky-microservis",
                "answered",
                _rag_result(
                    SKY_MS_PROJECT_ID,
                    [
                        _citation(
                            project_scope=SKY_MS_PROJECT_ID,
                            source_ref="api/customer_api.py",
                            source_digest=VALID_DIGEST_B,
                        )
                    ],
                ),
            ),
        ],
    )

    outcome = answer_natural_request(retrieval, proposed)

    assert outcome["answer"] == proposed
    assert outcome["proposed_explanation_accepted"] is True
    assert outcome["reason_codes"] == []


def test_missing_locator_is_replaced_with_uncertainty_answer() -> None:
    retrieval = _retrieval(
        "answered",
        [_entry("gpu-fusion", "answered", _rag_result(GPU_PROJECT_ID, [_citation(locator={})]))],
    )

    outcome = answer_natural_request(retrieval, "Kesin teknik iddia")

    assert outcome["answer"] == UNCERTAINTY_ANSWER
    assert outcome["proposed_explanation_accepted"] is False


@pytest.mark.parametrize(
    ("state", "evidence_found"),
    [("abstained-low-evidence", True), ("answered", False)],
)
def test_citation_cannot_override_retrieval_evidence_state(
    state: str, evidence_found: bool
) -> None:
    retrieval = _valid_single_retrieval()
    result = retrieval["results"][0]["result"]
    result["state"] = state
    result["evidence_found"] = evidence_found
    outcome = answer_natural_request(retrieval, "Kesin teknik iddia")
    assert outcome["answer"] == UNCERTAINTY_ANSWER
    assert outcome["proposed_explanation_accepted"] is False


def test_empty_locator_fields_are_replaced_with_uncertainty_answer() -> None:
    retrieval = _retrieval(
        "answered",
        [
            _entry(
                "gpu-fusion",
                "answered",
                _rag_result(
                    GPU_PROJECT_ID,
                    [_citation(locator={"relative_path": "  ", "object_name": None})],
                ),
            )
        ],
    )

    outcome = answer_natural_request(retrieval, "Kesin teknik iddia")

    assert outcome["answer"] == UNCERTAINTY_ANSWER
    assert outcome["proposed_explanation_accepted"] is False


def test_foreign_project_scope_citation_is_rejected() -> None:
    retrieval = _retrieval(
        "answered",
        [
            _entry(
                "gpu-fusion",
                "answered",
                _rag_result(
                    GPU_PROJECT_ID,
                    [_citation(project_scope=SKY_UI_PROJECT_ID, source_ref="ui/app.tsx")],
                ),
            )
        ],
    )

    outcome = answer_natural_request(retrieval, "Kesin teknik iddia")

    assert outcome["answer"] == UNCERTAINTY_ANSWER
    assert outcome["proposed_explanation_accepted"] is False
    assert outcome["reason_codes"] == ["citation-invalid"]


def test_slug_project_scope_does_not_match_uuid_project_id() -> None:
    # Eski hatanin regresyonu: citation project_scope slug, result project_id
    # UUID; esit kabul edilmez.
    retrieval = _retrieval(
        "answered",
        [
            _entry(
                "gpu-fusion",
                "answered",
                _rag_result(GPU_PROJECT_ID, [_citation(project_scope="gpu-fusion")]),
            )
        ],
    )

    outcome = answer_natural_request(retrieval, "Kesin teknik iddia")

    assert outcome["answer"] == UNCERTAINTY_ANSWER
    assert outcome["proposed_explanation_accepted"] is False


def test_mismatched_source_ref_and_digest_are_rejected() -> None:
    retrieval = _retrieval(
        "answered",
        [
            _entry(
                "gpu-fusion",
                "answered",
                _rag_result(
                    GPU_PROJECT_ID,
                    [
                        _citation(source_ref=""),
                        _citation(source_digest="sha256:yanlis-deger"),
                    ],
                ),
            )
        ],
    )

    outcome = answer_natural_request(retrieval, "Kesin teknik iddia")

    assert outcome["answer"] == UNCERTAINTY_ANSWER
    assert outcome["proposed_explanation_accepted"] is False


def test_malformed_source_digest_is_rejected() -> None:
    retrieval = _retrieval(
        "answered",
        [
            _entry(
                "gpu-fusion",
                "answered",
                _rag_result(GPU_PROJECT_ID, [_citation(source_digest="md5:abc")]),
            )
        ],
    )

    outcome = answer_natural_request(retrieval, "Kesin teknik iddia")

    assert outcome["answer"] == UNCERTAINTY_ANSWER
    assert outcome["proposed_explanation_accepted"] is False


def test_object_name_locator_is_accepted() -> None:
    retrieval = _retrieval(
        "answered",
        [
            _entry(
                "gpu-fusion",
                "answered",
                _rag_result(GPU_PROJECT_ID, [_citation(locator={"object_name": "t_audit_log"})]),
            )
        ],
    )

    outcome = answer_natural_request(retrieval, "Audit tablosu t_audit_log'dur.")

    assert outcome["answer"] == "Audit tablosu t_audit_log'dur."
    assert outcome["proposed_explanation_accepted"] is True


def test_outage_without_answered_project_gets_failure_answer() -> None:
    retrieval = _retrieval(
        "unavailable",
        [_entry("gpu-fusion", "unavailable", None, unavailable_reason="query-unavailable")],
        ("query-unavailable",),
    )

    outcome = answer_natural_request(retrieval, "Islem tamamlandi")

    assert outcome["answer"] == FAILURE_ANSWER
    assert outcome["proposed_explanation_accepted"] is False
    assert outcome["reason_codes"] == ["no-answered-project"]


def test_multi_project_outage_gets_failure_when_no_project_answered() -> None:
    retrieval = _retrieval(
        "unavailable",
        [
            _entry(
                "sky-spring-ui",
                "unavailable",
                None,
                unavailable_reason="project-unavailable",
            ),
            _entry(
                "sky-microservis",
                "unavailable",
                None,
                unavailable_reason="knowledge-scope-blocked",
            ),
        ],
        ("project-unavailable", "knowledge-scope-blocked"),
    )

    outcome = answer_natural_request(retrieval, "Karstirama tamamlandi")

    assert outcome["answer"] == FAILURE_ANSWER
    assert outcome["proposed_explanation_accepted"] is False


def test_clarification_required_status_gets_clarification_answer_not_failure() -> None:
    retrieval = _retrieval(
        "clarification-required",
        [],
        ("comparison-intent-required",),
    )

    outcome = answer_natural_request(retrieval, "Iki proje ayni akisi kullaniyor.")

    assert outcome["answer"] == CLARIFICATION_ANSWER
    assert outcome["answer"] != FAILURE_ANSWER
    assert outcome["proposed_explanation_accepted"] is False
    assert outcome["reason_codes"] == ["clarification-required"]


def test_fabricated_mutation_success_claim_is_replaced_with_uncertainty() -> None:
    retrieval = _valid_single_retrieval()
    fabricated = "Duzeltme tamamlandi; migration calistirildi ve deploy edildi."

    outcome = answer_natural_request(retrieval, fabricated)

    assert outcome["answer"] == UNCERTAINTY_ANSWER
    assert outcome["answer"] != fabricated
    assert outcome["proposed_explanation_accepted"] is False
    assert outcome["reason_codes"] == ["proposed-mutation-claim"]
    assert outcome["effect_claimed"] is False
    assert outcome["grants_authority"] is False


def test_mutation_claim_variants_are_rejected() -> None:
    retrieval = _valid_single_retrieval()
    for proposed in (
        "Yeni endpoint eklendi ve guncelleme yapildi.",
        "Deployment success: migration completed.",
    ):
        outcome = answer_natural_request(retrieval, proposed)
        assert outcome["answer"] == UNCERTAINTY_ANSWER, proposed
        assert outcome["proposed_explanation_accepted"] is False, proposed
        assert outcome["reason_codes"] == ["proposed-mutation-claim"], proposed


def test_failure_answer_does_not_claim_no_change_made() -> None:
    # Retrieval sorgusu gozlem kaydi yazabildigi icin "degisiklik yapilmadi"
    # iddiasina izin yok.
    assert "yapilmadi" not in FAILURE_ANSWER
    assert "yapılmadı" not in FAILURE_ANSWER
    assert "degisiklik" not in FAILURE_ANSWER
    assert "değişiklik" not in FAILURE_ANSWER


def test_proposed_explanation_may_be_none_and_is_rejected() -> None:
    outcome = answer_natural_request(_valid_single_retrieval(), None)

    assert outcome["answer"] == UNCERTAINTY_ANSWER
    assert outcome["proposed_explanation_accepted"] is False
    assert outcome["reason_codes"] == ["proposed-explanation-missing"]


def test_invalid_retrieval_schema_raises() -> None:
    retrieval = _valid_single_retrieval()
    retrieval["schema"] = "zekam-natural-request-retrieval/v2"
    with pytest.raises(ValidationFailed, match="schema"):
        answer_natural_request(retrieval, "Oneri")


def test_invalid_retrieval_missing_results_raises() -> None:
    retrieval = _valid_single_retrieval()
    del retrieval["results"]
    with pytest.raises(ValidationFailed, match="results"):
        answer_natural_request(retrieval, "Oneri")


def test_answered_status_with_empty_results_raises() -> None:
    retrieval = _retrieval("answered", [])
    with pytest.raises(ValidationFailed, match="bos results"):
        answer_natural_request(retrieval, "Oneri")


def test_unsupported_retrieval_status_raises() -> None:
    entry = _entry("gpu-fusion", "answered", _rag_result(GPU_PROJECT_ID, [_citation()]))
    retrieval = _retrieval("degraded", [entry])
    with pytest.raises(ValidationFailed, match="desteklenmiyor"):
        answer_natural_request(retrieval, "Oneri")


def test_answered_entry_with_non_dict_result_raises() -> None:
    retrieval = _retrieval("answered", [_entry("gpu-fusion", "answered", None)])
    with pytest.raises(ValidationFailed, match="dict"):
        answer_natural_request(retrieval, "Oneri")


def test_answered_entry_with_missing_project_id_raises() -> None:
    broken_result = {"schema": "zekam-embedded-rag-result/v1", "citations": []}
    retrieval = _retrieval("answered", [_entry("gpu-fusion", "answered", broken_result)])
    with pytest.raises(ValidationFailed, match="project_id"):
        answer_natural_request(retrieval, "Oneri")


def test_status_probe_project_id_must_match_rag_result() -> None:
    retrieval = _valid_single_retrieval()
    retrieval["results"][0]["project_id"] = SKY_UI_PROJECT_ID
    with pytest.raises(ValidationFailed, match="baglamasi"):
        answer_natural_request(retrieval, "Oneri")


def test_non_string_proposed_explanation_raises() -> None:
    with pytest.raises(ValidationFailed, match="proposed_explanation"):
        answer_natural_request(_valid_single_retrieval(), 123)  # type: ignore[arg-type]


@pytest.mark.parametrize("proposed", ["", "  ", "\n\t"])
def test_empty_explanation_is_not_accepted(proposed: str) -> None:
    outcome = answer_natural_request(_valid_single_retrieval(), proposed)
    assert outcome["answer"] == UNCERTAINTY_ANSWER
    assert outcome["reason_codes"] == ["proposed-explanation-missing"]
    assert outcome["proposed_explanation_accepted"] is False


def test_explanation_utf8_budget_is_enforced() -> None:
    outcome = answer_natural_request(
        _valid_single_retrieval(), "\u00e7" * (MAX_EXPLANATION_BYTES // 2 + 1)
    )
    assert outcome["reason_codes"] == ["proposed-explanation-too-large"]
    assert outcome["proposed_explanation_accepted"] is False


def test_valid_citation_cannot_hide_a_foreign_citation() -> None:
    retrieval = _valid_single_retrieval()
    retrieval["results"][0]["result"]["citations"].append(
        _citation(project_scope=SKY_UI_PROJECT_ID)
    )
    outcome = answer_natural_request(retrieval, "AuditService kaynagi bulundu.")
    assert outcome["reason_codes"] == ["citation-invalid"]
    assert outcome["proposed_explanation_accepted"] is False


@pytest.mark.parametrize(
    "locator",
    [
        {"relative_path": "src/foreign.py"},
        {"relative_path": "src/service/audit_service.py", "line_start": True, "line_end": 4},
        {"relative_path": "../private.py"},
        {"relative_path": "src/service/audit_service.py", "page": 0},
    ],
)
def test_invalid_or_mismatched_source_position_is_rejected(locator: dict[str, Any]) -> None:
    retrieval = _valid_single_retrieval()
    retrieval["results"][0]["result"]["citations"][0]["locator"] = locator
    outcome = answer_natural_request(retrieval, "AuditService kaynagi bulundu.")
    assert outcome["reason_codes"] == ["citation-invalid"]


def test_answer_cannot_silently_drop_a_requested_project() -> None:
    retrieval = _valid_single_retrieval()
    retrieval["project_refs"].append("sky-spring-ui")
    with pytest.raises(ValidationFailed, match="proje kapsami"):
        answer_natural_request(retrieval, "Iki proje icin kaynak bulundu.")


def test_plain_answer_shows_project_source_and_exact_position_without_changing_json() -> None:
    retrieval = _valid_single_retrieval()
    citation = retrieval["results"][0]["result"]["citations"][0]
    citation["locator"].update(line_start=4, line_end=9)
    outcome = answer_natural_request(retrieval, "AuditService kaynagi bulundu.")
    rendered = render_natural_request_answer(outcome)
    assert rendered == (
        "AuditService kaynagi bulundu.\n\nKaynaklar:\n"
        "- gpu-fusion: src/service/audit_service.py (satir 4-9)"
    )
    assert outcome["answer"] == "AuditService kaynagi bulundu."
    assert outcome["retrieval"] is retrieval


def test_plain_answer_does_not_publish_sources_for_rejected_explanation() -> None:
    outcome = answer_natural_request(_valid_single_retrieval(), "Islem tamamlandi")
    assert render_natural_request_answer(outcome) == UNCERTAINTY_ANSWER


def test_plain_outage_preserves_failure_message() -> None:
    retrieval = _retrieval(
        "unavailable", [_entry("gpu-fusion", "unavailable", None)]
    )
    outcome = answer_natural_request(retrieval, None)
    assert render_natural_request_answer(outcome) == FAILURE_ANSWER


def test_plain_comparison_keeps_same_named_sources_separate_by_project() -> None:
    retrieval = _retrieval(
        "answered",
        [
            _entry("gpu-fusion", "answered", _rag_result(GPU_PROJECT_ID, [_citation()])),
            _entry(
                "sky-spring-ui", "answered",
                _rag_result(SKY_UI_PROJECT_ID, [_citation(project_scope=SKY_UI_PROJECT_ID)]),
            ),
        ],
    )
    outcome = answer_natural_request(retrieval, "Her iki projede servis kaynagi var.")
    rendered = render_natural_request_answer(outcome)
    assert "- gpu-fusion: src/service/audit_service.py" in rendered
    assert "- sky-spring-ui: src/service/audit_service.py" in rendered
