"""WP-03.05: retrieval'den ayristirilmis, citation'a bagli aciklama kapisi.

Bu saf katman, ``natural_request_retrieval`` sonucunu ve modelden gelen
guvensiz aciklama onerisini birlestirir; kapsam ve kurallar:

- Aciklama uretimi retrieval'dan ayri bir alan olarak doner; retrieval
  govdesi ve citation'lari degistirilmeyip ayni govdede korunur.
- Proje kimligi: retrieval entry'si slug (``project_ref``), kayitli durum
  sorgusundan gelen UUID (``entry.project_id``), alt RAG
  sonucu ise proje UUID'sini (``result.project_id``, citation
  ``project_scope``) tasiyor. Slug -> proje ID baglamasi retrieval
  katmaninin kayitli proje sorgusuna guvenilir, bu kapinin disindaki
  bir baglamadir; bu kapilar UUID'yi slug'a esit saymaz ve onu
  slug'a karsilastirarak dogrulamaz. Kabul kontrolu citation'i
  ``entry.project_id`` ve ``result.project_id`` eslesmesine, ardindan
  citation'in ilgili ``result.project_id``'ye baglanmasina dayanir
  (``project_scope == project_id``).
- Gecerli citation: ``project_scope`` ilgili sonucun
  ``project_id``'siyle birebir eslesmeli, ``source_ref`` bos
  olmamali, ``source_digest`` gecerli ``sha256:`` formatinda olmali
  ve bos olmayan bir ``locator`` icinde ``relative_path`` ya da
  ``object_name`` tasmalidir.
- Onerilen metin kabul edilmek icin: tum istenen projeler yanitlanmis
  VE her yanitlanmis proje bos olmayan, tamami gecerli citation listesi tasimalidir.
  Herhangi bir proje erisilemezse ya da yanitlanmis bir projede
  gecerli citation yoksa, karsilastirma dahil onerilen metin asla
  kabul edilmez.
- Onerilen metin tamamlanmis bir islem/mutation basarisi iddia
  ediyorsa (bu kapilar effect receipt kabul etmez) metin sabit
  Turkce belirsizlik yanitiyla degistirilir; onerilen metin yoksa
  ayni sabit yanit doner.
- Retrieval durumu ``clarification-required`` ise sabit Turkce
  aciklama yaniti; hic proje yanitlanamadi (araç kesintisi,
  erisilemezlik) ise sabit Turkce basarisizlik yaniti doner.
- ``effect_claimed`` ve ``grants_authority`` her zaman ``False``
  kalir. Retrieval sorgusu gozlem kaydi yazabildigi icin
  basarisizlik yaniti "degisiklik yapilmadi" iddiasini tasimaz.
- Bu fonksiyon saf ve salt okunurdur: girdiye yazmaz, dosya acmaz,
  provider cagirmaz, tool calistirmaz ve yetki uretmez.
"""

from __future__ import annotations

import re
from typing import Any, Final

from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import ValidationFailed
from zekam.domain.knowledge import Locator

SCHEMA: Final = "zekam-natural-request-answer/v1"
MAX_EXPLANATION_BYTES: Final = 64 * 1024

#: Retrieval govdesi icin tam eslesen schema (exact allowlist).
_RETRIEVAL_SCHEMAS: Final = frozenset({"zekam-natural-request-retrieval/v1"})

#: Retrieval ``clarification-required`` durumunda donen sabit Turkce yanit.
CLARIFICATION_ANSWER: Final = (
    "Hangi proje icin sordugunuzu netlestiremedim; lutfen projeyi acikca "
    "belirtin. Proje belirlenmeden teknik bir cevap uretmiyorum."
)

#: Gecerli kaynak/canlit yoksa donen sabit Turkce belirsizlik yaniti.
UNCERTAINTY_ANSWER: Final = (
    "Bu soruya, kaynaklara dayanan dogrulanabilir bir cevap veremedim; "
    "kesin teknik iddia uretmuyorum. Gerekli kaynaklar ve kapsam daha "
    "netlestiginde tekrar sorabilirsiniz."
)

#: Proje erisilemez ya da arac kesintisi oldugunda donen sabit Turkce yanit.
#: Retrieval sorgusu gozlem kaydi yazabileceginden bu metin "degisiklik
#: yapilmadi" gibi bir iddia tasimaz.
FAILURE_ANSWER: Final = (
    "Bu proje icin arama aracina ulasilamadi ya da arama calismedi; bu "
    "yuzden bir cevap uretemedim. Bu bir islem basarisi degildir; lutfen "
    "daha sonra tekrar deneyin."
)

#: Effect receipt kabul edilmedigi icin, onerilen metindeki tamamlanmis
#: islem/mutation basarisi bildirimi bu desenle fail-closed tespit edilir.
#: Desen konservative'dir: yakinlasan metin de belirsizlik yanitiyla
#: degistirilir (sahte basari bir kabul yanlisi, eksik onemsemek
#: degil).
_MUTATION_CLAIM_RE: Final = re.compile(
    r"(?:"
    r"tamamland|basaril|başarıl|calistirildi|çalıştırıldı|uyguland|"
    r"yapild|yapıldı|eklend|silind|degistiril|değiştiril|guncellend|"
    r"güncellend|kaydedil|kayit alind|kayıt alınd|olusturuldu|oluşturuldu|"
    r"yayinland|yayınland|gonderildi|gönderildi|deploy edild|publish edild|"
    r"commit edild|push edild|merge edild"
    r"|\bcomplet(?:ed|ion)\b|\bsuccess(?:es|ful|fully)?\b|\bdeployed\b|"
    r"\bpublished\b|\bapplied\b|\bexecuted\b|\bcommitted\b|\bpushed\b|"
    r"\bmerged\b|\bcreated\b|\bdeleted\b|\bremoved\b|\bdropped\b|"
    r"\bupdated\b|\binserted\b|\bmigrated\b|\brolled\s+out\b"
    r")",
    re.IGNORECASE,
)


def _validate_retrieval(retrieval: Any) -> tuple[str, list[dict[str, Any]]]:
    """Retrieval govdesinin sozlesmesini fail-closed dogrular."""
    if not isinstance(retrieval, dict):
        raise ValidationFailed("Retrieval sonucu dict olmali")
    if retrieval.get("schema") not in _RETRIEVAL_SCHEMAS:
        raise ValidationFailed("Retrieval schema desteklenmiyor")
    status = retrieval.get("status")
    if not isinstance(status, str):
        raise ValidationFailed("Retrieval status gecersiz")
    if not isinstance(retrieval.get("reason_codes"), list):
        raise ValidationFailed("Retrieval reason_codes list olmali")
    results = retrieval.get("results")
    if not isinstance(results, list):
        raise ValidationFailed("Retrieval results list olmali")
    for entry in results:
        if not isinstance(entry, dict):
            raise ValidationFailed("Retrieval sonucu entry'si dict olmali")
        ref = entry.get("project_ref")
        if not isinstance(ref, str) or not ref:
            raise ValidationFailed("Retrieval project_ref gecersiz")
        if not isinstance(entry.get("status"), str):
            raise ValidationFailed("Retrieval entry status gecersiz")
    if status == "answered" and not results:
        raise ValidationFailed("Answered retrieval bos results tasiyamaz")
    if status == "answered":
        refs = retrieval.get("project_refs")
        actual_refs = [entry["project_ref"] for entry in results]
        if (
            not isinstance(refs, list)
            or refs != actual_refs
            or len(set(actual_refs)) != len(actual_refs)
        ):
            raise ValidationFailed("Retrieval proje kapsami sonuc listesiyle eslesmiyor")
    return status, results


def _validate_entry(entry: dict[str, Any]) -> None:
    """Yanitlanmis entry'nin RAG result govdesini fail-closed dogrular.

    ``entry.project_id`` kayitli proje status readback'inden gelir; RAG
    sonucunun UUID'si ile birebir eslesmelidir.
    """
    if entry["status"] != "answered":
        return
    result = entry.get("result")
    if result is None or not isinstance(result, dict):
        raise ValidationFailed("Yanitlanmis entry'nin retrieval result'i dict olmali")
    project_id = result.get("project_id")
    if not isinstance(project_id, str) or not project_id:
        raise ValidationFailed("Retrieval result project_id gecersiz")
    if entry.get("project_id") != project_id:
        raise ValidationFailed("Retrieval project_id baglamasi eslesmiyor")
    citations = result.get("citations")
    if not isinstance(citations, list):
        raise ValidationFailed("Retrieval result citations list olmali")
    for citation in citations:
        if not isinstance(citation, dict):
            raise ValidationFailed("Retrieval citation dict olmali")


def _locator_is_nonempty(locator: Any) -> bool:
    """Locator'un ``relative_path`` ya da ``object_name`` tasiyip tasimadigina bakar."""
    if not isinstance(locator, dict) or not locator:
        return False
    object_name = locator.get("object_name")
    if object_name is not None and (not isinstance(object_name, str) or not object_name.strip()):
        return False
    try:
        Locator(**locator)
    except (TypeError, ValidationFailed):
        return False
    relative_path = locator.get("relative_path")
    object_name = locator.get("object_name")
    has_relative = isinstance(relative_path, str) and bool(relative_path.strip())
    has_object = isinstance(object_name, str) and bool(object_name.strip())
    return has_relative or has_object


def _citation_matches(citation: Any, *, project_id: str) -> bool:
    """Tek citation'in ilgili sonucun projesine bagli, locator'li ref olup olmadigina bakar.

    Karstirilan proje, citation ``project_scope`` degeriyle ilgili RAG
    sonucunun ``project_id`` (UUID) degeri olarak baglanir.
    """
    if not isinstance(citation, dict):
        return False
    if citation.get("project_scope") != project_id:
        return False
    source_ref = citation.get("source_ref")
    if not isinstance(source_ref, str) or not source_ref.strip():
        return False
    source_digest = citation.get("source_digest")
    if not isinstance(source_digest, str):
        return False
    try:
        parse_digest(source_digest)
    except ValidationFailed:
        return False
    locator = citation.get("locator")
    if not _locator_is_nonempty(locator):
        return False
    relative_path = locator.get("relative_path")
    return relative_path is None or relative_path == source_ref


def _has_valid_citation(entry: dict[str, Any]) -> bool:
    """Require a nonempty citation set without foreign or malformed references."""
    if entry["result"].get("state") not in {"answered", "lexical-only-degraded"}:
        return False
    if entry["result"].get("evidence_found") is not True:
        return False
    project_id: str = entry["result"]["project_id"]
    citations = entry["result"]["citations"]
    return bool(citations) and all(
        _citation_matches(citation, project_id=project_id) for citation in citations
    )


def _evaluate(
    results: list[dict[str, Any]],
    proposed_explanation: str | None,
) -> tuple[str, list[str], bool]:
    """Yanit metnini, reason'lar ve onerinin kabul durumunu belirler."""
    answered = [entry for entry in results if entry["status"] == "answered"]
    if len(answered) != len(results):
        return FAILURE_ANSWER, ["project-unavailable"], False
    if any(not _has_valid_citation(entry) for entry in answered):
        return UNCERTAINTY_ANSWER, ["citation-invalid"], False
    if proposed_explanation is None or not proposed_explanation.strip():
        return UNCERTAINTY_ANSWER, ["proposed-explanation-missing"], False
    if len(proposed_explanation.encode("utf-8")) > MAX_EXPLANATION_BYTES:
        return UNCERTAINTY_ANSWER, ["proposed-explanation-too-large"], False
    if _MUTATION_CLAIM_RE.search(proposed_explanation) is not None:
        return UNCERTAINTY_ANSWER, ["proposed-mutation-claim"], False
    return proposed_explanation, [], True


def _envelope(
    retrieval: dict[str, Any],
    answer: str,
    reason_codes: list[str],
    accepted: bool,
) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "retrieval": retrieval,
        "answer": answer,
        "proposed_explanation_accepted": accepted,
        "reason_codes": reason_codes,
        "effect_claimed": False,
        "grants_authority": False,
    }


def render_natural_request_answer(answer: dict[str, Any]) -> str:
    """Render accepted evidence with project labels and original source locators.

    Only the existing citation gate can admit the explanation. This renderer
    does not generate prose, infer missing line/page positions or call a model.
    """

    if answer.get("proposed_explanation_accepted") is not True:
        return answer["answer"]
    admitted = answer_natural_request(answer["retrieval"], answer["answer"])
    text = admitted["answer"]
    if not admitted["proposed_explanation_accepted"]:
        return text
    sources: list[str] = []
    seen: set[tuple[str, str, str, str]] = set()
    for entry in admitted["retrieval"]["results"]:
        for citation in entry["result"]["citations"]:
            locator = citation["locator"]
            identity = (
                entry["project_ref"],
                citation["source_ref"],
                citation["source_digest"],
                digest(locator),
            )
            if identity in seen:
                continue
            seen.add(identity)
            position: list[str] = []
            if locator.get("line_start") is not None:
                position.append(f"satir {locator['line_start']}-{locator['line_end']}")
            if locator.get("page") is not None:
                position.append(f"sayfa {locator['page']}")
            if locator.get("heading_path"):
                position.append(" > ".join(locator["heading_path"]))
            if locator.get("object_name"):
                position.append(locator["object_name"])
            if locator.get("block_index") is not None:
                position.append(f"blok {locator['block_index']}")
            suffix = f" ({'; '.join(position)})" if position else ""
            sources.append(f"- {entry['project_ref']}: {citation['source_ref']}{suffix}")
    return text + "\n\nKaynaklar:\n" + "\n".join(sources)


def answer_natural_request(
    retrieval: dict[str, Any],
    proposed_explanation: str | None,
) -> dict[str, Any]:
    """Retrieval sonucunu ve guvensiz aciklama onerisini gecerli yanita cevirir.

    Saf, salt okunur fonksiyondur: girdiye mutasyon yapmaz, dosya acmaz,
    provider cagirmaz, tool calistirmaz ve yetki uretmez.

    - ``retrieval`` ``retrieve_natural_request`` donus govdesidir (exact
      schema kontrolu yapilir). Entry ``project_ref``'u slug, alt RAG
      sonucu ``project_id`` UUID'dir; bu kapilar status readback'inden
      gelen ``entry.project_id`` ile sonuc UUID'sini ve citation
      ``project_scope`` degerini zincir halinde dogrular.
    - ``proposed_explanation`` modelden gelen guvensiz metindir; kabul
      edilmek icin tum istenen projeler yanitlanmis, her yanitlanmis
      proje bos olmayan ve tamami gecerli citation (eslesen proje, ``source_ref``,
      ``source_digest`` ve bos olmayan locator) tasmalidir. Aksi halde
      sabit Turkce yanit doner.
    - Onerilen metin tamamlanmis islem/mutation basarisi iddia ediyorsa
      (bu kapilar effect receipt kabul etmez) sabit Turkce belirsizlik
      yanitiyla degistirilir.
    - Retrieval ``clarification-required`` ise sabit Turkce aciklama
      yaniti; hic proje yanitlanamadi ise sabit Turkce basarisizlik
      yaniti doner.
    - ``effect_claimed`` ve ``grants_authority`` her zaman ``False``.

    Dondurulen govde ``schema``, ``retrieval`` (orijinal, degistirilmis
    degil), ``answer``, ``proposed_explanation_accepted``,
    ``reason_codes``, ``effect_claimed`` ve ``grants_authority``
    alanlarini icerir.
    """
    status, results = _validate_retrieval(retrieval)
    for entry in results:
        _validate_entry(entry)
    if proposed_explanation is not None and not isinstance(proposed_explanation, str):
        raise ValidationFailed("proposed_explanation str olmali")

    if status == "clarification-required":
        return _envelope(retrieval, CLARIFICATION_ANSWER, ["clarification-required"], False)
    if status == "unavailable":
        return _envelope(retrieval, FAILURE_ANSWER, ["no-answered-project"], False)
    if status != "answered":
        raise ValidationFailed(f"Retrieval status desteklenmiyor: {status}")

    answer, reason_codes, accepted = _evaluate(results, proposed_explanation)
    return _envelope(retrieval, answer, reason_codes, accepted)
