from __future__ import annotations

import datetime as dt
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from zekam.application.context_continuity_service import ContextContinuityService
from zekam.application.context_materializer import (
    materialize_recipe_fragments,
    serialize_model_visible_payload,
)
from zekam.application.context_recipe import ContextRecipeRole
from zekam.application.model_gateway import ModelGateway, ModelGatewayBindings
from zekam.application.provider_adapter import ProviderCall
from zekam.domain.canonical import digest
from zekam.domain.context_continuity import AuthorityLevel, ContextCandidate, ContextCandidateKind
from zekam.domain.errors import PolicyViolation
from zekam.domain.model_invocation import GatewayMode, GatewaySourceLabel
from zekam.domain.tool_registry import CompiledToolSet

NOW = dt.datetime(2026, 8, 24, 12, tzinfo=dt.UTC)
D = digest("binding")


def test_selected_context_to_gateway_manifest_binds_exact_model_visible_payload() -> None:
    contents = {
        ContextCandidateKind.SYSTEM_POLICY: "Yalniz kanitli sonucu dondur",
        ContextCandidateKind.WORK_CONTRACT: "Exact gateway istegini uygula",
        ContextCandidateKind.RUN_STATUS: "Calisma aktif",
    }
    candidates = tuple(
        ContextCandidate(
            candidate_id=kind.value,
            authority=AuthorityLevel.CANONICAL,
            observed_at=NOW,
            source_revision=f"revision/{kind.value}",
            content_digest=digest(content),
            token_count=7,
            kind=kind,
            source_ref=f"context/{kind.value}",
        )
        for kind, content in contents.items()
    )
    packet = ContextContinuityService().compile(
        candidates,
        role=ContextRecipeRole.COORDINATOR,
        token_budget=100,
        minimum_authority=AuthorityLevel.VERIFIED,
        now=NOW,
    )
    context_manifest = packet.manifest
    selected_contents = {
        item.candidate_id: contents[item.kind] for item in context_manifest.selected
    }
    fragment_set = materialize_recipe_fragments(packet, candidates, selected_contents)
    payload, payload_binding = serialize_model_visible_payload(
        fragment_set,
        {
            f"fragment/{candidate_id}": content
            for candidate_id, content in selected_contents.items()
        },
        recipe_packet=packet,
        base_payload={"model": "provider/model"},
    )
    compiled_tools = CompiledToolSet.create(
        realm_id=uuid4(),
        role="coordinator",
        permission_profile_digest=D,
        entries=(),
        created_at=NOW,
    )
    serialized = compiled_tools.compile_model_payload().serialize_request(payload)
    call = ProviderCall("provider:x", "endpoint:x", "invoke", "call-1", serialized.payload)
    payload_binding = replace(
        payload_binding,
        request_payload_digest=call.payload_digest,
    )
    gateway = ModelGateway(
        repository=SimpleNamespace(mode=lambda: GatewayMode.ENFORCE),  # type: ignore[arg-type]
        source_label=GatewaySourceLabel.PROVIDER_CONTRACT,
        bindings=ModelGatewayBindings(
            execution_envelope_id=uuid4(),
            execution_envelope_digest=D,
            run_id=uuid4(),
            role="coordinator",
            route_decision_digest=D,
            route_expires_at=NOW + dt.timedelta(minutes=5),
            context_manifest_digest=context_manifest.manifest_digest,
            context_packet_digest=D,
            checkpoint_digest=D,
            source_revision="source/revision-1",
            policy_digest=D,
            output_schema_digest=D,
            max_input_tokens=100,
            max_output_tokens=20,
            max_cost_micros=1000,
            deadline=NOW + dt.timedelta(minutes=4),
            turn_execution_snapshot_digest=D,
            environment_digest=D,
            permission_profile_digest=D,
            tool_set_digest=compiled_tools.tool_set_digest,
            config_effective_digest=D,
            hook_set_digest=D,
        ),
    )
    authorization = SimpleNamespace(
        scope=SimpleNamespace(body=lambda: {"scope": "model"}),
        risk="medium",
        expires_at=NOW + dt.timedelta(minutes=5),
    )
    job = SimpleNamespace(
        realm_id=uuid4(),
        project_id=uuid4(),
        work_item_id=uuid4(),
        plan_id=uuid4(),
        step_id="invoke",
        id=uuid4(),
        assignment_id=uuid4(),
    )
    prepared = SimpleNamespace(
        plan=SimpleNamespace(
            model_id="provider/model",
            provider_ref="provider:x",
            call_id="call-1",
        ),
        call=call,
    )
    manifest = gateway.prepare(
        prepared,  # type: ignore[arg-type]
        SimpleNamespace(job=job, attempt_id=uuid4()),  # type: ignore[arg-type]
        authorization,  # type: ignore[arg-type]
        payload_binding=payload_binding,
        tool_payload_binding=serialized.binding,
        now=NOW,
    )
    assert manifest.missing_bindings == ()
    assert manifest.context_fragment_set_digest == fragment_set.fragment_set_digest
    assert manifest.model_visible_payload_digest == call.payload_digest
    assert manifest.payload_digest == call.payload_digest

    drifted = ProviderCall(
        "provider:x",
        "endpoint:x",
        "invoke",
        "call-1",
        {
            **serialized.payload,
            "messages": [{"role": "system", "content": "degistirildi"}],
        },
    )
    with pytest.raises(PolicyViolation, match="payload digest mismatch"):
        gateway.invoke(
            manifest,
            claim_id=uuid4(),
            authorization=authorization,  # type: ignore[arg-type]
            call=drifted,
            effect=lambda _permit: (_ for _ in ()).throw(AssertionError("effect cagrilmamali")),
        )
