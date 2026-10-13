"""WP-03.03: proje-kapsamli dogal dil retrieval.

Bu modul ``natural_request_scope`` kararini kayitli projelerin yerel RAG
sorgularina baglar; kapsam disinda bir etkisi yoktur:

- Kapsam ``selected`` degilse (``clarification-required`` / ``general``)
  sorgu calistirilmaz; en az gerekli clarification doner.
- Tek secili proje tam olarak bir ``query_registered_project`` cagrisi
  uretir; baska proje sorgulanmaz.
- Coklu secili proje yalnizca acik karsilastirma ifadesi ve
  ``selected_by == "route-fanout"`` kararinda
  acilir; her proje icin ayri bir cagri ve ayri etiketli sonuc doner.
  Baska bir secim kaynagi coklu projeyi asla fan-out'a cevirmez.
- Proje kayitli degilse ya da indeksi okunamiyorsa o proje ``unavailable``
  olarak raporlanir; embedding veya indeksleme baslatilmaz.
- Remote sorgu yetkilendirmesi varsayilan olarak yoktur;
  ``authorize_remote_query`` yalnizca explicit girdi olarak alt katmana
  iletilir.

Fonksiyon kaynaklara salt okunur erisir ve indekslemez. Alt katmandaki
``query_registered_project`` dogrulama gozlemi yazabilir.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Final

from zekam.application.natural_request_scope import NaturalRequestScope
from zekam.application.project_rag_runtime import (
    project_rag_status,
    query_registered_project,
)
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.identifiers import validate_slug

SCHEMA: Final = "zekam-natural-request-retrieval/v1"

#: Tek projede izni olan secim kaynaklari (exact allowlist).
_SINGLE_SELECTED_BY: Final = frozenset({"route", "explicit"})

#: Coklu projede tek izni olan fan-out kaynagi.
_FANOUT_SELECTED_BY: Final = "route-fanout"

_MAX_QUESTION_BYTES = 16_384
_COMPARISON = re.compile(
    r"(?i)(?<!\w)(?:karsilastir\w*|karşılaştır\w*|kiyas\w*|kıyas\w*|"
    r"fark\w*|compare\w*|comparison|versus|vs)(?!\w)"
)


def retrieve_natural_request(
    scope: NaturalRequestScope,
    home: Path,
    question: str,
    *,
    registered_project_refs: frozenset[str],
    authorize_remote_query: bool = False,
) -> dict[str, Any]:
    """``NaturalRequestScope`` kararini salt okunur RAG sorgularina baglar.

    Karar ``selected`` degilse sorgu calistirilmaz; tek secili proje tam
    olarak bir ``query_registered_project`` cagrisi uretir, coklu secili
    proje yalnizca ``route-fanout`` kararinda her proje icin ayri bir cagri
    ve ayri etiketli sonucla acilir. Indeksi okunamayan proje
    ``unavailable`` olarak raporlanir; embedding veya indeksleme
    baslatilmaz. Kaynak erisimi salt okunurdur, ancak sorgu alt katmani
    gozlem kaydi yazabilir. Dondurulen govde ``grants_authority=False``
    tasir; fonksiyon kendi basina yetki,
    onay veya execution uretmez.
    """

    if (
        not isinstance(question, str)
        or not question.strip()
        or len(question.encode("utf-8")) > _MAX_QUESTION_BYTES
    ):
        raise ValidationFailed("Retrieval sorusu bounded non-empty text olmali")
    for ref in registered_project_refs:
        validate_slug(ref)
    try:
        resolved_home = Path(home).resolve(strict=True)
    except OSError as exc:
        raise ValidationFailed("ZEKAM_HOME gecersiz") from exc

    question_digest = digest({"question": question})

    def result(
        status: str,
        refs: tuple[str, ...],
        reasons: tuple[str, ...],
        results: tuple[dict[str, Any], ...],
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "schema": SCHEMA,
            "status": status,
            "question_digest": question_digest,
            "scope": scope.as_dict(),
            "project_refs": list(refs),
            "reason_codes": list(reasons),
            "results": list(results),
            "query_count": sum(1 for item in results if item.get("query_attempted")),
            "remote_query_authorized": bool(authorize_remote_query),
            "source_access": "read-only",
            "observation_write_possible": any(item.get("query_attempted") for item in results),
            "index_triggered": False,
            "grants_authority": False,
        }
        return body | {"retrieval_digest": digest(body)}

    if scope.status != "selected":
        reasons = scope.reason_codes or ("retrieval-scope-not-selected",)
        return result("clarification-required", scope.project_refs, reasons, ())

    refs = tuple(scope.project_refs)
    if not refs:
        return result("clarification-required", (), ("retrieval-scope-empty",), ())
    if any(ref not in registered_project_refs for ref in refs):
        return result("clarification-required", refs, ("retrieval-project-unregistered",), ())
    if len(refs) == 1 and scope.selected_by not in _SINGLE_SELECTED_BY:
        return result("clarification-required", refs, ("retrieval-selected-by-invalid",), ())
    if len(refs) > 1 and scope.selected_by != _FANOUT_SELECTED_BY:
        return result("clarification-required", refs, ("retrieval-fanout-not-authorized",), ())
    if len(refs) > 1 and not _COMPARISON.search(question):
        return result("clarification-required", refs, ("comparison-intent-required",), ())

    results: list[dict[str, Any]] = []
    unavailable_reasons: list[str] = []
    for ref in refs:
        entry: dict[str, Any] = {
            "project_ref": ref,
            "selected_by": scope.selected_by,
            "query_attempted": False,
        }
        try:
            probe: dict[str, Any] = project_rag_status(resolved_home, ref)
        except PolicyViolation:
            entry["status"] = "unavailable"
            entry["unavailable_reason"] = "knowledge-scope-blocked"
            entry["result"] = None
            unavailable_reasons.append("knowledge-scope-blocked")
        except ValidationFailed:
            entry["status"] = "unavailable"
            entry["unavailable_reason"] = "project-unavailable"
            entry["result"] = None
            unavailable_reasons.append("project-unavailable")
        else:
            if probe.get("state") == "blocked":
                entry["status"] = "unavailable"
                entry["unavailable_reason"] = "knowledge-scope-blocked"
                entry["result"] = None
                unavailable_reasons.append("knowledge-scope-blocked")
            elif not probe.get("index_readable"):
                entry["status"] = "unavailable"
                entry["unavailable_reason"] = "index-unavailable"
                entry["result"] = None
                unavailable_reasons.append("index-unavailable")
            else:
                project_id = probe.get("project_id")
                if not isinstance(project_id, str) or not project_id:
                    entry["status"] = "unavailable"
                    entry["unavailable_reason"] = "project-binding-unavailable"
                    entry["result"] = None
                    unavailable_reasons.append("project-binding-unavailable")
                    results.append(entry)
                    continue
                entry["project_id"] = project_id
                entry["query_attempted"] = True
                try:
                    answer = query_registered_project(
                        resolved_home,
                        ref,
                        question,
                        authorize_remote_query=authorize_remote_query,
                    )
                except (PolicyViolation, ValidationFailed):
                    entry["status"] = "unavailable"
                    entry["unavailable_reason"] = "query-unavailable"
                    entry["result"] = None
                    unavailable_reasons.append("query-unavailable")
                else:
                    entry["status"] = "unavailable"
                    entry["unavailable_reason"] = "project-binding-mismatch"
                    entry["result"] = None
                    mismatch_reason = "project-binding-mismatch"
                    if isinstance(answer, dict) and answer.get("project_id") == project_id:
                        # WP-03.03: query seam sonucu istenen projeye baglanmali.
                        # UUID zorunludur. Ek proje etiketi varsa scope ile
                        # eslesmeli; authority iddiasi ve hatali etiket reddedilir.
                        labels = ("project_ref", "project_slug", "project")
                        if (
                            "grants_authority" in answer and answer["grants_authority"] is not False
                        ) or any(key in answer and answer[key] != ref for key in labels):
                            mismatch_reason = "query-result-refused"
                        else:
                            entry["status"] = "answered"
                            entry["result"] = answer
                    entry["unavailable_reason"] = (
                        None if entry["status"] == "answered" else mismatch_reason
                    )
                    if mismatch_reason is not None and entry["status"] != "answered":
                        unavailable_reasons.append(mismatch_reason)
        results.append(entry)

    answered = sum(1 for item in results if item["status"] == "answered")
    status = "answered" if answered else "unavailable"
    reasons = tuple(dict.fromkeys((*scope.reason_codes, *unavailable_reasons)))
    return result(status, refs, reasons, tuple(results))
