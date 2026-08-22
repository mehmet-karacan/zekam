"""P10-T05 gercek alt surec istemcisiyle adapter uctan uca akisi.

Mock yoktur: `tests/fixtures/fake_client.py` gercek bir surec olarak calisir.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from zekam.domain.canonical import digest
from zekam.domain.clients import ClientDescriptor, ClientKind, DispatchOutcome, DispatchRequest
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
        client_id="sahte-istemci",
        role="researcher",
        instruction_digest=digest("talimat"),
        context_manifest_digest=digest("manifest"),
        timeout_seconds=timeout,
    )


def test_basarili_dispatch_strict_envelope_dondurur(tmp_path: Path) -> None:
    request = _request()
    result = _adapter("success").dispatch(request, cwd=tmp_path)
    assert result.outcome is DispatchOutcome.SUCCESS
    assert result.payload["finding_count"] == 2
    assert result.payload["instruction_digest"] == request.instruction_digest
    assert result.as_dict()["grants_authority"] is False


def test_bozuk_json_sessizce_kabul_edilmez(tmp_path: Path) -> None:
    result = _adapter("bad-json").dispatch(_request(), cwd=tmp_path)
    assert result.outcome is DispatchOutcome.FAILED
    assert result.failure_category == "unparsable-result"


def test_bilinmeyen_outcome_failed_olur(tmp_path: Path) -> None:
    result = _adapter("unknown-outcome").dispatch(_request(), cwd=tmp_path)
    assert result.outcome is DispatchOutcome.FAILED
    assert result.failure_category == "unknown-outcome"


def test_istemci_hatasi_gorunur_kalir(tmp_path: Path) -> None:
    result = _adapter("failed").dispatch(_request(), cwd=tmp_path)
    assert result.outcome is DispatchOutcome.FAILED
    assert result.exit_code == 2
    assert result.failure_category == "adapter"


def test_asili_kalan_istemci_timeout_ile_iptal_edilir(tmp_path: Path) -> None:
    result = _adapter("hang").dispatch(_request(timeout=1), cwd=tmp_path)
    assert result.outcome is DispatchOutcome.TIMED_OUT
    assert result.failure_category == "timeout"


def test_iptal_beyani_olmayan_istemcide_timeout_gorunur_hata_verir(tmp_path: Path) -> None:
    adapter = _adapter("hang", capabilities=frozenset({"chat", "structured-result"}))
    with pytest.raises(PolicyViolation):
        adapter.dispatch(_request(timeout=1), cwd=tmp_path)
