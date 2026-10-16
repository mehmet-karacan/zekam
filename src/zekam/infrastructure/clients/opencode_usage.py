"""Host step-finish measurements, distinct from billing and provider requests.

Only the native event parser creates this optional CAS artifact field. The model
JSON contract deliberately cannot supply it. Missing dimensions stay unknown.
"""

from __future__ import annotations

import math
import re
from decimal import Decimal, ROUND_CEILING, localcontext
from typing import Any, Mapping

from zekam.domain.canonical import canonical_bytes, digest_of_bytes, parse_digest
from zekam.domain.errors import ValidationFailed

_COMPONENTS = ("input", "output", "reasoning", "cache_read", "cache_write")
_MAX_COUNT = 2**53 - 1
_IDENTITY = re.compile(r"opencode:([A-Za-z0-9_-]{1,200}):([A-Za-z0-9_-]{1,200})")


def _hash(value: str) -> str:
    return digest_of_bytes(value.encode("utf-8"))


def _digest(value: Any) -> None:
    if not isinstance(value, str):
        raise ValidationFailed("Native usage digest must be text")
    parse_digest(value)


def _count(value: Any) -> int | None:
    if value is None:
        return None
    if (
        type(value) not in {int, float}
        or value < 0
        or value > _MAX_COUNT
        or not math.isfinite(value)
        or int(value) != value
    ):
        raise ValidationFailed("Native usage token count invalid")
    return int(value)


def _usd(value: Any) -> str | None:
    if value is None:
        return None
    if type(value) not in {int, float} or not 0 <= value <= 1_000_000 or not math.isfinite(value):
        raise ValidationFailed("Native usage estimated USD invalid")
    if value == 0:
        return "0"
    return format(Decimal(str(value)).normalize(), "f")


def capture_step(part: dict[str, Any]) -> dict[str, Any]:
    """Call only after the event parser validates session/message/order/reason."""
    tokens = part.get("tokens")
    components = dict.fromkeys(_COMPONENTS)
    if tokens is not None:
        if not isinstance(tokens, dict) or set(tokens) - {"input", "output", "reasoning", "cache"}:
            raise ValidationFailed("Native usage token schema invalid")
        cache = tokens.get("cache")
        if cache is not None and (not isinstance(cache, dict) or set(cache) - {"read", "write"}):
            raise ValidationFailed("Native usage cache schema invalid")
        components = {
            "input": _count(tokens.get("input")),
            "output": _count(tokens.get("output")),
            "reasoning": _count(tokens.get("reasoning")),
            "cache_read": _count((cache or {}).get("read")),
            "cache_write": _count((cache or {}).get("write")),
        }
    cost = _usd(part.get("cost"))
    observed = cost is not None or any(value is not None for value in components.values())
    step_id = part.get("id")
    if observed and (
        not isinstance(step_id, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,200}", step_id) is None
    ):
        raise ValidationFailed("Native usage observed step identity required")
    return {
        "native_session_digest": _hash(part["sessionID"]),
        "native_message_digest": _hash(part["messageID"]),
        "native_step_digest": _hash(step_id) if observed else None,
        "finish_reason": part["reason"],
        "tokens": components,
        "host_estimated_cost_usd": cost,
    }


def _summary(steps: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not any(
        step["host_estimated_cost_usd"] is not None
        or any(value is not None for value in step["tokens"].values())
        for step in steps
    ):
        return None
    totals = {
        name: (
            sum(step["tokens"][name] for step in steps)
            if all(step["tokens"][name] is not None for step in steps)
            else None
        )
        for name in _COMPONENTS
    }
    complete = all(value is not None for value in totals.values())
    costs = [step["host_estimated_cost_usd"] for step in steps]
    with localcontext() as arithmetic:
        arithmetic.prec = 400
        cost = (
            sum((Decimal(item) for item in costs), Decimal(0))
            if all(item is not None for item in costs)
            else None
        )
        micros = (
            int((cost * 1_000_000).to_integral_value(rounding=ROUND_CEILING))
            if cost is not None
            else None
        )
    return {
        "schema": "zekam-opencode-native-usage/v1",
        "source": "native-step-finish",
        "scope": "completed-assistant-steps",
        "steps": steps,
        "token_components": totals,
        "token_measurement_complete": complete,
        "native_reported_tokens": sum(totals.values()) if complete else None,
        "host_estimated_cost_usd": format(cost, "f") if cost is not None else None,
        "estimated_cost_micros": micros,
        "provider_requests": None,
        "billing_verified": False,
        "grants_authority": False,
    }


def attach_native_usage(document: dict, steps: list[dict]) -> dict:
    usage = _summary(steps)
    if usage is not None:
        document["native_usage"] = usage
        validate_native_usage(document)
    return document


def validate_native_usage(artifact: Mapping[str, Any]) -> dict | None:
    """Recompute totals and bind every retained step to the native identity.

    CAS/assignment/receipt readers supply artifact authenticity; this validates
    its projection, never authenticates arbitrary caller JSON on its own.
    """
    if "native_usage" not in artifact:
        return None
    usage = artifact["native_usage"]
    meta = artifact.get("result")
    identity = (
        _IDENTITY.fullmatch(str(meta.get("agent_id", ""))) if isinstance(meta, dict) else None
    )
    steps = usage.get("steps") if isinstance(usage, dict) else None
    native = artifact.get("native_skill_invocation")
    expected_reasons = ["tool-calls", "stop"] if native is not None else ["stop"]
    if identity is None or not isinstance(steps, list) or len(steps) != len(expected_reasons):
        raise ValidationFailed("Native usage artifact identity/step count invalid")
    session, final_message = identity.groups()
    messages, step_ids = set(), set()
    for index, step in enumerate(steps):
        if not isinstance(step, dict) or set(step) != {
            "native_session_digest",
            "native_message_digest",
            "native_step_digest",
            "finish_reason",
            "tokens",
            "host_estimated_cost_usd",
        }:
            raise ValidationFailed("Native usage step projection invalid")
        for key in ("native_session_digest", "native_message_digest"):
            _digest(step[key])
        if (
            step["native_session_digest"] != _hash(session)
            or step["finish_reason"] != expected_reasons[index]
        ):
            raise ValidationFailed("Native usage session/order drift")
        if step["native_message_digest"] in messages:
            raise ValidationFailed("Native usage repeated assistant message")
        messages.add(step["native_message_digest"])
        components = step["tokens"]
        if not isinstance(components, dict) or set(components) != set(_COMPONENTS):
            raise ValidationFailed("Native usage components invalid")
        for value in components.values():
            # Projection has canonical integer counts; bool/float not accepted.
            if value is not None and (type(value) is not int or _count(value) != value):
                raise ValidationFailed("Native usage projected token count invalid")
        cost = step["host_estimated_cost_usd"]
        if cost is not None:
            if (
                not isinstance(cost, str)
                or len(cost) > 340
                or re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", cost) is None
                or Decimal(cost) > 1_000_000
            ):
                raise ValidationFailed("Native usage projected USD invalid")
        observed = cost is not None or any(value is not None for value in components.values())
        if observed:
            _digest(step["native_step_digest"])
            if step["native_step_digest"] in step_ids:
                raise ValidationFailed("Native usage repeated step identity")
            step_ids.add(step["native_step_digest"])
        elif step["native_step_digest"] is not None:
            raise ValidationFailed("Native usage absent measurement identity drift")
    if steps[-1]["native_message_digest"] != _hash(final_message):
        raise ValidationFailed("Native usage final message drift")
    if native is not None and (
        not isinstance(native, dict)
        or native.get("native_session_digest") != _hash(session)
        or native.get("native_message_digest") != steps[0]["native_message_digest"]
    ):
        raise ValidationFailed("Native usage skill tool message drift")
    expected = _summary(steps)
    if expected is None or canonical_bytes(expected) != canonical_bytes(usage):
        raise ValidationFailed("Native usage totals/authority drift")
    return usage
