"""Real production CLI delivery, not the composed-close test-only bookkeeping sink."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import pytest
from tests.integration.test_composed_close_pipeline import (
    _compile_and_deliver,
)
from tests.integration.test_composed_close_pipeline import (
    before_freeze as before_freeze,
)
from tests.integration.test_composed_close_pipeline import (
    frozen as frozen,
)
from tests.unit.test_local_continuity_environment import environment as environment
from tests.unit.test_local_startup_composition import composition as composition
from typer.testing import CliRunner

from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation
from zekam.interfaces.cli.main import app

pytestmark = pytest.mark.integration


def test_production_cli_bookkeeping_delivery_unblocks_exact_composed_close(
    frozen: dict[str, Any],
) -> None:
    value = frozen
    binding, request = value["binding"], value["request"]
    _compile_and_deliver(value)
    with pytest.raises(PolicyViolation, match="pending"):
        value["service"].finalize(binding, request.request_digest)
    with sqlite3.connect(value["path"]) as db:
        rows = db.execute(
            "select id,idempotency_key,payload_digest,event_kind from local_outbox"
            " where job_id=? and event_kind<>'continuity.compile' order by id",
            (request.job_id,),
        ).fetchall()
    assert {row[3] for row in rows} == {"job.enqueued", "job.completed"}
    assert len(rows) == 2
    delivered = []
    for _ in range(3):
        result = CliRunner().invoke(
            app, ["local-runtime", "outbox-once", "--home", str(value["home"])]
        )
        assert result.exit_code == 0, result.output
        delivered.append(json.loads(result.stdout)["claimed_outbox_id"])
    assert set(delivered[:2]) == {row[0] for row in rows}
    assert delivered[2] is None
    journal = value["home"] / "runtime/local-effects/outbox-delivery.journal"
    first_bytes = journal.read_bytes()
    assert sorted(first_bytes.splitlines()) == sorted(
        f"{key}\t{payload_digest}".encode() for _, key, payload_digest, _ in rows
    )
    with sqlite3.connect(value["path"]) as db:
        for event_id, key, payload_digest, _ in rows:
            assert db.execute(
                "select status,evidence_digest from local_outbox_receipt where outbox_id=?",
                (event_id,),
            ).fetchone() == (
                "delivered",
                digest({"idempotency_key": key, "payload_digest": payload_digest}),
            )
    receipt = value["service"].finalize(binding, request.request_digest)
    assert value["service"].finalize(binding, request.request_digest) == receipt
    assert value["store"].load(binding, request.request_digest).state == "complete"
    assert journal.read_bytes() == first_bytes
    # This proves local observation delivery, not external publication or native hooks.
