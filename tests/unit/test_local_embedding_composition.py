from __future__ import annotations

from types import SimpleNamespace

import pytest

from zekam.application import local_embedding_composition as subject
from zekam.domain.canonical import digest
from zekam.domain.security import DataClassification
from zekam.infrastructure.embedding.onnx_e5 import LocalE5Configuration

pytestmark = pytest.mark.unit


def test_verified_local_e5_binding_reuses_a_healthy_pinned_probe(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    configuration = LocalE5Configuration(tmp_path)
    profile = SimpleNamespace(profile_digest=digest("profile"))
    result = SimpleNamespace(profile=profile)
    probe_calls = 0

    class _Provider:
        def __init__(self, config: LocalE5Configuration) -> None:
            self.configuration = config

        def probe(self, _fixture):
            nonlocal probe_calls
            probe_calls += 1
            return result

        def health(self):
            return SimpleNamespace(healthy=True)

    provider = _Provider(configuration)
    monkeypatch.setattr(subject, "build_local_e5_provider", lambda _configuration: provider)
    subject._verified_e5_cache.clear()

    first = subject.build_verified_local_e5_embedding(
        classification=DataClassification.LOCAL_ONLY,
        configuration=configuration,
    )
    second = subject.build_verified_local_e5_embedding(
        classification=DataClassification.LOCAL_ONLY,
        configuration=configuration,
    )

    assert first is second
    assert first.provider is provider
    assert probe_calls == 1
