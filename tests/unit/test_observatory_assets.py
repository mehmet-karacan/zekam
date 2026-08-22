from __future__ import annotations

from importlib.resources import files


def test_observatory_assets_are_packaged_and_self_contained() -> None:
    static = files("zekam.interfaces.api").joinpath("static")
    index = static.joinpath("index.html").read_text(encoding="utf-8")
    script = static.joinpath("app.js").read_text(encoding="utf-8")
    style = static.joinpath("styles.css").read_text(encoding="utf-8")

    assert "brain-canvas" in index
    assert "client-grid" in index
    assert "active-session-count" in index
    assert "live-network-toggle" in index
    assert "ZEKAM OBSERVATORY" in index
    assert "EventSource" in script
    assert 'label: "OpenCode"' in script
    assert 'label: "Codex"' in script
    assert 'label: "Claude"' in script
    assert '"agent-session"' in script
    assert '"reports-observation"' in script
    assert "arrowT" in script
    assert "labelBoxes" in script
    assert "hash(edge.kind) % 7" not in script
    assert 'edge.kind.includes("active")' not in script
    assert "/running|recovery/" not in script
    assert "state.activeNodeIds.has(edge.source) && state.activeNodeIds.has(edge.target)" in script
    assert "font-size: 15px" in style
    assert "live-network-mode .report-section" in style
    assert "liveMode: false" in script
    assert "grid-template-columns: minmax(820px, 1fr) 480px" in style
    assert "height: clamp(760px, 78vh, 1020px)" in style
    assert (
        "const clientAnchors = { opencode: 0.32, codex: 0.5, claude: 0.68, zekam: 0.5 }" in script
    )
    assert "left.node_id.localeCompare(right.node_id)" in script
    assert "|| left.node_id.localeCompare(right.node_id)" in script
    assert "rail-event-panel" in index
    assert "agent-pulse-panel" in index
    assert index.index("agent-pulse-panel") < index.index("rail-event-panel")
    assert 'kind: "tool"' in script
    assert 'kind: "executes-tool"' in script
    assert "active_tool" in script
    assert '{ source: toolNodeId(agent), target: "system:zekam"' not in script
    assert "font-size: 14px; line-height: 1.45" in style
    assert ".lower-grid { display: none; }" in style
    assert "context.lineWidth = active ? 2.8 : 1" in script
    assert 'if (node.kind === "agent-session") return 8.2' in script
    assert ".agent-identity strong { display: block; font-size: 17px" in style
    assert ".sidebar { display: none; }" in style
    assert ".client-grid { grid-template-columns: repeat(3" in style
    assert "@media (max-width: 1000px)" in style
    assert ".client-grid, .agent-list { grid-template-columns: 1fr; }" in style
    assert "zekam-observatory-snapshot/v1" in script
    assert "@media (prefers-reduced-motion: reduce)" in style
    assert "https://" not in index
    assert "http://" not in index
