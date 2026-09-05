"""Mac native inventory does not claim actual hook delivery or model acceptance."""

from __future__ import annotations

import datetime as dt
import platform
import shutil
from pathlib import Path

import pytest

from zekam.application.local_client_identity import MAC_NATIVE_ARTIFACT_PINS
from zekam.domain.canonical import digest_of_bytes
from zekam.infrastructure.local_client_identity import inspect_macos_client

pytestmark = pytest.mark.e2e
AKILLI_SOURCE = Path("/Users/mkaracan/Projeler/akilli-kasa/src/akilli_kasa/api/saglik.py")


@pytest.mark.parametrize("pin", MAC_NATIVE_ARTIFACT_PINS, ids=lambda pin: pin.client_id)
def test_installed_macos_native_identity_is_not_lifecycle_acceptance(
    tmp_path: Path, pin: object
) -> None:
    from zekam.application.local_client_identity import MacNativeArtifactPin

    assert isinstance(pin, MacNativeArtifactPin)
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        pytest.skip("Separate exact Mac arm64 inventory; historical Windows tests remain")
    if not AKILLI_SOURCE.is_file():
        pytest.fail("The required read-only Akilli Kasa fixture is unavailable")
    source_digest = digest_of_bytes(AKILLI_SOURCE.read_bytes())
    command = "claude" if pin.client_id == "claude-code" else pin.client_id
    located = shutil.which(command)
    assert located is not None, f"Required Mac inventory executable missing: {command}"
    before_files = tuple(tmp_path.iterdir())
    observation = inspect_macos_client(pin.client_id, Path(located), dt.datetime.now(dt.UTC))
    body = observation.body()
    assert body["native_sha256"] == pin.native_sha256
    assert body["version"] == pin.version
    assert body["inventory_observed"] is True
    assert body["wire_contract_reviewed"] is body["lifecycle_proven"] is False
    assert body["grants_authority"] is body["hooks_activated"] is False
    assert body["external_provider_calls"] == 0
    assert body["subprocess_calls"] == 0
    assert body["runtime_version_probe"] == "not-run"
    assert body["runtime_version_observed"] is False
    assert tuple(tmp_path.iterdir()) == before_files
    assert digest_of_bytes(AKILLI_SOURCE.read_bytes()) == source_digest
