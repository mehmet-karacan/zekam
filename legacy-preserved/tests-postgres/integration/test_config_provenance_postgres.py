from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from psycopg import Error as PsycopgError
from typer.testing import CliRunner

from zekam.application.config import CONFIG_SCHEMA
from zekam.domain.canonical import digest
from zekam.domain.config_provenance import (
    ConfigLayer,
    ManagedFieldRequirement,
    ManagedRequirementMode,
    PermissionProfileRevision,
    compile_config_provenance,
)
from zekam.domain.errors import PolicyViolation
from zekam.domain.realm import Realm
from zekam.infrastructure.postgres.config_provenance_repository import (
    ConfigProvenanceRepository,
)
from zekam.infrastructure.postgres.connection import configure_session, connect
from zekam.infrastructure.postgres.core_repository import RealmRepository
from zekam.interfaces.cli.main import app

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


def _insert_forged_graph(
    connection: Any,
    realm_id: Any,
    layer_stack: list[str],
    fields: list[dict[str, Any]],
    effective_document: dict[str, Any],
) -> None:
    effective_digest = digest(effective_document)
    body = {
        "schema": "zekam-config-provenance-graph/v1",
        "layer_stack": layer_stack,
        "fields": fields,
        "effective_digest": effective_digest,
        "grants_authority": False,
    }
    with connection.cursor() as cursor:
        cursor.execute(
            "insert into security.config_provenance_snapshot"
            "(id,realm_id,layer_stack,field_decisions,effective_document,effective_digest,"
            "graph_digest,graph_body,created_at,grants_authority)"
            " values(%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s::jsonb,%s,false)",
            (
                uuid4(),
                realm_id,
                layer_stack,
                json.dumps(fields, default=str),
                json.dumps(effective_document),
                effective_digest,
                digest(body),
                json.dumps(body, default=str),
                dt.datetime.now(dt.UTC),
            ),
        )


def test_profile_and_config_graph_roundtrip_are_immutable_and_realm_scoped(
    realm_session: tuple[Any, Any], migrated_database: Any
) -> None:
    realm, connection = realm_session
    repository = ConfigProvenanceRepository(connection, realm.id)
    now = dt.datetime.now(dt.UTC)
    profile = PermissionProfileRevision.create(
        realm_id=realm.id,
        name="managed-workspace",
        revision=1,
        allowed_capabilities=("filesystem.read", "filesystem.write"),
        denied_capabilities=("network.access",),
        managed=True,
        created_at=now,
    )
    assert repository.store_profile(profile)[1] is True
    assert repository.store_profile(profile)[1] is False
    assert repository.latest_profile(profile.name) == profile
    graph = compile_config_provenance(
        (
            ConfigLayer("core-default", 10, {"runtime": {"network": False}}),
            ConfigLayer("user-config", 20, {"runtime": {"log_level": "DEBUG"}}),
        )
    )
    assert repository.store_graph(graph, created_at=now)[1] is True
    assert repository.store_graph(graph, created_at=now)[1] is False
    with connection.cursor() as cursor:
        cursor.execute(
            "select grants_authority,graph_digest from security.config_provenance_snapshot"
        )
        assert cursor.fetchone() == (False, graph.graph_digest)
    with pytest.raises(PsycopgError), connection.cursor() as cursor:
        cursor.execute(
            "update security.permission_profile_revision set managed=false where id=%s",
            (profile.id,),
        )
    connection.rollback()

    other = Realm.create(slug="config-provenance-other")
    with connect(migrated_database) as owner:
        configure_session(owner, role=None)
        RealmRepository(owner).create(other)
    with connect(migrated_database) as worker:
        configure_session(worker, realm_id=other.id)
        with worker.cursor() as cursor:
            cursor.execute("select count(*) from security.permission_profile_revision")
            assert cursor.fetchone()[0] == 0


def test_database_rejects_forged_profile_digest(realm_session: tuple[Any, Any]) -> None:
    realm, connection = realm_session
    profile = PermissionProfileRevision.create(
        realm_id=realm.id,
        name="forgery-test",
        revision=1,
        allowed_capabilities=("filesystem.read",),
        denied_capabilities=("network.access",),
        managed=True,
        created_at=dt.datetime.now(dt.UTC),
    )
    with pytest.raises(PsycopgError), connection.cursor() as cursor:
        cursor.execute(
            "insert into security.permission_profile_revision"
            "(id,realm_id,name,revision,allowed_capabilities,denied_capabilities,managed,"
            "created_at,profile_digest,profile_body,grants_authority)"
            " values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,false)",
            (
                profile.id,
                realm.id,
                profile.name,
                profile.revision,
                list(profile.allowed_capabilities),
                list(profile.denied_capabilities),
                profile.managed,
                profile.created_at,
                "sha256:" + "0" * 64,
                json.dumps(profile.body(), default=str),
            ),
        )
    connection.rollback()
    foreign_profile = PermissionProfileRevision.create(
        realm_id=Realm.create(slug="not-current").id,
        name=profile.name,
        revision=profile.revision,
        allowed_capabilities=profile.allowed_capabilities,
        denied_capabilities=profile.denied_capabilities,
        managed=profile.managed,
        created_at=profile.created_at,
    )
    with pytest.raises(PolicyViolation, match="cross-realm"):
        ConfigProvenanceRepository(connection, realm.id).store_profile(foreign_profile)


def test_database_rejects_hash_valid_but_semantically_forged_config_graph(
    realm_session: tuple[Any, Any],
) -> None:
    realm, connection = realm_session
    graph = compile_config_provenance(
        (ConfigLayer("core-default", 10, {"runtime": {"network": False}}),)
    )
    forged_document = {"runtime": {"network": True}}
    with pytest.raises(PsycopgError):
        _insert_forged_graph(
            connection,
            realm.id,
            list(graph.layer_stack),
            [field.body() for field in graph.fields],
            forged_document,
        )
    connection.rollback()


def test_database_rejects_hash_valid_managed_deny_and_lower_origin_forgeries(
    realm_session: tuple[Any, Any],
) -> None:
    realm, connection = realm_session
    requirement = ManagedFieldRequirement("runtime.network", ManagedRequirementMode.DENY)
    managed_graph = compile_config_provenance(
        (
            ConfigLayer(
                "managed-policy",
                10,
                {"runtime": {"network": False}},
                managed=True,
                requirements=(requirement,),
            ),
        )
    )
    deny_fields = json.loads(json.dumps([field.body() for field in managed_graph.fields]))
    deny_fields[0]["value"] = True
    deny_fields[0]["value_digest"] = digest(True)
    deny_fields[0]["candidates"][0]["value_digest"] = digest(True)
    with pytest.raises(PsycopgError):
        _insert_forged_graph(
            connection,
            realm.id,
            list(managed_graph.layer_stack),
            deny_fields,
            {"runtime": {"network": True}},
        )
    connection.rollback()

    exact_requirement = ManagedFieldRequirement(
        "runtime.sandbox", ManagedRequirementMode.EXACT, digest("strict")
    )
    exact_graph = compile_config_provenance(
        (
            ConfigLayer(
                "managed-policy",
                10,
                {"runtime": {"sandbox": "strict"}},
                managed=True,
                requirements=(exact_requirement,),
            ),
        )
    )
    exact_fields = json.loads(json.dumps([field.body() for field in exact_graph.fields]))
    exact_fields[0]["value"] = "relaxed"
    exact_fields[0]["value_digest"] = digest("relaxed")
    exact_fields[0]["candidates"][0]["value_digest"] = digest("relaxed")
    with pytest.raises(PsycopgError):
        _insert_forged_graph(
            connection,
            realm.id,
            list(exact_graph.layer_stack),
            exact_fields,
            {"runtime": {"sandbox": "relaxed"}},
        )
    connection.rollback()

    layered_graph = compile_config_provenance(
        (
            ConfigLayer("core-default", 10, {"runtime": {"mode": "core"}}),
            ConfigLayer("session", 20, {"runtime": {"mode": "session"}}),
        )
    )
    origin_fields = json.loads(json.dumps([field.body() for field in layered_graph.fields]))
    origin_fields[0]["origin"] = "core-default"
    origin_fields[0]["value"] = "core"
    origin_fields[0]["value_digest"] = digest("core")
    origin_fields[0]["candidates"][0]["selected"] = True
    origin_fields[0]["candidates"][0]["disabled_reason"] = None
    origin_fields[0]["candidates"][1]["selected"] = False
    origin_fields[0]["candidates"][1]["disabled_reason"] = "lower-origin-forgery"
    with pytest.raises(PsycopgError):
        _insert_forged_graph(
            connection,
            realm.id,
            list(layered_graph.layer_stack),
            origin_fields,
            {"runtime": {"mode": "core"}},
        )
    connection.rollback()


def test_cli_explains_latest_realm_profile_without_mutation(
    migrated_database: Any, tmp_path: Path
) -> None:
    realm = Realm.create(slug="config-cli-realm")
    with connect(migrated_database) as owner:
        configure_session(owner, role=None)
        RealmRepository(owner).create(realm)
    profile = PermissionProfileRevision.create(
        realm_id=realm.id,
        name="cli-profile",
        revision=1,
        allowed_capabilities=("filesystem.read",),
        denied_capabilities=("network.access",),
        managed=True,
        created_at=dt.datetime.now(dt.UTC),
    )
    with connect(migrated_database) as worker:
        configure_session(worker, realm_id=realm.id)
        ConfigProvenanceRepository(worker, realm.id).store_profile(profile)

    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text(
        f"schema: {CONFIG_SCHEMA}\n"
        "database:\n"
        f"  host: {migrated_database.host}\n"
        f"  port: {migrated_database.port}\n"
        f"  name: {migrated_database.name}\n"
        f"  user: {migrated_database.user}\n",
        encoding="utf-8",
    )
    result = CliRunner().invoke(
        app,
        [
            "permission",
            "profile",
            "explain",
            profile.name,
            "--realm",
            realm.slug,
            "--home",
            str(home),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.stderr
    document = json.loads(result.stdout)
    assert document["profile_digest"] == profile.profile_digest
    assert document["catalog_source"] == "postgres"
    with connect(migrated_database) as worker:
        configure_session(worker, realm_id=realm.id)
        with worker.cursor() as cursor:
            cursor.execute("select count(*) from security.permission_profile_revision")
            assert cursor.fetchone()[0] == 1
