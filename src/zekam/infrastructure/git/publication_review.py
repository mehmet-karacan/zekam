"""Bounded, immutable Git blob proof for a unit-test-only publication range."""

from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path

from zekam.domain.canonical import canonical_bytes, digest
from zekam.domain.errors import PolicyViolation
from zekam.domain.unit_test_engineering import normalize_relative_source
from zekam.infrastructure.git.publication import BoundGitPushTransport

_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
MAX_CHANGE_BYTES = 150_000
MAX_CHANGE_FILES = 512


def _blob_id(raw: bytes, width: int) -> str:
    body = b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw
    return (hashlib.sha1(body) if width == 40 else hashlib.sha256(body)).hexdigest()


def validate_review_changes(body, *, allowed_paths, head):
    if not isinstance(body, dict) or set(body) != {
        "schema",
        "base_revision",
        "head",
        "changes",
        "grants_authority",
        "change_digest",
    }:
        raise PolicyViolation("Publication review exact change proof required")
    if (
        body["schema"] != "zekam-publication-review-changes/v1"
        or body["grants_authority"] is not False
        or not isinstance(body["base_revision"], str)
        or _OID.fullmatch(body["base_revision"]) is None
        or not isinstance(body["head"], str)
        or _OID.fullmatch(body["head"]) is None
        or body["head"] != head
        or body["head"] == body["base_revision"]
        or len(body["head"]) != len(body["base_revision"])
        or body["change_digest"] != digest({k: v for k, v in body.items() if k != "change_digest"})
        or len(canonical_bytes(body)) > MAX_CHANGE_BYTES
        or not isinstance(body["changes"], list)
        or not 1 <= len(body["changes"]) <= MAX_CHANGE_FILES
    ):
        raise PolicyViolation("Publication review change proof identity/budget drift")
    paths = []
    for change in body["changes"]:
        if not isinstance(change, dict) or set(change) != {
            "path",
            "status",
            "old_mode",
            "new_mode",
            "old_blob",
            "new_blob",
            "before",
            "after",
        }:
            raise PolicyViolation("Publication review exact blob change required")
        path = normalize_relative_source(change["path"])
        if path != change["path"] or path not in allowed_paths or path in paths:
            raise PolicyViolation(
                "Publication review commit contains changes outside accepted tests"
            )
        paths.append(path)
        status = change["status"]
        if status not in {"A", "M"} or change["new_mode"] not in {"100644", "100755"}:
            raise PolicyViolation("Publication review deletion/link/submodule change rejected")
        old, new = change["old_blob"], change["new_blob"]
        if any(
            not isinstance(value, str) or len(value) != len(head) or _OID.fullmatch(value) is None
            for value in (old, new)
        ):
            raise PolicyViolation("Publication review full Git blob identity required")
        if (
            not isinstance(change["after"], str)
            or "\0" in change["after"]
            or _blob_id(change["after"].encode("utf-8"), len(new)) != new
        ):
            raise PolicyViolation("Publication review new blob text/hash drift")
        if status == "A":
            if (
                change["old_mode"] != "000000"
                or old != "0" * len(head)
                or change["before"] is not None
                or change["new_mode"] != "100644"
            ):
                raise PolicyViolation("Publication review new test metadata drift")
        elif (
            change["old_mode"] != change["new_mode"]
            or old == new
            or not isinstance(change["before"], str)
            or "\0" in change["before"]
            or _blob_id(change["before"].encode("utf-8"), len(old)) != old
        ):
            raise PolicyViolation("Publication review old blob or mode drift")
    if paths != sorted(paths):
        raise PolicyViolation("Publication review change ordering drift")
    return body


def capture_review_changes(root: Path, *, base_revision: str, head: str, allowed_paths):
    if (
        any(
            not isinstance(value, str) or _OID.fullmatch(value) is None
            for value in (base_revision, head)
        )
        or len(base_revision) != len(head)
        or base_revision == head
    ):
        raise PolicyViolation("Publication review distinct full commit range required")
    deadline = time.monotonic() + 30

    def command(*arguments):
        remaining = int(deadline - time.monotonic())
        if remaining < 1:
            raise PolicyViolation("Publication review total Git read budget exceeded")
        return BoundGitPushTransport(root, timeout_seconds=min(30, remaining))._command_bytes(
            *arguments
        )

    # No fetch, working tree mutation, external diff/textconv, rename guessing or
    # replace-object history is permitted. Missing/non-ancestor baseline fails.
    command("merge-base", "--is-ancestor", base_revision, head)
    raw = command(
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "--no-renames",
        "--raw",
        "--abbrev=64",
        "-z",
        base_revision,
        head,
        "--",
    )
    pieces = raw.split(b"\0")
    if (
        not pieces
        or pieces[-1] != b""
        or (len(pieces) - 1) % 2
        or not 1 <= (len(pieces) - 1) // 2 <= MAX_CHANGE_FILES
    ):
        raise PolicyViolation("Publication review complete bounded commit diff required")
    changes, size = [], 0
    try:
        for index in range(0, len(pieces) - 1, 2):
            header = pieces[index].decode("ascii").split(" ")
            path = pieces[index + 1].decode("utf-8", errors="strict")
            if len(header) != 5 or not header[0].startswith(":") or path not in allowed_paths:
                raise PolicyViolation(
                    "Publication review commit contains changes outside accepted tests"
                )
            old_mode, new_mode, old, new, status = header
            old_mode = old_mode[1:]
            if (
                status not in {"A", "M"}
                or new_mode not in {"100644", "100755"}
                or (status == "M" and old_mode != new_mode)
            ):
                raise PolicyViolation(
                    "Publication review only added/modified regular tests supported"
                )
            if any(
                _OID.fullmatch(value) is None or len(value) != len(head) for value in (old, new)
            ):
                raise PolicyViolation("Publication review exact blob OID required")
            before = None if status == "A" else command("cat-file", "blob", old)
            after = command("cat-file", "blob", new)
            size += len(after) + (0 if before is None else len(before))
            if (
                size > MAX_CHANGE_BYTES
                or b"\0" in after
                or (before is not None and b"\0" in before)
            ):
                raise PolicyViolation("Publication review binary/oversized diff rejected")
            changes.append(
                {
                    "path": path,
                    "status": status,
                    "old_mode": old_mode,
                    "new_mode": new_mode,
                    "old_blob": old,
                    "new_blob": new,
                    "before": None if before is None else before.decode("utf-8", errors="strict"),
                    "after": after.decode("utf-8", errors="strict"),
                }
            )
    except UnicodeError as exc:
        raise PolicyViolation("Publication review exact UTF-8 diff required") from exc
    body = {
        "schema": "zekam-publication-review-changes/v1",
        "base_revision": base_revision,
        "head": head,
        "changes": sorted(changes, key=lambda change: change["path"]),
        "grants_authority": False,
    }
    return validate_review_changes(
        body | {"change_digest": digest(body)}, allowed_paths=allowed_paths, head=head
    )
