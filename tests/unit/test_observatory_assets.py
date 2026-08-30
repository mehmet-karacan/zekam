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

    assert index.count("Zekam Canlı Yürütme Gözleme Merkezi") >= 1
    assert all(value not in index for value in ("Repository", "Commits", "Contributors"))
    for value in (
        "execution-canvas",
        "nav-rail",
        "Canlı Aktivite",
        "Runtime Integrity",
        "Session Inspector",
        "Session Registry",
        "Event Heatmap",
        "Agent / Client Sıralaması",
        "Durum ve Telemetri",
        "AÇIK CLI",
        "AKTİF OTURUM",
        "AKTİF AGENT",
        "ÇALIŞAN İŞ",
        "AÇIK CLAIM",
        "SON SİNYAL",
        "client-filter",
        "state-filter",
        "binding-filter",
        "time-window",
        "project-filter",
        "graph-fallback",
        "zoom-out",
        "zoom-in",
        "focus-selected",
        "view-reset",
        "motion-toggle",
        "view-toggle",
        "PROMPT / YANIT YOK",
        "RAW COMMAND LINE YOK",
        "READ-ONLY PROJECTION",
    ):
        assert value in index
    assert "/assets/styles.css?v=16" in index
    assert "/assets/app.js?v=16" in index
    assert "https://" not in index
    assert "http://" not in index

    for value in (
        'const snapshotSchema = "zekam-observatory-snapshot/v3"',
        "EventSource",
        'addEventListener("structure"',
        'addEventListener("telemetry"',
        "structureDigest",
        "telemetryDigest",
        "textContent",
        "terminal_receipt_bound",
        "binding_confidence",
        "current_action",
        "MAX_PARTICLES = 96",
        "MAX_LABELS = 72",
        "visibilitychange",
        "cancelAnimationFrame",
        "pointerdown",
        "wheel",
        "focusSelected",
        'diagnostics.get("diagnostics") === "graph"',
        "benchmarkGraph",
        "loadDiagnosticGraph",
        "SENTETİK DIAGNOSTICS",
        'event.key === " "',
        "requestAnimationFrame",
        "prefers-reduced-motion",
        "document.hidden",
        "MAX_RING = 120",
        "binding-filter",
        "time-window",
    ):
        assert value in script
    assert "Math.random" not in script
    assert "innerHTML" not in script
    assert "raw command" not in script.casefold()
    assert "prompt_content" not in script
    assert "model_response" not in script
    assert "state.snapshot?.graph" not in script
    assert "state.structure?.graph" not in script

    for value in (
        "--gold:#f6b84a",
        "--orange:#f07822",
        "--red:#df3b2f",
        "grid-template-columns:repeat(6",
        "grid-template-columns:176px minmax(0,1fr)",
        "grid-template-columns:minmax(0,1fr) clamp(320px,19vw,380px)",
        "grid-template-columns:1.45fr .9fr .9fr 1fr",
        "@media(max-width:1380px)",
        "@media(max-width:1024px)",
        "@media(max-width:700px)",
        "@media(prefers-reduced-motion:reduce)",
        "min-height:44px",
        "touch-action:none",
        "scrollbar-width:none",
        "html::-webkit-scrollbar",
    ):
        assert value in style


def test_live_execution_renderer_is_deterministic_bounded_and_dom_safe() -> None:
    script = (
        files("zekam.interfaces.api").joinpath("static/app.js").read_text(encoding="utf-8")
    )

    assert "Math.random" not in script
    assert "innerHTML" not in script
    assert "document.write" not in script
    assert "eval(" not in script
    assert "new Function" not in script
    assert "MAX_PARTICLES = 96" in script
    assert "MAX_LABELS = 72" in script
    assert "MAX_RING = 120" in script
    assert "zekamRuntimeDiagnostics" in script
    assert "streamConnectCount" in script
    assert "telemetryEventCount" in script
    assert "ringLimit: MAX_RING" in script
    assert 'document.getElementsByTagName("*").length' in script
    assert "frameMedianMs" in script
    assert "frameP95Ms" in script
    assert "cancelAnimationFrame" in script
    assert "visibilitychange" in script
    assert "benchmarkGraph(512, 1024" not in script


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
