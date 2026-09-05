"""PostgreSQL persistence for config provenance and permission profiles."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from zekam.domain.canonical import canonical_bytes
from zekam.domain.config_provenance import ConfigProvenanceGraph, PermissionProfileRevision
from zekam.domain.errors import PolicyViolation


@dataclass(frozen=True, slots=True)
class ConfigProvenanceRepository:
    connection: Any
    realm_id: UUID

    def store_profile(self, profile: PermissionProfileRevision) -> tuple[UUID, bool]:
        if profile.realm_id != self.realm_id:
            raise PolicyViolation("Permission profile cross-realm yazma reddedildi")
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into security.permission_profile_revision"
                "(id,realm_id,name,revision,allowed_capabilities,denied_capabilities,managed,"
                "created_at,profile_digest,profile_body,grants_authority)"
                " values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,false)"
                " on conflict(realm_id,profile_digest) do nothing returning id",
                (
                    profile.id,
                    self.realm_id,
                    profile.name,
                    profile.revision,
                    list(profile.allowed_capabilities),
                    list(profile.denied_capabilities),
                    profile.managed,
                    profile.created_at,
                    profile.profile_digest,
                    canonical_bytes(profile.body()).decode("utf-8"),
                ),
            )
            row = cursor.fetchone()
            if row is not None:
                return UUID(str(row[0])), True
            cursor.execute(
                "select id from security.permission_profile_revision"
                " where realm_id=%s and profile_digest=%s",
                (self.realm_id, profile.profile_digest),
            )
            return UUID(str(cursor.fetchone()[0])), False

    def latest_profile(self, name: str) -> PermissionProfileRevision:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select id,name,revision,allowed_capabilities,denied_capabilities,managed,"
                "created_at,profile_digest from security.permission_profile_revision"
                " where realm_id=%s and name=%s order by revision desc,id desc limit 1",
                (self.realm_id, name),
            )
            row = cursor.fetchone()
        if row is None:
            raise PolicyViolation("Named permission profile bulunamadi")
        return PermissionProfileRevision(
            UUID(str(row[0])),
            self.realm_id,
            str(row[1]),
            int(row[2]),
            tuple(str(value) for value in row[3]),
            tuple(str(value) for value in row[4]),
            bool(row[5]),
            row[6],
            str(row[7]),
        )

    def store_graph(self, graph: ConfigProvenanceGraph, *, created_at: Any) -> tuple[UUID, bool]:
        graph_id = uuid4()
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into security.config_provenance_snapshot"
                "(id,realm_id,layer_stack,field_decisions,effective_document,effective_digest,"
                "graph_digest,graph_body,created_at,grants_authority)"
                " values(%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s::jsonb,%s,false)"
                " on conflict(realm_id,graph_digest) do nothing returning id",
                (
                    graph_id,
                    self.realm_id,
                    list(graph.layer_stack),
                    canonical_bytes([field.body() for field in graph.fields]).decode("utf-8"),
                    canonical_bytes(graph.effective_document).decode("utf-8"),
                    graph.effective_digest,
                    graph.graph_digest,
                    canonical_bytes(graph.body()).decode("utf-8"),
                    created_at,
                ),
            )
            row = cursor.fetchone()
            if row is not None:
                return UUID(str(row[0])), True
            cursor.execute(
                "select id from security.config_provenance_snapshot"
                " where realm_id=%s and graph_digest=%s",
                (self.realm_id, graph.graph_digest),
            )
            return UUID(str(cursor.fetchone()[0])), False
