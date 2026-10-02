"""Managed global instruction sections for supported CLI clients."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from zekam.domain.canonical import digest_of_bytes
from zekam.domain.client_integration import ClientIntegrationId, ClientIntegrationPolicy
from zekam.domain.errors import ConfigurationError

_START = "<!-- zekam-managed-client-instructions/v1:start -->"
_END = "<!-- zekam-managed-client-instructions/v1:end -->"
_REPARSE_POINT = 0x400

_MANAGED_BODY = "\n".join(
    (
        _START,
        "## Zekam managed bootstrap",
        "",
        "- Zekam repository'si veya `zekam` CLI uzerinde is baslarken (selamlama ve genel "
        "sohbet haric) `zekam doctor --json` calistir.",
        "- Git pull/merge sonrasinda doctor pending migration veya eksik routine bildirirse "
        "ve kullanici local DB hazirlamayi yetkilendirdiyse `zekam doctor --hazirla --json` "
        "calistir; bu komut kayitli migration'lari bounded uygular ve final doctor yapar.",
        "- Proje-baglamli bilgi sorusunu once "
        '`zekam ask "<exact soru>" --json` ile bounded ve salt okunur olarak ara; '
        "retrieval authority degildir. Selamlama ve proje icermeyen genel soru icin "
        "doctor veya ask cagirma.",
        "- Proje mutation'ini yalniz registry'de cozulmus exact gercek source rootunda yap.",
        "- Repository `00_BASLA.md` iceriyorsa repository isinde (mutation, research, "
        "devam/recovery) onu uygula; kanonik Work Graph, "
        "lease, checkpoint, claim ve receipt durumunu sohbetten uydurma.",
        "- Salt okunur akistan write akimina sessiz gecme. Commit, push, migration, "
        "provider/model cagrisi ve diger effect'ler kendi exact plan, authorization, "
        "claim-before-effect ve terminal receipt kapilarini korur.",
        "- Secret, PII ve raw transcript'i prompt, log, projection veya Git'e yazma.",
        "- Obsidian projection salt okunur gorunumdur; kanonik authority yerel "
        "operational store'dur ve projection dosyalari elle degistirilmez.",
        "- Olcumlu loop durumunu raw transcript istemeden `zekam loop status "
        "<loop-id> --json` ile oku; metric ve stop reason kanonik operational store'dan gelir.",
        "- `zekam` kullanilamiyorsa pending talebi koru ve kurulum/onarimdan once "
        "kullanici onayi iste.",
        _END,
        "",
    )
)

# Bilinen eski managed govdeler yalniz govde icerik digest'iyle taninir (marker varligi
# sahiplik kaniti degildir). Digest, _START ile _END arasi (ikisi dahil, sondaki yeni satir
# haric, satir sonlari LF) metnin SHA-256'sidir. Yeni bir managed govde surumu yayinlanirken
# onceki govdenin digest'i buraya eklenir; kaynak commit gerekcesi yorumda tutulur.
KNOWN_LEGACY_BODY_DIGESTS: Mapping[str, str] = MappingProxyType(
    {
        # W01 (commit 94f4892) oncesi govde: her calismadan once doctor, genel soruya ask.
        "sha256:7b9015a9457f29e1d5b97643f64beb08f1bb9dec21ea1c66f488655922a66a58": (
            "managed-instructions/v1/pre-w01-94f4892"
        ),
    }
)
CURRENT_VERSION_ID = "managed-instructions/v1/current"
UNKNOWN_VERSION_ID = "unknown"


class ManagedInstructionConflict(ConfigurationError):
    """Managed section ownership cannot be proven; user text must stay untouched."""

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


_BROKEN = "managed-instruction-section-broken"
_DRIFT = "managed-instruction-ownership-drift"
_CRLF = "\r\n"
_LF = "\n"


def _lf(text: str) -> str:
    return text.replace(_CRLF, _LF)


def _body_digest(section_lf: str) -> str:
    return digest_of_bytes(section_lf.encode("utf-8"))


_CURRENT_SECTION = _MANAGED_BODY.rstrip(_LF)
CURRENT_BODY_DIGEST = _body_digest(_CURRENT_SECTION)


@dataclass(frozen=True, slots=True)
class ManagedSectionView:
    """Exact location and identity of the single managed section inside a file."""

    prefix: str
    section: str
    suffix: str
    body_digest: str
    version: str  # CURRENT_VERSION_ID, bilinen eski surum id'si veya UNKNOWN_VERSION_ID

    @property
    def prefix_digest(self) -> str:
        return digest_of_bytes(self.prefix.encode("utf-8"))

    @property
    def suffix_digest(self) -> str:
        return digest_of_bytes(self.suffix.encode("utf-8"))

    @property
    def owned(self) -> bool:
        return self.version != UNKNOWN_VERSION_ID


def inspect_managed_section(text: str) -> ManagedSectionView | None:
    """Locate the managed section; None when absent, ConfigurationError when broken."""

    starts = text.count(_START)
    ends = text.count(_END)
    if starts != ends or starts > 1:
        raise ManagedInstructionConflict(
            "Zekam managed instruction section bozuk veya duplicate", reason=_BROKEN
        )
    if starts == 0:
        return None
    begin = text.index(_START)
    end_at = text.index(_END)
    if end_at < begin:
        raise ManagedInstructionConflict(
            "Zekam managed instruction section bozuk veya duplicate", reason=_BROKEN
        )
    finish = end_at + len(_END)
    section = text[begin:finish]
    body_digest = _body_digest(_lf(section))
    if body_digest == CURRENT_BODY_DIGEST:
        version = CURRENT_VERSION_ID
    else:
        version = KNOWN_LEGACY_BODY_DIGESTS.get(body_digest, UNKNOWN_VERSION_ID)
    return ManagedSectionView(text[:begin], section, text[finish:], body_digest, version)


def _eol_for(text: str) -> str:
    return _CRLF if _CRLF in text and text.count(_CRLF) == text.count(_LF) else _LF


def _current_section(eol: str) -> str:
    return _CURRENT_SECTION.replace(_LF, eol)


def read_instruction_text(path: Path) -> str:
    """Read exact text (no newline translation) so user bytes survive a rewrite."""

    return path.read_bytes().decode("utf-8")


@dataclass(frozen=True, slots=True)
class ClientInstructionFilePlan:
    client_id: str
    path: Path
    content: str
    action: str
    previous_version: str | None = None
    previous_body_digest: str | None = None
    body_digest: str = ""
    prefix_digest: str = ""
    suffix_digest: str = ""


@dataclass(frozen=True, slots=True)
class ClientInstructionConflict:
    client_id: str
    path: Path
    reason: str


@dataclass(frozen=True, slots=True)
class ClientInstructionBootstrapPlan:
    files: tuple[ClientInstructionFilePlan, ...]
    conflicts: tuple[ClientInstructionConflict, ...] = ()

    @property
    def changes_required(self) -> bool:
        return any(item.action != "unchanged" for item in self.files)


def _unsafe(path: Path) -> bool:
    try:
        stat = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return False
    return path.is_symlink() or bool(getattr(stat, "st_file_attributes", 0) & _REPARSE_POINT)


def _assert_safe_home(user_home: Path) -> None:
    if not user_home.is_absolute() or not user_home.exists() or not user_home.is_dir():
        raise ConfigurationError("Client bootstrap absolute mevcut user home ister")
    if _unsafe(user_home):
        raise ConfigurationError("Client bootstrap user home symlink/reparse olamaz")


def _assert_safe_path(user_home: Path, path: Path) -> None:
    if path.parent == path or user_home not in path.parents:
        raise ConfigurationError("Client bootstrap hedefi user home altinda olmali")
    current = user_home
    for segment in path.relative_to(user_home).parts[:-1]:
        current = current / segment
        if current.exists() and (_unsafe(current) or not current.is_dir()):
            raise ConfigurationError("Client bootstrap parent symlink/reparse olamaz")
    if path.exists() and (_unsafe(path) or not path.is_file()):
        raise ConfigurationError("Client bootstrap hedefi regular file olmali")


def _render(existing: str) -> tuple[str, str, ManagedSectionView | None]:
    """Return (rendered, action, previous view); unknown/drifted section is a conflict."""

    view = inspect_managed_section(existing)
    if view is not None:
        if not view.owned:
            raise ManagedInstructionConflict(
                "Zekam managed instruction section ownership drift", reason=_DRIFT
            )
        eol = _CRLF if _CRLF in view.section else _LF
        rendered = view.prefix + _current_section(eol) + view.suffix
        if rendered == existing:
            return rendered, "unchanged", view
        # Yalniz govde digest'i bilinen eski surumse migrate; kullanici metni korunur.
        return rendered, "migrate", view
    eol = _eol_for(existing)
    managed = _current_section(eol) + eol
    separator = "" if not existing else (eol if existing.endswith(eol) else eol + eol)
    return existing + separator + managed, "create" if not existing else "update", None


def remove_managed_instruction_section(existing: str) -> tuple[str, bool]:
    """Remove only a digest-owned (current or known legacy) managed section."""

    view = inspect_managed_section(existing)
    if view is None:
        return existing, False
    if not view.owned:
        raise ManagedInstructionConflict(
            "Zekam managed instruction section ownership drift", reason=_DRIFT
        )
    prefix = view.prefix
    suffix = view.suffix
    eol = _CRLF if _CRLF in view.section else _LF
    if prefix.endswith(eol + eol):
        prefix = prefix[: -len(eol)]
    if suffix.startswith(eol):
        suffix = suffix[len(eol) :]
    return prefix + suffix, True


def _file_plan(
    client_id: ClientIntegrationId, path: Path, existing: str
) -> ClientInstructionFilePlan:
    content, action, view = _render(existing)
    after = inspect_managed_section(content)
    if after is None or after.version != CURRENT_VERSION_ID:
        raise ConfigurationError("Client bootstrap render sonrasi managed govde dogrulanamadi")
    return ClientInstructionFilePlan(
        client_id.value,
        path,
        content,
        action,
        previous_version=None if view is None else view.version,
        previous_body_digest=None if view is None else view.body_digest,
        body_digest=after.body_digest,
        prefix_digest=after.prefix_digest,
        suffix_digest=after.suffix_digest,
    )


def verify_instruction_readback(path: Path, plan: ClientInstructionFilePlan) -> None:
    """Fail closed unless the file holds exactly the planned section and user text."""

    view = inspect_managed_section(read_instruction_text(path))
    if (
        view is None
        or view.version != CURRENT_VERSION_ID
        or view.body_digest != plan.body_digest
        or view.prefix_digest != plan.prefix_digest
        or view.suffix_digest != plan.suffix_digest
    ):
        raise ConfigurationError("Client bootstrap readback managed govde/kullanici metni driftli")


def plan_client_instruction_bootstrap(
    *,
    user_home: Path,
    integration_policy: ClientIntegrationPolicy | None = None,
    collect_conflicts: bool = False,
) -> ClientInstructionBootstrapPlan:
    """Plan idempotent managed sections without changing client files.

    Unknown body/marker collisions raise by default; with ``collect_conflicts`` they are
    returned as per-file conflicts so the dry-run can show them while other files plan.
    """

    _assert_safe_home(user_home)
    policy = integration_policy or ClientIntegrationPolicy()
    targets = (
        (ClientIntegrationId.CODEX, user_home / ".codex" / "AGENTS.md"),
        (ClientIntegrationId.CLAUDE_CODE, user_home / ".claude" / "CLAUDE.md"),
        (ClientIntegrationId.OPENCODE, user_home / ".config" / "opencode" / "AGENTS.md"),
    )
    planned: list[ClientInstructionFilePlan] = []
    conflicts: list[ClientInstructionConflict] = []
    for client_id, path in targets:
        if not policy.enabled(client_id):
            continue
        _assert_safe_path(user_home, path)
        existing = read_instruction_text(path) if path.exists() else ""
        try:
            planned.append(_file_plan(client_id, path, existing))
        except ManagedInstructionConflict as exc:
            if not collect_conflicts:
                raise
            conflicts.append(ClientInstructionConflict(client_id.value, path, exc.reason))
    return ClientInstructionBootstrapPlan(tuple(planned), tuple(conflicts))


def apply_client_instruction_bootstrap(plan: ClientInstructionBootstrapPlan) -> None:
    """Atomically create or update only the planned managed sections."""

    for item in plan.files:
        user_home = (
            item.path.parents[1]
            if item.client_id
            in {
                ClientIntegrationId.CODEX.value,
                ClientIntegrationId.CLAUDE_CODE.value,
            }
            else item.path.parents[2]
        )
        _assert_safe_home(user_home)
        _assert_safe_path(user_home, item.path)
        current = read_instruction_text(item.path) if item.path.exists() else ""
        expected, action, _view = _render(current)
        if expected != item.content or action != item.action:
            raise ConfigurationError("Client bootstrap plani dosya drift nedeniyle stale")
        if item.action == "unchanged":
            continue
        item.path.parent.mkdir(parents=True, exist_ok=True)
        _assert_safe_path(user_home, item.path)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{item.path.name}.", dir=item.path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(item.content.encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(item.path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        verify_instruction_readback(item.path, item)
