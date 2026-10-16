"""Explicit builder/verifier model selection for the opt-in batch path."""

from __future__ import annotations

from typing import Any

from zekam.application.secret_detection import scan_text
from zekam.application.unit_test_secret_scan import find_secret_value
from zekam.domain.errors import PolicyViolation


def validate_model_id(value: str) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 256
        or value != value.strip()
        or value.startswith("-")
        or any(
            ord(char) < 33 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF for char in value
        )
        or "/" not in value
        or not all(value.split("/", 1))
        or scan_text(value, relative_path="unit-test/model-id")
        or find_secret_value({"model": value}) is not None
    ):
        raise PolicyViolation("Batch model kimligi bounded secret-free provider/model olmali")
    return value


def validate_batch_models(builder: str | None, verifier: str | None) -> None:
    if builder is None or verifier is None:
        raise PolicyViolation("Batch builder ve bagimsiz verifier icin iki explicit model ister")
    validate_model_id(builder)
    validate_model_id(verifier)
    # A different variant or provider label alone does not make the same model
    # independent. Alias equivalence still needs actual provider acceptance.
    builder_name = builder.split("/", 1)[1].split("#", 1)[0].casefold()
    verifier_name = verifier.split("/", 1)[1].split("#", 1)[0].casefold()
    if builder_name == verifier_name:
        raise PolicyViolation("Verifier builder ile ayni model veya farkli variant olamaz")


def batch_model_plan(
    builder: str | None,
    verifier: str | None,
    max_remote_calls: int,
    *,
    remote_required: bool = True,
) -> dict[str, Any]:
    models: dict[str, str | None] = {"builder": None, "verifier": None}
    blockers: list[str] = []
    for role, model in (("builder", builder), ("verifier", verifier)):
        if model is None:
            blockers.append(f"{role}-model-missing")
            continue
        try:
            models[role] = validate_model_id(model)
        except PolicyViolation:
            blockers.append(f"{role}-model-invalid")
    if not blockers:
        try:
            validate_batch_models(builder, verifier)
        except PolicyViolation:
            blockers.append("verifier-model-not-distinct")
    if (
        type(max_remote_calls) is not int
        or not (1 if remote_required else 0) <= max_remote_calls <= 10000
    ):
        blockers.append("remote-call-budget-missing-or-invalid")
    return {
        "schema": "zekam-test-batch-models/v1",
        "models": models,
        "max_remote_calls": max_remote_calls,
        "budget_counts": "all-agent-dispatches-including-verifiers-and-failures",
        "binding_status": "ready-for-runtime-check" if not blockers else "incomplete",
        "blockers": blockers,
        "independent_verification_proven": False,
    }
