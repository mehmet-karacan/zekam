"""Opaque HMAC capabilities for trusted local receipt producers and read-only verifiers."""

from __future__ import annotations

import base64
import hmac
from collections.abc import Mapping
from typing import Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from zekam.domain.canonical import canonical_json, digest
from zekam.domain.errors import ValidationFailed
from zekam.infrastructure.process_identity import process_incarnation_token


class ReceiptVerifier(Protocol):
    def matches_receipt_digest(
        self, unsigned_body: Mapping[str, object], receipt_digest: str
    ) -> bool: ...


class LocalAttestationSigner:
    def __init__(self, key: bytes) -> None:
        if type(key) is not bytes or len(key) != 32:
            raise ValidationFailed("Local attestation signer exact 32-byte key required")
        self.__key = key

    def seal_digest(self, unsigned_body: Mapping[str, object]) -> str:
        seal = hmac.digest(self.__key, canonical_json(unsigned_body).encode(), "sha256").hex()
        return digest(dict(unsigned_body) | {"runtime_attestation": f"hmac-sha256:{seal}"})


class LocalAttestationVerifier:
    def __init__(self, key: bytes) -> None:
        if type(key) is not bytes or len(key) != 32:
            raise ValidationFailed("Local attestation verifier exact 32-byte key required")
        self.__key = key

    def matches_receipt_digest(
        self, unsigned_body: Mapping[str, object], receipt_digest: str
    ) -> bool:
        seal = hmac.digest(self.__key, canonical_json(unsigned_body).encode(), "sha256").hex()
        expected = digest(
            dict(unsigned_body) | {"runtime_attestation": f"hmac-sha256:{seal}"}
        )
        return hmac.compare_digest(expected, receipt_digest)


class Ed25519ReceiptSigner:
    """Asymmetric signer suitable for a private key confined to one worker."""

    def __init__(self, private_key: Ed25519PrivateKey) -> None:
        self.__private_key = private_key

    def seal_digest(self, unsigned_body: Mapping[str, object]) -> str:
        signature = self.__private_key.sign(canonical_json(unsigned_body).encode())
        return "ed25519:" + base64.urlsafe_b64encode(signature).decode().rstrip("=")


class Ed25519ReceiptVerifier:
    def __init__(self, public_key: Ed25519PublicKey) -> None:
        self.__public_key = public_key

    def matches_receipt_digest(
        self, unsigned_body: Mapping[str, object], receipt_digest: str
    ) -> bool:
        if type(receipt_digest) is not str or not receipt_digest.startswith("ed25519:"):
            return False
        encoded = receipt_digest.removeprefix("ed25519:")
        try:
            signature = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
            self.__public_key.verify(signature, canonical_json(unsigned_body).encode())
        except (ValueError, InvalidSignature):
            return False
        return True


class LocalProcessIdentityVerifier:
    """Read back a PID-reuse-safe OS process incarnation at the trust boundary."""

    def matches_live_process(self, process_id: int, process_start_token: str) -> bool:
        return process_incarnation_token(process_id) == process_start_token
