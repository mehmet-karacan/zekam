"""Native oturumdan erisilen ortak start/checkpoint/close/resume yolu (W05, A24).

Ikinci bir session store YOKTUR. Olay zinciri mevcut durable lifecycle ledger'dadir
(``opencode_lifecycle.record_event``: icerik icermeyen, digest zincirli, kilitli yerel ledger;
adi tarihsel olarak OpenCode'dur ama olay sozlesmesi istemciden bagimsizdir). Zengin checkpoint
govdesi mevcut icerik adresli nesne deposuna (CAS) yazilir ve olaya yalniz digest referansi
(``resource``) eklenir.

Dogrulanmis ve beyan ayrimi:

- ``binding`` (proje/Work), ``source`` (git revision + worktree digest) ve ``evidence`` (CAS
  nesnesi / unit-test ledger ozeti) checkpoint ve resume anlarinda Zekam tarafindan okunur.
- ``self_reported`` (tamamlanan/bekleyen/sonraki aksiyon) native istemcinin beyanidir; dogrulanmis
  Work gecisi degildir. Resume bunu "client-self-reported" diye isaretler.
- Native session kimligi ve istemci surumu yalniz istemci verdiyse metadata olur; verilmezse
  uydurulmaz (ledger oturum kimligi ``zekam-generated`` olarak isaretlenir ve hicbir istemci
  oturumu gibi sunulmaz).

Hicbir komut yetki, onay veya Work kapanisi uretmez.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import stat
import subprocess
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zekam.application.composition import build_context
from zekam.application.opencode_lifecycle import recent_events, record_event
from zekam.application.secret_detection import SECRET_RULES, scan_text
from zekam.domain.canonical import canonical_bytes, digest, digest_of_bytes, parse_digest
from zekam.domain.errors import ValidationFailed, ZekamError
from zekam.domain.identifiers import new_uuid7
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore
from zekam.infrastructure.storage.local_cas import LocalContentAddressedStore

CHECKPOINT_SCHEMA = "zekam-native-checkpoint/v1"
RESUME_SCHEMA = "zekam-native-resume/v1"
RESOURCE_PREFIX = "native-checkpoint/"
SESSION_PREFIX = "native."

_CLIENT = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_IDENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,63}$")
_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./:-]{0,127}$")
_MAX_SUMMARY = 500
_MAX_EVIDENCE = 16
_GIT_TIMEOUT = 10
_GIT_STATUS_LIMIT = 2_000_000


class NativeContinuityError(ZekamError):
    """Kararli neden koduyla reddedilen native continuity islemi (caller girdisi yansitilmaz)."""

    code = "native-continuity-rejected"

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class NativeClient:
    """Istemcinin beyan ettigi kimlik; hicbiri Zekam tarafindan uydurulmaz."""

    name: str
    version: str | None = None
    native_session_id: str | None = None
    model_ref: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or _CLIENT.fullmatch(self.name) is None:
            raise NativeClientError("client-name-invalid")
        for value, pattern, reason in (
            (self.version, _VERSION, "client-version-invalid"),
            (self.native_session_id, _IDENT, "native-session-id-invalid"),
            (self.model_ref, _MODEL, "model-ref-invalid"),
        ):
            if value is not None and pattern.fullmatch(value) is None:
                raise NativeClientError(reason)

    def metadata(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "native_session_id": self.native_session_id,
            "model_ref": self.model_ref,
            "provenance": "client-reported",
        }


class NativeClientError(NativeContinuityError):
    code = "native-client-invalid"


def _utc(now: dt.datetime | None) -> dt.datetime:
    value = now or dt.datetime.now(dt.UTC)
    if value.tzinfo is None:
        raise NativeContinuityError("timestamp-timezone-required")
    return value


def _summary(value: str | None, label: str) -> str | None:
    """Bounded tek satir beyan; secret/PII benzeri metin reddedilir (asla yansitilmaz)."""

    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if len(text) > _MAX_SUMMARY or "\n" in text or "\r" in text:
        raise NativeContinuityError(f"{label}-bound-exceeded")
    if scan_text(text, relative_path="native-checkpoint.txt") or any(
        rule.pattern.search(text) is not None for rule in SECRET_RULES
    ):
        raise NativeContinuityError(f"{label}-secret-like")
    # Mutlak yol/kullanici dizini: portable ve PII'siz kayit icin yasak (lifecycle ile ayni).
    if re.search(r"(?:[A-Za-z]:[\\/]|(?:^|\s)/(?:Users|home|root|etc|var)/)", text):
        raise NativeContinuityError(f"{label}-absolute-path")
    return text


def ledger_session_id(client: NativeClient) -> tuple[str, str]:
    """(ledger oturum kimligi, kaynak). Native kimlik yoksa Zekam-uretimi ve isaretli."""

    if client.native_session_id is not None:
        return f"{SESSION_PREFIX}{client.name}.{client.native_session_id}", "client-reported"
    return f"{SESSION_PREFIX}{client.name}.zekam-{new_uuid7()}", "zekam-generated"


# -- yardimcilar: git kaynagi ------------------------------------------------------------


def _git(root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise NativeContinuityError("source-git-unavailable") from exc
    if result.returncode != 0 or len(result.stdout.encode("utf-8")) > _GIT_STATUS_LIMIT:
        raise NativeContinuityError("source-git-unavailable")
    return result.stdout


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(os.path.realpath(left)) == os.path.normcase(os.path.realpath(right))


def read_source_state(source_root: Path) -> dict[str, Any]:
    """Exact git kokunun revision + worktree digest'i; yol saklanmaz (yalniz kimlik digest'i)."""

    if not isinstance(source_root, Path) or not source_root.is_absolute():
        raise NativeContinuityError("source-root-absolute-required")
    if ".." in source_root.parts or not source_root.is_dir():
        raise NativeContinuityError("source-root-invalid")
    resolved = source_root.resolve(strict=True)
    toplevel = _git(resolved, "rev-parse", "--show-toplevel").strip()
    if not toplevel or not _same_path(Path(toplevel), resolved):
        raise NativeContinuityError("source-root-not-exact-git-toplevel")
    revision = _git(resolved, "rev-parse", "HEAD").strip()
    if re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise NativeContinuityError("source-revision-unreadable")
    status = _git(resolved, "status", "--porcelain=v1", "--untracked-files=all")
    lines = sorted(line for line in status.splitlines() if line.strip())
    contents = _dirty_content_digests(resolved, lines)
    return {
        "revision": revision,
        "dirty": bool(lines),
        "dirty_path_count": len(lines),
        # Durum satirlari + kirli dosyalarin icerik digest'i: ayni `M` satiri farkli icerigi
        # "identical" gostermez.
        "worktree_digest": digest(
            {"contract": "zekam-native-worktree/v2", "status": lines, "contents": contents}
        ),
        "root_identity_digest": digest(
            {"root": os.path.normcase(os.path.realpath(resolved)).replace("\\", "/")}
        ),
    }


_DIRTY_FILE_LIMIT = 5000
_DIRTY_BYTES_LIMIT = 64 * 1024 * 1024


def _dirty_content_digests(root: Path, lines: list[str]) -> list[str]:
    """Kirli/izlenmeyen dosyalarin icerik digest'leri; link ve eksik dosya ayri isaretlenir."""

    if len(lines) > _DIRTY_FILE_LIMIT:
        raise NativeContinuityError("source-dirty-path-limit-exceeded")
    total = 0
    result: list[str] = []
    for line in lines:
        relative = line[3:].split(" -> ")[-1].strip().strip('"')
        target = root / relative
        try:
            metadata = target.lstat()
        except OSError:
            result.append(f"{relative}:absent")
            continue
        if not stat.S_ISREG(metadata.st_mode):
            result.append(f"{relative}:non-regular")
            continue
        total += metadata.st_size
        if total > _DIRTY_BYTES_LIMIT:
            raise NativeContinuityError("source-dirty-bytes-limit-exceeded")
        try:
            result.append(f"{relative}:{digest_of_bytes(target.read_bytes())}")
        except OSError as exc:
            raise NativeContinuityError("source-dirty-file-unreadable") from exc
    return result


# -- yardimcilar: operational baglar ------------------------------------------------------


def _database(home: Path) -> Path:
    context = build_context(home=home)
    path = context.settings.database.sqlite_path(context.home)
    if not path.is_file():
        raise NativeContinuityError("operational-database-missing")
    return path


def _objects(home: Path) -> LocalContentAddressedStore:
    context = build_context(home=home)
    return LocalContentAddressedStore(
        (context.home / context.settings.object_store_relative).resolve()
    )


def read_binding(home: Path, project_ref: str, work_item_id: str | None) -> dict[str, Any]:
    """Proje ve (varsa) Work baglarini operational store'dan okur; yoksa reddeder."""

    store = SQLiteOperationalStore(_database(home))
    try:
        with store.unit_of_work() as uow:
            project = uow.resolve_project(project_ref)
            work: dict[str, Any] | None = None
            if work_item_id is not None:
                record = uow.get_work(work_item_id)
                if record.project_id != project.id:
                    raise NativeContinuityError("work-project-mismatch")
                work = {
                    "work_item_id": record.id,
                    "revision": record.revision,
                    "state": record.state,
                    "kind": record.kind,
                    "evidence_recorded": record.evidence_digest is not None,
                }
            uow.rollback()
    except NativeContinuityError:
        raise
    except ValidationFailed as exc:
        raise NativeContinuityError("project-or-work-not-found") from exc
    return {"project_id": project.id, "project_slug": project.slug, "work": work}


def _unit_test_summary(home: Path, request_digest: str) -> dict[str, Any]:
    store = SQLiteOperationalStore(_database(home))
    with store.unit_of_work() as uow:
        try:
            ledger = uow.unit_test_ledger()
        except ZekamError as exc:
            raise NativeContinuityError("evidence-unit-test-ledger-unavailable") from exc
        request = ledger.get_request(request_digest)
        if request is None:
            raise NativeContinuityError("evidence-unit-test-request-unknown")
        attempts = ledger.list_attempts(request_digest)
        latest: dict[str, Any] | None = None
        if attempts:
            receipt = ledger.get_attempt_receipt(attempts[-1].attempt_id)
            latest = {
                "attempt_id": attempts[-1].attempt_id,
                "ordinal": attempts[-1].ordinal,
                "receipt_status": None if receipt is None else receipt.status.value,
                "evidence_digest": None if receipt is None else receipt.evidence_digest,
            }
        terminals = ledger.list_terminals(request_digest)
        summary = {
            "attempt_count": len(attempts),
            "latest_attempt": latest,
            "unreceipted_attempts": list(ledger.unreceipted_attempts(request_digest)),
            "terminal_reasons": [item.stop_reason.value for item in terminals],
        }
        uow.rollback()
    return summary


def verify_evidence_ref(home: Path, ref: str) -> dict[str, Any]:
    """``object:``/``unit-test:`` kanit referansini Zekam'in okudugu kanitla dogrular."""

    kind, separator, value = ref.partition(":")
    if not separator or kind not in {"object", "unit-test"}:
        raise NativeContinuityError("evidence-ref-kind-unsupported")
    try:
        parse_digest(value)
    except ValidationFailed as exc:
        raise NativeContinuityError("evidence-ref-digest-invalid") from exc
    if kind == "object":
        try:
            data = _objects(home).get(value)  # digest yeniden hesaplanir
        except (ZekamError, OSError) as exc:
            raise NativeContinuityError("evidence-object-missing-or-corrupt") from exc
        return {"ref": ref, "kind": kind, "verified": {"size_bytes": len(data)}}
    return {"ref": ref, "kind": kind, "verified": _unit_test_summary(home, value)}


def _evidence_refs(refs: Iterable[str]) -> tuple[str, ...]:
    unique = tuple(dict.fromkeys(refs))
    if len(unique) > _MAX_EVIDENCE:
        raise NativeContinuityError("evidence-ref-bound-exceeded")
    return unique


# -- olay yardimcilari -------------------------------------------------------------------


def _session_events(home: Path, session_id: str) -> list[dict[str, Any]]:
    events = [
        item
        for item in recent_events(home, limit=5000, quarantine_invalid=False)
        if item.get("session_id") == session_id
    ]
    events.sort(key=lambda item: int(item["sequence"]))
    return events


def _require_open_native_session(home: Path, session_id: str) -> dict[str, Any]:
    if not session_id.startswith(SESSION_PREFIX):
        raise NativeContinuityError("session-not-native")
    events = _session_events(home, session_id)
    if not events or events[0].get("event_type") != "session.created":
        raise NativeContinuityError("session-not-started")
    if any(item.get("event_type") == "session.deleted" for item in events):
        raise NativeContinuityError("session-already-closed")
    return events[0]


# -- start -------------------------------------------------------------------------------


def start_native_session(
    home: Path,
    *,
    client: NativeClient,
    project_ref: str,
    work_item_id: str | None,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    binding = read_binding(home, project_ref, work_item_id)
    session_id, origin = ledger_session_id(client)
    event = record_event(
        home,
        event_type="session.created",
        session_id=session_id,
        delivery_id=f"native-start-{digest(session_id)[7:39]}",
        agent=client.name,
        model_ref=client.model_ref,
        task_label=_summary(f"native-session project:{binding['project_slug']}", "task-label"),
        now=_utc(now),
    )
    return {
        "schema": "zekam-native-start/v1",
        "session_id": session_id,
        "session_origin": origin,
        "client": client.metadata(),
        "binding": binding,
        "event_digest": event.document()["event_digest"],
        "durability": "local-ledger",
        "grants_authority": False,
        "approval_inherited": False,
    }


# -- checkpoint / close --------------------------------------------------------------------


def _checkpoint_document(
    *,
    kind: str,
    session_id: str,
    session_origin: str,
    client: NativeClient,
    binding: Mapping[str, Any],
    source: Mapping[str, Any],
    evidence: list[dict[str, Any]],
    completed: str | None,
    pending: str | None,
    next_safe_action: str | None,
) -> dict[str, Any]:
    return {
        "schema": CHECKPOINT_SCHEMA,
        "kind": kind,
        "session": {"ledger_session_id": session_id, "origin": session_origin},
        "client": client.metadata(),
        "binding": dict(binding),
        "source": dict(source),
        "evidence": evidence,
        "self_reported": {
            "provenance": "client-self-reported",
            "verified": False,
            "completed": completed,
            "pending": pending,
            "next_safe_action": next_safe_action,
        },
        "authority": {
            "grants_authority": False,
            "approval_inherited": False,
            "work_transition_verified": False,
        },
    }


def _record_checkpoint(
    home: Path,
    *,
    kind: str,
    session_id: str,
    client: NativeClient,
    project_ref: str,
    work_item_id: str | None,
    source_root: Path,
    evidence_refs: Iterable[str],
    completed: str | None,
    pending: str | None,
    next_safe_action: str | None,
    now: dt.datetime | None,
) -> dict[str, Any]:
    first = _require_open_native_session(home, session_id)
    generated = re.search(r"\.zekam-[0-9a-f]{8}-", session_id) is not None
    origin = "zekam-generated" if generated else "client-reported"
    if first.get("agent") not in {None, client.name}:
        raise NativeContinuityError("session-client-mismatch")
    if client.native_session_id is not None and session_id != (
        f"{SESSION_PREFIX}{client.name}.{client.native_session_id}"
    ):
        raise NativeContinuityError("native-session-id-mismatch")
    completed_text = _summary(completed, "completed")
    pending_text = _summary(pending, "pending")
    next_text = _summary(next_safe_action, "next-safe-action")
    if kind == "checkpoint" and not (completed_text or pending_text or next_text):
        raise NativeContinuityError("checkpoint-summary-required")
    binding = read_binding(home, project_ref, work_item_id)
    source = read_source_state(source_root)
    evidence = [verify_evidence_ref(home, ref) for ref in _evidence_refs(evidence_refs)]
    document = _checkpoint_document(
        kind=kind,
        session_id=session_id,
        session_origin=origin,
        client=client,
        binding=binding,
        source=source,
        evidence=evidence,
        completed=completed_text,
        pending=pending_text,
        next_safe_action=next_text,
    )
    payload = canonical_bytes(document)
    info = _objects(home).ensure().put(payload, media_type="application/json")
    store = SQLiteOperationalStore(_database(home))
    with store.unit_of_work() as uow:
        uow.register_artifact(
            artifact_digest=info.digest,
            media_type="application/json",
            size_bytes=len(payload),
            classification="local-private",
        )
        uow.commit()
    hex_digest = info.digest.removeprefix("sha256:")
    event = record_event(
        home,
        event_type="session.checkpoint",
        session_id=session_id,
        delivery_id=f"native-{kind}-{hex_digest[:40]}",
        agent=client.name,
        model_ref=client.model_ref,
        resource=f"{RESOURCE_PREFIX}{hex_digest}",
        completed_summary=completed_text,
        pending_summary=pending_text,
        next_action=next_text,
        task_label=_summary(
            f"native-{kind} rev:{source['revision'][:12]}",
            "task-label",
        ),
        now=_utc(now),
    )
    return {
        "schema": f"zekam-native-{kind}/v1",
        "session_id": session_id,
        "checkpoint_digest": info.digest,
        "event_digest": event.document()["event_digest"],
        "client": client.metadata(),
        "binding": binding,
        "source": {k: v for k, v in source.items() if k != "root_identity_digest"},
        "evidence": evidence,
        "completed_recorded": completed_text is not None,
        "pending_recorded": pending_text is not None,
        "next_safe_action_recorded": next_text is not None,
        "verification_scope": "binding+source+evidence-refs; summaries are client-self-reported",
        "work_transition_verified": False,
        "durability": "local-ledger+cas",
        "grants_authority": False,
        "approval_inherited": False,
    }


def checkpoint_native_session(
    home: Path,
    *,
    session_id: str,
    client: NativeClient,
    project_ref: str,
    work_item_id: str | None,
    source_root: Path,
    evidence_refs: Iterable[str] = (),
    completed: str | None = None,
    pending: str | None = None,
    next_safe_action: str | None = None,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    return _record_checkpoint(
        home,
        kind="checkpoint",
        session_id=session_id,
        client=client,
        project_ref=project_ref,
        work_item_id=work_item_id,
        source_root=source_root,
        evidence_refs=evidence_refs,
        completed=completed,
        pending=pending,
        next_safe_action=next_safe_action,
        now=now,
    )


def close_native_session(
    home: Path,
    *,
    session_id: str,
    client: NativeClient,
    project_ref: str,
    work_item_id: str | None,
    source_root: Path,
    evidence_refs: Iterable[str] = (),
    completed: str | None = None,
    pending: str | None = None,
    next_safe_action: str | None = None,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Son checkpoint + ``session.deleted``; Work'u kapatmaz ve basari iddia etmez."""

    result = _record_checkpoint(
        home,
        kind="close",
        session_id=session_id,
        client=client,
        project_ref=project_ref,
        work_item_id=work_item_id,
        source_root=source_root,
        evidence_refs=evidence_refs,
        completed=completed,
        pending=pending,
        next_safe_action=next_safe_action,
        now=now,
    )
    event = record_event(
        home,
        event_type="session.deleted",
        session_id=session_id,
        delivery_id=f"native-closed-{result['checkpoint_digest'][7:47]}",
        agent=client.name,
        now=_utc(now),
    )
    result["close_event_digest"] = event.document()["event_digest"]
    result["session_closed"] = True
    return result


# -- resume ------------------------------------------------------------------------------


def _latest_native_checkpoint_event(home: Path, session_id: str | None) -> dict[str, Any] | None:
    candidates = [
        item
        for item in recent_events(home, limit=5000, quarantine_invalid=False)
        if item.get("event_type") == "session.checkpoint"
        and str(item.get("resource") or "").startswith(RESOURCE_PREFIX)
        and (session_id is None or item.get("session_id") == session_id)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda item: (str(item["occurred_at"]), str(item["event_id"])))


def _load_checkpoint(home: Path, event: Mapping[str, Any]) -> dict[str, Any]:
    hex_digest = str(event["resource"]).removeprefix(RESOURCE_PREFIX)
    object_digest = f"sha256:{hex_digest}"
    try:
        parse_digest(object_digest)
        payload = _objects(home).get(object_digest)
        document = json.loads(payload.decode("utf-8"))
    except (ZekamError, OSError, ValueError) as exc:
        raise NativeContinuityError("checkpoint-object-missing-or-corrupt") from exc
    if (
        not isinstance(document, dict)
        or document.get("schema") != CHECKPOINT_SCHEMA
        or digest(document) != object_digest
        or document.get("session", {}).get("ledger_session_id") != event.get("session_id")
    ):
        raise NativeContinuityError("checkpoint-document-drift")
    return document


def _source_verdict(recorded: Mapping[str, Any], source_root: Path) -> dict[str, Any]:
    try:
        current = read_source_state(source_root)
    except NativeContinuityError as exc:
        return {"state": "unavailable", "reason": exc.reason}
    if current["revision"] != recorded.get("revision"):
        state = "revision-drift"
    elif current["worktree_digest"] != recorded.get("worktree_digest"):
        state = "worktree-changed"
    else:
        state = "identical"
    return {
        "state": state,
        "recorded_revision": recorded.get("revision"),
        "current_revision": current["revision"],
        "recorded_dirty": recorded.get("dirty"),
        "current_dirty": current["dirty"],
        "same_source_root": current["root_identity_digest"] == recorded.get("root_identity_digest"),
    }


def _binding_verdict(home: Path, recorded: Mapping[str, Any]) -> dict[str, Any]:
    work = recorded.get("work")
    try:
        current = read_binding(
            home,
            str(recorded["project_id"]),
            None if work is None else str(work["work_item_id"]),
        )
    except NativeContinuityError as exc:
        return {"state": "invalid", "reason": exc.reason}
    current_work = current["work"]
    if work is None:
        return {"state": "valid", "project_id": current["project_id"], "work": None}
    assert current_work is not None
    closed = current_work["state"] in {"completed", "cancelled", "archived"}
    changed = (current_work["revision"], current_work["state"]) != (
        work.get("revision"),
        work.get("state"),
    )
    return {
        "state": "work-closed" if closed else ("work-changed" if changed else "valid"),
        "project_id": current["project_id"],
        "recorded_work": {"revision": work.get("revision"), "state": work.get("state")},
        "current_work": {"revision": current_work["revision"], "state": current_work["state"]},
    }


def _evidence_verdicts(home: Path, recorded: list[dict[str, Any]]) -> list[dict[str, Any]]:
    verdicts: list[dict[str, Any]] = []
    for item in recorded:
        ref = str(item.get("ref", ""))
        try:
            current = verify_evidence_ref(home, ref)
        except NativeContinuityError as exc:
            verdicts.append({"ref": ref, "state": "missing", "reason": exc.reason})
            continue
        unchanged = current.get("verified") == item.get("verified")
        verdicts.append({"ref": ref, "state": "unchanged" if unchanged else "changed"})
    return verdicts


def resume_native_checkpoint(
    home: Path,
    *,
    source_root: Path,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Son native checkpoint'i yeniden dogrular; hayali ilerleme/yetki uretmez."""

    base: dict[str, Any] = {
        "schema": RESUME_SCHEMA,
        "read_only": True,
        "grants_authority": False,
        "approval_inherited": False,
        "work_transition_verified": False,
    }
    event = _latest_native_checkpoint_event(home, session_id)
    if event is None:
        return base | {
            "status": "no-checkpoint",
            "checkpoint": None,
            "next_safe_action": (
                "Native checkpoint yok; kanonik Work kaydindan (`zekam resume`) devam edin, "
                "ilerleme uydurmayin."
            ),
        }
    try:
        document = _load_checkpoint(home, event)
    except NativeContinuityError as exc:
        return base | {
            "status": "integrity-error",
            "reason": exc.reason,
            "checkpoint": None,
            "next_safe_action": (
                "Checkpoint kaniti dogrulanamadi; kullanmayin, kanonik Work'ten devam edin."
            ),
        }
    binding = _binding_verdict(home, document["binding"])
    source = _source_verdict(document["source"], source_root)
    evidence = _evidence_verdicts(home, list(document["evidence"]))
    self_reported = document["self_reported"]
    if binding["state"] == "invalid":
        status = "binding-invalid"
    elif binding["state"] == "work-closed":
        status = "work-closed"
    elif source["state"] in {"revision-drift", "unavailable"}:
        status = "source-drift" if source["state"] == "revision-drift" else "source-unavailable"
    elif any(item["state"] == "missing" for item in evidence):
        status = "evidence-missing"
    elif (
        source["state"] == "worktree-changed"
        or binding["state"] == "work-changed"
        or any(item["state"] == "changed" for item in evidence)
    ):
        status = "changed-since-checkpoint"
    else:
        status = "ready"
    guidance = {
        "ready": self_reported.get("next_safe_action")
        or "Kayitli bekleyen isi kanonik Work kaydiyla eslestirerek surdurun.",
        "changed-since-checkpoint": (
            "Checkpoint'ten beri degisiklik var; kayitli beyani kullanmadan once degisen "
            "kaynak/Work/kanit durumunu yeniden dogrulayin."
        ),
        "source-drift": (
            "Kaynak revision'i checkpoint'ten farkli; onceki ilerleme bu revision icin "
            "dogrulanmamistir. Diff'i inceleyip yeniden olcum/dogrulama yapin."
        ),
        "source-unavailable": "Kaynak koku okunamadi; exact git kokunu verip tekrar deneyin.",
        "evidence-missing": (
            "Kayitli kanit referansi bulunamadi; basari/ilerleme iddiasini kullanmayin."
        ),
        "work-closed": "Bagli Work kapali; yeni hedefi kanonik Work kaydina baglayin.",
        "binding-invalid": "Proje/Work baglari dogrulanamadi; kanonik kayitla yeniden eslestirin.",
    }[status]
    return base | {
        "status": status,
        "checkpoint": {
            "digest": f"sha256:{str(event['resource']).removeprefix(RESOURCE_PREFIX)}",
            "kind": document["kind"],
            "session_id": event["session_id"],
            "session_origin": document["session"]["origin"],
            "recorded_at": event["occurred_at"],
            "client": document["client"],
            "recorded_binding": document["binding"],
            "recorded_source_revision": document["source"]["revision"],
            "evidence_refs": [item["ref"] for item in document["evidence"]],
            "self_reported": self_reported,
        },
        "verification": {"binding": binding, "source": source, "evidence": evidence},
        "next_safe_action": guidance,
        "next_safe_action_source": (
            "client-self-reported"
            if status == "ready" and self_reported.get("next_safe_action")
            else "zekam-verification"
        ),
    }
