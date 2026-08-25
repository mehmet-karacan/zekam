"""Persistence boundary for authority-free memory hygiene review queues."""

from __future__ import annotations

from dataclasses import dataclass

from zekam.application.transcript_corpus_import import ContentAddressedStore
from zekam.domain.canonical import digest_of_bytes
from zekam.domain.errors import ValidationFailed
from zekam.domain.memory_hygiene import MemoryHygieneReviewQueue


@dataclass(frozen=True, slots=True)
class StoredMemoryHygieneQueue:
    queue_digest: str
    object_digest: str
    review_status: str = "review-required"
    grants_authority: bool = False


def persist_hygiene_review_queue(
    queue: MemoryHygieneReviewQueue, store: ContentAddressedStore
) -> StoredMemoryHygieneQueue:
    queue.validate()
    payload = queue.to_bytes()
    info = store.put(
        payload,
        media_type="application/vnd.zekam.memory-hygiene-review-queue+json",
        metadata={
            "queue_digest": queue.queue_digest,
            "review_status": "review-required",
        },
    )
    if (
        info.digest != digest_of_bytes(payload)
        or not store.exists(info.digest)
        or store.get(info.digest) != payload
    ):
        raise ValidationFailed("hijyen review kuyrugu CAS dogrulamasi basarisiz")
    return StoredMemoryHygieneQueue(queue.queue_digest, info.digest)
