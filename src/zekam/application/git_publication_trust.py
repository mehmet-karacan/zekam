"""Read public producer trust pinned by the active canonical config revision."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping
from uuid import UUID

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from zekam.application.bounded_file import read_bounded_bytes
from zekam.application.local_attestation import Ed25519ReceiptVerifier, ReceiptVerifier
from zekam.domain.canonical import digest
from zekam.domain.errors import ConfigurationError, PolicyViolation
from zekam.infrastructure.local_file_security import (
    path_or_parent_is_link_or_reparse,
    private_regular,
)
from zekam.infrastructure.sqlite.operational_schema import _connect


@dataclass(frozen=True, slots=True)
class PublicationTrust:
    realm_id: UUID
    config_revision_id: str
    config_digest: str
    trust_digest: str
    verifiers: Mapping[str, Mapping[UUID, ReceiptVerifier]]


def load_publication_trust(home: Path) -> PublicationTrust:
    """Read only; never create keys, activate config or adopt a caller's trust file."""
    path = home / "config" / "git-publication-trust.json"
    if path_or_parent_is_link_or_reparse(path) or not private_regular(path):
        raise ConfigurationError("Publication trusted producer registry hazir/private degil")
    try:
        document = json.loads(read_bounded_bytes(path, max_bytes=32 * 1024))
        if (
            not isinstance(document, dict)
            or set(document) != {"schema", "revision", "realm_id", "producers"}
            or document["schema"] != "zekam-git-publication-trust/v1"
            or type(document["revision"]) is not int
            or document["revision"] < 1
            or type(document["producers"]) is not list
            or not 3 <= len(document["producers"]) <= 64
        ):
            raise ValueError("shape")
        realm_id = UUID(document["realm_id"])
        if str(realm_id) != document["realm_id"]:
            raise ValueError("realm")
        roles: dict[str, dict[UUID, ReceiptVerifier]] = {
            role: {} for role in ("approval", "test", "verification")
        }
        identities, public_keys = set(), set()
        for producer in document["producers"]:
            if not isinstance(producer, dict) or set(producer) != {"id", "role", "public_key"}:
                raise ValueError("producer")
            identity = UUID(producer["id"])
            key = base64.b64decode(producer["public_key"], validate=True)
            if (
                str(identity) != producer["id"]
                or producer["role"] not in roles
                or identity in identities
                or key in public_keys
                or len(key) != 32
                or base64.b64encode(key).decode("ascii") != producer["public_key"]
            ):
                raise ValueError("identity/key")
            identities.add(identity)
            public_keys.add(key)
            roles[producer["role"]][identity] = Ed25519ReceiptVerifier(
                Ed25519PublicKey.from_public_bytes(key)
            )
        if any(not producers for producers in roles.values()):
            raise ValueError("role")
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise ConfigurationError(
            "Publication registry bounded public-key sozlesmesine uymuyor"
        ) from exc
    connection = _connect(home / "state" / "operational.db", read_only=True)
    try:
        rows = connection.execute(
            "select id,config_digest,sanitized_json from config_revision where active=1 limit 2"
        ).fetchall()
    finally:
        connection.close()
    if len(rows) != 1:
        raise ConfigurationError("Publication exact aktif config revision ister")
    row = rows[0]
    try:
        config = json.loads(row["sanitized_json"])
        if not isinstance(config, dict):
            raise ValueError("config")
    except (ValueError, TypeError) as exc:
        raise ConfigurationError("Publication active config object sozlesmesine uymuyor") from exc
    observed_digest = digest(document)
    publication = config.get("git_publication", {})
    if (
        digest(config) != row["config_digest"]
        or not isinstance(publication, dict)
        or publication.get("trust_digest") != observed_digest
    ):
        raise PolicyViolation("Publication registry active config trust digest drift")
    return PublicationTrust(
        realm_id,
        row["id"],
        row["config_digest"],
        observed_digest,
        MappingProxyType({role: MappingProxyType(values) for role, values in roles.items()}),
    )
