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
    assert "EventSource" in script
    assert 'label: "OpenCode"' in script
    assert 'label: "Codex"' in script
    assert 'label: "Claude"' in script
    assert '"agent-session"' in script
    assert "zekam-observatory-snapshot/v1" in script
    assert "@media (prefers-reduced-motion: reduce)" in style
    assert "https://" not in index
    assert "http://" not in index
