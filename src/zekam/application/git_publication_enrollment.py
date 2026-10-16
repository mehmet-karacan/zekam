"""Initial public registry enrollment under an exact canonical Work claim.

No signing key creation or import, no config activation, no existing-file
adoption or overwrite. The durable config pin must have been admitted already.
"""

from __future__ import annotations

import os
from pathlib import Path
from uuid import UUID

from zekam.application.bounded_file import read_bounded_bytes
from zekam.application.canonical_job_runtime import replay_canonical_job, run_canonical_job
from zekam.application.config import load_settings
from zekam.application.git_publication_trust import (
    _strict_json,
    parse_publication_registry,
    publication_config_anchor,
)
from zekam.application.local_runtime import LocalJobBinding
from zekam.domain.canonical import canonical_bytes, digest, digest_of_bytes
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.infrastructure.local_file_security import (
    path_or_parent_is_link_or_reparse,
    private_directory,
    private_regular,
    restrict_private_file,
    restrict_private_tree,
)

OPERATION = "publication.trust.enroll"


def load_trust_enrollment_plan(path: Path) -> dict:
    try:
        result = _strict_json(
            read_bounded_bytes(path, max_bytes=64 * 1024).decode("utf-8", errors="strict")
        )
        if not isinstance(result, dict):
            raise ValueError("object")
        return result
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise ValidationFailed("Publication enrollment bounded strict plan required") from exc


def _path(home):
    return home / "config" / "git-publication-trust.json"


def _private_boundary(home):
    path = _path(home)
    if path_or_parent_is_link_or_reparse(path) or not private_directory(home):
        raise PolicyViolation("Publication enrollment requires a real private home")
    parent = path.parent
    if parent.exists() and not private_directory(parent):
        raise PolicyViolation("Publication enrollment config directory is not private")
    return path


def build_trust_enrollment_plan(home: Path, raw: bytes, *, project_id: str) -> dict:
    if str(UUID(project_id)) != project_id:
        raise PolicyViolation("Publication enrollment canonical project UUID required")
    registry, realm, _roles = parse_publication_registry(raw)
    if registry["revision"] != 1:
        raise PolicyViolation(
            "Publication initial enrollment requires revision one; rotation is separate"
        )
    config_id, config_digest, pin = publication_config_anchor(home)
    settings = load_settings(home=home)
    if (
        settings.git_publication.trust_digest != pin
        or digest(settings.sanitized()) != config_digest
        or digest(registry) != pin
    ):
        raise PolicyViolation(
            "Publication enrollment current settings/active config/registry drift"
        )
    path = _private_boundary(home)
    if path.exists():
        raise PolicyViolation(
            "Publication enrollment cannot adopt or overwrite an existing registry"
        )
    body = {
        "schema": "zekam-publication-trust-enrollment-plan/v1",
        "operation": OPERATION,
        "project_id": project_id,
        "config_revision_id": config_id,
        "config_digest": config_digest,
        "realm_id": str(realm),
        "registry": registry,
        "trust_digest": pin,
        "registry_byte_digest": digest_of_bytes(canonical_bytes(registry) + b"\n"),
        "target": "config/git-publication-trust.json",
        "existing_target_policy": "reject",
        "signing_keys_created": 0,
        "grants_authority": False,
    }
    return body | {"plan_digest": digest(body)}


def _owner(plan, binding):
    if type(binding) is not LocalJobBinding:
        raise PolicyViolation("Publication enrollment exact Work binding required")
    binding.__post_init__()
    expected = {
        "schema",
        "operation",
        "project_id",
        "config_revision_id",
        "config_digest",
        "realm_id",
        "registry",
        "trust_digest",
        "registry_byte_digest",
        "target",
        "existing_target_policy",
        "signing_keys_created",
        "grants_authority",
        "plan_digest",
    }
    if not isinstance(plan, dict) or set(plan) != expected:
        raise PolicyViolation("Publication enrollment exact plan shape required")
    registry, realm, _roles = parse_publication_registry(canonical_bytes(plan["registry"]))
    if (
        set(plan) != expected
        or plan["schema"] != "zekam-publication-trust-enrollment-plan/v1"
        or plan["operation"] != OPERATION
        or registry["revision"] != 1
        or plan["realm_id"] != str(realm)
        or digest(registry) != plan["trust_digest"]
        or digest_of_bytes(canonical_bytes(registry) + b"\n") != plan["registry_byte_digest"]
        or plan["target"] != "config/git-publication-trust.json"
        or plan["existing_target_policy"] != "reject"
        or type(plan["signing_keys_created"]) is not int
        or plan["signing_keys_created"] != 0
        or plan["grants_authority"] is not False
        or digest({key: value for key, value in plan.items() if key != "plan_digest"})
        != plan["plan_digest"]
        or binding.plan_digest != plan["plan_digest"]
        or any(
            getattr(binding, key) != plan[key]
            for key in ("project_id", "config_revision_id", "config_digest")
        )
    ):
        raise PolicyViolation("Publication enrollment plan/Work/policy binding drift")


def _read_effect(home, plan):
    path = _private_boundary(home)
    if not private_regular(path):
        raise PolicyViolation("Publication enrollment private registry readback unavailable")
    raw = read_bounded_bytes(path, max_bytes=32 * 1024)
    registry, _realm, _roles = parse_publication_registry(raw)
    if raw != canonical_bytes(plan["registry"]) + b"\n" or digest(registry) != plan["trust_digest"]:
        raise PolicyViolation("Publication enrollment registry readback drift")


def enroll_publication_trust(
    home: Path, *, plan: dict, binding: LocalJobBinding, validate_inputs
) -> dict:
    _owner(plan, binding)
    input_digest = digest({"plan_digest": plan["plan_digest"]})
    replay = replay_canonical_job(
        home, binding=binding, operation=OPERATION, input_digest=input_digest
    )

    def expected_result():
        return {
            "schema": "zekam-publication-trust-enrollment-result/v1",
            "plan": plan,
            "registry_byte_digest": plan["registry_byte_digest"],
            "trust_digest": plan["trust_digest"],
            "file_readback_verified": True,
            "signing_keys_created": 0,
            "producer_execution_verified": False,
            "publication_authorized": False,
            "provider_calls": 0,
            "grants_authority": False,
        }

    if replay is not None:
        _read_effect(home, plan)
        if canonical_bytes(
            {
                key: value
                for key, value in replay.items()
                if key not in {"canonical_job_id", "canonical_replayed"}
            }
        ) != canonical_bytes(expected_result()):
            raise PolicyViolation("Publication enrollment completed result drift")
        return replay

    def validate():
        validate_inputs()
        rebuilt = build_trust_enrollment_plan(
            home, canonical_bytes(plan["registry"]), project_id=plan["project_id"]
        )
        if canonical_bytes(rebuilt) != canonical_bytes(plan):
            raise PolicyViolation("Publication enrollment current plan drift")

    def execute(cancel):
        validate()
        if cancel.is_set():
            raise PolicyViolation("Publication enrollment cancelled before write")
        path = _private_boundary(home)
        if not path.parent.exists():
            path.parent.mkdir(mode=0o700)
            restrict_private_tree(path.parent)
        _private_boundary(home)
        # Exclusive creation arbitrates even competing projects. Never truncate,
        # replace or recover an existing file based on matching prose/digests.
        descriptor = os.open(
            path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600
        )
        with os.fdopen(descriptor, "wb") as stream:
            restrict_private_file(path)
            if not private_regular(path) or cancel.is_set():
                raise PolicyViolation("Publication enrollment private write admission failed")
            stream.write(canonical_bytes(plan["registry"]) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        _read_effect(home, plan)
        return expected_result()

    result = run_canonical_job(
        home,
        binding=binding,
        operation=OPERATION,
        input_digest=input_digest,
        execute=execute,
        validate_inputs=validate,
    )
    _read_effect(home, plan)
    return result
