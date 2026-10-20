"""Metadata schema answer module for Zekam embedded RAG."""

import hashlib
import re
import unicodedata
from typing import Any, Dict, List, Optional, Tuple

from zekam.application.metadata_schema_facts import parse_metadata_facts
from zekam.domain.canonical import digest
from zekam.domain.retrieval import answer_semantics

_SCHEMA = "zekam-embedded-rag-result/v1"
_SUMMARY_SCHEMA = "zekam-metadata-schema-summary/v1"
_INTENT = "database-schema"
_CHANNELS = ["database-metadata"]
_STATE_ANSWERED = "answered"
_STATE_ABSTAIN = "abstained-low-evidence"
_REASON_INSUFFICIENT = "insufficient-metadata-evidence"
_MAX_CHUNKS = 4096
_MAX_BATCH = 256
_MAX_TEXT_BYTES = 16 * 1024 * 1024
_MAX_TABLE_CLAIMS = 6
_MAX_RELATION_CLAIMS = 8
_MAX_CITATIONS = 128
_MAX_CHARS = 12000
_MAX_TABLE_PROOFS = 20

_SAFE_IDENT = re.compile(r"^[A-Za-z][A-Za-z0-9_$#]{0,127}$")
_QUALIFIED = re.compile(r"^([A-Za-z][A-Za-z0-9_$#]{0,127})\.([A-Za-z][A-Za-z0-9_$#]{0,127})$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")

_CONTEXT_WORDS = {
    "database", "schema", "veritaban", "veri tabani", "table", "tablo", "pk", "fk",
    "primary key", "foreign key",
}
_INTENT_WORDS = {
    "structure", "yapi", "mimari", "architecture", "key", "anahtar",
    "iliski", "relation", "list", "temel", "core", "sema", "schema", "goster",
}


def _normalize_query(query: str) -> str:
    q = query.casefold().replace("\u0131", "i")
    q = unicodedata.normalize("NFKD", q)
    q = "".join(c for c in q if not unicodedata.combining(c))
    return " ".join(q.casefold().split())


def _is_safe_ident(name: str) -> bool:
    return bool(_SAFE_IDENT.fullmatch(name))


def _is_qualified(name: str) -> bool:
    return bool(_QUALIFIED.fullmatch(name))


def _is_sha256(value: str) -> bool:
    return bool(_SHA256.fullmatch(value))


def _digest_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _query_has_context(normalized: str) -> bool:
    tokens = re.findall(r"[a-z0-9_]+", normalized)
    token_set = set(tokens)
    has_context = False
    for word in _CONTEXT_WORDS:
        if " " in word:
            if word in normalized:
                has_context = True
                break
        elif word in token_set or (word in {"veritaban", "tablo", "table", "schema"} and any(t.startswith(word) for t in tokens)):
            has_context = True
            break
    if not has_context:
        return False
    return True


def _query_has_intent(normalized: str) -> bool:
    tokens = re.findall(r"[a-z0-9_]+", normalized)
    token_set = set(tokens)
    for word in _INTENT_WORDS:
        if " " in word:
            if word in normalized:
                return True
        elif any(t.startswith(word) for t in tokens):
            return True
    return False


def is_database_schema_query(query: str) -> bool:
    normalized = _normalize_query(query)
    tokens = set(re.findall(r"[a-z0-9_]+", normalized))
    return _query_has_context(normalized) and (
        _query_has_intent(normalized) or bool(tokens & {"pk", "fk"})
    )


def _extract_identifier_tokens(query: str) -> List[str]:
    tokens = re.findall(r"`([^`]+)`|([A-Za-z][A-Za-z0-9_$#]{0,127}(?:\.[A-Za-z][A-Za-z0-9_$#]{0,127})?)", query)
    result = []
    for backtick, plain in tokens:
        if backtick:
            result.append(backtick)
        elif plain:
            result.append(plain)
    return result


def _validate_locator(locator: Any, source_ref: str, source_revision: str,
                      source_digest: str, content_digest: str,
                      text: str, table: str) -> Optional[str]:
    if not isinstance(locator, dict):
        return "locator-not-dict"
    object_name = locator.get("object_name")
    if not isinstance(object_name, str) or not object_name.endswith(":TABLE"):
        return "locator-object-name-invalid"
    qualified = object_name[:-6]
    if not _QUALIFIED.fullmatch(qualified):
        return "locator-object-name-invalid"
    owner, name = qualified.split(".", 1)
    if not _is_safe_ident(owner) or not _is_safe_ident(name):
        return "locator-ident-invalid"
    if table and table != qualified:
        return "locator-table-mismatch"
    if source_ref != "oracle/table/" + name:
        return "source-ref-mismatch"
    if not _is_sha256(source_digest):
        return "source-digest-invalid"
    if not _is_sha256(content_digest):
        return "content-digest-invalid"
    expected = _digest_bytes(text.encode("utf-8"))
    if content_digest != expected:
        return "content-digest-mismatch"
    return None


def _build_records(views: Dict[str, Any], identities: Dict[str, Any],
                   generation: Any, project_id: str) -> Tuple[List[Dict], Optional[str]]:
    records = []
    total_bytes = 0
    table_digests = {}
    for chunk_id, view in views.items():
        text = view.text
        if not isinstance(text, str) or not text.strip():
            return [], "invalid-view"
        total_bytes += len(text.encode("utf-8"))
        if total_bytes > _MAX_TEXT_BYTES:
            return [], "text-exceeds-limit"
        identity = identities.get(chunk_id)
        if identity is None:
            return [], "missing-identity"
        source_id = identity.get("source_id")
        source_ref = identity.get("source_ref")
        source_digest = identity.get("source_digest")
        content_digest = identity.get("content_digest")
        source_revision = identity.get("source_revision")
        if not all(isinstance(v, str) and v for v in (source_id, source_ref, source_digest, content_digest, source_revision)):
            return [], "identity-incomplete"
        if source_revision != generation.source_revision:
            return [], "revision-mismatch"
        if not _is_sha256(source_id) or not _is_sha256(source_digest) or not _is_sha256(content_digest):
            return [], "digest-invalid"
        expected_content = _digest_bytes(text.encode("utf-8"))
        if content_digest != expected_content:
            return [], "content-digest-mismatch"
        locator = view.locator.as_dict()
        err = _validate_locator(locator, source_ref, generation.source_revision,
                                source_digest, content_digest, text, None)
        if err:
            return [], err
        table = locator["object_name"][:-6]
        if not table or not _is_qualified(table):
            return [], "table-not-qualified"
        if table in table_digests and table_digests[table] != source_digest:
            return [], "table-source-digest-conflict"
        table_digests[table] = source_digest
        records.append({
            "chunk_id": chunk_id,
            "text": text,
            "table": table,
            "proof": {
                "chunk_id": chunk_id,
                "source_ref": source_ref,
                "source_digest": source_digest,
                "content_digest": content_digest,
                "locator": locator,
            },
        })
    return records, None


def _select_tables(facts: Dict, query: str) -> Tuple[List[str], Optional[str]]:
    tables = facts.get("tables", {})
    if not tables:
        return [], "no-tables"
    tokens = _extract_identifier_tokens(query)
    known = set()
    for t in tables:
        known.add(t.casefold())
        parts = t.split(".", 1)
        if len(parts) == 2:
            known.add(parts[1].casefold())
    selected = []
    explicit = re.findall(r"`([^`]+)`|(?<![A-Za-z0-9_$#])([A-Za-z][A-Za-z0-9_$#]*\.[A-Za-z][A-Za-z0-9_$#]*)(?![A-Za-z0-9_$#])", query)
    if any((quoted or qualified).casefold() not in known for quoted, qualified in explicit):
        return [], "explicit-table-not-found"
    for tok in tokens:
        tok_cf = tok.casefold()
        if tok_cf in known:
            for t in tables:
                if t.casefold() == tok_cf or t.split(".", 1)[-1].casefold() == tok_cf:
                    if t not in selected:
                        selected.append(t)
    if selected:
        if len(selected) > _MAX_TABLE_CLAIMS:
            return [], "too-many-selected-tables"
        return selected, None
    # fallback: top by FK degree
    relations = facts.get("relations", [])
    degree: Dict[str, int] = dict.fromkeys(tables, 0)
    for rel in relations:
        src = rel.get("source_table")
        tgt = rel.get("target_table")
        if src in tables:
            degree[src] = degree.get(src, 0) + 1
        if tgt in tables and tgt != src:
            degree[tgt] = degree.get(tgt, 0) + 1
    ranked = sorted(degree.items(), key=lambda x: (-x[1], x[0]))
    selected = [t for t, _ in ranked[:_MAX_TABLE_CLAIMS]]
    if not selected:
        selected = sorted(tables.keys())[:_MAX_TABLE_CLAIMS]
    return selected, None


def _build_table_claims(facts: Dict, selected: List[str]) -> List[Dict]:
    tables = facts.get("tables", {})
    relations = facts.get("relations", [])
    claims = []
    for table in selected:
        info = tables.get(table)
        if not info:
            continue
        pk = info.get("primary_key")
        pk_columns = []
        if pk and isinstance(pk, dict):
            pk_columns = pk.get("columns", [])
        fk_count = len(info.get("foreign_keys", []))
        proofs = list({p["chunk_id"]: p for p in info["proofs"]}.values())
        structural_degree = 0
        for rel in relations:
            if rel.get("source_table") == table:
                structural_degree += 1
            elif rel.get("target_table") == table:
                structural_degree += 1
        claims.append({
            "table": table,
            "pk_columns": pk_columns,
            "declared_fk_count": fk_count,
            "structural_degree": structural_degree,
            "proofs": proofs,
        })
    return claims


def _build_relationship_claims(facts: Dict, selected: List[str]) -> List[Dict]:
    relations = facts.get("relations", [])
    queues = {t: [] for t in selected}
    for rel in relations:
        for t in (rel.get("source_table"), rel.get("target_table")):
            if t in queues:
                queues[t].append(rel)
    for t in queues:
        queues[t].sort(key=lambda r: (0 if r.get("source_table") == t else 1, r.get("constraint_name") or "", r.get("source_table") or "", r.get("target_table") or ""))
    chosen, seen = [], set()
    while len(chosen) < _MAX_RELATION_CLAIMS:
        progressed = False
        for t in selected:
            if len(chosen) >= _MAX_RELATION_CLAIMS:
                break
            while queues[t]:
                rel = queues[t].pop(0)
                key = (rel.get("source_table"), rel.get("constraint_name"))
                if key not in seen:
                    seen.add(key)
                    chosen.append(rel)
                    progressed = True
                    break
            if len(chosen) >= _MAX_RELATION_CLAIMS:
                break
        if not progressed:
            break
    return chosen


def _collect_citations(table_claims: List[Dict], relationship_claims: List[Dict],
                       identities: Dict[str, Any], project_id: str) -> List[Dict]:
    seen = set()
    citations = []
    all_proofs = []
    for claim in table_claims:
        all_proofs.extend(claim.get("proofs", []))
    for rel in relationship_claims:
        if rel.get("source_proof"):
            all_proofs.append(rel["source_proof"])
        if rel.get("target_proof"):
            all_proofs.append(rel["target_proof"])
    for proof in all_proofs:
        if not isinstance(proof, dict):
            continue
        chunk_id = proof.get("chunk_id")
        if not chunk_id or chunk_id in seen:
            continue
        identity = identities.get(chunk_id)
        if not identity:
            continue
        seen.add(chunk_id)
        citations.append({
            "source_id": identity.get("source_id"),
            "project_scope": project_id,
            "source_ref": proof.get("source_ref"),
            "source_digest": proof.get("source_digest"),
            "content_digest": proof.get("content_digest"),
            "source_revision": identity.get("source_revision"),
            "chunk_id": chunk_id,
            "locator_type": "database-object",
            "locator": proof.get("locator"),
        })
    return citations


def _render(summary: Dict, citations: List[Dict]) -> str:
    lines = []
    lines.append("Son indekslenen offline metadata snapshotina gore.")
    lines.append("Temel tablo secimi giren+cikan FK baglanti sayisina dayanir; is kritikligi hakkinda cikarim yapilmaz.")
    lines.append("")

    # Build citation number map: chunk_id -> index+1
    cid_map = {}
    for idx, cit in enumerate(citations):
        cid = cit.get("chunk_id")
        if cid:
            cid_map[cid] = idx + 1

    def _fmt_proof(proof: Dict) -> str:
        if not proof:
            return ""
        cid = proof.get("chunk_id")
        if cid and cid in cid_map:
            return f"[{cid_map[cid]}]"
        return ""

    lines.append("Tablolar:")
    table_claims = summary.get("table_claims", [])
    if not table_claims:
        lines.append("  (secilen kanitlarda tablo tanimi yok)")
    for i, claim in enumerate(table_claims):
        table = claim.get("table", "")
        pk_cols = claim.get("pk_columns", [])
        pk_str = ", ".join(pk_cols) if pk_cols else "metadata icinde PK tanimi yok"
        declared_fk = claim.get("declared_fk_count", 0)
        degree = claim.get("structural_degree", 0)

        # Collect all proof chunk_ids for this table
        proof_cids = []
        for p in claim.get("proofs", []):
            cid = p.get("chunk_id")
            if cid and cid not in proof_cids:
                proof_cids.append(cid)
        proof_refs = " ".join(f"[{cid_map[c]}]" for c in proof_cids if c in cid_map)

        lines.append(f"  {i+1}. {table} PK=[{pk_str}] cikan_beyanli_fk={declared_fk} derece={degree} kanit={proof_refs}")

    lines.append("")
    lines.append("Iliskiler:")
    rel_claims = summary.get("relationship_claims", [])
    if not rel_claims:
        lines.append("  (secilen kanitlarda kanitlanmis FK iliskisi yok; bu, veritabaninda iliski olmadigi anlamina gelmez)")
    for i, rel in enumerate(rel_claims):
        src_table = rel.get("source_table", "")
        src_cols = ", ".join(rel.get("source_columns", []))
        tgt_table = rel.get("target_table", "")
        tgt_cols = ", ".join(rel.get("target_columns", []))
        constraint_name = rel.get("constraint_name", "")
        status = rel.get("status", "")
        validated = rel.get("validated", False)
        target_key = rel.get("target_key", "")

        src_proof = _fmt_proof(rel.get("source_proof", {}))
        tgt_proof = _fmt_proof(rel.get("target_proof", {}))

        # Deduplicate if same proof
        if src_proof and tgt_proof and src_proof == tgt_proof:
            proof_str = f"kanit={src_proof}"
        else:
            proof_str = f"kaynak_kanit={src_proof} hedef_kanit={tgt_proof}"

        lines.append(f"  {i+1}. {src_table}({src_cols}) -> {tgt_table}({tgt_cols}) "
                     f"kisit={constraint_name} hedef_anahtar={target_key} durum={status} dogrulanmis={validated} {proof_str}")

    lines.append("")
    lines.append("Kaynaklar:")
    for i, cit in enumerate(citations):
        locator = cit.get("locator", {})
        obj_name = locator.get("object_name", "")
        chunk_id = cit.get("chunk_id", "")
        source_ref = cit.get("source_ref", "")
        lines.append(f"  [{i+1}] Oracle lokator={obj_name} chunk_id={chunk_id} kaynak={source_ref}")

    cov = summary.get("coverage", {})
    lines.append("")
    lines.append(f"Kapsam: toplam_tablo={cov.get('total_tables',0)} secilen_tablo={cov.get('selected_tables',0)} "
                 f"cozulmus_fk={cov.get('resolved_fk_count',0)} cozulmemis_fk={cov.get('unresolved_fk_count',0)} "
                 f"kismi_ornek={cov.get('partial', False)}")

    lines.append("")
    lines.append("Not: Bu cikti canli veritabanindan degil, indekslenmis offline metadata snapshotindan uretilmistir. "
                 "Bu tanimlar canli DB kisit etkinligi, veri kardinalitesi veya is rolleri icin kanit degildir.")

    return "\n".join(lines)


def _build_summary(project_id: str, generation: Any, table_claims: List[Dict],
                   relationship_claims: List[Dict], coverage: Dict, citations: List[Dict]) -> Dict:
    text = _render({"table_claims": table_claims, "relationship_claims": relationship_claims,
                    "coverage": coverage}, citations)
    summary = {
        "schema": _SUMMARY_SCHEMA,
        "status": "supported",
        "text": text,
        "table_claims": table_claims,
        "relationship_claims": relationship_claims,
        "coverage": coverage,
        "project_id": project_id,
        "generation_digest": generation.generation_digest,
        "source_revision": generation.source_revision,
    }
    summary["summary_digest"] = digest(summary)
    return summary


def build_metadata_schema_result(index: Any, query: str, *, project_id: str, generation: Any) -> Dict:
    gd = generation.generation_digest
    candidates = index.metadata_schema_candidates(project_id, generation_digest=gd)
    chunk_ids = candidates.get("chunk_ids", ())
    truncated = candidates.get("truncated", False)
    total_chunks = candidates.get("total_chunks", 0)
    if truncated or len(chunk_ids) > _MAX_CHUNKS or total_chunks != len(chunk_ids) or len(set(chunk_ids)) != len(chunk_ids):
        return _abstain_result(project_id, query, generation, "candidate-validation-failed")
    batch_size = _MAX_BATCH
    views = {}
    identities = {}
    for i in range(0, len(chunk_ids), batch_size):
        batch = tuple(chunk_ids[i:i + batch_size])
        views.update(index.views(project_id, batch, generation_digest=gd))
        identities.update(index.source_identities(project_id, batch, generation_digest=gd))
    if set(views.keys()) != set(identities.keys()) or set(views.keys()) != set(chunk_ids):
        return _abstain_result(project_id, query, generation, "missing-identity")
    records, err = _build_records(views, identities, generation, project_id)
    if err:
        return _abstain_result(project_id, query, generation, err)
    try:
        facts = parse_metadata_facts(records)
    except ValueError:
        return _abstain_result(project_id, query, generation, "parse-failed")
    selected, err = _select_tables(facts, query)
    if err:
        return _abstain_result(project_id, query, generation, err)
    table_claims = _build_table_claims(facts, selected)
    relationship_claims = _build_relationship_claims(facts, selected)
    citations = _collect_citations(table_claims, relationship_claims, identities, project_id)
    if len(citations) > _MAX_CITATIONS:
        return _abstain_result(project_id, query, generation, "citation-limit-exceeded")
    coverage = {
        "total_tables": len(facts.get("tables", {})),
        "selected_tables": len(selected),
        "resolved_fk_count": len(facts.get("relations", [])),
        "unresolved_fk_count": facts.get("unresolved_fk_count", 0),
        "partial": len(selected) < len(facts.get("tables", {})) or len(relationship_claims) < len(facts.get("relations", [])) or facts.get("unresolved_fk_count", 0) > 0,
    }
    summary = _build_summary(project_id, generation, table_claims, relationship_claims, coverage, citations)
    if len(summary["text"]) > _MAX_CHARS:
        return _abstain_result(project_id, query, generation, "text-limit-exceeded")
    used_ids = [c["chunk_id"] for c in citations]
    result = {
        "schema": _SCHEMA,
        "state": _STATE_ANSWERED,
        "project_id": project_id,
        "query_digest": digest({"query": query}),
        "generation_digest": gd,
        "source_revision": generation.source_revision,
        "tree_digest": generation.tree_digest,
        "embedding_profile_digest": generation.embedding_profile_digest,
        "provider_profile_digest": generation.provider_profile_digest,
        "query_intent": _INTENT,
        "searched_channels": _CHANNELS,
        "citations": citations,
        "used_chunk_ids": used_ids,
        "evidence_found": bool(citations),
        "evidence_sufficient": bool(citations),
        "index_freshness": "current",
        "snapshot_only": False,
        "stale_reasons": [],
        "fallback_allowed": False,
        "answer_excerpt": None,
        "answer_excerpts": [],
        "answer_excerpt_meta": None,
        "architecture_summary": None,
        "metadata_schema_summary": summary,
        "coverage_notes": [],
        "reference_expansion": [],
        "relationship_state": None,
        "explanation": [],
        "tokens_used": 0,
    }
    result.update(answer_semantics(_STATE_ANSWERED, evidence_found=bool(citations)))
    result["retrieval_digest"] = digest(result)
    return result


def _abstain_result(project_id: str, query: str, generation: Any, reason: str) -> Dict:
    gd = generation.generation_digest
    summary = {
        "schema": _SUMMARY_SCHEMA,
        "status": "insufficient",
        "text": "Yetersiz kanit.",
        "table_claims": [],
        "relationship_claims": [],
        "coverage": {},
        "project_id": project_id,
        "generation_digest": gd,
        "source_revision": generation.source_revision,
        "reason_code": reason,
    }
    summary["summary_digest"] = digest(summary)
    result = {
        "schema": _SCHEMA,
        "state": _STATE_ABSTAIN,
        "project_id": project_id,
        "query_digest": digest({"query": query}),
        "generation_digest": gd,
        "source_revision": generation.source_revision,
        "tree_digest": generation.tree_digest,
        "embedding_profile_digest": generation.embedding_profile_digest,
        "provider_profile_digest": generation.provider_profile_digest,
        "query_intent": _INTENT,
        "searched_channels": _CHANNELS,
        "citations": [],
        "used_chunk_ids": [],
        "evidence_found": False,
        "evidence_sufficient": False,
        "index_freshness": "current",
        "snapshot_only": False,
        "stale_reasons": [],
        "fallback_allowed": False,
        "answer_excerpt": None,
        "answer_excerpts": [],
        "answer_excerpt_meta": None,
        "architecture_summary": None,
        "metadata_schema_summary": summary,
        "coverage_notes": [],
        "reference_expansion": [],
        "relationship_state": None,
        "explanation": [],
        "tokens_used": 0,
    }
    result.update(answer_semantics(_STATE_ABSTAIN, evidence_found=False))
    result["retrieval_digest"] = digest(result)
    return result


_V_SAFE_ID = r'[A-Za-z][A-Za-z0-9_$#]{0,127}'
_V_QUALIFIED = r'(?:' + _V_SAFE_ID + r')\.(?:' + _V_SAFE_ID + r')'
_V_OBJECT_NAME = re.compile(r'(' + _V_QUALIFIED + r'):TABLE\Z')
_CITATION_KEYS = {"chunk_id", "source_id", "source_ref", "source_digest", "content_digest", "locator_type", "locator", "project_scope", "source_revision"}
_PROOF_KEYS = {"chunk_id", "source_ref", "source_digest", "content_digest", "locator"}
_TABLE_CLAIM_KEYS = {"table", "pk_columns", "declared_fk_count", "structural_degree", "proofs"}
_REL_CLAIM_KEYS = {"source_table", "source_columns", "target_table", "target_columns", "constraint_name", "status", "validated", "source_proof", "target_proof", "target_key"}
_COVERAGE_KEYS = {"total_tables", "selected_tables", "resolved_fk_count", "unresolved_fk_count", "partial"}
_STATUSES = {"ENABLED", "DISABLED", "UNKNOWN"}
_VALIDATED = {"VALIDATED", "NOT VALIDATED", "NOVALIDATE", "UNKNOWN"}


def _is_sha256_str(s):
    return isinstance(s, str) and re.fullmatch(r'sha256:[0-9a-f]{64}', s) is not None


def _is_safe_ident_v(s):
    return isinstance(s, str) and re.fullmatch(_V_SAFE_ID, s) is not None


def _is_qualified_v(s):
    return isinstance(s, str) and re.fullmatch(_V_QUALIFIED, s) is not None


def _is_exact_count(v):
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def _object_name_table(locator):
    if not isinstance(locator, dict):
        return None
    obj = locator.get("object_name")
    if not isinstance(obj, str):
        return None
    m = _V_OBJECT_NAME.fullmatch(obj)
    return m.group(1) if m else None


def _check_citation(cit):
    if not isinstance(cit, dict) or set(cit.keys()) != _CITATION_KEYS:
        return None
    cid = cit["chunk_id"]
    if not isinstance(cid, str) or not cid or len(cid) > 512:
        return None
    for key in ("source_id", "source_digest", "content_digest"):
        if not _is_sha256_str(cit[key]):
            return None
    if cit["locator_type"] != "database-object":
        return None
    table = _object_name_table(cit["locator"])
    if table is None:
        return None
    if cit["source_ref"] != "oracle/table/" + table.split(".", 1)[1]:
        return None
    return (cid, table)


def _check_proof(proof, citations_by_id, expect_table):
    if not isinstance(proof, dict) or set(proof.keys()) != _PROOF_KEYS:
        return False
    cid = proof["chunk_id"]
    if not isinstance(cid, str) or cid not in citations_by_id:
        return False
    cit = citations_by_id[cid]
    for key in ("source_ref", "source_digest", "content_digest", "locator"):
        if proof[key] != cit[key]:
            return False
    return _object_name_table(proof["locator"]) == expect_table


def _check_col_list(cols):
    if not isinstance(cols, list) or not cols:
        return False
    if any(not _is_safe_ident_v(c) for c in cols):
        return False
    return len(set(cols)) == len(cols)


def metadata_schema_explanation(result: Dict) -> Optional[str]:
    try:
        if not isinstance(result, dict) or result.get("schema") != _SCHEMA:
            return None
        if result.get("index_freshness") != "current" or result.get("snapshot_only") is not False:
            return None
        if result.get("state") != _STATE_ANSWERED:
            return None
        project_id = result.get("project_id")
        if not isinstance(project_id, str) or not project_id:
            return None
        summary = result.get("metadata_schema_summary")
        if not isinstance(summary, dict):
            return None
        if summary.get("schema") != _SUMMARY_SCHEMA or summary.get("status") != "supported":
            return None
        if summary.get("project_id") != project_id:
            return None
        if summary.get("generation_digest") != result.get("generation_digest"):
            return None
        for field in ("generation_digest", "tree_digest", "embedding_profile_digest", "provider_profile_digest"):
            if not _is_sha256_str(result.get(field)):
                return None
        revision = result.get("source_revision")
        if not isinstance(revision, str) or not revision or len(revision) > 2048:
            return None
        if summary.get("source_revision") != revision:
            return None
        citations = result.get("citations")
        if not isinstance(citations, list) or not citations or len(citations) > _MAX_CITATIONS:
            return None
        citations_by_id = {}
        citation_order = []
        for cit in citations:
            checked = _check_citation(cit)
            if checked is None:
                return None
            cid, table = checked
            if cit["project_scope"] != project_id or cit["source_revision"] != revision:
                return None
            if cid in citations_by_id:
                return None
            citations_by_id[cid] = cit
            citation_order.append(cid)
        if result.get("used_chunk_ids") != citation_order:
            return None
        used_ids = set()
        table_claims = summary.get("table_claims", [])
        if not isinstance(table_claims, list) or not table_claims or len(table_claims) > _MAX_TABLE_CLAIMS:
            return None
        claimed_tables = set()
        for claim in table_claims:
            if not isinstance(claim, dict) or set(claim.keys()) != _TABLE_CLAIM_KEYS:
                return None
            if not _is_qualified_v(claim["table"]) or claim["table"] in claimed_tables:
                return None
            claimed_tables.add(claim["table"])
            if not isinstance(claim["pk_columns"], list):
                return None
            if claim["pk_columns"] and not _check_col_list(claim["pk_columns"]):
                return None
            if not _is_exact_count(claim["declared_fk_count"]) or not _is_exact_count(claim["structural_degree"]):
                return None
            proofs = claim["proofs"]
            if not isinstance(proofs, list) or not proofs or len(proofs) > _MAX_CITATIONS:
                return None
            for proof in proofs:
                if not _check_proof(proof, citations_by_id, claim["table"]):
                    return None
                used_ids.add(proof["chunk_id"])
        rel_claims = summary.get("relationship_claims", [])
        if not isinstance(rel_claims, list) or len(rel_claims) > _MAX_RELATION_CLAIMS:
            return None
        for rel in rel_claims:
            if not isinstance(rel, dict) or set(rel.keys()) != _REL_CLAIM_KEYS:
                return None
            if not _is_qualified_v(rel["source_table"]) or not _is_qualified_v(rel["target_table"]):
                return None
            if rel["source_table"] not in claimed_tables and rel["target_table"] not in claimed_tables:
                return None
            if not _check_col_list(rel["source_columns"]) or not _check_col_list(rel["target_columns"]):
                return None
            if len(rel["source_columns"]) != len(rel["target_columns"]):
                return None
            if not _is_safe_ident_v(rel["constraint_name"]) or not _is_safe_ident_v(rel["target_key"]):
                return None
            if rel["status"] not in _STATUSES or rel["validated"] not in _VALIDATED:
                return None
            if not isinstance(rel["source_proof"], dict) or not isinstance(rel["target_proof"], dict):
                return None
            if not _check_proof(rel["source_proof"], citations_by_id, rel["source_table"]):
                return None
            if not _check_proof(rel["target_proof"], citations_by_id, rel["target_table"]):
                return None
            used_ids.add(rel["source_proof"]["chunk_id"])
            used_ids.add(rel["target_proof"]["chunk_id"])
        if used_ids != set(citations_by_id.keys()):
            return None
        coverage = summary.get("coverage")
        if not isinstance(coverage, dict) or set(coverage.keys()) != _COVERAGE_KEYS:
            return None
        for key in ("total_tables", "selected_tables", "resolved_fk_count", "unresolved_fk_count"):
            if not _is_exact_count(coverage[key]):
                return None
        if not isinstance(coverage["partial"], bool):
            return None
        if coverage["selected_tables"] != len(table_claims) or coverage["selected_tables"] > coverage["total_tables"]:
            return None
        if coverage["resolved_fk_count"] < len(rel_claims):
            return None
        if (coverage["selected_tables"] < coverage["total_tables"] or len(rel_claims) < coverage["resolved_fk_count"] or coverage["unresolved_fk_count"] > 0) and not coverage["partial"]:
            return None
        expected_text = _render(summary, citations)
        if not isinstance(expected_text, str) or len(expected_text) > 12000:
            return None
        if summary.get("text") != expected_text:
            return None
        summary_check = dict(summary)
        sd = summary_check.pop("summary_digest", None)
        if sd != digest(summary_check):
            return None
        return expected_text
    except (TypeError, KeyError, ValueError):
        return None
