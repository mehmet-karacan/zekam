"""P10-T05 gercek alt surec istemcisiyle adapter uctan uca akisi.

Mock yoktur: `tests/fixtures/fake_client.py` gercek bir surec olarak calisir.
"""

from __future__ import annotations

import sys
from pathlib import Path
from uuid import uuid4

import pytest

from zekam.domain.canonical import digest
from zekam.domain.clients import (
    ClientDescriptor,
    ClientKind,
    DispatchOutcome,
    DispatchRequest,
    _issue_canonical_dispatch_permit,
)
from zekam.domain.errors import PolicyViolation
from zekam.infrastructure.clients.adapters import SubprocessClientAdapter

pytestmark = pytest.mark.e2e

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "fake_client.py"


def _adapter(mode: str, *, capabilities: frozenset[str] | None = None) -> SubprocessClientAdapter:
    return SubprocessClientAdapter(
        ClientDescriptor(
            kind=ClientKind.INTERNAL,
            client_id="sahte-istemci",
            executable=str(FIXTURE),
            capabilities=capabilities
            or frozenset({"chat", "code", "structured-result", "cancellation"}),
        ),
        launcher=(sys.executable,),
        env=(("ZEKAM_FAKE_CLIENT_MODE", mode),),
    )


def _request(timeout: int = 30) -> DispatchRequest:
    return DispatchRequest(
        assignment_id=uuid4(),
        invocation_id=uuid4(),
        client_id="sahte-istemci",
        role="researcher",
        instruction_digest=digest("talimat"),
        context_manifest_digest=digest("manifest"),
        timeout_seconds=timeout,
    )


def _dispatch(adapter, request: DispatchRequest, cwd: Path):  # type: ignore[no-untyped-def]
    return adapter.dispatch(
        request,
        cwd=cwd,
        permit=_issue_canonical_dispatch_permit(request.assignment_id, request.invocation_id),
    )


def test_basarili_dispatch_strict_envelope_dondurur(tmp_path: Path) -> None:
    request = _request()
    result = _dispatch(_adapter("success"), request, tmp_path)
    assert result.outcome is DispatchOutcome.SUCCESS
    assert result.payload["finding_count"] == 2
    assert result.payload["instruction_digest"] == request.instruction_digest
    assert result.as_dict()["grants_authority"] is False


def test_bozuk_json_sessizce_kabul_edilmez(tmp_path: Path) -> None:
    request = _request()
    result = _dispatch(_adapter("bad-json"), request, tmp_path)
    assert result.outcome is DispatchOutcome.FAILED
    assert result.failure_category == "unparsable-result"


def test_bilinmeyen_outcome_failed_olur(tmp_path: Path) -> None:
    request = _request()
    result = _dispatch(_adapter("unknown-outcome"), request, tmp_path)
    assert result.outcome is DispatchOutcome.FAILED
    assert result.failure_category == "unknown-outcome"


def test_istemci_hatasi_gorunur_kalir(tmp_path: Path) -> None:
    request = _request()
    result = _dispatch(_adapter("failed"), request, tmp_path)
    assert result.outcome is DispatchOutcome.FAILED
    assert result.exit_code == 2
    assert result.failure_category == "adapter"


def test_asili_kalan_istemci_timeout_ile_iptal_edilir(tmp_path: Path) -> None:
    request = _request(timeout=1)
    result = _dispatch(_adapter("hang"), request, tmp_path)
    assert result.outcome is DispatchOutcome.TIMED_OUT
    assert result.failure_category == "timeout"


@pytest.mark.parametrize(
    "mode,category",
    [
        ("success-nonzero", "process-exit"),
        ("spoof-exit", "exit-status-drift"),
        ("duplicate-json", "unparsable-result"),
        ("invalid-utf8", "invalid-encoding"),
        ("stderr-overflow", "output-limit"),
    ],
)
def test_process_evidence_cannot_be_overridden_by_success_json(
    tmp_path: Path, mode: str, category: str
) -> None:
    result = _dispatch(_adapter(mode), _request(), tmp_path)
    assert result.outcome is DispatchOutcome.FAILED
    assert result.failure_category == category
    assert result.payload == {}


def test_iptal_beyani_olmayan_istemcide_timeout_gorunur_hata_verir(tmp_path: Path) -> None:
    adapter = _adapter("hang", capabilities=frozenset({"chat", "structured-result"}))
    with pytest.raises(PolicyViolation):
        request = _request(timeout=1)
        _dispatch(adapter, request, tmp_path)
