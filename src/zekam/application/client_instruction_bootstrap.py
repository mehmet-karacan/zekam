"""Managed global instruction sections for supported CLI clients."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from zekam.domain.errors import ConfigurationError

_START = "<!-- zekam-managed-client-instructions/v1:start -->"
_END = "<!-- zekam-managed-client-instructions/v1:end -->"
_REPARSE_POINT = 0x400

_MANAGED_BODY = "\n".join(
    (
        _START,
        "## Zekam managed bootstrap",
        "",
        "- Zekam ile ilgili calismadan once `zekam doctor --json` calistir.",
        "- Genel veya proje-baglamli soruyu once "
        '`zekam ask "<exact soru>" --json` ile bounded ve salt okunur olarak ara; '
        "retrieval authority degildir.",
        "- Proje mutation'ini yalniz registry'de cozulmus exact gercek source rootunda yap.",
        "- Repository `00_BASLA.md` iceriyorsa tamamen uygula; kanonik Work Graph, "
        "lease, checkpoint, claim ve receipt durumunu sohbetten uydurma.",
        "- Salt okunur akistan write akimina sessiz gecme. Commit, push, migration, "
        "provider/model cagrisi ve diger effect'ler kendi exact plan, authorization, "
        "claim-before-effect ve terminal receipt kapilarini korur.",
        "- Secret, PII ve raw transcript'i prompt, log, projection veya Git'e yazma.",
        "- Obsidian projection salt okunur gorunumdur; kanonik authority PostgreSQL'dir "
        "ve projection dosyalari elle degistirilmez.",
        "- `zekam` kullanilamiyorsa pending talebi koru ve kurulum/onarimdan once "
        "kullanici onayi iste.",
        _END,
        "",
    )
)


@dataclass(frozen=True, slots=True)
class ClientInstructionFilePlan:
    client_id: str
    path: Path
    content: str
    action: str


@dataclass(frozen=True, slots=True)
class ClientInstructionBootstrapPlan:
    files: tuple[ClientInstructionFilePlan, ...]

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


def _render(existing: str) -> tuple[str, str]:
    starts = existing.count(_START)
    ends = existing.count(_END)
    if starts != ends or starts > 1:
        raise ConfigurationError("Zekam managed instruction section bozuk veya duplicate")
    if starts == 1:
        begin = existing.index(_START)
        finish = existing.index(_END, begin) + len(_END)
        rendered = existing[:begin] + _MANAGED_BODY.rstrip("\n") + existing[finish:]
        action = "unchanged" if rendered == existing else "update"
        return rendered, action
    separator = "" if not existing else ("\n" if existing.endswith("\n") else "\n\n")
    return existing + separator + _MANAGED_BODY, "create" if not existing else "update"


def plan_client_instruction_bootstrap(*, user_home: Path) -> ClientInstructionBootstrapPlan:
    """Plan idempotent managed sections without changing client files."""

    _assert_safe_home(user_home)
    targets = (
        ("codex", user_home / ".codex" / "AGENTS.md"),
        ("claude", user_home / ".claude" / "CLAUDE.md"),
        ("opencode", user_home / ".config" / "opencode" / "AGENTS.md"),
    )
    planned: list[ClientInstructionFilePlan] = []
    for client_id, path in targets:
        _assert_safe_path(user_home, path)
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        content, action = _render(existing)
        planned.append(ClientInstructionFilePlan(client_id, path, content, action))
    return ClientInstructionBootstrapPlan(tuple(planned))


def apply_client_instruction_bootstrap(plan: ClientInstructionBootstrapPlan) -> None:
    """Atomically create or update only the planned managed sections."""

    for item in plan.files:
        user_home = (
            item.path.parents[1] if item.client_id in {"codex", "claude"} else item.path.parents[2]
        )
        _assert_safe_home(user_home)
        _assert_safe_path(user_home, item.path)
        current = item.path.read_text(encoding="utf-8") if item.path.exists() else ""
        expected, action = _render(current)
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
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(item.content)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(item.path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
