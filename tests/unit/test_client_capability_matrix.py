"""WP-02.02 — Surum bazli, istemci-bazli native yetenek destegi matrisi.

`ClientCapabilityProfile` her istemci icin metadata/instruction/hook/MCP/shell/
subagent destegini surum + kanit seviyesi + destek siniri olarak AYRI ayri kaydeder.

Kanit duzeni (bu hostta yalniz OpenCode/Codex kurulu):
  * `version`/`version_basis` = Zekam tarafinin GENERATOR (reviewed) surumudur;
    hosttaki native kurulumdan BAYRAK ve surum olarak ayri tutulur.
  * `host_present`/`host_version` = bu hostta gozlenen native CLI kanitidir.
  * `ready` ancak `observed` kanitla; `observed` kaniti yalniz host kanitiyla
    tasinir. Hostta kurulu olmayan istemci (Claude/Gemini) hicbir hucre
    `ready`/`observed` iddia edemez — generator kanitli hucreler `declared`/
    `unobserved` kalir ve acik destek siniri korunur (NL-039 / GAP-07).
"""

from __future__ import annotations

from typing import Any

import pytest

from zekam.application.capability_profile import (
    SUPPORTED_CAPABILITIES,
    CapabilityStatus,
    ClientCapabilityProfile,
    client_capability_matrix,
)

pytestmark = pytest.mark.unit

_READY = CapabilityStatus.READY.value
_DECLARED = CapabilityStatus.DECLARED.value
_NOT_SUPPORTED = CapabilityStatus.NOT_SUPPORTED.value
_PARTIAL = CapabilityStatus.PARTIAL.value


def _matrix(**observed):
    return client_capability_matrix(observed_hosts=observed or None)


def _profile(matrix, client: str) -> ClientCapabilityProfile:
    return next(p for p in matrix.clients if p.client == client)


def _cells(profile: ClientCapabilityProfile) -> dict[str, Any]:
    return {item.capability: item for item in profile.capabilities}


def test_matrix_covers_all_four_supported_clients() -> None:
    matrix = _matrix()
    assert {item.client for item in matrix.clients} == {
        "opencode", "codex", "claude-code", "gemini"
    }


def test_matrix_schema_is_versioned_and_read_only() -> None:
    matrix = _matrix()
    assert matrix.schema.startswith("zekam-client-capability-matrix/")
    assert matrix.schema.endswith("/v2")
    assert matrix.read_only is True
    assert matrix.grants_authority is False
    assert matrix.matrix_digest.startswith("sha256:")


def test_every_cell_is_versioned_with_evidence_level_and_boundary() -> None:
    matrix = _matrix(opencode="v2.0.27", codex="codex-cli 0.162.0")
    for profile in matrix.clients:
        assert profile.version  # generator (reviewed) surumu
        assert profile.version_basis  # surumun kanit/beyan kaynagi
        assert profile.support_limit  # acik destek siniri
        assert profile.adapter_plan is not None
        assert len(profile.adapter_plan) >= 1
        for support in profile.capabilities:
            assert support.capability in SUPPORTED_CAPABILITIES
            assert support.status in CapabilityStatus.all()
            assert support.support_limit  # acik destek siniri; komut ezberi degil
            # Destek yoksa destek siniri acik; komut ezberi degil.
            if support.status == _NOT_SUPPORTED:
                assert support.support_limit
            # Kanitsiz `ready` iddia edilmez: `ready` ancak `observed` kanitla.
            if support.status == _READY:
                assert support.evidence_level == "observed"


def test_supported_capability_dimensions_are_the_task_contract_set() -> None:
    assert set(SUPPORTED_CAPABILITIES) == {
        "metadata",
        "instruction",
        "hook",
        "mcp",
        "shell",
        "subagent",
    }


def test_metadata_is_reported_separately_with_explicit_boundary() -> None:
    """`metadata` boyutu istemci kimligi/surumunu ayri hucre olarak raporlar.

    Hucre native metadata (host CLI surumu) kanitini `host_version` alanindan
    ayri tutar; acceptance kosusuna kadar `declared` kalir, `ready` iddia etmez.
    """
    matrix = _matrix(opencode="opencode v2.0.27")
    for profile in matrix.clients:
        metadata = _cells(profile)["metadata"]
        assert metadata.status == _DECLARED
        assert metadata.evidence_level != "observed"
        assert metadata.support_limit
        assert metadata.version


def test_matrix_cells_are_deterministic() -> None:
    assert _matrix().matrix_digest == _matrix().matrix_digest
    assert _matrix(opencode="v1").matrix_digest == _matrix(opencode="v1").matrix_digest


def test_matrix_digest_changes_when_host_evidence_changes() -> None:
    # Host kaniti matris govdesine girer: ayni generator surumu, farkli host.
    assert _matrix().matrix_digest != _matrix(opencode="v2.0.27").matrix_digest
    assert _matrix(opencode="v2.0.27").matrix_digest != _matrix(opencode="v2.0.28").matrix_digest


def test_matrix_digest_changes_when_support_changes() -> None:
    base = _matrix()
    drifted = _shift(base, client="codex", capability="hook", to=CapabilityStatus.NOT_SUPPORTED)
    assert base.matrix_digest != drifted.matrix_digest


def test_absent_host_claims_no_ready_and_records_no_host_version() -> None:
    """Bu hostta kurulu olmayan Claude/Gemini `ready`+`observed` iddia etmez.

    Generator surumu yine de `reviewed-generator` bazinda kalir; host kaniti
    (`host_present`/`host_version`) ayri alanda yok olarak raporlanir.
    """
    matrix = _matrix()  # hic host kaniti yok

    for client in ("claude-code", "gemini", "opencode", "codex"):
        profile = _profile(matrix, client)
        assert profile.host_present is False
        assert profile.host_version is None
        # Generator surumu host kaniti yoksa da tanimli ve reviewed-generator.
        assert profile.version
        assert profile.version_basis == "reviewed-generator"
        for cell in profile.capabilities:
            # Hiçbir hücre `ready`+`observed` iddia edemez.
            assert not (cell.status == _READY and cell.evidence_level == "observed")
            # Host kaniti yoksa generator kanitli hücreler `declared`/`unobserved`.
            if cell.evidence_level == "observed":
                pytest.fail(f"{client}.{cell.capability} host kanitsiz observed tasiyor")


def test_present_host_remains_declared_until_native_acceptance() -> None:
    """Binary varligi native davranis kabulunun yerine gecmez.

    `host_version` gozlenen native surumdur ve generator `version` alanindan
    BAYRAK ve surum olarak ayri tutulur.
    """
    matrix = _matrix(opencode="opencode v2.0.27")
    opencode = _profile(matrix, "opencode")

    assert opencode.host_present is True
    assert opencode.host_version == "opencode v2.0.27"
    # Generator surumu host surumundan ayri (reviewed generator id degil host surumu).
    assert opencode.version != opencode.host_version
    assert opencode.version_basis == "reviewed-generator"

    cells = _cells(opencode)
    for capability in ("instruction", "hook", "mcp"):
        assert cells[capability].status == _DECLARED
        assert cells[capability].evidence_level == "unobserved"


def test_absent_host_keeps_support_boundary_even_when_other_host_present() -> None:
    """OpenCode kurulu olsa bile kurulu olmayan Claude/Gemini degrade kalir."""
    matrix = _matrix(opencode="opencode v2.0.27", codex="codex-cli 0.162.0")

    for client in ("claude-code", "gemini"):
        profile = _profile(matrix, client)
        assert profile.host_present is False
        assert profile.host_version is None
        for cell in profile.capabilities:
            assert not (cell.status == _READY and cell.evidence_level == "observed")
            if cell.evidence_level == "observed":
                pytest.fail(f"{client}.{cell.capability} host kanitsiz observed tasiyor")

    # Kurulu istemciler kendi host surumlarini ayri alanda tasir.
    assert _profile(matrix, "codex").host_version == "codex-cli 0.162.0"
    assert _profile(matrix, "opencode").host_version == "opencode v2.0.27"


def test_host_version_is_separate_field_from_generator_version() -> None:
    """`host_version` (gozlenen native surum) generator `version` degil."""
    matrix = client_capability_matrix(
        observed_hosts={
            "opencode": "opencode v2.0.27",
            "codex": "codex-cli 0.162.0",
            "claude-code": "2.1.224 (Claude Code)",
            "gemini": "gemini 1.0.0",
        }
    )
    for profile in matrix.clients:
        assert profile.host_present is True
        assert profile.host_version is not None
        # Generator (reviewed) surumu host surumundan bagimsiz tanimlanir.
        assert profile.version
        assert profile.version_basis == "reviewed-generator"


def test_observed_hosts_lists_only_present_clients() -> None:
    matrix = _matrix(opencode="opencode v2.0.27", codex="codex-cli 0.162.0")
    assert set(matrix.observed_hosts) == {"opencode", "codex"}
    assert "claude-code" not in matrix.observed_hosts
    assert "gemini" not in matrix.observed_hosts


def test_shell_is_never_ready_and_never_observed() -> None:
    # Shell `allow` hedefi WP-02.03'e baglidir; hicbir host kanitinda `ready` degil.
    for observed in ({}, {"opencode": "v1", "codex": "v1", "claude-code": "v1", "gemini": "v1"}):
        matrix = _matrix(**observed)
        for profile in matrix.clients:
            shell = _cells(profile)["shell"]
            assert shell.status != _READY
            assert shell.evidence_level != "observed"


def test_gemini_has_no_managed_hook_and_mcp() -> None:
    # Gemini icin Zekam managed hook/MCP uretilmez; host kaniti olsa bile bu sinir degismez.
    matrix = _matrix(gemini="gemini 1.0.0")
    cells = _cells(_profile(matrix, "gemini"))
    assert cells["hook"].status == _NOT_SUPPORTED
    assert cells["mcp"].status == _NOT_SUPPORTED


def test_no_unsupported_native_feature_is_claimed() -> None:
    # `ready`/`partial`/`declared`/`observed` haricinde sahte destek durumu uretilmez.
    for observed in ({}, {"opencode": "v1"}, {"gemini": "v1"}):
        matrix = _matrix(**observed)
        for profile in matrix.clients:
            for support in profile.capabilities:
                assert support.status in CapabilityStatus.all()


def _shift(
    matrix, *, client: str, capability: str, to: CapabilityStatus
) -> ClientCapabilityProfile:
    def rebuild(profile):
        if profile.client != client:
            return profile
        cells = []
        for item in profile.capabilities:
            if item.capability == capability:
                cells.append(
                    type(item)(
                        capability=item.capability,
                        status=to.value,
                        version=item.version,
                        evidence_level=item.evidence_level,
                        support_limit=item.support_limit,
                    )
                )
            else:
                cells.append(item)
        return ClientCapabilityProfile(
            client=profile.client,
            version=profile.version,
            version_basis=profile.version_basis,
            capabilities=tuple(cells),
            adapter_plan=profile.adapter_plan,
            support_limit=profile.support_limit,
            host_present=profile.host_present,
            host_version=profile.host_version,
        )

    return type(matrix)(
        matrix.schema,
        matrix.generated_from,
        tuple(rebuild(p) for p in matrix.clients),
        observed_hosts=matrix.observed_hosts,
        read_only=True,
        grants_authority=False,
    )
