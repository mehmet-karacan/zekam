"""P16-T01..T06 komut sozlesmesi, telemetri ve projeksiyon testleri."""

from __future__ import annotations

import datetime as dt

import pytest

from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.observability import (
    CANONICAL_COMMANDS,
    REQUIRED_TILES,
    CommandContract,
    DerivedGraph,
    GraphEdge,
    GraphNode,
    McpCapability,
    McpNegotiation,
    OperationsDashboard,
    ProjectionTile,
    SpanKind,
    Surface,
    TelemetryAttribute,
    TelemetrySpan,
    command_names,
    correlate,
    missing_commands,
)

NOW = dt.datetime(2026, 8, 21, tzinfo=dt.UTC)


# -- T01: komut sozlesmesi -----------------------------------------------------


def test_mutasyon_yapan_komut_uygula_bayragi_ister() -> None:
    with pytest.raises(PolicyViolation):
        CommandContract("tehlikeli", "yazar", mutating=True)


def test_salt_okunur_komut_authorization_istemez() -> None:
    with pytest.raises(ValidationFailed):
        CommandContract("liste", "okur", mutating=False, requires_authorization=True)


def test_komut_basarili_cikis_kodunu_bildirmeli() -> None:
    with pytest.raises(ValidationFailed):
        CommandContract("x", "y", mutating=False, exit_codes=(4,))


def test_kanonik_komut_yuzeyi_tutarli() -> None:
    for contract in CANONICAL_COMMANDS:
        if contract.mutating:
            assert contract.requires_apply_flag, f"{contract.name} --uygula istemeli"
    assert len(set(command_names())) == len(CANONICAL_COMMANDS), "komut adlari tekrar edemez"


def test_eksik_komutlar_gorunur() -> None:
    assert missing_commands(command_names()) == ()
    partial = tuple(name for name in command_names() if name != "doctor")
    assert missing_commands(partial) == ("doctor",)


# -- T04: telemetri -----------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    ["api_key", "authorization", "prompt_text", "response_body", "user_password", "cookie"],
)
def test_yasak_telemetri_alani_reddedilir(key: str) -> None:
    with pytest.raises(PolicyViolation):
        TelemetryAttribute(key=key, value="x")


def test_secret_benzeri_deger_reddedilir() -> None:
    with pytest.raises(PolicyViolation):
        TelemetryAttribute(key="not", value="Bearer abcdefgh12345678")
    with pytest.raises(PolicyViolation):
        TelemetryAttribute(key="not", value="-----BEGIN PRIVATE KEY-----")


def test_kisisel_path_reddedilir() -> None:
    with pytest.raises(PolicyViolation):
        TelemetryAttribute(key="kaynak", value="C:\\Users\\biri\\zekam")
    with pytest.raises(PolicyViolation):
        TelemetryAttribute(key="kaynak", value="/home/biri/zekam")


def test_mesru_telemetri_alani_kabul_edilir() -> None:
    attribute = TelemetryAttribute(key="work_item_id", value="ZEKAM-P16-T04")
    assert attribute.as_pair() == ("work_item_id", "ZEKAM-P16-T04")
    assert TelemetryAttribute(key="chunk_count", value=42).value == 42


def _span(**kwargs: object) -> TelemetrySpan:
    defaults: dict[str, object] = {
        "name": "work.create",
        "kind": SpanKind.USE_CASE,
        "surface": Surface.CLI,
        "trace_id": "t-1",
        "span_id": "s-1",
        "started_at": NOW,
        "duration_ms": 12,
    }
    defaults.update(kwargs)
    return TelemetrySpan(**defaults)  # type: ignore[arg-type]


def test_span_correlation_zorunlu() -> None:
    with pytest.raises(ValidationFailed):
        _span(trace_id="  ")
    with pytest.raises(ValidationFailed):
        _span(span_id="")


def test_span_kendi_ebeveyni_olamaz() -> None:
    with pytest.raises(ValidationFailed):
        _span(parent_span_id="s-1")


def test_tekrar_eden_telemetri_anahtari_reddedilir() -> None:
    attribute = TelemetryAttribute(key="proje", value="zekam")
    with pytest.raises(ValidationFailed):
        _span(attributes=(attribute, attribute))


def test_span_correlation_gruplanir() -> None:
    spans = (
        _span(trace_id="t-1", span_id="s-1"),
        _span(trace_id="t-1", span_id="s-2", parent_span_id="s-1"),
        _span(trace_id="t-2", span_id="s-3"),
    )
    grouped = correlate(spans)
    assert grouped["t-1"] == ("s-1", "s-2")
    assert grouped["t-2"] == ("s-3",)


def test_hata_kategorisi_basariyi_belirler() -> None:
    assert _span().succeeded is True
    assert _span(error_category="adapter").succeeded is False
    assert _span(error_category="adapter").as_dict()["error_category"] == "adapter"


def test_span_ciktisi_icerik_tasimaz() -> None:
    document = _span(attributes=(TelemetryAttribute(key="unit_count", value=3),)).as_dict()
    assert set(document["attributes"]) == {"unit_count"}
    assert "content" not in document
    assert "prompt" not in document


# -- T05: dashboard -----------------------------------------------------------


def _tiles() -> tuple[ProjectionTile, ...]:
    return tuple(
        ProjectionTile(
            key=name,
            title=name.title(),
            value=index,
            drill_down=f"zekam {name} list",
        )
        for index, name in enumerate(REQUIRED_TILES)
    )


def test_dashboard_zorunlu_projeksiyonlari_ister() -> None:
    eksik = tuple(item for item in _tiles() if item.key != "memory")
    with pytest.raises(ValidationFailed) as error:
        OperationsDashboard(generated_at=NOW, tiles=eksik)
    assert "memory" in str(error.value)


def test_dashboard_salt_okunur_ve_authority_uretmez() -> None:
    with pytest.raises(PolicyViolation):
        OperationsDashboard(generated_at=NOW, tiles=_tiles(), read_only=False)
    with pytest.raises(PolicyViolation):
        OperationsDashboard(generated_at=NOW, tiles=_tiles(), grants_authority=True)
    dashboard = OperationsDashboard(generated_at=NOW, tiles=_tiles())
    assert dashboard.as_dict()["grants_authority"] is False
    assert dashboard.as_dict()["read_only"] is True


def test_her_kare_drill_down_baglantisi_tasir() -> None:
    with pytest.raises(ValidationFailed):
        ProjectionTile(key="work", title="Work", value=1, drill_down="  ")
    dashboard = OperationsDashboard(generated_at=NOW, tiles=_tiles())
    assert all(item["drill_down"] for item in dashboard.as_dict()["tiles"])


# -- T06: derived graph -------------------------------------------------------


def _graph(**kwargs: object) -> DerivedGraph:
    nodes = (
        GraphNode("w1", "work", "Is 1", "zekam work show w1"),
        GraphNode("r1", "run", "Run 1", "zekam run status r1"),
    )
    defaults: dict[str, object] = {
        "nodes": nodes,
        "edges": (GraphEdge("w1", "r1", "executed-by"),),
        "source_digest": digest("kaynak"),
    }
    defaults.update(kwargs)
    return DerivedGraph(**defaults)  # type: ignore[arg-type]


def test_graph_derived_ve_authority_uretmez() -> None:
    with pytest.raises(PolicyViolation):
        _graph(derived=False)
    with pytest.raises(PolicyViolation):
        _graph(grants_authority=True)
    assert _graph().as_dict()["derived"] is True


def test_graph_kanonik_kayda_drill_down_saglar() -> None:
    assert _graph().drill_down("w1") == "zekam work show w1"
    with pytest.raises(ValidationFailed):
        _graph().drill_down("yok")


def test_kenar_bilinmeyen_dugume_baglanamaz() -> None:
    with pytest.raises(ValidationFailed):
        _graph(edges=(GraphEdge("w1", "bilinmeyen", "x"),))


def test_dugum_kanonik_referans_ister() -> None:
    with pytest.raises(ValidationFailed):
        GraphNode("w1", "work", "Is", "  ")


def test_kenar_kendine_baglanamaz() -> None:
    with pytest.raises(ValidationFailed):
        GraphEdge("w1", "w1", "self")


# -- T03: MCP -----------------------------------------------------------------


def test_mutasyon_yapan_mcp_araci_authorization_ister() -> None:
    with pytest.raises(PolicyViolation):
        McpCapability(name="apply", kind="tool", mutating=True)


def test_bilinmeyen_mcp_turu_reddedilir() -> None:
    with pytest.raises(ValidationFailed):
        McpCapability(name="x", kind="widget")


def test_mcp_authority_sahibi_olamaz() -> None:
    with pytest.raises(PolicyViolation):
        McpNegotiation(client_supported=frozenset({"tool"}), offered=(), authority_owner="istemci")


def test_yetenek_uzlasmasi_istemci_kumesiyle_sinirli() -> None:
    offered = (
        McpCapability(name="work-list", kind="tool"),
        McpCapability(name="repo", kind="resource"),
        McpCapability(name="ozet", kind="prompt"),
    )
    negotiation = McpNegotiation(client_supported=frozenset({"tool", "resource"}), offered=offered)
    assert [item.name for item in negotiation.negotiated()] == ["work-list", "repo"]
    assert [item.name for item in negotiation.rejected()] == ["ozet"]
    assert negotiation.as_dict()["authority_owner"] == "zekam"
