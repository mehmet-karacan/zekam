"""Persistent public trust pins survive the actual settings/config path."""

from types import SimpleNamespace

import pytest
import yaml

from tests.unit.test_git_publication_trust import trust_home  # noqa: F401
from zekam.application.config import GitPublicationSettings, load_settings
from zekam.domain.canonical import digest
from zekam.domain.errors import ConfigurationError, PolicyViolation
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore
from zekam.interfaces.cli import git_publication as cli


def _config(home, value):
    (home / "config.yaml").write_text(
        yaml.safe_dump({"schema": "zekam-config/v1", "git_publication": value}), encoding="utf-8"
    )


def test_actual_settings_pin_survives_sanitized_active_config_and_cli(trust_home, monkeypatch):
    home, _path, document = trust_home
    pin = digest(document)
    _config(home, {"trust_digest": pin})
    settings = load_settings(home=home, environ={})
    assert settings.git_publication.trust_digest == pin
    assert settings.sanitized()["git_publication"] == {"trust_digest": pin}
    assert settings.config_provenance.effective_document["git_publication"]["trust_digest"] == pin
    sanitized = settings.sanitized()
    with SQLiteOperationalStore(home / "state/operational.db").unit_of_work() as uow:
        uow.activate_config(
            config_digest=digest(sanitized), task_digest=digest("task"), sanitized_config=sanitized
        )
        uow.commit()
    monkeypatch.setattr(
        cli, "_context", lambda _home: SimpleNamespace(home=home, settings=settings)
    )
    context, trust, ledger = cli._components(home)
    assert context.settings is settings
    assert trust.trust_digest == pin and ledger is not None


def test_unconfigured_settings_do_not_add_pin_or_create_registry(tmp_path):
    settings = load_settings(home=tmp_path, environ={})
    assert settings.git_publication.trust_digest is None
    assert "git_publication" not in settings.sanitized()
    assert not (tmp_path / "config/git-publication-trust.json").exists()


@pytest.mark.parametrize(
    "value",
    [
        None,
        {},
        [],
        {"trust_digest": None},
        {"trust_digest": True},
        {"trust_digest": 1},
        {"trust_digest": "bad"},
        {"trust_digest": "sha256:" + "A" * 64},
        {"trust_digest": digest("pin"), "registry_path": "caller-selected"},
    ],
)
def test_configured_pin_is_strict_public_digest_only(tmp_path, value):
    _config(tmp_path, value)
    with pytest.raises(ConfigurationError):
        load_settings(home=tmp_path, environ={})


@pytest.mark.parametrize(
    "key,value",
    [
        ("git_publication", {"trust_digest": digest("session")}),
        ("git_publication.trust_digest", digest("session")),
        ("cli.integrations.codex", True),
    ],
)
def test_session_cannot_override_persistent_publication_trust(tmp_path, key, value):
    with pytest.raises(ConfigurationError, match="Session override"):
        load_settings(
            home=tmp_path,
            environ={},
            session_overrides={key: value},
        )


def test_cli_rejects_missing_or_different_loaded_pin(trust_home, monkeypatch):
    home, _path, _document = trust_home
    settings = load_settings(home=home, environ={})
    monkeypatch.setattr(
        cli, "_context", lambda _home: SimpleNamespace(home=home, settings=settings)
    )
    with pytest.raises(ConfigurationError, match="kalici config"):
        cli._components(home)
    _config(home, {"trust_digest": digest("different-registry")})
    settings = load_settings(home=home, environ={})
    with pytest.raises(PolicyViolation, match="loaded settings/active registry"):
        cli._components(home)


@pytest.mark.parametrize("value", [True, 1, {}, "invalid"])
def test_typed_publication_settings_reject_invalid_pin(value):
    with pytest.raises(ConfigurationError):
        GitPublicationSettings(value)
