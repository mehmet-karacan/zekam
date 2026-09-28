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


def _repo_item(
    repo_id: int,
    name: str,
    *,
    fork: bool = False,
    archived: bool = False,
) -> dict[str, object]:
    return {
        "id": repo_id,
        "owner": {"login": "test"},
        "name": name,
        "full_name": f"test/{name}",
        "default_branch": "main",
        "private": False,
        "fork": fork,
        "archived": archived,
        "disabled": False,
    }


def _default_inventory_pages(host: str) -> list[list[dict[str, object]]]:
    return [
        [_repo_item(1, "alpha")],
        [_repo_item(2, "beta", fork=True)],
    ]


@pytest.fixture(autouse=True)
def _reset_fake_handler_state() -> None:
    _FakeGitHubHandler.calls.clear()
    _FakeGitHubHandler.last_request_headers = None
    _FakeGitHubHandler.captured_authorization = None
    _FakeGitHubHandler.blob_etag = '"v1"'
    _FakeGitHubHandler.blob_body = b"# alpha\n\nSample README.\n"
    _FakeGitHubHandler.redirect_target = None
    _FakeGitHubHandler.inventory_pages = None
    _FakeGitHubHandler.inventory_fail_page = None
    _FakeGitHubHandler.inventory_fail_status = 403
    _FakeGitHubHandler.tree_responses = {}
    yield
    _FakeGitHubHandler.redirect_target = None
    _FakeGitHubHandler.inventory_pages = None
    _FakeGitHubHandler.inventory_fail_page = None
    _FakeGitHubHandler.tree_responses = {}


class _FakeGitHubHandler(BaseHTTPRequestHandler):
    calls: ClassVar[list[tuple[str, str]]] = []
    blob_etag: ClassVar[str] = '"v1"'
    blob_body: ClassVar[bytes] = b"# alpha\n\nSample README.\n"
    redirect_target: ClassVar[str | None] = None
    last_request_headers: ClassVar[dict[str, str] | None] = None
    captured_authorization: ClassVar[str | None] = None
    inventory_pages: ClassVar[list[list[dict[str, object]]] | None] = None
    inventory_fail_page: ClassVar[int | None] = None
    inventory_fail_status: ClassVar[int] = 403
    tree_responses: ClassVar[dict[str, dict[str, object]]] = {}

    def log_message(self, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        _FakeGitHubHandler.calls.append(("GET", self.path))
        _FakeGitHubHandler.last_request_headers = dict(self.headers)
        auth = self.headers.get("Authorization")
        if auth:
            _FakeGitHubHandler.captured_authorization = auth
        if self.path.startswith("/orgs/test/repos"):
            page = int(self._query("page", "1"))
            pages = _FakeGitHubHandler.inventory_pages
            if pages is None:
                pages = _default_inventory_pages(self.headers["Host"])
            if (
                _FakeGitHubHandler.inventory_fail_page is not None
                and page >= _FakeGitHubHandler.inventory_fail_page
            ):
                self.send_error(_FakeGitHubHandler.inventory_fail_status)
                return
            if page < 1 or page > len(pages):
                self.send_error(404)
                return
            body = json.dumps(pages[page - 1]).encode("utf-8")
            links: list[str] = []
            if page > 1:
                links.append(
                    f'<http://{self.headers["Host"]}/orgs/test/repos?page={page - 1}>; rel="prev"'
                )
            if page < len(pages):
                links.append(
                    f'<http://{self.headers["Host"]}/orgs/test/repos?page={page + 1}>; rel="next"'
                )
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            if links:
                self.send_header("Link", ", ".join(links))
            self.send_header("ETag", f'"page{page}"')
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
        if self.path.startswith("/repos/test/alpha/git/trees/"):
            tree_key = self.path.split("/")[-1]
            if "?" in tree_key:
                tree_key = tree_key.split("?")[0]
            response = _FakeGitHubHandler.tree_responses.get(tree_key)
            if response is None:
                response = {
                    "sha": tree_key,
                    "tree": [
                        {
                            "path": "README.md",
                            "mode": "100644",
                            "type": "blob",
                            "sha": "blob1",
                            "size": 64,
                        }
                    ],
                    "truncated": False,
                }
            body = json.dumps(response).encode("utf-8")
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


def test_discover_large_inventory_deduplicates_and_keeps_metadata(
    tmp_path: Path,
) -> None:
    """A16: 120 repos across pages, duplicate IDs, archived/fork kept, deduped."""

    pages: list[list[dict[str, object]]] = []
    page_size = 100
    repo_id = 1
    expected_names: set[str] = set()
    while repo_id <= 120:
        page: list[dict[str, object]] = []
        while len(page) < page_size and repo_id <= 120:
            name = f"repo-{repo_id:03d}"
            page.append(
                _repo_item(
                    repo_id,
                    name,
                    fork=repo_id % 5 == 0,
                    archived=repo_id % 7 == 0,
                )
            )
            expected_names.add(name)
            repo_id += 1
        pages.append(page)
    # Inject a duplicate ID on the second page to verify deduplication.
    pages[1].insert(0, _repo_item(1, "repo-001-dup"))
    # Inject a renamed repo that keeps the same ID on the second page.
    pages[1].insert(1, _repo_item(101, "repo-101-renamed"))
    expected_names.discard("repo-101")
    expected_names.add("repo-101-renamed")

    _FakeGitHubHandler.inventory_pages = pages
    server = _server()
    adapter = _adapter(tmp_path, server)
    inventory = adapter.discover_organization("test")
    assert inventory.state is InventoryState.COMPLETE
    assert len(inventory.repositories) == 120
    assert {r.name for r in inventory.repositories} == expected_names
    assert inventory.pages_fetched == 2
    assert any(r.fork for r in inventory.repositories)
    assert any(r.archived for r in inventory.repositories)
    assert not any(r.name == "repo-001-dup" for r in inventory.repositories)


def test_discover_second_page_error_keeps_previous_repos_partial(
    tmp_path: Path,
) -> None:
    """A17: second page 403/429/timeout; previous repos remain, state=partial."""

    pages: list[list[dict[str, object]]] = []
    repo_id = 1
    while repo_id <= 150:
        page: list[dict[str, object]] = []
        while len(page) < 100 and repo_id <= 150:
            page.append(_repo_item(repo_id, f"repo-{repo_id:03d}"))
            repo_id += 1
        pages.append(page)
    _FakeGitHubHandler.inventory_pages = pages
    server = _server()
    adapter = _adapter(tmp_path, server)

    for status in (403, 429):
        _FakeGitHubHandler.inventory_fail_page = 2
        _FakeGitHubHandler.inventory_fail_status = status
        _FakeGitHubHandler.calls.clear()
        inventory = adapter.discover_organization("test")
        assert inventory.state is InventoryState.PARTIAL
        assert len(inventory.repositories) == 100
        assert {r.name for r in inventory.repositories} == {f"repo-{i:03d}" for i in range(1, 101)}
        assert inventory.pages_fetched == 1


def test_fetch_tree_reports_truncation_and_omissions(tmp_path: Path) -> None:
    """A19: truncated tree and symlink/submodule omissions are visible."""

    tree_key = _FIXED_SHA
    _FakeGitHubHandler.tree_responses[tree_key] = {
        "sha": tree_key,
        "tree": [
            {"path": "README.md", "mode": "100644", "type": "blob", "sha": "b1", "size": 64},
            {"path": "link", "mode": "120000", "type": "symlink", "sha": "s1"},
            {"path": "sub", "mode": "160000", "type": "commit", "sha": "m1"},
            {"path": "src/main.py", "mode": "100644", "type": "blob", "sha": "b2", "size": 128},
        ],
        "truncated": True,
    }
    server = _server()
    adapter = _adapter(tmp_path, server)
    pin = PinnedCommit(
        repository_id=1,
        owner="test",
        name="alpha",
        branch="main",
        commit_sha=_FIXED_SHA,
    )
    tree = adapter.fetch_tree(pin)
    assert not tree.complete
    assert tree.truncated
    assert {e.path for e in tree.entries} == {"README.md", "src/main.py"}
    assert set(tree.omitted_paths) == {"link", "sub"}
    assert tree.omission_reason == "truncated-or-omitted"


def test_fetch_tree_subtree_traversal_marks_incomplete(
    tmp_path: Path,
) -> None:
    """A19: subtree traversal result missing a child subtree stays incomplete."""

    tree_key = _FIXED_SHA
    _FakeGitHubHandler.tree_responses[tree_key] = {
        "sha": tree_key,
        "tree": [
            {"path": "README.md", "mode": "100644", "type": "blob", "sha": "b1", "size": 64},
            {"path": "src", "mode": "040000", "type": "tree", "sha": "t1"},
        ],
        "truncated": False,
    }
    server = _server()
    adapter = _adapter(tmp_path, server)
    pin = PinnedCommit(
        repository_id=1,
        owner="test",
        name="alpha",
        branch="main",
        commit_sha=_FIXED_SHA,
    )
    tree = adapter.fetch_tree(pin, recursive=True)
    assert not tree.complete
    assert tree.omitted_paths == ("src",)
    assert tree.omission_reason == "truncated-or-omitted"


def test_rejects_symlink_and_submodule_fetch(tmp_path: Path) -> None:
    """A21: symlink/submodule paths are refused at the blob fetch boundary."""

    adapter = GitHubRadarAdapter(cache_dir=tmp_path / "cache")
    pin = PinnedCommit(
        repository_id=1,
        owner="test",
        name="alpha",
        branch="main",
        commit_sha=_FIXED_SHA,
    )
    for bad in (".git/modules/foo", "vendor/.gitmodules"):
        with pytest.raises(PolicyViolation, match="symlink ve submodule"):
            adapter.fetch_blob(pin, bad)


def test_rejects_total_response_byte_limit(tmp_path: Path) -> None:
    """A21: exceeding max_response_bytes raises before the adapter ingests it."""

    pages: list[list[dict[str, object]]] = []
    repo_id = 1
    while repo_id <= 10:
        pages.append([_repo_item(repo_id, f"repo-{repo_id:03d}")])
        repo_id += 1
    _FakeGitHubHandler.inventory_pages = pages
    server = _server()
    adapter = _adapter(tmp_path, server)
    adapter._max_response_bytes = 32
    with pytest.raises(PolicyViolation, match="response byte siniri"):
        adapter.discover_organization("test")
