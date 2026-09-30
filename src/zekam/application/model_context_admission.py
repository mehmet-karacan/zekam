"""Model baglam limiti admission'i (RAG26-R08).

Saglayici cagrisindan ONCE tam payload'in (girdi + cikti rezervi + emniyet payi)
modelin baglamina sigdigini dogrular. Bilinmeyen limit fail-closed reddedilir;
limit model adindan tahmin edilmez. Bu modul ag cagrisi yapmaz ve config yazmaz.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from zekam.domain.errors import ValidationFailed

SAFETY_MIN_TOKENS = 512
SAFETY_RATIO = 0.05


@dataclass(frozen=True, slots=True)
class ModelLimits:
    """Config'te acikca tanimli baglam ve cikti limiti."""

    context: int
    output: int


@dataclass(frozen=True, slots=True)
class ContextAdmission:
    allowed: bool
    reason: str
    context_limit: int | None
    input_tokens: int
    output_reserve: int
    safety_margin: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "context_limit": self.context_limit,
            "input_tokens": self.input_tokens,
            "output_reserve": self.output_reserve,
            "safety_margin": self.safety_margin,
        }


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def model_limits(config: Mapping[str, Any], provider_id: str, model_id: str) -> ModelLimits | None:
    """Effective config'ten exact `provider.<p>.models.<m>.limit` okur; yoksa None."""

    provider = (config.get("provider") or {}).get(provider_id)
    if not isinstance(provider, Mapping):
        return None
    model = (provider.get("models") or {}).get(model_id)
    if not isinstance(model, Mapping):
        return None
    limit = model.get("limit")
    if not isinstance(limit, Mapping):
        return None
    context = _positive_int(limit.get("context"))
    output = _positive_int(limit.get("output"))
    if context is None or output is None:
        return None
    return ModelLimits(context=context, output=output)


def models_missing_limits(
    config: Mapping[str, Any], *, exclude: frozenset[str] = frozenset()
) -> tuple[tuple[str, str], ...]:
    """Limiti tanimsiz (provider, model) ciftleri; doctor icin salt okunur rapor.

    ``exclude``: baglam limiti gerektirmeyen model kimlikleri (embedding, rerank, ses).
    """

    missing: list[tuple[str, str]] = []
    for provider_id, provider in sorted((config.get("provider") or {}).items()):
        if not isinstance(provider, Mapping):
            continue
        for model_id in sorted(provider.get("models") or {}):
            if model_id in exclude:
                continue
            if model_limits(config, provider_id, model_id) is None:
                missing.append((provider_id, model_id))
    return tuple(missing)


def admit_model_request(
    limits: ModelLimits | None,
    *,
    input_tokens: int,
    output_reserve: int,
) -> ContextAdmission:
    """Girdi + cikti rezervi + emniyet payi baglama sigmiyorsa provider'a gitme."""

    if _positive_int(input_tokens) is None or _positive_int(output_reserve) is None:
        raise ValidationFailed("Input ve output rezervi pozitif tam sayi olmali")
    if limits is None:
        return ContextAdmission(
            False, "unknown-context-limit", None, input_tokens, output_reserve, 0
        )
    margin = max(SAFETY_MIN_TOKENS, math.ceil(limits.context * SAFETY_RATIO))
    if output_reserve > limits.output:
        return ContextAdmission(
            False,
            "output-reserve-exceeds-model-output",
            limits.context,
            input_tokens,
            output_reserve,
            margin,
        )
    if input_tokens + output_reserve + margin > limits.context:
        return ContextAdmission(
            False, "context-overflow", limits.context, input_tokens, output_reserve, margin
        )
    return ContextAdmission(True, "admitted", limits.context, input_tokens, output_reserve, margin)
