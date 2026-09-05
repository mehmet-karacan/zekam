from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from psycopg import Error as PsycopgError

from zekam.application.diagnostic_trace import (
    AesGcmTraceCipher,
    DiagnosticTraceReducer,
    DiagnosticTraceRetentionService,
    DiagnosticTraceWriter,
)
from zekam.application.work_graph import WorkGraphService
from zekam.domain.canonical import canonical_bytes
from zekam.domain.diagnostic_trace import (
    DiagnosticTracePolicy,
    TraceBundle,
    TraceEventType,
    TraceVisibility,
)
from zekam.domain.project import Project
from zekam.domain.work import WorkType
from zekam.infrastructure.postgres.diagnostic_trace_repository import (
    PostgresDiagnosticTraceRepository,
)
from zekam.infrastructure.postgres.project_repository import ProjectRepository
from zekam.infrastructure.storage.local_cas import LocalContentAddressedStore

pytestmark = [pytest.mark.integration, pytest.mark.postgres]
NOW = dt.datetime(2026, 8, 25, 8, tzinfo=dt.UTC)
KEY = b"d" * 32


def _runtime(
    realm: Any,
    connection: Any,
    tmp_path: Path,
    *,
    now: dt.datetime = NOW,
    retention_days: int = 7,
    policy: DiagnosticTracePolicy | None = None,
):  # type: ignore[no-untyped-def]
    repository = PostgresDiagnosticTraceRepository(connection, realm.id)
    store = LocalContentAddressedStore(tmp_path / "encrypted-trace").ensure()
    cipher = AesGcmTraceCipher(os.urandom)
    policy = policy or DiagnosticTracePolicy(
        enabled=True,
        retention_days=retention_days,
        encryption_key_ref="secretref:diagnostic-trace-key-v1",
        export_allowed=True,
    )
    writer = DiagnosticTraceWriter(repository, store, cipher, lambda _: KEY)
    bundle = writer.open_bundle(
        realm_id=realm.id,
        trace_ref=f"trace-{uuid4()}",
        policy=policy,
        project_id=None,
        work_item_id=None,
        run_id=None,
        root_assignment_id=None,
        root_client_session_id="postgres-session",
        now=now,
    )
    assert bundle is not None
    return repository, store, cipher, policy, writer, bundle


def test_plaintext_quota_boundary_matches_writer_and_database(
    realm_session: tuple[Any, Any], tmp_path: Path
) -> None:
    realm, connection = realm_session
    payload = {"content": "x" * 256}
    plain_size = len(canonical_bytes(payload))
    boundary_policy = DiagnosticTracePolicy(
        enabled=True,
        max_payload_bytes=plain_size,
        max_events=1,
        max_total_bytes=plain_size,
        encryption_key_ref="secretref:diagnostic-trace-key-v1",
    )
    repository, store, _, policy, writer, bundle = _runtime(
        realm, connection, tmp_path, policy=boundary_policy
    )

    result = writer.write(
        bundle=bundle,
        policy=policy,
        event_type=TraceEventType.MODEL_REQUEST,
        visibility=TraceVisibility.MODEL_VISIBLE,
        payload=payload,
        correlation={"session": "quota-boundary"},
        occurred_at=NOW,
    )
    assert result.state == "recorded"
    assert repository.usage(bundle.id) == (1, plain_size)
    with connection.cursor() as cursor:
        cursor.execute(
            "select plain_size_bytes,cipher_size_bytes from diagnostics.payload_ref"
            " where realm_id=%s and trace_id=%s",
            (realm.id, bundle.id),
        )
        plain, cipher = cursor.fetchone()
    assert plain == plain_size
    assert cipher == len(store.get(repository.list_events(bundle.id)[0].payload_ref))
    assert cipher > plain


def test_encrypted_payload_precedes_contiguous_metadata_and_reduces_deterministically(
    realm_session: tuple[Any, Any], tmp_path: Path
) -> None:
    realm, connection = realm_session
    repository, store, cipher, policy, writer, bundle = _runtime(realm, connection, tmp_path)
    first = writer.write(
        bundle=bundle,
        policy=policy,
        event_type=TraceEventType.MODEL_REQUEST,
        visibility=TraceVisibility.MODEL_VISIBLE,
        payload={"content": "provider-visible", "password": "never-persist"},
        correlation={"session": "postgres-session"},
        occurred_at=NOW,
    )
    writer.write(
        bundle=bundle,
        policy=policy,
        event_type=TraceEventType.AGENT_SPAWN,
        visibility=TraceVisibility.RUNTIME_ONLY,
        payload={"agent": "child-one"},
        correlation={"session": "postgres-session", "parent_event_id": str(first.event_id)},
        occurred_at=NOW + dt.timedelta(seconds=1),
    )
    events = repository.list_events(bundle.id)
    assert [event.sequence for event in events] == [1, 2]
    assert events[0].previous_event_digest is None
    assert events[1].previous_event_digest == events[0].event_digest
    with connection.cursor() as cursor:
        cursor.execute(
            "select count(*),bool_and(not grants_authority) from diagnostics.trace_event"
            " where realm_id=%s and trace_id=%s",
            (realm.id, bundle.id),
        )
        assert cursor.fetchone() == (2, True)
        cursor.execute(
            "select column_name from information_schema.columns"
            " where table_schema='diagnostics' and table_name in ('trace_event','payload_ref')"
            " and column_name in ('payload','payload_bytes','plaintext')"
        )
        assert cursor.fetchall() == []
    assert b"never-persist" not in store.get(events[0].payload_ref)

    repository.close(bundle.id)
    bundle = repository.get_bundle(bundle.id)
    reducer = DiagnosticTraceReducer(repository, store, cipher, lambda _: KEY)
    first_reduction = reducer.reduce(
        bundle,
        reduced_at=NOW + dt.timedelta(minutes=1),
        authorization_ref="auth:test-reduce",
    )
    second_reduction = reducer.reduce(
        bundle,
        reduced_at=NOW + dt.timedelta(minutes=2),
        authorization_ref="auth:test-reduce",
    )
    assert first_reduction.output_digest == second_reduction.output_digest
    with connection.cursor() as cursor:
        cursor.execute(
            "select count(*) from diagnostics.reduction where realm_id=%s and trace_id=%s",
            (realm.id, bundle.id),
        )
        assert cursor.fetchone()[0] == 1
        cursor.execute(
            "select reduced_body=diagnostics.expected_reduction_body(trace_id)"
            " from diagnostics.reduction where realm_id=%s and trace_id=%s",
            (realm.id, bundle.id),
        )
        assert cursor.fetchone()[0] is True
        cursor.execute(
            "select has_table_privilege(current_user,'diagnostics.payload_ref','insert'),"
            "has_table_privilege(current_user,'diagnostics.trace_event','insert'),"
            "has_table_privilege(current_user,'diagnostics.reduction','insert')"
        )
        assert cursor.fetchone() == (False, False, False)
        cursor.execute(
            "select operation,count(*) from diagnostics.access_event"
            " where realm_id=%s and trace_id=%s group by operation order by operation",
            (realm.id, bundle.id),
        )
        assert cursor.fetchall() == [("read", 3), ("reduce", 2)]

    with (
        pytest.raises(PsycopgError, match="direct memory candidate"),
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "insert into memory.candidate"
            "(id,realm_id,scope,memory_class,content,author_ref,evidence,created_at)"
            " values(%s,%s,'global-user','semantic','forged trace memory','actor',"
            "%s::jsonb,%s)",
            (
                uuid4(),
                realm.id,
                '[{"kind":"diagnostic-trace","reference":"db:diagnostics.trace/one"}]',
                NOW,
            ),
        )
    connection.rollback()


def test_database_rejects_forged_sequence_digest_and_model_visibility(
    realm_session: tuple[Any, Any], tmp_path: Path
) -> None:
    realm, connection = realm_session
    repository, _, _, policy, writer, bundle = _runtime(realm, connection, tmp_path)
    writer.write(
        bundle=bundle,
        policy=policy,
        event_type=TraceEventType.ERROR,
        visibility=TraceVisibility.DIAGNOSTIC_ONLY,
        payload={"category": "safe"},
        correlation={"session": "postgres-session"},
        occurred_at=NOW,
    )
    with connection.cursor() as cursor:
        cursor.execute(
            "select id from diagnostics.payload_ref where realm_id=%s and trace_id=%s",
            (realm.id, bundle.id),
        )
        payload_id = cursor.fetchone()[0]
    with pytest.raises(PsycopgError), connection.cursor() as cursor:
        cursor.execute(
            "insert into diagnostics.trace_event"
            "(id,realm_id,trace_id,sequence,event_type,visibility,occurred_at,correlation,"
            "payload_ref_id,previous_event_digest,event_digest,event_body,grants_authority)"
            " values(%s,%s,%s,99,'tool-result','model-visible',%s,%s::jsonb,%s,null,%s,"
            "%s::jsonb,false)",
            (
                uuid4(),
                realm.id,
                bundle.id,
                NOW,
                '{"session":"postgres-session"}',
                payload_id,
                "sha256:" + "0" * 64,
                "{}",
            ),
        )
    connection.rollback()
    assert repository.usage(bundle.id)[0] == 1


def test_trace_backend_failure_is_best_effort_and_leaves_no_canonical_mutation(
    realm_session: tuple[Any, Any], tmp_path: Path
) -> None:
    realm, connection = realm_session
    repository, store, _, policy, writer, bundle = _runtime(realm, connection, tmp_path)
    with connection.cursor() as cursor:
        cursor.execute("select count(*) from work.work_item where realm_id=%s", (realm.id,))
        work_before = cursor.fetchone()[0]
        cursor.execute("select count(*) from runtime.effect_claim where realm_id=%s", (realm.id,))
        claims_before = cursor.fetchone()[0]

    # Bos correlation DB triggerinda reddedilir; encrypted CAS object orphan kalabilir,
    # fakat diagnostic metadata ve canonical Work/effect state'i degismez.
    result = writer.write_best_effort(
        bundle=bundle,
        policy=policy,
        event_type=TraceEventType.ERROR,
        visibility=TraceVisibility.DIAGNOSTIC_ONLY,
        payload={"category": "trace-only"},
        correlation={},
        occurred_at=NOW,
    )
    assert result.state == "failed"
    assert repository.usage(bundle.id) == (0, 0)
    assert len(tuple(store.iter_objects())) == 1
    with connection.cursor() as cursor:
        cursor.execute("select count(*) from work.work_item where realm_id=%s", (realm.id,))
        assert cursor.fetchone()[0] == work_before
        cursor.execute("select count(*) from runtime.effect_claim where realm_id=%s", (realm.id,))
        assert cursor.fetchone()[0] == claims_before


def test_expired_trace_purge_deletes_cas_and_records_authorized_receipt(
    realm_session: tuple[Any, Any], tmp_path: Path
) -> None:
    realm, connection = realm_session
    created_at = NOW - dt.timedelta(days=2)
    repository, store, _, _policy, _writer, bundle = _runtime(
        realm,
        connection,
        tmp_path,
        now=created_at,
        retention_days=1,
    )
    result = DiagnosticTraceRetentionService(repository, store).purge_expired(
        now=NOW,
        authorization_ref="auth:purge-expired-test",
    )
    assert result.purged_trace_ids == (bundle.id,)
    assert result.deleted_payload_count == 0
    assert tuple(store.iter_objects()) == ()
    with connection.cursor() as cursor:
        cursor.execute(
            "select state,purged_at,purge_receipt_digest from diagnostics.trace_bundle"
            " where realm_id=%s and id=%s",
            (realm.id, bundle.id),
        )
        state, purged_at, receipt = cursor.fetchone()
        assert state == "purged" and purged_at == NOW
        assert str(receipt).startswith("sha256:")
        cursor.execute(
            "select authorization_ref from diagnostics.access_event"
            " where realm_id=%s and trace_id=%s and operation='purge'",
            (realm.id, bundle.id),
        )
        assert cursor.fetchone()[0] == "auth:purge-expired-test"


def test_bundle_rejects_cross_project_work_causal_scope(
    realm_session: tuple[Any, Any],
) -> None:
    realm, connection = realm_session
    projects = ProjectRepository(connection, realm.id)
    project_one = projects.add(Project.create(realm=realm, slug="trace-one", now=NOW))
    project_two = projects.add(
        Project.create(realm=realm, slug="trace-two", now=NOW + dt.timedelta(microseconds=1))
    )
    work = WorkGraphService(connection, realm).create_item(
        project_id=project_two.id,
        type=WorkType.TASK,
        title="Foreign trace work",
        now=NOW + dt.timedelta(seconds=1),
    )
    policy = DiagnosticTracePolicy(enabled=True, encryption_key_ref="secretref:trace")
    bundle = TraceBundle(
        id=uuid4(),
        realm_id=realm.id,
        trace_ref=f"trace-scope-{uuid4()}",
        project_id=project_one.id,
        work_item_id=work.id,
        run_id=None,
        root_assignment_id=None,
        root_client_session_id="scope-test",
        policy=policy,
        created_at=NOW + dt.timedelta(seconds=2),
        expires_at=NOW + dt.timedelta(days=7, seconds=2),
    )
    with pytest.raises(PsycopgError, match="work/project causal binding drift"):
        PostgresDiagnosticTraceRepository(connection, realm.id).create_bundle(bundle)
    connection.rollback()
