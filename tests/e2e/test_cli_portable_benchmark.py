from __future__ import annotations

import json
from pathlib import Path

from tests.unit.test_portable_benchmark import _fixture
from typer.testing import CliRunner

from zekam.interfaces.cli.main import app


def test_cli_inspects_portable_benchmark_without_provider_or_foreign_execution(
    tmp_path: Path,
) -> None:
    _fixture(tmp_path)

    result = CliRunner().invoke(
        app, ["model", "portable-inspect", "--root", str(tmp_path), "--json"]
    )

    assert result.exit_code == 0, result.output
    document = json.loads(result.output)
    assert document["schema"] == "zekam-portable-benchmark-inspection/v1"
    assert document["models"]["total"] == 1
    assert document["provider_calls"] == 0
    assert document["foreign_code_executed"] is False
