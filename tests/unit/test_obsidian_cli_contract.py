from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

from zekam.domain.canonical import digest
from zekam.domain.markdown_projection import ObsidianProfile
from zekam.interfaces.cli import memory as memory_cli


PROJECT_ID = UUID("00000000-0000-0000-0000-000000000101")


class _RealmSession:
    def __init__(self, home: str | None, realm: str) -> None:
        assert home is None
        assert realm == "yerel"

    def __enter__(self) -> SimpleNamespace:
        return SimpleNamespace()

    def __exit__(self, *_args: object) -> None:
        return None


class _Store:
    def __init__(self, bundle: SimpleNamespace) -> None:
        self.bundle = bundle

    def verify_current(
        self,
        realm_slug: str,
        project_id: UUID,
        profile: ObsidianProfile,
        *,
        expected_projection_digest: str,
        expected_manifest_digest: str,
        expected_receipt_digest: str,
    ) -> dict[str, object]:
        assert realm_slug == "yerel"
        assert project_id == PROJECT_ID
        assert profile is ObsidianProfile.PUBLIC_SAFE
        assert expected_projection_digest == self.bundle.projection_digest
        assert expected_manifest_digest == self.bundle.manifest_digest
        assert expected_receipt_digest == self.bundle.receipt_digest
        return {
            "schema": "zekam-obsidian-verification/v1",
            "project_id": str(PROJECT_ID),
            "status": "passed",
        }


def test_obsidian_status_binds_live_projection_manifest_and_receipt(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    bundle = SimpleNamespace(
        projection_digest=digest("projection"),
        manifest_digest=digest("manifest"),
        receipt_digest=digest("receipt"),
        source_snapshot_digest=digest("snapshot"),
        policy_digest=digest("policy"),
    )
    emitted: list[dict[str, object]] = []
    monkeypatch.setattr(memory_cli, "RealmSession", _RealmSession)
    monkeypatch.setattr(memory_cli, "_obsidian_bundle", lambda *_args: bundle)
    monkeypatch.setattr(memory_cli, "_obsidian_store", lambda _home: _Store(bundle))
    monkeypatch.setattr(memory_cli, "_emit", emitted.append)

    memory_cli.obsidian_status(
        project_id=PROJECT_ID,
        profile=ObsidianProfile.PUBLIC_SAFE,
        realm="yerel",
        home=None,
    )

    assert emitted[0]["status"] == "passed"
    assert emitted[0]["current"] is True
