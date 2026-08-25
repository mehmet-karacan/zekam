"""Model/context experiment proposal artifact persistence."""

from __future__ import annotations

from dataclasses import dataclass

from zekam.application.transcript_corpus_import import ContentAddressedStore
from zekam.domain.canonical import digest_of_bytes
from zekam.domain.errors import ValidationFailed
from zekam.domain.model_context_experiment import ModelContextExperimentResult


@dataclass(frozen=True, slots=True)
class StoredExperimentProposal:
    experiment_digest: str
    object_digest: str
    review_status: str = "review-required"
    grants_authority: bool = False


def persist_experiment_proposal(
    result: ModelContextExperimentResult, store: ContentAddressedStore
) -> StoredExperimentProposal:
    result.__post_init__()
    payload = result.to_bytes()
    info = store.put(
        payload,
        media_type="application/vnd.zekam.model-context-experiment+json",
        metadata={
            "experiment_digest": result.experiment_digest,
            "review_status": "review-required",
        },
    )
    if (
        info.digest != digest_of_bytes(payload)
        or not store.exists(info.digest)
        or store.get(info.digest) != payload
    ):
        raise ValidationFailed("experiment proposal CAS dogrulamasi basarisiz")
    return StoredExperimentProposal(result.experiment_digest, info.digest)
