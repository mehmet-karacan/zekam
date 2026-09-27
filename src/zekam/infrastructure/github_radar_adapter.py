"""Read-only GitHub public source discovery, pin and cache adapter.

This adapter supports the engineering-radar campaign discover/analyse stages.
It is intentionally not a generic Git client: it fetches public organization
inventory, resolves a branch HEAD to an immutable commit, and reads blobs from
that commit through the GitHub API.  All network calls are host-allowlisted and
all fetched blobs are content-addressed in the user artifact cache.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlparse
from urllib.request import build_opener

from zekam.domain.errors import PolicyViolation, ValidationFailed

GITHUB_API_HOST: Final = "api.github.com"
GITHUB_RAW_HOST: Final = "raw.githubusercontent.com"
DEFAULT_ALLOWED_HOSTS: Final = frozenset({GITHUB_API_HOST, GITHUB_RAW_HOST})
DEFAULT_PER_PAGE: Final = 100
MAX_INVENTORY_REPOS: Final = 10_000
MAX_SELECTED_REPOS: Final = 12
MAX_SELECTED_PATHS: Final = 192
MAX_TOTAL_REQUESTS: Final = 100
MAX_TOTAL_RESPONSE_BYTES: Final = 32 * 1024 * 1024
MAX_BLOB_BYTES: Final = 2 * 1024 * 1024
MAX_REDIRECTS: Final = 2


class PolicyViolation(PolicyViolation):
    """Radar-specific policy violation."""


class _NoCredentialRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Redirect handler that refuses credential forwarding and host escape."""

    def __init__(self, allowed_hosts: frozenset[str]) -> None:
        self._allowed_hosts = allowed_hosts

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        parsed = urlparse(newurl)
        if parsed.username is not None or parsed.password is not None:
            raise PolicyViolation("Redirect credential forwarding yasak")
        if parsed.hostname not in self._allowed_hosts:
            raise PolicyViolation("Redirect host izinli degil")
        new_req = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new_req is not None:
            new_req.remove_header("Authorization")
        return new_req


class InventoryState(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"


@dataclass(frozen=True, slots=True)
class RepositoryRecord:
    repository_id: int
    owner: str
    name: str
    full_name: str
    default_branch: str
    visibility: str
    fork: bool
    archived: bool
    disabled: bool
    description: str | None = None
    language: str | None = None
    license_spdx: str | None = None
    pushed_at: str | None = None
    observed_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def as_dict(self) -> dict[str, Any]:
        return {
            "repository_id": self.repository_id,
            "owner": self.owner,
            "name": self.name,
            "full_name": self.full_name,
            "default_branch": self.default_branch,
            "visibility": self.visibility,
            "fork": self.fork,
            "archived": self.archived,
            "disabled": self.disabled,
            "description": self.description,
            "language": self.language,
            "license_spdx": self.license_spdx,
            "pushed_at": self.pushed_at,
            "observed_at": self.observed_at,
        }


@dataclass(frozen=True, slots=True)
class PinnedCommit:
    repository_id: int
    owner: str
    name: str
    branch: str
    commit_sha: str
    pinned_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def as_dict(self) -> dict[str, Any]:
        return {
            "repository_id": self.repository_id,
            "owner": self.owner,
            "name": self.name,
            "branch": self.branch,
            "commit_sha": self.commit_sha,
            "pinned_at": self.pinned_at,
        }


@dataclass(frozen=True, slots=True)
class SourceBlob:
    repository_id: int
    commit_sha: str
    path: str
    blob_sha: str | None
    raw_bytes: bytes | None
    raw_content_digest: str | None
    complete: bool
    omission_reason: str | None = None

    def __post_init__(self) -> None:
        if self.raw_bytes is not None and self.raw_content_digest is None:
            raise ValidationFailed("SourceBlob raw bytes varsa digest zorunlu")
        if not self.complete and self.omission_reason is None:
            raise ValidationFailed("tamamlanmamis blob omission_reason ister")


@dataclass(frozen=True, slots=True)
class FetchReceipt:
    method: str
    url: str
    status_code: int | None
    response_bytes: int
    observed_at: str
    etag: str | None = None
    link_header: str | None = None
    cache_hit: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "url": self.url,
            "status_code": self.status_code,
            "response_bytes": self.response_bytes,
            "observed_at": self.observed_at,
            "etag": self.etag,
            "link_header": self.link_header,
            "cache_hit": self.cache_hit,
        }


@dataclass(frozen=True, slots=True)
class GitHubInventory:
    owner: str
    repositories: tuple[RepositoryRecord, ...]
    state: InventoryState
    pages_fetched: int
    total_requests: int
    total_response_bytes: int
    receipts: tuple[FetchReceipt, ...]
    next_safe_action: str
    observed_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


class GitHubRadarAdapter:
    """Bounded public GitHub source adapter with content-addressed blob cache."""

    def __init__(
        self,
        *,
        cache_dir: Path,
        token: str | None = None,
        max_requests: int = MAX_TOTAL_REQUESTS,
        max_response_bytes: int = MAX_TOTAL_RESPONSE_BYTES,
        allowed_hosts: frozenset[str] = DEFAULT_ALLOWED_HOSTS,
        url_opener: urllib.request.OpenerDirector | None = None,
    ) -> None:
        if not cache_dir.is_absolute():
            raise ValidationFailed("GitHub radar cache absolute path ister")
        self._cache_dir = cache_dir
        self._token = token
        self._max_requests = max_requests
        self._max_response_bytes = max_response_bytes
        self._allowed_hosts = allowed_hosts
        if url_opener is None:
            self._url_opener: urllib.request.OpenerDirector | None = build_opener(
                _NoCredentialRedirectHandler(self._allowed_hosts)
            )
        else:
            self._url_opener = url_opener
        self._request_count = 0
        self._response_byte_count = 0
        self._receipts: list[FetchReceipt] = []

    def discover_organization(
        self, owner: str, *, max_repos: int = MAX_INVENTORY_REPOS
    ) -> GitHubInventory:
        """Page through the public organization repository list."""

        if not owner or "/" in owner or "?" in owner:
            raise ValidationFailed("GitHub owner girdisi gecersiz")
        repositories: list[RepositoryRecord] = []
        seen: set[int] = set()
        url: str | None = (
            f"https://{GITHUB_API_HOST}/orgs/{owner}/repos?per_page={DEFAULT_PER_PAGE}"
        )
        pages = 0
        while url is not None and len(repositories) < max_repos:
            if self._request_count >= self._max_requests:
                break
            data, receipt = self._get_json(url, max_bytes=4 * 1024 * 1024)
            if data is None:
                break
            self._receipts.append(receipt)
            if not isinstance(data, list):
                break
            pages += 1
            for item in data:
                if not isinstance(item, dict):
                    continue
                repo_id = item.get("id")
                if not isinstance(repo_id, int) or repo_id in seen:
                    continue
                seen.add(repo_id)
                repositories.append(_record_from_api(repo_id, item))
            url = _next_page_url(receipt.link_header)
            if url is not None:
                url = self._validate_url(url)
        state = InventoryState.COMPLETE if url is None else InventoryState.PARTIAL
        return GitHubInventory(
            owner=owner,
            repositories=tuple(repositories),
            state=state,
            pages_fetched=pages,
            total_requests=self._request_count,
            total_response_bytes=self._response_byte_count,
            receipts=tuple(self._receipts),
            next_safe_action="analyse" if repositories else "needs-discovery",
        )

    def pin_commit(self, record: RepositoryRecord, branch: str | None = None) -> PinnedCommit:
        """Resolve the default branch HEAD to an immutable commit SHA."""

        branch = branch or record.default_branch
        url = (
            f"https://{GITHUB_API_HOST}/repos/{record.owner}/{record.name}/git/refs/heads/{branch}"
        )
        data, _receipt = self._get_json(url)
        if not isinstance(data, dict):
            raise ValidationFailed("GitHub branch ref beklenen sekilde degil")
        object_data = data.get("object")
        if not isinstance(object_data, dict):
            raise ValidationFailed("GitHub branch ref object beklenen sekilde degil")
        commit_sha = object_data.get("sha")
        if not isinstance(commit_sha, str):
            raise ValidationFailed("GitHub branch commit sha beklenen sekilde degil")
        return PinnedCommit(
            repository_id=record.repository_id,
            owner=record.owner,
            name=record.name,
            branch=branch,
            commit_sha=commit_sha,
        )

    def fetch_blob(
        self, pin: PinnedCommit, path: str, *, max_bytes: int = MAX_BLOB_BYTES
    ) -> SourceBlob:
        """Fetch one blob from the pinned commit via the raw content host."""

        if not path or ".." in path or path.startswith("/") or "\\" in path:
            raise ValidationFailed("GitHub blob path gecersiz")
        if pin.commit_sha != _clean_sha(pin.commit_sha):
            raise ValidationFailed("GitHub commit sha gecersiz")
        url = f"https://{GITHUB_RAW_HOST}/{pin.owner}/{pin.name}/{pin.commit_sha}/{path}"
        cached, _cached_etag = self._cache_lookup(url)
        # Always issue a conditional request so that a changed blob is
        # revalidated rather than silently served from cache (A20).
        data, receipt = self._get_bytes(url, max_bytes=max_bytes)
        if data is None:
            if receipt.status_code == 304 and cached is not None:
                return cached
            return SourceBlob(
                repository_id=pin.repository_id,
                commit_sha=pin.commit_sha,
                path=path,
                blob_sha=None,
                raw_bytes=None,
                raw_content_digest=None,
                complete=False,
                omission_reason="fetch-failed",
            )
        content_digest = "sha256:" + hashlib.sha256(data).hexdigest()
        self._cache_store(url, content_digest, data, receipt.etag, pin, path)
        return SourceBlob(
            repository_id=pin.repository_id,
            commit_sha=pin.commit_sha,
            path=path,
            blob_sha=_git_blob_sha(data),
            raw_bytes=data,
            raw_content_digest=content_digest,
            complete=True,
        )

    def _cache_lookup(self, url: str) -> tuple[SourceBlob | None, str | None]:
        # Cache metadata is keyed by URL digest; content is keyed by content digest.
        meta_path = self._cache_dir / "github-meta" / _url_digest(url)
        if not meta_path.exists():
            return None, None
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None, None
        content_digest = meta.get("content_digest")
        etag = meta.get("etag")
        if not isinstance(content_digest, str):
            return None, None
        blob_path = self._cache_dir / "github-blob" / content_digest.replace(":", os.sep)
        if not blob_path.exists():
            return None, None
        try:
            data = blob_path.read_bytes()
        except OSError:
            return None, None
        if "sha256:" + hashlib.sha256(data).hexdigest() != content_digest:
            return None, None
        pin = meta.get("pin", {})
        return (
            SourceBlob(
                repository_id=pin.get("repository_id", 0),
                commit_sha=pin.get("commit_sha", ""),
                path=pin.get("path", ""),
                blob_sha=_git_blob_sha(data),
                raw_bytes=data,
                raw_content_digest=content_digest,
                complete=True,
                omission_reason=None,
            ),
            etag,
        )

    def _cache_store(
        self,
        url: str,
        content_digest: str,
        data: bytes,
        etag: str | None,
        pin: PinnedCommit | None = None,
        path: str | None = None,
    ) -> None:
        blob_dir = self._cache_dir / "github-blob" / content_digest.replace(":", os.sep)
        blob_dir.parent.mkdir(parents=True, exist_ok=True)
        blob_dir.write_bytes(data)
        meta_path = self._cache_dir / "github-meta" / _url_digest(url)
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta: dict[str, Any] = {
            "content_digest": content_digest,
            "etag": etag,
            "url": url,
        }
        if pin is not None:
            meta["pin"] = {
                "repository_id": pin.repository_id,
                "commit_sha": pin.commit_sha,
                "path": path or "",
            }
        meta_path.write_text(
            json.dumps(meta, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )

    def _get_json(
        self, url: str, *, max_bytes: int = MAX_BLOB_BYTES
    ) -> tuple[Any | None, FetchReceipt]:
        data, receipt = self._get_bytes(url, max_bytes=max_bytes)
        if data is None:
            return None, receipt
        try:
            return json.loads(data.decode("utf-8")), receipt
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationFailed("GitHub response JSON degil") from exc

    def _get_bytes(
        self, url: str, *, max_bytes: int = MAX_BLOB_BYTES
    ) -> tuple[bytes | None, FetchReceipt]:
        if self._request_count >= self._max_requests:
            return None, self._receipt("GET", url, None, 0)
        url = self._validate_url(url)
        request = urllib.request.Request(url, method="GET")
        if self._token is not None:
            request.add_header("Authorization", f"Bearer {self._token}")
        request.add_header("Accept", "application/vnd.github+json")
        request.add_header("User-Agent", "Zekam-Radar/1")
        etag_path = self._cache_dir / "github-etag" / _url_digest(url)
        if etag_path.exists():
            try:
                etag = etag_path.read_text(encoding="utf-8").strip()
                if etag:
                    request.add_header("If-None-Match", etag)
            except OSError:
                pass
        try:
            if self._url_opener is not None:
                response = self._url_opener.open(request, timeout=30)
            else:
                response = urllib.request.urlopen(request, timeout=30)
            with response:
                body = response.read(max_bytes + 1)
                status = response.status
                etag = response.headers.get("ETag")
                link_header = response.headers.get("Link")
        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                cached, _etag = self._cache_lookup(url)
                if cached is not None and cached.raw_bytes is not None:
                    return cached.raw_bytes, self._receipt(
                        "GET", url, 304, len(cached.raw_bytes), etag=etag, cache_hit=True
                    )
                return None, self._receipt("GET", url, 304, 0, cache_hit=False)
            return None, self._receipt("GET", url, exc.code, 0)
        except urllib.error.URLError as exc:
            raise ValidationFailed(f"GitHub request basarisiz: {exc}") from exc
        if etag:
            etag_path.parent.mkdir(parents=True, exist_ok=True)
            etag_path.write_text(etag, encoding="utf-8")
        self._request_count += 1
        self._response_byte_count += len(body)
        if len(body) > max_bytes:
            return None, self._receipt(
                "GET", url, status, len(body), etag=etag, link_header=link_header
            )
        return body, self._receipt(
            "GET", url, status, len(body), etag=etag, link_header=link_header
        )

    def _validate_url(self, url: str) -> str:
        parsed = urlparse(url)
        scheme_ok = parsed.scheme == "https" or (
            parsed.scheme == "http" and _is_loopback_host(parsed.hostname)
        )
        if not scheme_ok:
            raise PolicyViolation("GitHub radar yalnizca https kabul eder")
        if parsed.hostname not in self._allowed_hosts:
            raise PolicyViolation(f"GitHub radar host izinli degil: {parsed.hostname}")
        if parsed.username is not None or parsed.password is not None:
            raise PolicyViolation("GitHub radar URL userinfo tasiyamaz")
        if _is_private_host(parsed.hostname):
            raise PolicyViolation("GitHub radar private/loopback/link-local IP kabul etmez")
        return url

    def _receipt(
        self,
        method: str,
        url: str,
        status_code: int | None,
        response_bytes: int,
        *,
        etag: str | None = None,
        link_header: str | None = None,
        cache_hit: bool = False,
    ) -> FetchReceipt:
        return FetchReceipt(
            method=method,
            url=url,
            status_code=status_code,
            response_bytes=response_bytes,
            observed_at=datetime.now(UTC).isoformat(),
            etag=etag,
            link_header=link_header,
            cache_hit=cache_hit,
        )


def _record_from_api(repository_id: int, item: dict[str, Any]) -> RepositoryRecord:
    owner = item.get("owner", {})
    owner_login = owner.get("login", "") if isinstance(owner, dict) else ""
    license_info = item.get("license")
    return RepositoryRecord(
        repository_id=repository_id,
        owner=owner_login,
        name=item.get("name", ""),
        full_name=item.get("full_name", ""),
        default_branch=item.get("default_branch", "main"),
        visibility="public" if item.get("private") is False else "unknown",
        fork=bool(item.get("fork")),
        archived=bool(item.get("archived")),
        disabled=bool(item.get("disabled")),
        description=item.get("description"),
        language=item.get("language"),
        license_spdx=license_info.get("spdx_id") if isinstance(license_info, dict) else None,
        pushed_at=item.get("pushed_at"),
    )


def _next_page_url(link_header: str | None) -> str | None:
    """Parse GitHub Link header and return the next page URL if present."""

    if not link_header:
        return None
    for part in link_header.split(","):
        section = part.strip()
        if section.endswith('rel="next"'):
            url_section = section.split(";")[0].strip()
            if url_section.startswith("<") and url_section.endswith(">"):
                return url_section[1:-1]
    return None


def _url_digest(url: str) -> str:
    return "sha256:" + hashlib.sha256(url.encode("utf-8")).hexdigest()


def _git_blob_sha(data: bytes) -> str:
    """Compute Git blob object id (SHA-1) for the fetched bytes."""

    header = f"blob {len(data)}\0".encode()
    return hashlib.sha1(header + data).hexdigest()


def _is_loopback_host(hostname: str | None) -> bool:
    if not hostname:
        return False
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return hostname.casefold() == "localhost"


def _is_private_host(hostname: str | None) -> bool:
    """Reject non-loopback private/link-local/reserved IP addresses."""

    if not hostname:
        return False
    try:
        addr = ipaddress.ip_address(hostname)
    except ValueError:
        return False
    if addr.is_loopback:
        return False
    return bool(
        addr.is_private
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
    )


def _clean_sha(value: str) -> str:
    """Return the value if it looks like a lowercase hex SHA; otherwise empty."""

    if len(value) not in {40, 64}:
        return ""
    try:
        int(value, 16)
    except ValueError:
        return ""
    return value
