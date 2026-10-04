from __future__ import annotations

from types import SimpleNamespace

from zekam.domain.canonical import digest
from zekam.interfaces.cli import model


def test_reviewed_plan_digest_routes_to_provider_runner(monkeypatch, capsys) -> None:
    reviewed_digest = digest("reviewed-plan")
    called: dict[str, object] = {}

    monkeypatch.setattr(
        model,
        "build_native_campaign",
        lambda **_: SimpleNamespace(plan=SimpleNamespace(plan_digest=digest("native-plan"))),
    )
    monkeypatch.setattr(
        model,
        "_reviewed_campaign_plan",
        lambda: ({"state": "planned", "plan_digest": reviewed_digest}, object()),
    )
    monkeypatch.setattr(model, "default_opencode_config_file", lambda: object())

    def run(database, config_file, manifest):
        called.update(database=database, config_file=config_file, manifest=manifest)
        return {
            "state": "completed",
            "provider_calls": 1,
            "skipped_health_failed": 0,
        }

    monkeypatch.setattr(model, "execute_reviewed_campaign", run)
    model.campaign_run_command(
        plan_digest=reviewed_digest,
        apply=True,
        output_json=True,
        home="campaign-routing-test",
    )

    output = capsys.readouterr().out
    assert '"provider_calls": 1' in output
    assert called["manifest"] is not None
    assert str(called["database"]).endswith("benchmarklar\\opencode-campaign.db")
