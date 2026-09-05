"""Exact opt-in v2 close candidate contract and durable pipeline evidence."""

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
from tests.unit.test_local_continuity import _resolver
from tests.unit.test_local_continuity import continuity as continuity
from tests.unit.test_local_continuity_close import OWNER, _drain_runtime
from tests.unit.test_local_continuity_close import close as close

from zekam.application.local_continuity_close import (
    CANDIDATE_RECIPE_DIGEST,
    CloseCandidateBundle,
    CloseCandidateClaim,
    CloseSummary,
    FrozenClose,
    candidate_recipe_body,
)
from zekam.domain.canonical import canonical_json, digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.infrastructure.sqlite.local_continuity import SQLiteContinuityStore
from zekam.infrastructure.sqlite.local_continuity_close import SQLiteCloseStore
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore


def _claim(close: dict[str, Any], text: str) -> CloseCandidateClaim:
    return CloseCandidateClaim(text, close["summary"].sources, close["summary"].evidence)


def _sensitive_sample() -> str:
    return "api_" + "key" + "=" + '"' + "abc123456789" + '"'


def _bundle(close: dict[str, Any]) -> CloseCandidateBundle:
    return CloseCandidateBundle(
        memory=(_claim(close, "Remember the verified health endpoint evidence."),),
        decision=(_claim(close, "Keep health checks provider-free during startup."),),
        skill=(_claim(close, "Verify the bounded endpoint before reporting readiness."),),
        failure=(_claim(close, "Treat missing durable evidence as a failed close."),),
    )


def _freeze_v2(
    close: dict[str, Any], candidates: CloseCandidateBundle | None = None
) -> FrozenClose:
    return cast(
        FrozenClose,
        close["store"].freeze_v2(
            close["binding"],
            close["summary"],
            _bundle(close) if candidates is None else candidates,
            checkpoint_digest=close["checkpoint"],
            manifest_digest=close["manifest"],
            expected_tail=close["base"].tail(close["binding"]),
        ),
    )


def _artifact(payload: bytes) -> dict[str, Any]:
    text = payload.decode("utf-8")
    marker = "\n```json\n"
    start = text.index(marker) + len(marker)
    end = text.index("\n```\n", start)
    value = json.loads(text[start:end])
    assert isinstance(value, dict)
    return value


def test_reviewed_recipe_literal_digest_and_shape_are_frozen() -> None:
    recipe = candidate_recipe_body()
    assert len(canonical_json(recipe).encode("utf-8")) == 3235
    assert digest(recipe) == CANDIDATE_RECIPE_DIGEST
    assert CANDIDATE_RECIPE_DIGEST == (
        "sha256:bbaab5423540620e4764e2c379d9cdd5ae919aa464fe46b9e5a1b375fe2558b3"
    )
    assert recipe["category_order"] == [
        "memory",
        "decision",
        "skill",
        "failure",
    ]
    assert recipe["projection_order"] == [
        "daylog",
        "handoff",
        "memory",
        "decision",
        "skill",
        "failure",
    ]
    recipe["version"] = 999
    assert candidate_recipe_body()["version"] == 2


def test_v1_remains_exact_two_projection_legacy_contract(close: dict[str, Any]) -> None:
    request = close["store"].freeze(
        close["binding"],
        close["summary"],
        checkpoint_digest=close["checkpoint"],
        manifest_digest=close["manifest"],
        expected_tail=close["base"].tail(close["binding"]),
    )
    projections = request.projections(close["binding"])
    assert request.input_body["schema"] == "zekam-local-close/v1"
    assert tuple(item.manifest.note_kind for item in projections) == ("daylog", "handoff")
    assert all(b"generator_version: local-close/v1" in item.payload for item in projections)
    assert "candidate_bundle" not in request.input_body


def test_v2_happy_restart_rebuild_and_terminal_receipts_are_exact(
    close: dict[str, Any],
) -> None:
    candidates = _bundle(close)
    request = _freeze_v2(close, candidates)
    assert _freeze_v2(close, candidates) == request
    projections = request.projections(close["binding"])
    assert len(projections) == 6
    assert tuple(item.manifest.note_kind for item in projections) == (
        "daylog",
        "handoff",
        "note",
        "decision",
        "skill",
        "failure",
    )
    suffixes = (
        "memory-candidates",
        "decision-candidates",
        "skill-candidates",
        "failure-candidates",
    )
    for category, suffix, item in zip(
        ("memory", "decision", "skill", "failure"), suffixes, projections[2:], strict=True
    ):
        assert item.manifest.portable_ref.endswith(f"/{suffix}-{request.request_digest[7:]}.md")
        body = _artifact(item.payload)
        assert set(body) == set(candidate_recipe_body()["artifact_body_keys"])
        assert body["category"] == category
        assert (
            body["note_kind"]
            == {
                "memory": "note",
                "decision": "decision",
                "skill": "skill",
                "failure": "failure",
            }[category]
        )
        assert body["scope"] == {
            "binding_digest": close["binding"].binding_digest,
            "session_id": close["binding"].session_id,
            "external_session_id": close["binding"].external_session_id,
            "client_id": close["binding"].client_id,
            "device_id": close["binding"].device_id,
            "project_id": close["binding"].project_id,
            "realm_id": close["binding"].realm_id,
            "work_item_id": close["binding"].work_item_id,
            "run_id": close["binding"].run_id,
            "source_snapshot_id": close["binding"].source_snapshot_id,
            "task_digest": close["binding"].task_digest,
            "plan_digest": close["binding"].plan_digest,
            "policy_digest": close["binding"].policy_digest,
        }
        assert body["candidate_count"] == 1
        assert body["abstention"] is None
        assert body["provider_called"] is False
        assert body["model_summary"] is False
        assert body["semantic_inference"] is False
        assert body["candidate_state"] == "inbox"
        assert body["activation"] == "human-review-required"
        assert body["grants_authority"] is False
        assert body["approval_inherited"] is False
        assert body["executable"] is False
        assert (
            body["candidates"][0]["claim_digest"] == getattr(candidates, category)[0].claim_digest
        )
        assert set(body["candidates"][0]) == set(candidate_recipe_body()["candidate_body_keys"])

    close["service"].compile_once(close["binding"], request.request_digest, **OWNER)
    close["service"].deliver_once(close["binding"], request.request_digest, **OWNER)
    _drain_runtime(close)
    receipt = close["service"].finalize(close["binding"], request.request_digest)
    reopened = SQLiteCloseStore(
        SQLiteContinuityStore(close["base"].path, source_resolver=_resolver(close["binding"])),
        SQLiteLocalRuntimeStore(close["base"].path),
        close["files"],
        source_probe=close["probe"],
    )
    loaded = reopened.load(close["binding"], request.request_digest)
    assert loaded.state == "complete"
    assert loaded.projections(close["binding"]) == projections
    assert reopened.finalize(close["binding"], request.request_digest) == receipt
    with sqlite3.connect(close["base"].path) as db:
        assert (
            db.execute(
                "select count(*) from knowledge_note where state='inbox' and authorship='generated'"
            ).fetchone()[0]
            == 6
        )
        assert (
            db.execute("select count(*) from knowledge_note where state='active'").fetchone()[0]
            == 0
        )
        stored = json.loads(
            db.execute("select input_json from continuity_close_request").fetchone()[0]
        )
    assert CloseCandidateBundle.from_body(stored["candidate_bundle"]) == candidates


def test_empty_v2_is_truthful_and_does_not_infer_summary_fields(close: dict[str, Any]) -> None:
    request = _freeze_v2(close, CloseCandidateBundle())
    for item in request.projections(close["binding"])[2:]:
        body = _artifact(item.payload)
        assert body["candidate_count"] == 0
        assert body["candidates"] == []
        assert body["abstention"] == "no-explicit-candidates"
        assert close["summary"].performed[0] not in item.payload.decode("utf-8")
        assert close["summary"].next_safe_step not in item.payload.decode("utf-8")


@pytest.mark.parametrize(
    "text",
    [
        None,
        True,
        "",
        " leading",
        "trailing ",
        "line\nbreak",
        "x" * 2049,
        _sensitive_sample(),
    ],
)
def test_claim_text_wrong_type_blank_control_or_secret_rejects_before_write(
    close: dict[str, Any], text: Any
) -> None:
    with pytest.raises((ValidationFailed, PolicyViolation)):
        CloseCandidateClaim(text, close["summary"].sources, close["summary"].evidence)
    assert close["runtime"].status().ready_jobs == 0


def test_claim_lone_surrogate_direct_and_decoded_body_are_typed_rejections(
    close: dict[str, Any],
) -> None:
    invalid = chr(0xD800)
    with pytest.raises(ValidationFailed, match="UTF-8"):
        CloseCandidateClaim(invalid, close["summary"].sources, close["summary"].evidence)
    decoded = json.loads(r'"\ud800"')
    body = {
        "schema": "zekam-close-candidate-claim/v1",
        "text": decoded,
        "source_refs": close["summary"].sources,
        "evidence_refs": close["summary"].evidence,
    }
    with pytest.raises(ValidationFailed, match="UTF-8"):
        CloseCandidateClaim.from_body(body)
    assert close["runtime"].status().ready_jobs == 0


def test_claim_refs_wrong_order_duplicate_malformed_and_foreign_reject(
    close: dict[str, Any],
) -> None:
    source = close["summary"].sources[0]
    evidence = close["summary"].evidence[0]
    with pytest.raises(ValidationFailed, match="canonical"):
        CloseCandidateClaim("A bounded literal.", (source, ("a-ref", digest("a"))), (evidence,))
    with pytest.raises(ValidationFailed, match="canonical"):
        CloseCandidateClaim("A bounded literal.", (source, source), (evidence,))
    foreign = CloseCandidateClaim(
        "A bounded literal.", (("foreign/ref", digest("foreign")),), (evidence,)
    )
    with pytest.raises(PolicyViolation, match="provenance"):
        _freeze_v2(close, CloseCandidateBundle(memory=(foreign,)))
    assert close["runtime"].status().ready_jobs == 0


def test_claim_ref_and_canonical_document_caps_are_independent(close: dict[str, Any]) -> None:
    source = close["summary"].sources[0]
    evidence = close["summary"].evidence[0]
    with pytest.raises(ValidationFailed, match="one to four"):
        CloseCandidateClaim("Bounded literal.", (source,) * 5, (evidence,))
    long_sources = tuple(
        (f"s{index}-" + "x" * 500, digest(f"source-{index}")) for index in range(4)
    )
    long_evidence = tuple(
        (f"e{index}-" + "y" * 500, digest(f"evidence-{index}")) for index in range(4)
    )
    with pytest.raises(ValidationFailed, match="canonical byte bound"):
        CloseCandidateClaim("Bounded literal.", long_sources, long_evidence)


def test_bundle_rejects_noncanonical_order_global_duplicate_and_bounds(
    close: dict[str, Any],
) -> None:
    first = _claim(close, "First literal candidate.")
    second = _claim(close, "Second literal candidate.")
    ordered = tuple(sorted((first, second), key=lambda item: item.candidate_id("memory")))
    with pytest.raises(ValidationFailed, match="canonical order"):
        CloseCandidateBundle(memory=tuple(reversed(ordered)))
    with pytest.raises(ValidationFailed, match="duplicate claim"):
        CloseCandidateBundle(memory=(first,), decision=(first,))
    nine = tuple(
        sorted(
            (_claim(close, f"Bounded category candidate {index}.") for index in range(9)),
            key=lambda item: item.candidate_id("memory"),
        )
    )
    with pytest.raises(ValidationFailed, match="bounded tuple"):
        CloseCandidateBundle(memory=nine)
    one_more = (_claim(close, "Seventeenth bounded candidate."),)
    sixteen = tuple(
        sorted(
            (_claim(close, f"Total candidate {index}.") for index in range(8)),
            key=lambda item: item.candidate_id("memory"),
        )
    )
    other_eight = tuple(
        sorted(
            (_claim(close, f"Other total candidate {index}.") for index in range(8)),
            key=lambda item: item.candidate_id("decision"),
        )
    )
    with pytest.raises(ValidationFailed, match="count"):
        CloseCandidateBundle(memory=sixteen, decision=other_eight, skill=one_more)
    large_memory = tuple(
        sorted(
            (_claim(close, f"M{index}-" + "x" * 1000) for index in range(8)),
            key=lambda item: item.candidate_id("memory"),
        )
    )
    large_decision = tuple(
        sorted(
            (_claim(close, f"D{index}-" + "y" * 1000) for index in range(8)),
            key=lambda item: item.candidate_id("decision"),
        )
    )
    with pytest.raises(ValidationFailed, match="byte bound"):
        CloseCandidateBundle(memory=large_memory, decision=large_decision)


def test_v1_then_v2_and_changed_v2_replay_are_payload_drift(close: dict[str, Any]) -> None:
    legacy = close["store"].freeze(
        close["binding"],
        close["summary"],
        checkpoint_digest=close["checkpoint"],
        manifest_digest=close["manifest"],
        expected_tail=close["base"].tail(close["binding"]),
    )
    with pytest.raises(PolicyViolation, match="drift"):
        _freeze_v2(close)
    assert close["store"].load(close["binding"], legacy.request_digest) == legacy
    with sqlite3.connect(close["base"].path) as db:
        assert db.execute("select count(*) from continuity_close_request").fetchone()[0] == 1
        assert db.execute("select count(*) from local_job").fetchone()[0] == 1


def test_v1_input_cannot_be_relabelled_or_extended_as_v2(close: dict[str, Any]) -> None:
    legacy = close["store"].freeze(
        close["binding"],
        close["summary"],
        checkpoint_digest=close["checkpoint"],
        manifest_digest=close["manifest"],
        expected_tail=close["base"].tail(close["binding"]),
    )
    extended = {**legacy.input_body, "candidate_bundle": CloseCandidateBundle().body()}
    forged = replace(legacy, input_body=extended)
    with pytest.raises(PolicyViolation, match="payload digest"):
        forged.projections(close["binding"])
    relabelled = {
        **legacy.input_body,
        "schema": "zekam-local-close/v2",
        "projection_recipe": "local-close-candidates/v2",
        "candidate_recipe_digest": CANDIDATE_RECIPE_DIGEST,
        "candidate_bundle": CloseCandidateBundle().body(),
    }
    forged = replace(legacy, input_body=relabelled)
    with pytest.raises(PolicyViolation, match="payload digest"):
        forged.projections(close["binding"])


def test_changed_v2_replay_rejects_without_second_authority_record(
    close: dict[str, Any],
) -> None:
    original = _freeze_v2(close)
    changed = CloseCandidateBundle(memory=(_claim(close, "A different literal memory candidate."),))
    with pytest.raises(PolicyViolation, match="drift"):
        _freeze_v2(close, changed)
    assert close["store"].load(close["binding"], original.request_digest) == original
    with sqlite3.connect(close["base"].path) as db:
        assert db.execute("select count(*) from continuity_close_request").fetchone()[0] == 1
        assert db.execute("select count(*) from local_job").fetchone()[0] == 1


@pytest.mark.parametrize("value", [None, True, 1, [], {}, "bundle"])
def test_freeze_v2_requires_typed_bundle_before_transaction(
    close: dict[str, Any], value: Any
) -> None:
    with pytest.raises(ValidationFailed, match="candidate bundle"):
        close["store"].freeze_v2(
            close["binding"],
            close["summary"],
            value,
            checkpoint_digest=close["checkpoint"],
            manifest_digest=close["manifest"],
            expected_tail=close["base"].tail(close["binding"]),
        )
    assert close["runtime"].status().ready_jobs == 0


def test_freeze_v2_rejects_summary_and_bundle_subclasses_before_write(
    close: dict[str, Any],
) -> None:
    class SummarySubclass(CloseSummary):
        pass

    class BundleSubclass(CloseCandidateBundle):
        pass

    summary = SummarySubclass(**close["summary"].body())
    bundle = BundleSubclass()
    for candidate_summary, candidates in (
        (summary, CloseCandidateBundle()),
        (close["summary"], bundle),
    ):
        with pytest.raises(ValidationFailed, match="exact summary"):
            close["store"].freeze_v2(
                close["binding"],
                candidate_summary,
                candidates,
                checkpoint_digest=close["checkpoint"],
                manifest_digest=close["manifest"],
                expected_tail=close["base"].tail(close["binding"]),
            )
    with sqlite3.connect(close["base"].path) as db:
        assert db.execute("select count(*) from continuity_close_request").fetchone()[0] == 0
        assert db.execute("select count(*) from local_job").fetchone()[0] == 0


def test_bundle_rejects_claim_subclass_before_overridable_methods(close: dict[str, Any]) -> None:
    class ClaimSubclass(CloseCandidateClaim):
        def candidate_id(self, category: str) -> str:
            pytest.fail(f"Subclass method must not run for {category}")

    claim = ClaimSubclass(
        "Untrusted subclass candidate.", close["summary"].sources, close["summary"].evidence
    )
    with pytest.raises(ValidationFailed, match="typed claim"):
        CloseCandidateBundle(memory=(claim,))


def test_v2_low_level_schema_recipe_bundle_and_extra_field_tamper_rejects(
    close: dict[str, Any],
) -> None:
    request = _freeze_v2(close)
    for mutation in (
        {**request.input_body, "schema": "zekam-local-close/v3"},
        {**request.input_body, "candidate_recipe_digest": digest("forged")},
        {**request.input_body, "extra": False},
        {
            **request.input_body,
            "candidate_bundle": {
                **request.input_body["candidate_bundle"],
                "recipe_digest": digest("forged-bundle-recipe"),
            },
        },
    ):
        forged = replace(request, request_digest=digest(mutation), input_body=mutation)
        with pytest.raises((PolicyViolation, ValidationFailed)):
            forged.projections(close["binding"])


def test_persisted_v2_bundle_tamper_is_detected_before_projection_use(
    close: dict[str, Any],
) -> None:
    request = _freeze_v2(close)
    changed = dict(request.input_body)
    changed_bundle = dict(changed["candidate_bundle"])
    changed_bundle["memory"] = []
    changed["candidate_bundle"] = changed_bundle
    with sqlite3.connect(close["base"].path) as db:
        db.execute("drop trigger continuity_close_request_immutable_update")
        db.execute(
            "update continuity_close_request set input_json=? where request_digest=?",
            (canonical_json(changed), request.request_digest),
        )
    with pytest.raises(PolicyViolation, match="integrity drift"):
        close["store"].load(close["binding"], request.request_digest)
    with sqlite3.connect(close["base"].path) as db:
        assert db.execute("select count(*) from knowledge_note").fetchone()[0] == 0
        assert db.execute("select count(*) from close_receipt").fetchone()[0] == 0


def test_concurrent_v2_freeze_creates_one_request_job_and_outbox(close: dict[str, Any]) -> None:
    candidates = _bundle(close)
    with ThreadPoolExecutor(max_workers=2) as executor:
        requests = list(executor.map(lambda _: _freeze_v2(close, candidates), range(2)))
    assert requests[0] == requests[1]
    with sqlite3.connect(close["base"].path) as db:
        assert db.execute("select count(*) from continuity_close_request").fetchone()[0] == 1
        assert db.execute("select count(*) from local_job").fetchone()[0] == 1
        assert (
            db.execute(
                "select count(*) from local_outbox where event_kind='continuity.compile'"
            ).fetchone()[0]
            == 1
        )


def test_partial_six_projection_publish_requires_explicit_repair(
    close: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _freeze_v2(close)
    original = close["files"].create_note
    calls = 0

    def interrupted(manifest: Any, payload: bytes) -> Path:
        nonlocal calls
        calls += 1
        if calls == 4:
            raise OSError("interrupted v2 candidate publication")
        return cast(Path, original(manifest, payload))

    monkeypatch.setattr(close["files"], "create_note", interrupted)
    with pytest.raises(OSError, match="interrupted"):
        close["service"].compile_once(close["binding"], request.request_digest, **OWNER)
    assert close["store"].load(close["binding"], request.request_digest).state == (
        "recovery-required"
    )
    monkeypatch.setattr(close["files"], "create_note", original)
    close["service"].repair_generated_candidates(
        close["binding"], request.request_digest, repair_key="reviewed-v2-repair", **OWNER
    )
    close["service"].deliver_once(close["binding"], request.request_digest, **OWNER)
    _drain_runtime(close)
    close["service"].finalize(close["binding"], request.request_digest)
    with sqlite3.connect(close["base"].path) as db:
        assert db.execute("select count(*) from knowledge_note").fetchone()[0] == 6
