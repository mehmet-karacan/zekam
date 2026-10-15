"""Wire input must preserve exact typed/computed plan fields and never mint authority."""

import json
from uuid import uuid4

import pytest

from zekam.application.git_publication import GitPublicationPlan, GitPublicationTarget
from zekam.application.git_publication_wire import publication_plan_from_body
from zekam.application.local_runtime import LocalJobBinding
from zekam.domain.canonical import canonical_json, digest
from zekam.domain.errors import ValidationFailed


def _plan():
    binding = LocalJobBinding(
        str(uuid4()),
        str(uuid4()),
        1,
        str(uuid4()),
        str(uuid4()),
        digest("config"),
        digest("parent"),
        source_snapshot_id=str(uuid4()),
    )
    return GitPublicationPlan(
        uuid4(),
        uuid4(),
        binding,
        GitPublicationTarget(
            digest("root"), "origin", digest("remote"), "refs/heads/main", "a" * 40
        ),
        digest("test"),
        digest("review"),
    )


def test_plan_wire_round_trip_keeps_computed_digest():
    plan = _plan()
    assert publication_plan_from_body(json.loads(canonical_json(plan.body()))) == plan


@pytest.mark.parametrize("change", ["authority", "head", "scope", "boolean-revision", "extra-key"])
def test_plan_wire_rejects_computed_or_scope_injection(change):
    body = json.loads(canonical_json(_plan().body()))
    if change == "authority":
        body["grants_authority"] = True
    elif change == "head":
        body["target"]["head"] = "b" * 40
    elif change == "scope":
        body["binding"]["project_id"] = str(uuid4())
    elif change == "boolean-revision":
        body["binding"]["work_revision"] = True
    else:
        body["shell"] = "fixture-command-must-not-run"
    with pytest.raises(ValidationFailed):
        publication_plan_from_body(body)
