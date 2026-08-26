"""Load the reviewed, provider-neutral memory routing policy."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.memory_routing import (
    MEMORY_ROUTING_SCHEMA,
    MemoryCapabilityClass,
    MemoryRoutingPolicy,
    MemoryWorkload,
    MemoryWorkloadRoute,
)
from zekam.domain.model_routing import AgentRole

_ROUTE_FIELDS = {
    "capability_class",
    "role",
    "risk",
    "deterministic_prepass",
    "model_optional",
    "independent_review",
}


def default_memory_routing_policy_file() -> Path:
    return Path(__file__).resolve().parents[3] / "config" / "memory_routing_policy.yaml"


def load_memory_routing_policy(path: Path | None = None) -> MemoryRoutingPolicy:
    candidate = path or default_memory_routing_policy_file()
    if candidate.is_symlink():
        raise PolicyViolation("Memory routing policy symlink olamaz")
    target = candidate.resolve(strict=True)
    if not target.is_file() or target.stat().st_size > 64 * 1024:
        raise PolicyViolation("Memory routing policy guvenli regular file olmali")
    document = yaml.safe_load(target.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or set(document) != {
        "schema",
        "provider_calls_default",
        "workloads",
    }:
        raise ValidationFailed("Memory routing policy exact shape ister")
    workloads = document["workloads"]
    if (
        document["schema"] != MEMORY_ROUTING_SCHEMA
        or document["provider_calls_default"] is not False
        or not isinstance(workloads, dict)
        or set(workloads) != {item.value for item in MemoryWorkload}
    ):
        raise ValidationFailed("Memory routing policy schema/workload seti gecersiz")
    routes: list[MemoryWorkloadRoute] = []
    for workload in MemoryWorkload:
        raw = workloads[workload.value]
        if not isinstance(raw, dict) or set(raw) != _ROUTE_FIELDS:
            raise ValidationFailed("Memory workload route exact shape ister")
        try:
            routes.append(
                MemoryWorkloadRoute(
                    workload=workload,
                    capability_class=MemoryCapabilityClass(str(raw["capability_class"])),
                    role=AgentRole(str(raw["role"])),
                    risk=str(raw["risk"]),
                    deterministic_prepass=raw["deterministic_prepass"] is True,
                    model_optional=raw["model_optional"] is True,
                    independent_review=raw["independent_review"] is True,
                )
            )
        except ValueError as exc:
            raise ValidationFailed("Memory workload route enum degeri gecersiz") from exc
    return MemoryRoutingPolicy(routes=tuple(routes), provider_calls_default=False)


def sanitized_memory_routing_policy(policy: MemoryRoutingPolicy) -> dict[str, Any]:
    return {
        "schema": MEMORY_ROUTING_SCHEMA,
        "provider_calls_default": policy.provider_calls_default,
        "workload_count": len(policy.routes),
        "workloads": [item.body() for item in sorted(policy.routes, key=lambda x: x.workload)],
        "policy_digest": policy.policy_digest,
        "grants_authority": False,
    }
