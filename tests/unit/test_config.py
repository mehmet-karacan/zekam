"""Yapilandirma yukleme ve secret reddi testleri."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from zekam.application.config import (
    CONFIG_SCHEMA,
    USER_CONFIG_FILE,
    DatabaseSettings,
    PersistenceBackend,
    core_root,
    database_password,
    default_config_file,
    load_settings,
)
from zekam.domain.errors import ConfigurationError, PolicyViolation

pytestmark = pytest.mark.unit


def _write(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def test_core_default_file_exists_and_parses(home_root: Path) -> None:
    assert default_config_file().is_file()
    settings = load_settings(home=home_root, environ={})
    assert "core-default" in settings.sources
    assert settings.database.name == "zekam"
    assert settings.database.backend is PersistenceBackend.POSTGRESQL
    assert settings.knowledge.embedding_dimension == 1024
    assert settings.knowledge.embedding_distance == "cosine"


def test_user_config_overrides_core_default(home_root: Path) -> None:
    _write(
        home_root / USER_CONFIG_FILE,
        f"schema: {CONFIG_SCHEMA}\ndatabase:\n  port: 6543\n",
    )
    settings = load_settings(home=home_root, environ={})
    assert settings.database.port == 6543
    assert settings.sources == ("core-default", "user-config", "managed-policy")


def test_removed_product_config_schema_is_rejected(home_root: Path) -> None:
    removed_slug = "".join(chr(item) for item in (101, 110, 97, 105))
    _write(home_root / USER_CONFIG_FILE, f"schema: {removed_slug}-config/v1\n")
    with pytest.raises(ConfigurationError, match="Desteklenmeyen yapilandirma semasi"):
        load_settings(home=home_root, environ={})


def test_environment_overrides_user_config(home_root: Path) -> None:
    _write(
        home_root / USER_CONFIG_FILE,
        f"schema: {CONFIG_SCHEMA}\ndatabase:\n  port: 6543\n",
    )
    settings = load_settings(home=home_root, environ={"ZEKAM_DATABASE_PORT": "7777"})
    assert settings.database.port == 7777
    assert settings.sources == (
        "core-default",
        "user-config",
        "managed-policy",
        "environment",
    )


def test_managed_runtime_policy_and_profile_are_applied_by_normal_loader(
    home_root: Path,
) -> None:
    settings = load_settings(home=home_root, environ={})
    assert settings.runtime.network_default == "deny"
    assert settings.runtime.permission_profile == "workspace-write-no-network"
    assert settings.permission_profile is not None
    assert settings.permission_profile.managed is True
    assert settings.permission_profile.grants_authority is False
    assert "managed-policy" in settings.sources
    field = settings.config_provenance.explain("runtime.network_default")
    assert field.origin == "managed-policy"
    assert field.managed_requirement is not None


def test_session_cannot_open_managed_network_or_permission_profile(home_root: Path) -> None:
    with pytest.raises(PolicyViolation, match="Managed exact"):
        load_settings(
            home=home_root,
            environ={},
            session_overrides={"runtime": {"network_default": "allow"}},
        )
    with pytest.raises(PolicyViolation, match="Managed exact"):
        load_settings(
            home=home_root,
            environ={},
            session_overrides={"runtime": {"permission_profile": "read-only"}},
        )
    with pytest.raises(PolicyViolation, match="managed deny"):
        load_settings(
            home=home_root,
            environ={},
            session_permission_capabilities=("network.access",),
        )


def test_safe_session_override_is_provenance_visible(home_root: Path) -> None:
    settings = load_settings(
        home=home_root,
        environ={},
        session_overrides={"runtime": {"log_level": "DEBUG"}},
        session_permission_capabilities=("filesystem.read",),
    )
    assert settings.runtime.log_level == "DEBUG"
    assert settings.sources[-1] == "session"
    assert settings.config_provenance.explain("runtime.log_level").origin == "session"


def test_invalid_environment_value_is_rejected(home_root: Path) -> None:
    with pytest.raises(ConfigurationError):
        load_settings(home=home_root, environ={"ZEKAM_DATABASE_PORT": "asla-sayi-degil"})


def test_sqlite_config_uses_portable_home_relative_path(home_root: Path) -> None:
    _write(
        home_root / USER_CONFIG_FILE,
        f"schema: {CONFIG_SCHEMA}\ndatabase:\n  backend: sqlite\n",
    )
    settings = load_settings(home=home_root, environ={})
    assert settings.database.backend is PersistenceBackend.SQLITE
    assert (
        settings.database.sqlite_path(home_root)
        == (home_root / "global" / "runtime" / "zekam.sqlite3").resolve()
    )


def test_sqlite_path_escape_is_rejected(home_root: Path) -> None:
    _write(
        home_root / USER_CONFIG_FILE,
        f"schema: {CONFIG_SCHEMA}\ndatabase:\n"
        "  backend: sqlite\n  sqlite_relative_path: ../escape.sqlite3\n",
    )
    with pytest.raises(ConfigurationError, match="portable"):
        load_settings(home=home_root, environ={}).database.sqlite_path(home_root)


def test_sqlite_never_falls_back_to_postgresql_dsn(home_root: Path) -> None:
    _write(
        home_root / USER_CONFIG_FILE,
        f"schema: {CONFIG_SCHEMA}\ndatabase:\n  backend: sqlite\n",
    )
    settings = load_settings(home=home_root, environ={})
    with pytest.raises(ConfigurationError, match="libpq"):
        settings.database.dsn()


@pytest.mark.parametrize(
    "body",
    [
        "database:\n  password: gizli\n",
        "database:\n  api_key: gizli\n",
        "runtime:\n  token: gizli\n",
    ],
)
def test_secret_key_in_config_file_is_rejected(home_root: Path, body: str) -> None:
    _write(home_root / USER_CONFIG_FILE, f"schema: {CONFIG_SCHEMA}\n{body}")
    with pytest.raises(ConfigurationError):
        load_settings(home=home_root, environ={})


def test_unsupported_config_schema_is_rejected(home_root: Path) -> None:
    _write(home_root / USER_CONFIG_FILE, "schema: zekam-config/v0\n")
    with pytest.raises(ConfigurationError):
        load_settings(home=home_root, environ={})


def test_non_mapping_config_is_rejected(home_root: Path) -> None:
    _write(home_root / USER_CONFIG_FILE, "- bir\n- iki\n")
    with pytest.raises(ConfigurationError):
        load_settings(home=home_root, environ={})


def test_dsn_omits_password_when_absent() -> None:
    database = DatabaseSettings(host="localhost", port=5432, name="zekam", user="zekam")
    assert "password=" not in database.dsn()
    assert "password=gizli" in database.dsn("gizli")


def test_sanitized_view_never_contains_password() -> None:
    database = DatabaseSettings(host="localhost", port=5432, name="zekam", user="zekam")
    rendered = repr(database.sanitized())
    assert "password" not in rendered


def test_settings_sanitized_view_is_secret_free(home_root: Path) -> None:
    settings = load_settings(home=home_root, environ={})
    rendered = repr(settings.sanitized()).lower()
    for forbidden in ("password", "token", "secret", "api_key"):
        assert forbidden not in rendered


def test_clients_parse_exact_absolute_existing_executable(home_root: Path) -> None:
    home_root.mkdir(parents=True)
    executable = home_root / "opencode.exe"
    executable.write_bytes(b"MZ")
    _write(
        home_root / USER_CONFIG_FILE,
        f"schema: {CONFIG_SCHEMA}\nclients:\n  - name: opencode\n    executable: '{executable}'\n",
    )

    settings = load_settings(home=home_root, environ={})

    assert len(settings.clients) == 1
    assert settings.clients[0].name == "opencode"
    assert settings.clients[0].executable == executable.resolve()
    assert settings.sanitized()["clients"] == [
        {"name": "opencode", "executable": str(executable.resolve())}
    ]


@pytest.mark.parametrize(
    "clients_yaml",
    [
        "clients: {name: opencode, executable: opencode.exe}\n",
        "clients:\n  - name: opencode\n    executable: opencode.exe\n",
    ],
)
def test_invalid_clients_fail_closed(home_root: Path, clients_yaml: str) -> None:
    _write(home_root / USER_CONFIG_FILE, f"schema: {CONFIG_SCHEMA}\n{clients_yaml}")
    with pytest.raises(ConfigurationError):
        load_settings(home=home_root, environ={})


def test_missing_or_directory_client_executable_fails_closed(home_root: Path) -> None:
    home_root.mkdir(parents=True)
    for target in (home_root / "missing.exe", home_root):
        _write(
            home_root / USER_CONFIG_FILE,
            f"schema: {CONFIG_SCHEMA}\nclients:\n  - name: opencode\n    executable: '{target}'\n",
        )
        with pytest.raises(ConfigurationError):
            load_settings(home=home_root, environ={})


@pytest.mark.parametrize("duplicate", ["name", "executable"])
def test_duplicate_client_name_or_executable_fails_closed(home_root: Path, duplicate: str) -> None:
    home_root.mkdir(parents=True)
    first = home_root / "first.exe"
    second = home_root / "second.exe"
    first.write_bytes(b"MZ")
    second.write_bytes(b"MZ")
    second_name = "OpenCode" if duplicate == "name" else "codex"
    second_path = first if duplicate == "executable" else second
    _write(
        home_root / USER_CONFIG_FILE,
        f"schema: {CONFIG_SCHEMA}\nclients:\n"
        f"  - name: opencode\n    executable: '{first}'\n"
        f"  - name: {second_name}\n    executable: '{second_path}'\n",
    )
    with pytest.raises(ConfigurationError, match="Duplicate client"):
        load_settings(home=home_root, environ={})


def test_client_unknown_field_fails_closed(home_root: Path) -> None:
    home_root.mkdir(parents=True)
    executable = home_root / "opencode.exe"
    executable.write_bytes(b"MZ")
    _write(
        home_root / USER_CONFIG_FILE,
        f"schema: {CONFIG_SCHEMA}\nclients:\n"
        f"  - name: opencode\n    executable: '{executable}'\n    extra: forbidden\n",
    )
    with pytest.raises(ConfigurationError, match="exact name ve executable"):
        load_settings(home=home_root, environ={})


def test_secret_key_nested_in_client_list_is_rejected(home_root: Path) -> None:
    _write(
        home_root / USER_CONFIG_FILE,
        f"schema: {CONFIG_SCHEMA}\nclients:\n"
        "  - name: opencode\n    executable: 'C:/Windows/notepad.exe'\n"
        "    token: gizli\n",
    )
    with pytest.raises(ConfigurationError, match="secret alani"):
        load_settings(home=home_root, environ={})


def test_database_password_only_comes_from_environment() -> None:
    assert database_password({}) is None
    assert database_password({"ZEKAM_DATABASE_PASSWORD": "gizli"}) == "gizli"


def test_removed_product_environment_locator_is_not_resolved() -> None:
    removed_prefix = "".join(chr(item) for item in (69, 78, 65, 73))
    assert database_password({f"{removed_prefix}_DATABASE_PASSWORD": "deger"}) is None


def test_persistence_backend_cannot_be_overridden_at_runtime(home_root: Path) -> None:
    with pytest.raises(ConfigurationError, match="runtime override yasak"):
        load_settings(home=home_root, environ={"ZEKAM_DATABASE_BACKEND": "sqlite"})


def test_core_root_is_resolvable_in_both_install_modes() -> None:
    root = core_root()
    assert root.is_dir()
    assert (root / "src" / "zekam" / "__init__.py").is_file() or (root / "__init__.py").is_file()


def test_operator_database_environment_does_not_leak_into_tests() -> None:
    """Kabuktan verilen `ZEKAM_DATABASE_*` testlere sizmamalidir.

    Sizarsa CLI kabul testleri fixture veritabani yerine gercek gelistirme
    veritabanina yazar; ZEKAM-DEF-002 bu yoldan olustu. `clean_environ`
    autouse oldugu icin bu test degiskenlerin silinmis olmasini dogrular.
    """
    izole = (
        "ZEKAM_HOME",
        "ZEKAM_DATABASE_BACKEND",
        "ZEKAM_DATABASE_HOST",
        "ZEKAM_DATABASE_PORT",
        "ZEKAM_DATABASE_NAME",
        "ZEKAM_DATABASE_USER",
        "ZEKAM_DATABASE_SSLMODE",
    )
    assert [key for key in izole if key in os.environ] == []
