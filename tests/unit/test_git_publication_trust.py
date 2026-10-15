"""Private, config-pinned public producer trust; no signing key reaches the loader."""

import base64
import json
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from zekam.application.git_publication_trust import load_publication_trust
from zekam.domain.canonical import canonical_json, digest
from zekam.domain.errors import ConfigurationError, PolicyViolation
from zekam.infrastructure.local_file_security import restrict_private_tree
from zekam.infrastructure.sqlite.operational_schema import bootstrap_v9
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore


@pytest.fixture
def trust_home(tmp_path):
    home = tmp_path / "home"
    (home / "state").mkdir(parents=True)
    (home / "config").mkdir()
    database = home / "state" / "operational.db"
    bootstrap_v9(database)
    producers = []
    for role in ("approval", "test", "verification"):
        public_key = (
            Ed25519PrivateKey.generate()
            .public_key()
            .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        )
        producers.append(
            {
                "id": str(uuid4()),
                "role": role,
                "public_key": base64.b64encode(public_key).decode("ascii"),
            }
        )
    document = {
        "schema": "zekam-git-publication-trust/v1",
        "revision": 1,
        "realm_id": str(uuid4()),
        "producers": producers,
    }
    path = home / "config" / "git-publication-trust.json"
    path.write_text(canonical_json(document), encoding="utf-8")
    config = {"git_publication": {"trust_digest": digest(document)}}
    with SQLiteOperationalStore(database).unit_of_work() as uow:
        uow.activate_config(
            config_digest=digest(config), task_digest=digest("task"), sanitized_config=config
        )
        uow.commit()
    restrict_private_tree(home)
    return home, path, document


def test_public_trust_is_read_only_and_anchored_to_active_config(trust_home):
    home, path, document = trust_home
    before = (home / "state" / "operational.db").read_bytes()
    trust = load_publication_trust(home)
    assert trust.trust_digest == digest(document)
    assert set(trust.verifiers) == {"approval", "test", "verification"}
    assert all(len(values) == 1 for values in trust.verifiers.values())
    assert not hasattr(next(iter(trust.verifiers["approval"].values())), "seal_digest")
    assert (home / "state" / "operational.db").read_bytes() == before
    document["revision"] += 1
    path.write_text(canonical_json(document), encoding="utf-8")
    with pytest.raises(PolicyViolation, match="digest drift"):
        load_publication_trust(home)


@pytest.mark.parametrize("change", ["duplicate-key", "duplicate-id", "missing-role", "private-key"])
def test_trust_rejects_key_aliases_and_private_material(trust_home, change):
    home, path, document = trust_home
    if change == "duplicate-key":
        document["producers"][2]["public_key"] = document["producers"][1]["public_key"]
    elif change == "duplicate-id":
        document["producers"][2]["id"] = document["producers"][1]["id"]
    elif change == "missing-role":
        document["producers"][2]["role"] = "test"
    else:
        document["producers"][0]["private_key"] = "fixture-never-accepted"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ConfigurationError):
        load_publication_trust(home)
