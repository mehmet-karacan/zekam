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


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _strict_json(raw):
    def reject(_value):
        raise ValueError("nonfinite JSON")

    return json.loads(raw, object_pairs_hook=_unique, parse_constant=reject)


def parse_publication_registry(raw: bytes) -> tuple[dict, UUID, dict]:
    """Pure public-key schema validation shared by enrollment and trust reads."""
    try:
        if type(raw) is not bytes or len(raw) > 32 * 1024:
            raise ValueError("bounded bytes")
        document = _strict_json(raw.decode("utf-8", errors="strict"))
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
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError, UnicodeError) as exc:
        raise ConfigurationError(
            "Publication registry bounded public-key sozlesmesine uymuyor"
        ) from exc
    return document, realm_id, roles


def publication_config_anchor(home: Path) -> tuple[str, str, str]:
    """Read only: the one existing active config's exact public registry pin."""
    connection = _connect(home / "state" / "operational.db", read_only=True)
    try:
        rows = connection.execute(
            "select id,config_digest,case when length(cast(sanitized_json as blob))<=4194304 "
            "then sanitized_json else null end as sanitized_json "
            "from config_revision where active=1 limit 2"
        ).fetchall()
    finally:
        connection.close()
    if len(rows) != 1:
        raise ConfigurationError("Publication exact aktif config revision ister")
    row = rows[0]
    try:
        config = _strict_json(row["sanitized_json"])
        if not isinstance(config, dict):
            raise ValueError("config")
    except (ValueError, TypeError, RecursionError) as exc:
        raise ConfigurationError("Publication active config object sozlesmesine uymuyor") from exc
    publication = config.get("git_publication", {})
    if (
        digest(config) != row["config_digest"]
        or not isinstance(publication, dict)
        or set(publication) != {"trust_digest"}
        or type(publication.get("trust_digest")) is not str
    ):
        raise PolicyViolation("Publication registry active config trust digest drift")
    from zekam.domain.canonical import parse_digest

    parse_digest(publication["trust_digest"])
    return row["id"], row["config_digest"], publication["trust_digest"]


def load_publication_trust(home: Path) -> PublicationTrust:
    """Read only; never create keys, activate config or adopt a caller's trust file."""
    path = home / "config" / "git-publication-trust.json"
    if path_or_parent_is_link_or_reparse(path) or not private_regular(path):
        raise ConfigurationError("Publication trusted producer registry hazir/private degil")
    try:
        raw = read_bounded_bytes(path, max_bytes=32 * 1024)
    except OSError as exc:
        raise ConfigurationError("Publication registry bounded read unavailable") from exc
    document, realm_id, roles = parse_publication_registry(raw)
    config_id, config_digest, pin = publication_config_anchor(home)
    observed_digest = digest(document)
    if pin != observed_digest:
        raise PolicyViolation("Publication registry active config trust digest drift")
    return PublicationTrust(
        realm_id,
        config_id,
        config_digest,
        observed_digest,
        MappingProxyType({role: MappingProxyType(values) for role, values in roles.items()}),
    )
