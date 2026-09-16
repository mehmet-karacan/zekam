"""MCP registry contracts and built-in server protocol tests."""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request

import pytest

from zekam.application.atlassian_mcp_server import (
    _base_url,
    _SameOriginRedirectHandler,
    response_for,
    serve,
)
from zekam.domain.client_integration import ClientIntegrationId
from zekam.domain.errors import ConfigurationError
from zekam.domain.mcp_integration import McpServerRegistration, McpTransport


def test_stdio_registration_is_secret_free_and_deterministic() -> None:
    registration = McpServerRegistration(
        name="innova-atlassian",
        transport=McpTransport.STDIO,
        command=("zekam", "mcp", "serve", "innova-atlassian"),
        env_vars=("JIRA_API_TOKEN", "CONFLUENCE_API_TOKEN"),
        clients=(ClientIntegrationId.OPENCODE, ClientIntegrationId.CODEX),
    )

    assert registration.as_dict()["env_vars"] == [
        "JIRA_API_TOKEN",
        "CONFLUENCE_API_TOKEN",
    ]
    assert "Bearer" not in json.dumps(registration.as_dict())
    assert registration.registration_digest.startswith("sha256:")


@pytest.mark.parametrize("invalid", ["TOKEN=value", "bad-name!", " A"])
def test_registration_rejects_invalid_environment_reference(invalid: str) -> None:
    with pytest.raises(ConfigurationError):
        McpServerRegistration(
            name="sample",
            transport=McpTransport.STDIO,
            command=("sample",),
            env_vars=(invalid,),
        )


def test_http_registration_rejects_url_credentials() -> None:
    with pytest.raises(ConfigurationError):
        McpServerRegistration(
            name="sample",
            transport=McpTransport.HTTP,
            url="https://user:pass@example.test/mcp",
        )


def test_bearer_registration_and_builtin_api_require_https() -> None:
    with pytest.raises(ConfigurationError, match="HTTPS"):
        McpServerRegistration(
            name="sample",
            transport=McpTransport.HTTP,
            url="http://example.test/mcp",
            bearer_token_env_var="API_TOKEN",
        )
    with pytest.raises(RuntimeError, match="gecersiz"):
        _base_url({"API_BASE": "http://example.test"}, "API_BASE", "https://default.test")


def test_builtin_api_refuses_cross_origin_redirect() -> None:
    request = urllib.request.Request("https://jira.example.test/rest/api/2/search")
    with pytest.raises(urllib.error.HTTPError, match="cross-origin"):
        _SameOriginRedirectHandler().redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            "https://attacker.example.test/collect",
        )


def test_builtin_server_lists_only_read_tools() -> None:
    response = response_for({"jsonrpc": "2.0", "id": 7, "method": "tools/list"})
    assert response is not None
    names = [item["name"] for item in response["result"]["tools"]]
    assert names == [
        "jira_search",
        "jira_get_issue",
        "confluence_search",
        "confluence_get_page",
    ]
    assert all("create" not in name and "update" not in name for name in names)


def test_builtin_server_stdio_handshake() -> None:
    source = io.StringIO(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}) + "\n")
    target = io.StringIO()
    serve(source, target)
    response = json.loads(target.getvalue())
    assert response["id"] == 1
    assert response["result"]["serverInfo"]["name"] == "innova-atlassian-local"
    assert response["result"]["capabilities"] == {"tools": {}}
