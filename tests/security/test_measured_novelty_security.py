"""Canonical measured-loop novelty digest security gates on real PostgreSQL."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

from zekam.domain.canonical import canonical_json, digest

pytestmark = [pytest.mark.security, pytest.mark.postgres]


def _body(objective_digest: str) -> dict[str, str]:
    return {
        "objective_digest": objective_digest,
        "artifact_digest": digest("artifact"),
        "hypothesis_digest": digest("hypothesis"),
        "patch_digest": digest("patch"),
        "failure_signature": digest("failure"),
        "action_semantics_digest": digest("action"),
    }


def test_db_recomputes_novelty_digest_and_rejects_forged_or_extra_components(
    realm_session: tuple[Any, Any],
) -> None:
    _realm, connection = realm_session
    objective_digest = digest("objective")
    body = _body(objective_digest)
    with connection.cursor() as cursor:
        cursor.execute(
            "select runtime.assert_loop_novelty_body(%s::jsonb,%s,%s),"
            " continuity.jsonb_digest(%s::jsonb)",
            (canonical_json(body), digest(body), objective_digest, canonical_json(body)),
        )
        assert cursor.fetchone()[1] == digest(body)
    for forged_body, supplied_digest, expected_objective in (
        (body, digest("forged-supplied"), objective_digest),
        ({**body, "raw_prompt": digest("forbidden-extra")}, digest(body), objective_digest),
        (body, digest(body), digest("different-objective")),
        (
            {**body, "hypothesis_digest": None},
            digest({**body, "hypothesis_digest": None}),
            objective_digest,
        ),
    ):
        with (
            pytest.raises(
                psycopg.Error,
                match="supplied digest canonical body ile uyusmuyor",
            ),
            connection.transaction(),
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "select runtime.assert_loop_novelty_body(%s::jsonb,%s,%s)",
                (canonical_json(forged_body), supplied_digest, expected_objective),
            )
