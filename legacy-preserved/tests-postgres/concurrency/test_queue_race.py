"""Kuyruk yaris testleri.

Sozlesme kabul kriterleri (`harness/ORKESTRASYON_DAG_QUEUE_LEASE_FENCING.md`):

- 20 worker ayni job'a kosar, tek claim olur
- eski fence ile complete reddedilir
- parent/child path catisir
- farkli projeler paralel calisir
- crash-after-claim recovery-required olur
- yinelenen enqueue tek job uretir
"""

from __future__ import annotations

import datetime as dt
import secrets
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from zekam.application.config import DatabaseSettings
from zekam.application.execution import ExecutionHost
from zekam.application.project_integration import ProjectIntegrationService
from zekam.application.realm_context import bootstrap_realm
from zekam.domain.realm import Realm
from zekam.domain.resources import parse_requests
from zekam.domain.runtime import AttemptOutcome, Job, JobKind, JobState
from zekam.infrastructure.postgres.connection import configure_session, connect

pytestmark = [pytest.mark.concurrency, pytest.mark.postgres]

WORKER_COUNT = 20
DIGEST = "sha256:" + "a" * 64


@pytest.fixture
def arena(migrated_database: DatabaseSettings, tmp_path: Path) -> Iterator[dict[str, Any]]:
    """Ayni realm ve projeye bagli cok sayida worker baglantisi."""
    slug = f"kuyruk-{secrets.token_hex(4)}"
    managers: list[Any] = []
    connections: list[Any] = []
    with connect(migrated_database) as primary:
        realm = bootstrap_realm(primary, slug=slug).realm
        root = tmp_path / "kaynak"
        root.mkdir()
        project = ProjectIntegrationService(primary, realm).register(source_path=root)

        try:
            for _ in range(WORKER_COUNT):
                manager = connect(migrated_database)
                connection = manager.__enter__()
                configure_session(connection, realm_id=realm.id)
                managers.append(manager)
                connections.append(connection)
            yield {
                "realm": realm,
                "project_id": project.id,
                "primary": primary,
                "connections": connections,
            }
        finally:
            for manager in managers:
                manager.__exit__(None, None, None)


def _job(realm: Realm, project_id: Any, **overrides: Any) -> Job:
    defaults: dict[str, Any] = {
        "realm_id": realm.id,
        "project_id": project_id,
        "kind": JobKind.MUTATION,
        "idempotency_key": f"job-{secrets.token_hex(4)}",
        "resources": parse_requests(write=("path:zekam:a.py",)),
        "required_capabilities": ("sandbox.write",),
    }
    defaults.update(overrides)
    return Job.create(**defaults)


def test_twenty_workers_race_and_only_one_claims(arena: dict[str, Any]) -> None:
    realm, project_id = arena["realm"], arena["project_id"]
    primary = arena["primary"]
    job, _ = ExecutionHost(primary, realm.id).jobs.enqueue(_job(realm, project_id))

    claims = []
    for index, connection in enumerate(arena["connections"]):
        host = ExecutionHost(connection, realm.id, worker_label=f"worker-{index}")
        claimed = host.jobs.claim_next(
            worker_label=f"worker-{index}", capabilities=("sandbox.write",)
        )
        if claimed is not None:
            claims.append(claimed)

    assert len(claims) == 1
    assert claims[0].job.id == job.id
    assert claims[0].lease.fencing_token == 1


def test_twenty_workers_share_twenty_jobs_without_duplication(arena: dict[str, Any]) -> None:
    realm, project_id = arena["realm"], arena["project_id"]
    primary_host = ExecutionHost(arena["primary"], realm.id)
    for index in range(WORKER_COUNT):
        primary_host.jobs.enqueue(
            _job(
                realm,
                project_id,
                idempotency_key=f"paralel-{index}",
                resources=parse_requests(write=(f"path:zekam:{index}.py",)),
            )
        )

    claimed_ids: list[Any] = []
    for index, connection in enumerate(arena["connections"]):
        host = ExecutionHost(connection, realm.id, worker_label=f"worker-{index}")
        work = host.jobs.claim_next(worker_label=f"worker-{index}", capabilities=("sandbox.write",))
        if work is not None:
            claimed_ids.append(work.job.id)

    assert len(claimed_ids) == WORKER_COUNT
    assert len(set(claimed_ids)) == WORKER_COUNT


def test_stale_fence_cannot_complete_the_job(arena: dict[str, Any]) -> None:
    realm, project_id = arena["realm"], arena["project_id"]
    primary = arena["primary"]
    host = ExecutionHost(primary, realm.id)
    host.jobs.enqueue(_job(realm, project_id))

    first = host.jobs.claim_next(worker_label="worker-a", capabilities=("sandbox.write",))
    assert first is not None

    # Lease suresi doldu ve is yeniden kuyruga dondu.
    later = dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)
    host.jobs.reclaim_expired(now=later)

    second = host.jobs.claim_next(
        worker_label="worker-b", capabilities=("sandbox.write",), now=later
    )
    assert second is not None
    assert second.lease.fencing_token == first.lease.fencing_token + 1

    # Eski sahip artik sonuc yayimlayamaz.
    assert not host.jobs.complete(
        first.job.id,
        token=first.owner_token,
        fencing_token=first.lease.fencing_token,
        outcome=AttemptOutcome.SUCCEEDED,
        result_digest=DIGEST,
    )
    assert not host.jobs.heartbeat(
        first.job.id, token=first.owner_token, fencing_token=first.lease.fencing_token
    )
    assert host.jobs.get(first.job.id).state is JobState.RUNNING


def test_parent_and_child_paths_cannot_run_together(arena: dict[str, Any]) -> None:
    realm, project_id = arena["realm"], arena["project_id"]
    first_host = ExecutionHost(arena["connections"][0], realm.id, worker_label="worker-a")
    second_host = ExecutionHost(arena["connections"][1], realm.id, worker_label="worker-b")

    first_host.jobs.enqueue(
        _job(
            realm,
            project_id,
            idempotency_key="ust",
            priority=10,
            resources=parse_requests(write=("path:zekam:src",)),
        )
    )
    first_host.jobs.enqueue(
        _job(
            realm,
            project_id,
            idempotency_key="alt",
            priority=20,
            resources=parse_requests(write=("path:zekam:src/inner.py",)),
        )
    )

    first = first_host.acquire_work(capabilities=("sandbox.write",))
    assert first is not None
    with pytest.raises(Exception, match="kilit catismasi"):
        second_host.acquire_work(capabilities=("sandbox.write",))


def test_different_projects_run_in_parallel(
    arena: dict[str, Any], migrated_database: DatabaseSettings, tmp_path: Path
) -> None:
    realm, project_id = arena["realm"], arena["project_id"]
    primary = arena["primary"]
    other_root = tmp_path / "digeri"
    other_root.mkdir()
    other = ProjectIntegrationService(primary, realm).register(
        source_path=other_root, slug="digeri"
    )

    host = ExecutionHost(primary, realm.id)
    host.jobs.enqueue(
        _job(
            realm,
            project_id,
            idempotency_key="birinci",
            resources=parse_requests(write=("path:zekam:a.py",)),
        )
    )
    host.jobs.enqueue(
        _job(
            realm,
            other.id,
            idempotency_key="ikinci",
            resources=parse_requests(write=("path:digeri:a.py",)),
        )
    )

    first_host = ExecutionHost(arena["connections"][0], realm.id, worker_label="worker-a")
    second_host = ExecutionHost(arena["connections"][1], realm.id, worker_label="worker-b")
    first = first_host.acquire_work(capabilities=("sandbox.write",))
    second = second_host.acquire_work(capabilities=("sandbox.write",))

    assert first is not None
    assert second is not None
    assert first.job.id != second.job.id


def test_crash_after_claim_becomes_recovery_required(arena: dict[str, Any]) -> None:
    realm, project_id = arena["realm"], arena["project_id"]
    host = ExecutionHost(arena["primary"], realm.id, worker_label="worker-a")
    host.jobs.enqueue(_job(realm, project_id))
    work = host.acquire_work(capabilities=("sandbox.write",), lease_seconds=1)
    assert work is not None

    # Effect claim yazildi, ardindan worker cokuyor: receipt yok.
    host.claim_effect(
        work,
        operation="file-write",
        effect_digest=DIGEST,
        authorization_digest=DIGEST,
        resources=parse_requests(write=("path:zekam:a.py",)),
        adapter_digest=DIGEST,
    )

    later = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=5)
    host.jobs.reclaim_expired(now=later)

    assert host.jobs.get(work.job.id).state is JobState.RECOVERY_REQUIRED
    # Yeniden claim edilemez: is artik ready degil.
    assert (
        host.jobs.claim_next(worker_label="worker-b", capabilities=("sandbox.write",), now=later)
        is None
    )


def test_duplicate_enqueue_from_two_connections_creates_one_job(
    arena: dict[str, Any],
) -> None:
    realm, project_id = arena["realm"], arena["project_id"]
    first_host = ExecutionHost(arena["connections"][0], realm.id)
    second_host = ExecutionHost(arena["connections"][1], realm.id)

    first, created_first = first_host.jobs.enqueue(_job(realm, project_id, idempotency_key="tek"))
    second, created_second = second_host.jobs.enqueue(
        _job(realm, project_id, idempotency_key="tek")
    )

    assert created_first
    assert not created_second
    assert first.id == second.id
