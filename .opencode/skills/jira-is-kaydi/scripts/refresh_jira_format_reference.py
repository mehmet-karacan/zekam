#!/usr/bin/env python3
"""Safely refresh the single Jira Wiki formatting reference on skill use."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import json
import os
import re
import tempfile
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

SOURCE_URL = "https://ouryahoo.atlassian.net/secure/WikiRendererHelpAction.jspa?section=all"
NORMALIZER_VERSION = "jira-wiki-help-v1"
MAX_SOURCE_BYTES = 8 * 1024 * 1024
EXPECTED_SECTIONS = (
    "headings",
    "text effects",
    "text breaks",
    "links",
    "lists",
    "images",
    "attachments",
    "tables",
    "advanced formatting",
    "misc",
)
REQUIRED_SYNTAX = (
    "Text Formatting Notation Help",
    "Notation Comment",
    "h1.",
    "*strong*",
    "[#anchor]",
    "||heading 1||",
    "{noformat}",
    "\\X",
)
REFERENCE = Path(__file__).resolve().parents[1] / "references" / "JIRA_FORMAT_REFERENCE.md"
_META_BOUNDARY = re.compile(r"\A---\n(?P<meta>.*?)\n---\n", re.DOTALL)
_BASELINE = re.compile(
    r"(?s)(<!-- BEGIN_SOURCE_BASELINE -->\n```json\n).*?(\n```\n<!-- END_SOURCE_BASELINE -->)"
)


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(microsecond=0)


def parse_time(value: str | None) -> dt.datetime | None:
    if value in {None, "", "null"}:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(dt.UTC)


def parse_metadata(document: str) -> dict[str, str | None]:
    match = _META_BOUNDARY.match(document)
    if match is None:
        raise ValueError("reference frontmatter missing")
    result: dict[str, str | None] = {}
    for line in match.group("meta").splitlines():
        key, separator, raw = line.partition(":")
        if not separator or not key.strip():
            raise ValueError("reference metadata malformed")
        value = raw.strip().strip("'")
        result[key.strip()] = None if value == "null" else value
    return result


def should_refresh(
    metadata: dict[str, str | None], now: dt.datetime, *, force: bool = False
) -> tuple[bool, str]:
    retry = parse_time(metadata.get("next_retry_after"))
    if not force and retry is not None and now < retry:
        return False, "retry-cooldown"
    if force:
        return True, "forced"
    if metadata.get("bootstrap_required") == "true":
        return True, "bootstrap-required"
    normalizer = metadata.get("normalizer_version")
    if normalizer is not None and normalizer != NORMALIZER_VERSION:
        return True, "normalizer-version-changed"
    checked = parse_time(metadata.get("last_checked_at"))
    if checked is None:
        return True, "missing-or-invalid-last-check"
    days = int(metadata.get("refresh_after_days") or "30")
    expired = now - checked > dt.timedelta(days=days)
    return expired, "ttl-expired" if expired else "fresh"


class _VisibleText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0
        self.pre = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript", "nav", "footer"}:
            self.hidden += 1
        if tag in {"pre", "code"}:
            if self.pre == 0:
                self.parts.append("\n<<ZEKAM_PRE>>\n")
            self.pre += 1
        elif tag in {"h1", "h2", "h3", "h4", "p", "li", "tr", "br"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "nav", "footer"} and self.hidden:
            self.hidden -= 1
        if tag in {"pre", "code"} and self.pre:
            self.pre -= 1
            if self.pre == 0:
                self.parts.append("\n<<ZEKAM_END_PRE>>\n")
        elif tag in {"h1", "h2", "h3", "h4", "p", "li", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self.hidden:
            return
        self.parts.append(data if self.pre else re.sub(r"\s+", " ", data))


def normalize_source(payload: bytes) -> dict[str, object]:
    text = payload.decode("utf-8", errors="strict")
    lowered = text.casefold()
    login_markers = ("password", "log in", "sign in")
    if "<form" in lowered and any(marker in lowered for marker in login_markers):
        raise ValueError("login page rejected")
    denied_markers = ("access denied", "unauthorized", "forbidden", "captcha")
    if any(marker in lowered for marker in denied_markers):
        raise ValueError("access or challenge page rejected")
    parser = _VisibleText()
    parser.feed(text)
    visible = html.unescape("".join(parser.parts)).replace("\r", "")
    lines: list[str] = []
    in_pre = False
    for raw_line in visible.splitlines():
        if raw_line == "<<ZEKAM_PRE>>":
            in_pre = True
            continue
        if raw_line == "<<ZEKAM_END_PRE>>":
            in_pre = False
            continue
        line = (
            raw_line.rstrip()
            if in_pre
            else re.sub(r"[ \t]+", " ", raw_line).strip()
        )
        if line:
            lines.append(line)
    joined = "\n".join(lines)
    missing = [section for section in EXPECTED_SECTIONS if section not in joined.casefold()]
    if missing:
        raise ValueError("expected Jira help sections missing: " + ", ".join(missing))
    if any(marker not in joined for marker in REQUIRED_SYNTAX):
        raise ValueError("Jira help structure or syntax markers missing")
    if joined.count("Notation Comment") < 8:
        raise ValueError("Jira help section structure incomplete")
    return {
        "schema": "zekam-jira-format-source-baseline/v1",
        "normalizer_version": NORMALIZER_VERSION,
        "sections": list(EXPECTED_SECTIONS),
        "normalized_text": joined,
    }


def canonical_baseline(document: dict[str, object]) -> str:
    return json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class _AllowedRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        parsed = urlparse(newurl)
        allowed = urlparse(SOURCE_URL)
        if parsed.scheme != "https" or parsed.hostname != allowed.hostname:
            raise HTTPError(newurl, code, "redirect target rejected", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


@dataclass(frozen=True)
class FetchResult:
    status: int
    resolved_url: str
    payload: bytes | None
    etag: str | None
    last_modified: str | None
    content_type: str | None = "text/html"


def fetch_source(metadata: dict[str, str | None]) -> FetchResult:
    headers = {"Accept": "text/html", "User-Agent": "Zekam-Jira-Format-Reference/1"}
    if metadata.get("etag"):
        headers["If-None-Match"] = str(metadata["etag"])
    if metadata.get("last_modified"):
        headers["If-Modified-Since"] = str(metadata["last_modified"])
    request = Request(SOURCE_URL, headers=headers, method="GET")
    opener = build_opener(_AllowedRedirects())
    try:
        response = opener.open(request, timeout=20)
    except HTTPError as exc:
        if exc.code == 304:
            return FetchResult(
                304,
                SOURCE_URL,
                None,
                metadata.get("etag"),
                metadata.get("last_modified"),
            )
        raise
    with response:
        resolved = response.geturl()
        parsed = urlparse(resolved)
        allowed = urlparse(SOURCE_URL)
        if (
            resolved != SOURCE_URL
            or parsed.scheme != "https"
            or parsed.hostname != allowed.hostname
        ):
            raise ValueError("resolved source URL rejected")
        length = response.headers.get("Content-Length")
        if length and int(length) > MAX_SOURCE_BYTES:
            raise ValueError("source exceeds size limit")
        payload = response.read(MAX_SOURCE_BYTES + 1)
        if len(payload) > MAX_SOURCE_BYTES:
            raise ValueError("source exceeds size limit")
        return FetchResult(
            int(response.status),
            resolved,
            payload,
            response.headers.get("ETag"),
            response.headers.get("Last-Modified"),
            response.headers.get("Content-Type"),
        )


def _replace_meta(document: str, updates: dict[str, str | int | bool | None]) -> str:
    match = _META_BOUNDARY.match(document)
    if match is None:
        raise ValueError("reference frontmatter missing")
    lines = match.group("meta").splitlines()
    positions = {line.partition(":")[0]: index for index, line in enumerate(lines)}
    for key, value in updates.items():
        if key not in positions:
            raise ValueError(f"unknown metadata field: {key}")
        if value is None:
            rendered = "null"
        elif isinstance(value, bool):
            rendered = str(value).lower()
        elif isinstance(value, int):
            rendered = str(value)
        else:
            rendered = "'" + str(value).replace("'", "''") + "'"
        lines[positions[key]] = f"{key}: {rendered}"
    frontmatter = "---\n" + "\n".join(lines) + "\n---\n"
    return frontmatter + document[match.end() :]


def _baseline_text(document: str) -> str | None:
    match = _BASELINE.search(document)
    if match is None:
        raise ValueError("source baseline markers missing")
    value = document[match.start(1) + len(match.group(1)) : match.start(2)]
    return None if value.strip() == "null" else value


def validate_reference(document: str) -> dict[str, str | None]:
    metadata = parse_metadata(document)
    try:
        payload = document.split("<!-- BEGIN_REFERENCE_PAYLOAD -->\n", 1)[1].split(
            "<!-- END_REFERENCE_PAYLOAD -->", 1
        )[0]
    except IndexError as exc:
        raise ValueError("reference payload markers missing") from exc
    payload_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    if metadata.get("reference_content_sha256") != payload_hash:
        raise ValueError("reference payload hash mismatch")
    seed_hash = metadata.get("seed_content_sha256")
    if seed_hash is None or not re.fullmatch(r"[0-9a-f]{64}", seed_hash):
        raise ValueError("seed reference hash missing or invalid")
    baseline = _baseline_text(document)
    source_hash = metadata.get("source_content_sha256")
    if baseline is None:
        if source_hash is not None or metadata.get("bootstrap_required") != "true":
            raise ValueError("source baseline state invalid")
        return metadata
    parsed = json.loads(baseline)
    if (
        not isinstance(parsed, dict)
        or parsed.get("schema") != "zekam-jira-format-source-baseline/v1"
    ):
        raise ValueError("source baseline schema invalid")
    normalized_text = parsed.get("normalized_text")
    if parsed.get("sections") != list(EXPECTED_SECTIONS):
        raise ValueError("source baseline sections invalid")
    if (
        not isinstance(normalized_text, str)
        or len(normalized_text) < 500
        or any(marker not in normalized_text for marker in REQUIRED_SYNTAX)
        or normalized_text.count("Notation Comment") < 8
    ):
        raise ValueError("source baseline normalized content invalid")
    expected = hashlib.sha256((baseline + "\n").encode("utf-8")).hexdigest()
    if source_hash != expected:
        raise ValueError("source baseline hash mismatch")
    if parsed.get("normalizer_version") != metadata.get("normalizer_version"):
        raise ValueError("source baseline normalizer mismatch")
    return metadata


def _atomic_write(path: Path, document: str) -> None:
    validate_reference(document)
    previous = path.read_bytes() if path.exists() else None
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(document)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if path.read_text(encoding="utf-8") != document:
            if previous is not None:
                restore_descriptor, restore_temporary = tempfile.mkstemp(
                    prefix=f".{path.name}.", suffix=".restore", dir=path.parent
                )
                try:
                    with os.fdopen(restore_descriptor, "wb") as restore_stream:
                        restore_stream.write(previous)
                        restore_stream.flush()
                        os.fsync(restore_stream.fileno())
                    os.replace(restore_temporary, path)
                finally:
                    if os.path.exists(restore_temporary):
                        os.unlink(restore_temporary)
            raise OSError("reference atomic write readback drift; previous copy restored")
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def refresh(
    path: Path,
    *,
    now: dt.datetime,
    force: bool = False,
    fetcher: Callable[[dict[str, str | None]], FetchResult] = fetch_source,
) -> dict[str, object]:
    document = path.read_text(encoding="utf-8")
    metadata = validate_reference(document)
    required, reason = should_refresh(metadata, now, force=force)
    if not required:
        return {"status": "not-required", "reason": reason, "network_calls": 0}
    if reason == "normalizer-version-changed" and not force:
        return {"status": "rebaseline-required", "reason": reason, "network_calls": 0}
    lock = path.with_suffix(path.suffix + ".lock")
    try:
        lock_fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise RuntimeError("another refresh is in progress") from exc
    try:
        os.close(lock_fd)
        attempted = now.isoformat().replace("+00:00", "Z")
        try:
            result = fetcher(metadata)
            prior_baseline = _baseline_text(document)
            if result.status == 304:
                if prior_baseline is None or metadata.get("source_content_sha256") is None:
                    raise ValueError("304 without a valid baseline")
                updated = _replace_meta(
                    document,
                    {
                        "last_attempt_at": attempted,
                        "last_checked_at": attempted,
                        "last_check_status": "not-modified",
                        "last_error": None,
                        "next_retry_after": None,
                        "bootstrap_required": False,
                    },
                )
                _atomic_write(path, updated)
                return {"status": "not-modified", "reason": reason, "network_calls": 1}
            if result.status != 200 or result.payload is None:
                raise ValueError(f"unexpected HTTP status {result.status}")
            if result.content_type is None or not result.content_type.casefold().startswith(
                "text/html"
            ):
                raise ValueError("source content type rejected")
            baseline = canonical_baseline(normalize_source(result.payload))
            source_hash = hashlib.sha256((baseline + "\n").encode()).hexdigest()
            changed = prior_baseline != baseline
            revision_delta = 1 if changed and prior_baseline is not None else 0
            revision = int(metadata.get("reference_revision") or "1") + revision_delta
            updated = _BASELINE.sub(
                lambda match: match.group(1) + baseline + match.group(2), document, count=1
            )
            values: dict[str, str | int | bool | None] = {
                "resolved_url": result.resolved_url,
                "last_attempt_at": attempted,
                "last_checked_at": attempted,
                "last_changed_at": attempted if changed else metadata.get("last_changed_at"),
                "normalizer_version": NORMALIZER_VERSION,
                "source_content_sha256": source_hash,
                "reference_revision": revision,
                "etag": result.etag,
                "last_modified": result.last_modified,
                "bootstrap_required": False,
                "last_check_status": "changed" if changed else "unchanged",
                "last_error": None,
                "next_retry_after": None,
            }
            updated = _replace_meta(updated, values)
            if changed and prior_baseline is not None:
                previous_hash = metadata.get("source_content_sha256")
                previous_revision = metadata.get("reference_revision")
                previous_normalizer = metadata.get("normalizer_version")
                updated = (
                    updated.rstrip("\n")
                    + f"\n\n### {attempted} — Kaynak tabanı değişti\n\n"
                    + f"Revizyon `{previous_revision}` → `{revision}`; kaynak SHA-256 "
                    + f"`{previous_hash}` → `{source_hash}`; normalizer "
                    + f"`{previous_normalizer}` → `{NORMALIZER_VERSION}`. Değişiklik "
                    + "normalleştirilmiş teknik tabandadır; hedef Jira etkisi doğrulanmadı ve "
                    + "etkin kurum alt kümesi otomatik genişletilmedi.\n"
                )
            _atomic_write(path, updated)
            return {
                "status": "changed" if changed else "unchanged",
                "reason": reason,
                "source_content_sha256": source_hash,
                "network_calls": 1,
            }
        except (HTTPError, URLError, TimeoutError, UnicodeError, ValueError) as exc:
            retry = now + dt.timedelta(hours=int(metadata.get("retry_after_failure_hours") or "24"))
            failed = _replace_meta(
                document,
                {
                    "last_attempt_at": attempted,
                    "last_check_status": "failed",
                    "last_error": type(exc).__name__,
                    "next_retry_after": retry.isoformat().replace("+00:00", "Z"),
                },
            )
            _atomic_write(path, failed)
            return {
                "status": "failed",
                "reason": reason,
                "error": type(exc).__name__,
                "network_calls": 1,
            }
    finally:
        with suppress(FileNotFoundError):
            lock.unlink()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("status", "refresh"))
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--reference", type=Path, default=REFERENCE)
    args = parser.parse_args()
    document = args.reference.read_text(encoding="utf-8")
    try:
        metadata = validate_reference(document)
    except (ValueError, json.JSONDecodeError) as exc:
        if args.command == "status":
            result = {
                "reference": str(args.reference.resolve()),
                "reference_valid": False,
                "refresh_required": False,
                "reason": "reference-invalid",
                "error": type(exc).__name__,
                "network_calls": 0,
            }
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 2
        raise
    required, reason = should_refresh(metadata, utc_now(), force=args.force)
    if args.command == "status":
        result = {
            "reference": str(args.reference.resolve()),
            "reference_valid": True,
            "refresh_required": required,
            "reason": reason,
            "last_checked_at": metadata.get("last_checked_at"),
            "last_check_status": metadata.get("last_check_status"),
            "network_calls": 0,
        }
    else:
        result = refresh(args.reference, now=utc_now(), force=args.force)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 1 if result.get("status") == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
