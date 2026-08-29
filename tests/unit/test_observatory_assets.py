from __future__ import annotations

from importlib.resources import files

import pytest

from zekam.interfaces.api.observatory import validated_lan_hosts
from zekam.interfaces.cli.ui import validate_lan_bind_host


def test_live_execution_assets_are_packaged_content_free_and_self_contained() -> None:
    static = files("zekam.interfaces.api").joinpath("static")
    index = static.joinpath("index.html").read_text(encoding="utf-8")
    script = static.joinpath("app.js").read_text(encoding="utf-8")
    style = static.joinpath("styles.css").read_text(encoding="utf-8")

    assert index.count("Zekam Canlı Yürütme Gözleme Merkezi") >= 2
    assert "Repository" not in index
    assert "Commits" not in index
    assert "Contributors" not in index
    assert "execution-canvas" in index
    assert "Canlı Oturumlar" in index
    assert "Session Registry" in index
    assert "Canlı Olay Akışı" in index
    assert "Queue / Lease / Receipt" in index
    assert "Kaynak Kullanımı" in index
    metrics = (
        "AÇIK CLI",
        "CANLI OTURUM",
        "AKTİF AGENT",
        "ÇALIŞAN TOOL",
        "RECOVERY",
        "RECEIPTLESS",
    )
    for metric in metrics:
        assert metric in index
    assert "client-filter" in index
    assert "state-filter" in index
    assert "project-filter" in index
    assert "graph-fallback" in index
    assert "/assets/styles.css?v=14" in index
    assert "/assets/app.js?v=14" in index
    assert "https://" not in index
    assert "http://" not in index

    assert 'const snapshotSchema = "zekam-observatory-snapshot/v3"' in script
    assert "EventSource" in script
    assert 'addEventListener("structure"' in script
    assert 'addEventListener("telemetry"' in script
    assert "structureDigest" in script
    assert "telemetryDigest" in script
    assert "Math.random" not in script
    assert "textContent" in script
    assert "innerHTML" not in script
    assert "raw command" not in script.casefold()
    assert "prompt_content" not in script
    assert "model_response" not in script
    assert "terminal_receipt_bound" in script
    assert "binding_confidence" in script
    assert "current_action" in script
    assert "project-filter" in index
    assert 'diagnostics.get("diagnostics") === "graph"' in script
    assert "benchmarkGraph" in script

    assert "--gold: #ffc15a" in style
    assert "--orange: #f45f22" in style
    assert "--red: #f03a2f" in style
    assert "grid-template-columns: repeat(6" in style
    assert "grid-template-columns: minmax(0, 1fr) 330px" in style
    assert "grid-template-columns: 1.1fr 1fr 1.1fr .85fr" in style
    assert "@media (max-width: 1380px)" in style
    assert "@media (max-width: 1000px)" in style
    assert "@media (prefers-reduced-motion: reduce)" in style


def test_ui_lan_binding_requires_explicit_flag() -> None:
    source = files("zekam.interfaces.cli").joinpath("ui.py").read_text(encoding="utf-8")

    assert '"--allow-lan"' in source
    assert "if host not in _LOOPBACK_HOSTS:" in source
    assert "if not allow_lan:" in source
    assert "allowed_hosts=() if lan_host is None else (lan_host,)" in source


def test_ui_lan_binding_accepts_only_exact_non_loopback_ip() -> None:
    assert validate_lan_bind_host("192.168.1.183") == "192.168.1.183"
    assert validated_lan_hosts(("192.168.1.183",)) == ("192.168.1.183",)

    for unsafe in ("*", "0.0.0.0", "::", "*.example", "localhost", "127.0.0.1"):
        with pytest.raises(ValueError):
            validate_lan_bind_host(unsafe)
        with pytest.raises(ValueError):
            validated_lan_hosts((unsafe,))
