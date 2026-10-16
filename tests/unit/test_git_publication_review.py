"""Review preparation protocol fixtures; not live model or publication acceptance."""

from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from tests.unit.test_canonical_job_runtime import world as world
from tests.unit.test_unit_test_measurement import request
from zekam.application import git_publication_review as subject
from zekam.application.git_publication import GitPublicationPlan, GitPublicationTarget
from zekam.application.unit_test_provider_profile import BatchProvider, BatchProviderProfile
from zekam.domain.canonical import canonical_bytes, digest, digest_of_bytes
from zekam.domain.errors import PolicyViolation
from zekam.infrastructure.clients.opencode_batch import opencode_batch_adapter
from zekam.infrastructure.storage.local_cas import LocalContentAddressedStore
from zekam.infrastructure.unit_test_runner.tree_evidence import measured_tree


@pytest.fixture
def review_world(world, tmp_path, monkeypatch):
    """Real source bytes/SQLite/CAS, explicitly synthetic upstream receipt readers."""
    root = tmp_path / "source"
    production = "src/main/A.java"
    test = "src/test/ATest.java"
    contents = {production: b"class A {}\n", test: b"class ATest {}\n"}
    for path, raw in contents.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    files = {test: digest_of_bytes(contents[test])}
    objects = LocalContentAddressedStore(world.home / "artifacts/sha256")

    def put(body):
        return objects.put(canonical_bytes(body), media_type="application/json").digest

    tree = measured_tree(files, files, ("",))
    measured = {
        "production_digests": [[production, digest_of_bytes(contents[production])]],
        "build_config_digest": digest("pom"),
        "plan_digest": digest("maven-plan"),
    }
    old_record = put({"binding_digest": put(measured)})
    terminal = put({"state": {"accepted_files": sorted(files.items()), "plan": {"scenarios": []}}})
    plan = GitPublicationPlan(
        uuid4(),
        uuid4(),
        world.binding,
        GitPublicationTarget(
            digest("root"), "origin", digest("remote"), "refs/heads/main", "a" * 40
        ),
        digest("pending-test"),
        digest("pending-verifier"),
    )
    profile = BatchProviderProfile(
        (
            BatchProvider(
                "fixture",
                "http://127.0.0.1:9999/v1",
                "loopback",
                (
                    ("builder", "builder-upstream"),
                    ("reviewer", "reviewer-upstream"),
                    ("alias", "builder-upstream"),
                ),
            ),
        )
    )
    adapter = opencode_batch_adapter(
        "unused-opencode", objects=objects, model_id="fixture/reviewer", provider_profile=profile
    )
    history = {
        "project_id": world.binding.project_id,
        "work_item_id": world.binding.work_item_id,
        "source_snapshot_id": world.binding.source_snapshot_id,
        "measured_test_tree": {"evidence_digest": put(tree)},
        "measurement_record_digest": old_record,
        "terminal_evidence_digest": terminal,
        "builder_execution_digests": [digest("builder-execution")],
        "job_id": str(uuid4()),
        "execution_id": str(uuid4()),
        "source_revision": "b" * 40,
        "source_evidence_digest": digest("builder-receipt"),
    }
    native = {
        "job_id": str(uuid4()),
        "source_evidence_digest": digest("native-receipt"),
        "candidate_digest": digest("native-candidate"),
        "measurement_record_digest": digest("native-measurement"),
        "test_counts": {"passed": 1},
        "execution_id": str(uuid4()),
    }
    candidate = {
        "files": [list(pair) for pair in sorted(files.items())],
        "test_tree_digest": tree["before_digest"],
    }
    builder = {
        "native_execution_observed": True,
        "realm_id": str(plan.realm_id),
        "provider_profile_digest": profile.profile_digest,
        "configured_model": "fixture/builder",
        "configured_upstream": "builder-upstream",
    }
    current = {
        "source_job_id": native["job_id"],
        "test_tree_digest": tree["before_digest"],
        "subject_digest": plan.subject_digest,
        "source_evidence_digest": native["source_evidence_digest"],
    }
    reads = []

    def current_source(*_args, **_kwargs):
        reads.append(True)
        return current.copy()

    req = replace(request(), source_binding_id=world.source.id)
    monkeypatch.setattr(subject, "read_publication_test_source", current_source)
    monkeypatch.setattr(
        subject,
        "_read_native_measurement_source",
        lambda *_args: (native, req, {}, measured, candidate),
    )
    monkeypatch.setattr(subject, "read_verifier_source", lambda *_args: history)
    monkeypatch.setattr(subject, "read_agent_execution", lambda *_args: builder)
    monkeypatch.setattr(subject, "resolve_project_source", lambda *_args: root)
    from zekam.infrastructure.git.publication_review import _blob_id

    changes = {
        "schema": "zekam-publication-review-changes/v1",
        "base_revision": history["source_revision"],
        "head": plan.target.head,
        "grants_authority": False,
        "changes": [
            {
                "path": test,
                "status": "A",
                "old_mode": "000000",
                "new_mode": "100644",
                "old_blob": "0" * 40,
                "new_blob": _blob_id(contents[test], 40),
                "before": None,
                "after": contents[test].decode(),
            }
        ],
    }
    changes["change_digest"] = digest(changes)
    monkeypatch.setattr(subject, "capture_review_changes", lambda *_args, **_kwargs: changes)
    return SimpleNamespace(
        world=world,
        root=root,
        plan=plan,
        adapter=adapter,
        history=history,
        measured=measured,
        builder=builder,
        current=current,
        reads=reads,
        objects=objects,
    )


def prepare(w, **kwargs):
    return subject.prepare_publication_review(
        w.world.home,
        publication=w.plan,
        test_reference="native",
        builder_reference="builder",
        adapter=kwargs.pop("adapter", w.adapter),
        **kwargs,
    )


def test_preparation_binds_complete_scope_budget_without_exporting_code(review_world):
    w = review_world
    prepared = prepare(w)
    public = prepared.to_document()
    assert set(prepared.plan) == subject._PLAN_FIELDS
    assert prepared.plan["subject_digest"] == w.plan.subject_digest
    assert prepared.plan["budget"]["max_agent_calls"] == 1
    assert prepared.plan["context_manifest_digest"] == digest(
        subject._manifest(prepared.context, w.adapter.execution_binding_digest)
    )
    assert prepared.context["accepted_test_files"] == [["src/test/ATest.java", "class ATest {}\n"]]
    assert b"class ATest" not in canonical_bytes(public)
    assert public["model_calls"] == 0 and public["source_contents_included"] is False
    assert public["grants_authority"] is False and len(w.reads) == 2


@pytest.mark.parametrize(
    "change", ["alias", "missing-profile", "profile-drift", "non-native", "upstream-drift"]
)
def test_preparation_rejects_unqualified_or_same_upstream_builder(review_world, change):
    w = review_world
    adapter = w.adapter
    if change == "alias":
        adapter = replace(adapter, model_id="fixture/alias")
    elif change == "missing-profile":
        adapter = replace(adapter, provider_profile=None)
    elif change == "profile-drift":
        w.builder["provider_profile_digest"] = digest("different")
    elif change == "non-native":
        w.builder["native_execution_observed"] = False
    else:
        w.builder["configured_upstream"] = "different"
    with pytest.raises(PolicyViolation):
        prepare(w, adapter=adapter)


@pytest.mark.parametrize(
    "change",
    ["production", "accepted-test", "old-binding", "old-tree", "cas", "context-budget", "readback"],
)
def test_preparation_rejects_partial_stale_or_unbound_context(review_world, monkeypatch, change):
    w = review_world
    adapter = w.adapter
    if change == "production":
        (w.root / "src/main/A.java").write_text("class Changed {}", encoding="utf-8")
    elif change == "accepted-test":
        (w.root / "src/test/ATest.java").write_text("class Changed {}", encoding="utf-8")
    elif change == "old-binding":
        w.measured["build_config_digest"] = digest("different")
    elif change == "old-tree":
        del w.history["measured_test_tree"]
    elif change == "cas":
        adapter = replace(adapter, objects=LocalContentAddressedStore(w.world.home / "other-cas"))
    elif change == "context-budget":
        monkeypatch.setattr(subject, "MAX_CONTEXT_BYTES", 1)
    else:

        def changed(*_args, **_kwargs):
            w.reads.append(True)
            return w.current | (
                {"source_evidence_digest": digest("changed")} if len(w.reads) > 1 else {}
            )

        monkeypatch.setattr(subject, "read_publication_test_source", changed)
    with pytest.raises(PolicyViolation):
        prepare(w, adapter=adapter)


@pytest.mark.parametrize("timeout", [True, 0, 601, "300"])
def test_preparation_rejects_invalid_budget_before_source_access(review_world, timeout):
    with pytest.raises(PolicyViolation, match="timeout"):
        prepare(review_world, timeout_seconds=timeout)
    assert not review_world.reads


def test_review_run_requires_distinct_admitted_plan_before_dispatch(review_world, monkeypatch):
    w = review_world
    monkeypatch.setattr(
        subject, "run_canonical_job", lambda *_args, **_kwargs: pytest.fail("unadmitted dispatch")
    )
    with pytest.raises(PolicyViolation, match="distinct admitted"):
        subject.run_publication_review(
            w.world.home,
            publication=w.plan,
            test_reference="native",
            builder_reference="builder",
            adapter=w.adapter,
            review_binding=w.world.binding,
        )


@pytest.mark.parametrize(
    "body,expected",
    [
        ({"verdict": "accepted", "new_material_risks": []}, "accepted"),
        ({"verdict": "rejected"}, "rejected"),
        (
            {
                "verdict": "accepted",
                "scenario_results": [{"scenario_id": "S1", "status": "unresolved"}],
            },
            "rejected",
        ),
        ({"verdict": "invented"}, "unavailable"),
    ],
)
def test_decision_does_not_promote_unresolved_or_invalid_review(body, expected):
    assert subject._decision(body, request()) == expected


def test_source_reader_requires_real_completed_canonical_receipt(world):
    with pytest.raises(PolicyViolation, match="completed canonical receipt"):
        subject.read_publication_review(world.home, str(uuid4()))


@pytest.mark.parametrize("change", [None, "same-session", "same-upstream", "wrong-profile"])
def test_actual_execution_independence_is_required_before_terminal_result(
    review_world, monkeypatch, change
):
    w = review_world
    plan = prepare(w).plan
    w.builder["native_identity"] = "opencode:builder-session:builder-message"
    reviewer = {
        "native_execution_observed": True,
        "configured_model": plan["reviewer_model"],
        "configured_upstream": plan["reviewer_upstream"],
        "provider_profile_digest": plan["provider_profile_digest"],
        "execution_binding_digest": plan["execution_binding_digest"],
        "native_identity": "opencode:reviewer-session:reviewer-message",
    }
    if change == "same-session":
        reviewer["native_identity"] = "opencode:builder-session:another-message"
    elif change == "same-upstream":
        w.builder["configured_upstream"] = plan["reviewer_upstream"]
    elif change == "wrong-profile":
        reviewer["provider_profile_digest"] = digest("another-profile")
    if change is None:
        subject._independent_execution(w.objects, plan, reviewer)
    else:
        with pytest.raises(PolicyViolation):
            subject._independent_execution(w.objects, plan, reviewer)
