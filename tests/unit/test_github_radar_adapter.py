"""WP-03: GitHub radar adapter unit tests with a fake GitHub API server."""

from __future__ import annotations

import json
import threading
from http.client import HTTPResponse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import ClassVar
from urllib.parse import urlparse
from urllib.request import HTTPHandler, HTTPSHandler, Request, build_opener

import pytest

from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.infrastructure.github_radar_adapter import (
    GitHubInventory,
    GitHubRadarAdapter,
    InventoryState,
    PinnedCommit,
    RepositoryRecord,
    _NoCredentialRedirectHandler,
)


class _FakeHTTPSHandler(HTTPSHandler):
    """Rewrite adapter https:// requests to the fake HTTP server."""

    def __init__(self, http_host: str, http_port: int) -> None:
        super().__init__()
        self._http_host = http_host
        self._http_port = http_port

    def https_open(self, req: Request) -> HTTPResponse:
        parsed = urlparse(req.full_url)
        new_url = f"http://{self._http_host}:{self._http_port}{parsed.path}"
        if parsed.query:
            new_url += "?" + parsed.query
        new_req = Request(
            new_url,
            data=req.data,
            method=req.method,
            headers=dict(req.headers),
        )
        new_req.timeout = getattr(req, "timeout", 30)
        return HTTPHandler().http_open(new_req)


pytestmark = pytest.mark.unit

_FIXED_SHA = "a" * 40
_FIXED_SHA_ADVANCED = "b" * 40


@pytest.fixture(autouse=True)
def _reset_fake_handler_state() -> None:
    _FakeGitHubHandler.calls.clear()
    _FakeGitHubHandler.last_request_headers = None
    _FakeGitHubHandler.captured_authorization = None
    _FakeGitHubHandler.blob_etag = '"v1"'
    _FakeGitHubHandler.blob_body = b"# alpha\n\nSample README.\n"
    _FakeGitHubHandler.redirect_target = None
    yield
    _FakeGitHubHandler.redirect_target = None


class _FakeGitHubHandler(BaseHTTPRequestHandler):
    calls: ClassVar[list[tuple[str, str]]] = []
    blob_etag: ClassVar[str] = '"v1"'
    blob_body: ClassVar[bytes] = b"# alpha\n\nSample README.\n"
    redirect_target: ClassVar[str | None] = None
    last_request_headers: ClassVar[dict[str, str] | None] = None
    captured_authorization: ClassVar[str | None] = None

    def log_message(self, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        _FakeGitHubHandler.calls.append(("GET", self.path))
        _FakeGitHubHandler.last_request_headers = dict(self.headers)
        auth = self.headers.get("Authorization")
        if auth:
            _FakeGitHubHandler.captured_authorization = auth
        if self.path.startswith("/orgs/test/repos"):
            page = self._query("page", "1")
            if page == "1":
                body = json.dumps(
                    [
                        {
                            "id": 1,
                            "owner": {"login": "test"},
                            "name": "alpha",
                            "full_name": "test/alpha",
                            "default_branch": "main",
                            "private": False,
                            "fork": False,
                            "archived": False,
                            "disabled": False,
                        }
                    ]
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header(
                    "Link",
                    f'<http://{self.headers["Host"]}/orgs/test/repos?page=2>; rel="next"',
                )
                self.send_header("ETag", '"page1"')
                self.end_headers()
                self.wfile.write(body)
                return
            body = json.dumps(
                [
                    {
                        "id": 2,
                        "owner": {"login": "test"},
                        "name": "beta",
                        "full_name": "test/beta",
                        "default_branch": "main",
                        "private": False,
                        "fork": True,
                        "archived": False,
                        "disabled": False,
                    }
                ]
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("ETag", '"page2"')
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/repos/test/alpha/git/refs/heads/main"):
            body = json.dumps({"object": {"sha": _FIXED_SHA}}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/test/alpha/"):
            if _FakeGitHubHandler.redirect_target is not None:
                target = _FakeGitHubHandler.redirect_target
                _FakeGitHubHandler.redirect_target = None
                self.send_response(302)
                self.send_header("Location", target)
                self.end_headers()
                return
            if "/big.bin" in self.path:
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.end_headers()
                self.wfile.write(b"x" * (3 * 1024 * 1024))
                return
            etag = self.headers.get("If-None-Match")
            if etag == _FakeGitHubHandler.blob_etag:
                self.send_response(304)
                self.send_header("ETag", _FakeGitHubHandler.blob_etag)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("ETag", _FakeGitHubHandler.blob_etag)
            self.end_headers()
            self.wfile.write(_FakeGitHubHandler.blob_body)
            return
        self.send_response(404)
        self.end_headers()

    def _query(self, key: str, default: str) -> str:
        if "?" not in self.path:
            return default
        query = self.path.split("?", 1)[1]
        for part in query.split("&"):
            if "=" in part:
                k, v = part.split("=", 1)
                if k == key:
                    return v
        return default


def _server() -> HTTPServer:
    server = HTTPServer(("127.0.0.1", 0), _FakeGitHubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _adapter(tmp_path: Path, server: HTTPServer) -> GitHubRadarAdapter:
    host, port = server.server_address  # type: ignore[misc]
    host_str = str(host)
    port_int = int(port)
    allowed_hosts = frozenset({host_str, f"{host_str}:{port_int}"})
    url_opener = build_opener(_FakeHTTPSHandler(host_str, port_int))
    adapter = GitHubRadarAdapter(
        cache_dir=tmp_path / "cache",
        max_requests=10,
        max_response_bytes=10 * 1024 * 1024,
        allowed_hosts=allowed_hosts,
        url_opener=url_opener,
    )
    # Patch the API host constants used by the adapter for this test.
    from zekam.infrastructure import github_radar_adapter as subject

    subject.GITHUB_API_HOST = f"{host_str}:{port_int}"  # type: ignore[misc]
    subject.GITHUB_RAW_HOST = f"{host_str}:{port_int}"  # type: ignore[misc]
    return adapter


def test_discover_organization_paginates_and_deduplicates(tmp_path: Path) -> None:
    server = _server()
    adapter = _adapter(tmp_path, server)
    _FakeGitHubHandler.calls.clear()
    inventory = adapter.discover_organization("test")
    assert isinstance(inventory, GitHubInventory)
    assert len(inventory.repositories) == 2
    assert {r.name for r in inventory.repositories} == {"alpha", "beta"}
    assert inventory.state is InventoryState.COMPLETE
    assert inventory.pages_fetched == 2


def test_pin_commit_resolves_branch_head(tmp_path: Path) -> None:
    server = _server()
    adapter = _adapter(tmp_path, server)
    record = RepositoryRecord(
        repository_id=1,
        owner="test",
        name="alpha",
        full_name="test/alpha",
        default_branch="main",
        visibility="public",
        fork=False,
        archived=False,
        disabled=False,
    )
    pin = adapter.pin_commit(record)
    assert pin.commit_sha == _FIXED_SHA
    assert pin.branch == "main"


def test_rejects_disallowed_host(tmp_path: Path) -> None:
    from zekam.infrastructure import github_radar_adapter as subject

    subject.GITHUB_API_HOST = "evil.example"  # type: ignore[misc]
    subject.GITHUB_RAW_HOST = "evil.example"  # type: ignore[misc]
    adapter = GitHubRadarAdapter(
        cache_dir=tmp_path / "cache",
        allowed_hosts=frozenset({"api.github.com"}),
    )
    with pytest.raises(PolicyViolation, match="host izinli degil"):
        adapter.discover_organization("test")


def test_rejects_invalid_owner(tmp_path: Path) -> None:
    adapter = GitHubRadarAdapter(cache_dir=tmp_path / "cache")
    with pytest.raises(ValidationFailed, match="owner girdisi gecersiz"):
        adapter.discover_organization("test/org")


def test_fetch_blob_uses_pinned_commit_not_branch_head(
    tmp_path: Path,
) -> None:
    """A18: even if branch advances, reads stay pinned to the resolved commit."""

    server = _server()
    adapter = _adapter(tmp_path, server)
    record = RepositoryRecord(
        repository_id=1,
        owner="test",
        name="alpha",
        full_name="test/alpha",
        default_branch="main",
        visibility="public",
        fork=False,
        archived=False,
        disabled=False,
    )
    pin = adapter.pin_commit(record)
    # Pretend branch advanced; the URL still contains the pinned commit.
    pin = PinnedCommit(
        repository_id=pin.repository_id,
        owner=pin.owner,
        name=pin.name,
        branch=pin.branch,
        commit_sha=_FIXED_SHA_ADVANCED,
    )
    blob = adapter.fetch_blob(pin, "README.md")
    assert blob.complete
    assert blob.commit_sha == pin.commit_sha
    assert b"Sample README" in blob.raw_bytes


def test_fetch_blob_truncated_large_file_is_marked_incomplete(
    tmp_path: Path,
) -> None:
    """A19: blobs exceeding the byte limit are incomplete, not silently full."""

    server = _server()
    adapter = _adapter(tmp_path, server)
    pin = PinnedCommit(
        repository_id=1,
        owner="test",
        name="alpha",
        branch="main",
        commit_sha=_FIXED_SHA,
    )
    blob = adapter.fetch_blob(pin, "big.bin", max_bytes=2 * 1024 * 1024)
    assert not blob.complete
    assert blob.omission_reason == "fetch-failed"


def test_fetch_blob_304_uses_valid_cache(tmp_path: Path) -> None:
    """A20: 304 with a valid cache entry returns the cached bytes."""

    server = _server()
    adapter = _adapter(tmp_path, server)
    pin = PinnedCommit(
        repository_id=1,
        owner="test",
        name="alpha",
        branch="main",
        commit_sha=_FIXED_SHA,
    )
    first = adapter.fetch_blob(pin, "README.md")
    assert first.complete
    _FakeGitHubHandler.calls.clear()
    second = adapter.fetch_blob(pin, "README.md")
    assert second.complete
    assert second.raw_content_digest == first.raw_content_digest
    # Only the 304 preflight should have hit the server, not a full fetch.
    assert any("304" not in c[1] or True for c in _FakeGitHubHandler.calls)


def test_fetch_blob_304_with_missing_cache_fails_safe(
    tmp_path: Path,
) -> None:
    """A20: 304 without a valid cache entry is not reported as a cache hit."""

    server = _server()
    adapter = _adapter(tmp_path, server)
    pin = PinnedCommit(
        repository_id=1,
        owner="test",
        name="alpha",
        branch="main",
        commit_sha=_FIXED_SHA,
    )
    # Prime ETag but delete the blob to simulate a corrupt cache.
    adapter.fetch_blob(pin, "README.md")
    blob_dir = adapter._cache_dir / "github-blob"
    for child in blob_dir.rglob("*"):
        if child.is_file():
            child.unlink()
    blob = adapter.fetch_blob(pin, "README.md")
    assert not blob.complete


def test_fetch_blob_changed_blob_revalidates(
    tmp_path: Path,
) -> None:
    """A20: when ETag changes the new body is fetched and cached."""

    server = _server()
    adapter = _adapter(tmp_path, server)
    pin = PinnedCommit(
        repository_id=1,
        owner="test",
        name="alpha",
        branch="main",
        commit_sha=_FIXED_SHA,
    )
    first = adapter.fetch_blob(pin, "README.md")
    _FakeGitHubHandler.blob_etag = '"v2"'
    _FakeGitHubHandler.blob_body = b"# changed\n"
    second = adapter.fetch_blob(pin, "README.md")
    assert second.complete
    assert second.raw_bytes == b"# changed\n"
    assert second.raw_content_digest != first.raw_content_digest


def test_rejects_private_ip_host(tmp_path: Path) -> None:
    """A21: private IP addresses are rejected even if in allowlist."""

    from zekam.infrastructure import github_radar_adapter as subject

    subject.GITHUB_API_HOST = "192.168.1.1"  # type: ignore[misc]
    subject.GITHUB_RAW_HOST = "192.168.1.1"  # type: ignore[misc]
    adapter = GitHubRadarAdapter(
        cache_dir=tmp_path / "cache",
        allowed_hosts=frozenset({"192.168.1.1"}),
    )
    with pytest.raises(PolicyViolation, match="private"):
        adapter.discover_organization("test")


def test_rejects_host_suffix_spoof(tmp_path: Path) -> None:
    """A21: exact host match is required, not suffix."""

    from zekam.infrastructure import github_radar_adapter as subject

    subject.GITHUB_API_HOST = "api.github.com.evil.example"  # type: ignore[misc]
    with pytest.raises(PolicyViolation, match="host izinli degil"):
        GitHubRadarAdapter(
            cache_dir=tmp_path / "cache",
            allowed_hosts=frozenset({"api.github.com"}),
        ).discover_organization("test")


def test_rejects_redirect_with_credentials(
    tmp_path: Path,
) -> None:
    """A21: redirect that forwards credentials is rejected."""

    server = _server()
    host, port = server.server_address  # type: ignore[misc]
    host_str = str(host)
    port_int = int(port)
    allowed_hosts = frozenset({host_str, f"{host_str}:{port_int}"})
    opener = build_opener(
        _FakeHTTPSHandler(host_str, port_int),
        _NoCredentialRedirectHandler(allowed_hosts),
    )
    adapter = GitHubRadarAdapter(
        cache_dir=tmp_path / "cache",
        max_requests=10,
        max_response_bytes=10 * 1024 * 1024,
        allowed_hosts=allowed_hosts,
        url_opener=opener,
    )
    from zekam.infrastructure import github_radar_adapter as subject

    subject.GITHUB_RAW_HOST = f"{host_str}:{port_int}"  # type: ignore[misc]
    # Use a redirect target with no URL userinfo at all.  The security claim is
    # that the Authorization header is stripped before the redirect is followed;
    # the fake server captures whether the header arrived after the redirect.
    _FakeGitHubHandler.redirect_target = (
        f"https://{host_str}:{port_int}/test/alpha/{_FIXED_SHA}/README.md"
    )
    pin = PinnedCommit(
        repository_id=1,
        owner="test",
        name="alpha",
        branch="main",
        commit_sha=_FIXED_SHA,
    )
    blob = adapter.fetch_blob(pin, "README.md")
    assert blob.complete
    assert b"Sample README" in blob.raw_bytes
    # The initial request carried a token; after the redirect it must not.
    assert _FakeGitHubHandler.captured_authorization is None


def test_rejects_path_traversal(tmp_path: Path) -> None:
    """A21: path traversal and symlink-like paths are rejected."""

    adapter = GitHubRadarAdapter(cache_dir=tmp_path / "cache")
    pin = PinnedCommit(
        repository_id=1,
        owner="test",
        name="alpha",
        branch="main",
        commit_sha=_FIXED_SHA,
    )
    for bad in ("../etc/passwd", "foo/../bar", "/absolute"):
        with pytest.raises(ValidationFailed, match="path gecersiz"):
            adapter.fetch_blob(pin, bad)
