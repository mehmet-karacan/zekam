"""Bounded public-source refresh that can only emit a non-executable review candidate."""

from __future__ import annotations

import datetime as dt
import hashlib
import http.client
import ipaddress
import socket
import ssl
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit

from zekam.application.local_attestation import ReceiptVerifier
from zekam.application.secret_detection import scan_text
from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed

MAX_REFRESH_BYTES = 1_048_576
_TEXT_TYPES = frozenset(
    {
        "application/json",
        "application/xml",
        "text/html",
        "text/markdown",
        "text/plain",
        "text/xml",
    }
)
_SCRIPT_TYPES = frozenset(
    {
        "application/javascript",
        "application/x-executable",
        "application/x-msdownload",
        "application/x-sh",
        "text/javascript",
    }
)
_EXECUTABLE_MAGIC = (
    b"MZ",
    b"\x7fELF",
    b"#!",
    b"\xca\xfe\xba\xbe",
    b"\xcf\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf",
)


class ReceiptSigner(Protocol):
    def seal_digest(self, unsigned_body: dict[str, object]) -> str: ...


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection whose TCP destination cannot trigger a second DNS lookup."""

    def __init__(
        self,
        host: str,
        destination_ip: str,
        allowed_ips: frozenset[str],
        *,
        timeout: int,
    ) -> None:
        context = ssl.create_default_context()
        super().__init__(host, 443, timeout=timeout, context=context)
        self._ssl_context = context
        self._destination_ip = destination_ip
        self._allowed_ips = allowed_ips

    def connect(self) -> None:
        raw_socket = socket.create_connection(
            (self._destination_ip, 443), timeout=self.timeout
        )
        try:
            peer_ip = str(raw_socket.getpeername()[0])
            _assert_global_address(peer_ip)
            if peer_ip not in self._allowed_ips:
                raise PolicyViolation(
                    "Source transport peer is outside the validated DNS address set"
                )
            self.sock = self._ssl_context.wrap_socket(raw_socket, server_hostname=self.host)
        except BaseException:
            raw_socket.close()
            raise


def _content_digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _instant(value: dt.datetime) -> str:
    if type(value) is not dt.datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationFailed("Source refresh timestamp must be timezone-aware")
    return value.astimezone(dt.UTC).replace(microsecond=0).isoformat()


def _public_exact_url(value: str) -> tuple[str, str]:
    if type(value) is not str or not value.strip() or value != value.strip():
        raise ValidationFailed("Source refresh exact URL required")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.port is not None
        or parsed.hostname != parsed.hostname.lower()
    ):
        raise PolicyViolation("Source refresh requires credential-free canonical HTTPS URL")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise PolicyViolation("Source refresh private or non-global network target rejected")
    if parsed.hostname == "localhost" or parsed.hostname.endswith(".localhost"):
        raise PolicyViolation("Source refresh private network target rejected")
    canonical = f"https://{parsed.hostname}{parsed.path or '/'}"
    if value != canonical:
        raise PolicyViolation("Source refresh URL must be exact and canonical")
    return canonical, parsed.hostname


def _assert_global_address(value: str) -> None:
    if type(value) is not str or not value.strip():
        raise ValidationFailed("Source refresh peer IP required")
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise ValidationFailed("Source refresh peer IP invalid") from exc
    if not address.is_global:
        raise PolicyViolation("Source refresh private or non-global peer rejected")


@dataclass(frozen=True, slots=True)
class PublicSourceRecord:
    url: str
    publisher: str
    topic_scope: str
    content_type: str
    license_note: str
    last_successful_version: str
    last_successful_digest: str
    last_accessed_at: dt.datetime

    def __post_init__(self) -> None:
        _public_exact_url(self.url)
        for value, label in (
            (self.publisher, "publisher"),
            (self.topic_scope, "topic scope"),
            (self.license_note, "license note"),
            (self.last_successful_version, "last successful version"),
        ):
            if type(value) is not str or not value.strip() or len(value.encode()) > 4096:
                raise ValidationFailed(f"Source refresh bounded {label} required")
        if type(self.content_type) is not str or self.content_type not in _TEXT_TYPES:
            raise ValidationFailed("Source refresh registered content type invalid")
        parse_digest(self.last_successful_digest)
        _instant(self.last_accessed_at)

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-public-source-record/v1",
            "url": self.url,
            "publisher": self.publisher,
            "topic_scope": self.topic_scope,
            "content_type": self.content_type,
            "license_note": self.license_note,
            "last_successful_version": self.last_successful_version,
            "last_successful_digest": self.last_successful_digest,
            "last_accessed_at": _instant(self.last_accessed_at),
            "read_authority_only": True,
        }

    @property
    def record_digest(self) -> str:
        return digest(self.body())


@dataclass(frozen=True, slots=True)
class SourceRefreshPolicy:
    allowed_urls: tuple[str, ...]
    max_response_bytes: int = MAX_REFRESH_BYTES

    def __post_init__(self) -> None:
        if (
            type(self.allowed_urls) is not tuple
            or not self.allowed_urls
            or self.allowed_urls != tuple(sorted(set(self.allowed_urls)))
        ):
            raise ValidationFailed("Source refresh exact canonical allowlist required")
        for value in self.allowed_urls:
            _public_exact_url(value)
        if (
            type(self.max_response_bytes) is not int
            or isinstance(self.max_response_bytes, bool)
            or not 1 <= self.max_response_bytes <= MAX_REFRESH_BYTES
        ):
            raise ValidationFailed("Source refresh response bound invalid")

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-source-refresh-policy/v1",
            "allowed_urls": list(self.allowed_urls),
            "max_response_bytes": self.max_response_bytes,
            "redirect_policy": "deny",
            "execution_authority": False,
        }

    @property
    def policy_digest(self) -> str:
        return digest(self.body())


@dataclass(frozen=True, slots=True)
class SourceRefreshResponse:
    requested_url: str
    final_url: str
    status_code: int
    content_type: str
    body: bytes
    fetched_at: dt.datetime
    peer_ip: str
    transport_receipt_digest: str
    credentials_requested: bool = False

    def __post_init__(self) -> None:
        _public_exact_url(self.requested_url)
        _public_exact_url(self.final_url)
        if type(self.status_code) is not int or isinstance(self.status_code, bool):
            raise ValidationFailed("Source refresh exact status code required")
        if type(self.content_type) is not str or not self.content_type.strip():
            raise ValidationFailed("Source refresh content type required")
        if type(self.body) is not bytes:
            raise ValidationFailed("Source refresh exact response bytes required")
        if type(self.credentials_requested) is not bool:
            raise ValidationFailed("Source refresh credential flag must be exact bool")
        _assert_global_address(self.peer_ip)
        parse_digest(self.transport_receipt_digest)
        _instant(self.fetched_at)

    def transport_receipt_body(self) -> dict[str, object]:
        return {
            "schema": "zekam-source-transport-receipt/v1",
            "requested_url": self.requested_url,
            "final_url": self.final_url,
            "status_code": self.status_code,
            "content_type": self.content_type,
            "body_digest": _content_digest(self.body),
            "body_size": len(self.body),
            "fetched_at": _instant(self.fetched_at),
            "peer_ip": self.peer_ip,
            "credentials_requested": self.credentials_requested,
        }


class BoundedPublicSourceTransport:
    """Direct HTTPS transport: no proxy, redirect, credential or private-peer fallback."""

    def __init__(
        self,
        policy: SourceRefreshPolicy,
        signer: ReceiptSigner,
        *,
        timeout_seconds: int = 20,
    ) -> None:
        if type(policy) is not SourceRefreshPolicy or not hasattr(signer, "seal_digest"):
            raise ValidationFailed("Source transport exact policy and signer required")
        if (
            type(timeout_seconds) is not int
            or isinstance(timeout_seconds, bool)
            or not 1 <= timeout_seconds <= 60
        ):
            raise ValidationFailed("Source transport timeout must be 1..60 seconds")
        self._policy = policy
        self._signer = signer
        self._timeout = timeout_seconds

    def fetch(self, url: str, *, now: dt.datetime) -> SourceRefreshResponse:
        canonical, host = _public_exact_url(url)
        if canonical not in self._policy.allowed_urls:
            raise PolicyViolation("Source transport URL is outside exact public allowlist")
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        if not addresses:
            raise PolicyViolation("Source transport DNS returned no address")
        resolved_ips = frozenset(str(address[4][0]) for address in addresses)
        for address in resolved_ips:
            _assert_global_address(address)
        path = urlsplit(canonical).path or "/"
        connection = _PinnedHTTPSConnection(
            host,
            sorted(resolved_ips)[0],
            resolved_ips,
            timeout=self._timeout,
        )
        try:
            connection.request(
                "GET",
                path,
                headers={
                    "Accept": ", ".join(sorted(_TEXT_TYPES)),
                    "Cache-Control": "no-cache",
                },
            )
            response = connection.getresponse()
            if connection.sock is None:
                raise PolicyViolation("Source transport peer socket unavailable")
            peer_ip = str(connection.sock.getpeername()[0])
            _assert_global_address(peer_ip)
            if 300 <= response.status <= 399:
                raise PolicyViolation("Source transport redirects are rejected")
            if response.status in {401, 403, 407}:
                raise PolicyViolation("Source transport credential-requiring source rejected")
            content_length = response.getheader("Content-Length")
            if content_length is not None:
                try:
                    declared = int(content_length)
                except ValueError as exc:
                    raise PolicyViolation("Source transport Content-Length invalid") from exc
                if declared < 0 or declared > self._policy.max_response_bytes:
                    raise PolicyViolation("Source transport declared response exceeds bound")
            body = response.read(self._policy.max_response_bytes + 1)
            draft = SourceRefreshResponse(
                canonical,
                canonical,
                response.status,
                response.getheader("Content-Type", ""),
                body,
                now,
                peer_ip,
                digest("transport-receipt-placeholder"),
                False,
            )
            return SourceRefreshResponse(
                draft.requested_url,
                draft.final_url,
                draft.status_code,
                draft.content_type,
                draft.body,
                draft.fetched_at,
                draft.peer_ip,
                self._signer.seal_digest(draft.transport_receipt_body()),
                False,
            )
        finally:
            connection.close()


@dataclass(frozen=True, slots=True)
class SourceReviewCandidate:
    source_record_digest: str
    previous_content_digest: str
    observed_content_digest: str
    policy_digest: str
    source_url: str
    publisher: str
    topic_scope: str
    content_type: str
    license_note: str
    previous_source_version: str
    observed_at: dt.datetime
    zekam_problem: str
    expected_gain: str
    estimated_cost: str
    validation_scenario: str
    difference_from_current: str
    proposed_action: str = "review-only"
    executable: bool = False
    grants_authority: bool = False

    def __post_init__(self) -> None:
        for value in (
            self.source_record_digest,
            self.previous_content_digest,
            self.observed_content_digest,
            self.policy_digest,
        ):
            parse_digest(value)
        _public_exact_url(self.source_url)
        for value in (
            self.publisher,
            self.topic_scope,
            self.license_note,
            self.previous_source_version,
            self.zekam_problem,
            self.expected_gain,
            self.estimated_cost,
            self.validation_scenario,
            self.difference_from_current,
        ):
            if type(value) is not str or not value.strip() or len(value.encode()) > 4096:
                raise ValidationFailed("Source review candidate bounded rationale required")
        if self.content_type not in _TEXT_TYPES:
            raise ValidationFailed("Source review candidate content type invalid")
        if self.proposed_action != "review-only" or self.executable or self.grants_authority:
            raise PolicyViolation(
                "External source can only create a non-executable review candidate"
            )
        _instant(self.observed_at)

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-source-review-candidate/v1",
            "source_record_digest": self.source_record_digest,
            "previous_content_digest": self.previous_content_digest,
            "observed_content_digest": self.observed_content_digest,
            "policy_digest": self.policy_digest,
            "source_url": self.source_url,
            "publisher": self.publisher,
            "topic_scope": self.topic_scope,
            "content_type": self.content_type,
            "license_note": self.license_note,
            "previous_source_version": self.previous_source_version,
            "observed_at": _instant(self.observed_at),
            "zekam_problem": self.zekam_problem,
            "expected_gain": self.expected_gain,
            "estimated_cost": self.estimated_cost,
            "validation_scenario": self.validation_scenario,
            "difference_from_current": self.difference_from_current,
            "proposed_action": "review-only",
            "executable": False,
            "dependency_install_allowed": False,
            "provider_calls": 0,
            "source_instructions_are_data": True,
            "grants_authority": False,
        }

    @property
    def candidate_digest(self) -> str:
        return digest(self.body())


@dataclass(frozen=True, slots=True)
class SourceRefreshResult:
    changed: bool
    observed_content_digest: str
    candidate: SourceReviewCandidate | None

    def __post_init__(self) -> None:
        parse_digest(self.observed_content_digest)
        if type(self.changed) is not bool:
            raise ValidationFailed("Source refresh changed flag must be exact bool")
        if self.changed != (self.candidate is not None):
            raise ValidationFailed("Source refresh changed/candidate state drift")


def refresh_public_source(
    *,
    source: PublicSourceRecord,
    policy: SourceRefreshPolicy,
    response: SourceRefreshResponse,
    transport_verifier: ReceiptVerifier,
    zekam_problem: str,
    expected_gain: str,
    estimated_cost: str,
    validation_scenario: str,
    difference_from_current: str,
) -> SourceRefreshResult:
    """Validate already-fetched bytes; never fetch, execute, install or activate content."""
    if (
        type(source) is not PublicSourceRecord
        or type(policy) is not SourceRefreshPolicy
        or type(response) is not SourceRefreshResponse
        or not hasattr(transport_verifier, "matches_receipt_digest")
    ):
        raise ValidationFailed("Exact source refresh inputs required")
    source.__post_init__()
    policy.__post_init__()
    response.__post_init__()
    if source.url not in policy.allowed_urls or response.requested_url != source.url:
        raise PolicyViolation("Source refresh URL is outside exact public allowlist")
    if response.final_url != response.requested_url:
        raise PolicyViolation("Source refresh redirects are rejected")
    if response.credentials_requested or response.status_code in {401, 403, 407}:
        raise PolicyViolation("Source refresh credential-requiring source rejected")
    if response.status_code != 200:
        raise PolicyViolation("Source refresh requires exact successful response")
    media_type = response.content_type.split(";", 1)[0].strip().lower()
    if media_type in _SCRIPT_TYPES or media_type not in _TEXT_TYPES:
        raise PolicyViolation("Source refresh binary or script content rejected")
    if media_type != source.content_type:
        raise PolicyViolation("Source refresh registered content type drift")
    if not response.body or len(response.body) > policy.max_response_bytes:
        raise PolicyViolation("Source refresh response size is empty or exceeds bound")
    if response.body.startswith(_EXECUTABLE_MAGIC) or b"\x00" in response.body[:4096]:
        raise PolicyViolation("Source refresh executable or binary body rejected")
    try:
        decoded = response.body.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise PolicyViolation("Source refresh body must be strict UTF-8 text") from exc
    if scan_text(decoded, relative_path="public-source-refresh.txt"):
        raise PolicyViolation("Source refresh body contains secret-like material")
    if not transport_verifier.matches_receipt_digest(
        response.transport_receipt_body(), response.transport_receipt_digest
    ):
        raise PolicyViolation("Source refresh transport receipt is untrusted")
    observed = _content_digest(response.body)
    if observed == source.last_successful_digest:
        return SourceRefreshResult(False, observed, None)
    candidate = SourceReviewCandidate(
        source.record_digest,
        source.last_successful_digest,
        observed,
        policy.policy_digest,
        source.url,
        source.publisher,
        source.topic_scope,
        media_type,
        source.license_note,
        source.last_successful_version,
        response.fetched_at,
        zekam_problem,
        expected_gain,
        estimated_cost,
        validation_scenario,
        difference_from_current,
    )
    return SourceRefreshResult(True, observed, candidate)
