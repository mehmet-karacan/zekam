from __future__ import annotations

import hashlib
import json
from pathlib import Path

from typer.testing import CliRunner

from zekam.domain.app_server_protocol import PROTOCOL_VERSION, protocol_schema_bundle
from zekam.interfaces.cli.main import app
from zekam.protocol.generation import (
    GENERATED_RESOURCE_NAMES,
    generate_protocol_artifacts,
    protocol_artifact_digest,
    render_protocol_artifacts,
)

runner = CliRunner()


def test_generated_protocol_resources_are_tracked_and_byte_deterministic() -> None:
    output = Path("src/zekam/protocol")
    first = render_protocol_artifacts()
    second = render_protocol_artifacts()
    assert first == second
    assert tuple(first) == GENERATED_RESOURCE_NAMES
    assert generate_protocol_artifacts(output, check=True)


def test_checksums_bind_every_generated_resource_except_checksum_file() -> None:
    artifacts = render_protocol_artifacts()
    expected = {
        name: hashlib.sha256(content.encode("utf-8")).hexdigest()
        for name, content in artifacts.items()
        if name != "SHA256SUMS"
    }
    observed = {
        line.split("  ", 1)[1]: line.split("  ", 1)[0]
        for line in artifacts["SHA256SUMS"].splitlines()
    }
    assert observed == expected


def test_protocol_metadata_is_exact_version_and_schema_bound() -> None:
    artifacts = render_protocol_artifacts()
    version = json.loads(artifacts["protocol-version.json"])
    compatibility = json.loads(artifacts["compatibility-matrix.json"])
    methods = json.loads(artifacts["event-methods.json"])["methods"]
    assert version["protocol_version"] == PROTOCOL_VERSION
    assert compatibility["entries"] == [
        {
            "compatibility": "exact-version",
            "protocol_version": PROTOCOL_VERSION,
            "schema_bundle_digest": version["schema_bundle_digest"],
        }
    ]
    assert methods == sorted(methods)
    assert "initialize" in methods
    assert json.loads(artifacts["schema-bundle.json"]) == protocol_schema_bundle()


def test_protocol_cli_digest_and_generators(tmp_path: Path) -> None:
    digest_result = runner.invoke(app, ["protocol", "digest"])
    assert digest_result.exit_code == 0
    assert digest_result.stdout.strip() == protocol_artifact_digest()

    schema_target = tmp_path / "schema.json"
    schema_result = runner.invoke(
        app, ["protocol", "generate-json-schema", "--output", str(schema_target)]
    )
    assert schema_result.exit_code == 0
    assert (
        schema_target.read_text(encoding="utf-8")
        == render_protocol_artifacts()["schema-bundle.json"]
    )

    types_target = tmp_path / "client.ts"
    types_result = runner.invoke(
        app, ["protocol", "generate-typescript", "--output", str(types_target)]
    )
    assert types_result.exit_code == 0
    assert (
        types_target.read_text(encoding="utf-8") == render_protocol_artifacts()["client-types.ts"]
    )
