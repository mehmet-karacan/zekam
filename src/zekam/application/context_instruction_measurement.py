"""Istemci instruction kaynaklarinin salt okunur etkin yuk olcumu (AKTIF_GOREV 5.2).

Dosyalar yalniz okunur; hicbir sey yazilmaz, tokenizer indirilmez ve ag cagrisi yapilmaz.
Diskte bulunan ve istemcinin yukleyecegi belgelenen kaynaklar ``discovered_only`` olarak
raporlanir (yukleme gozlenmedi). Istemcinin gizli system prompt'u ve tool serialization'i
``unobservable`` girdi olarak gorunur; bu yuzden rapor tam etkin token sayisi iddia etmez.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from zekam.domain.client_integration import ClientIntegrationId
from zekam.domain.context_measurement import (
    EffectiveContextReport,
    EffectiveLoadEntry,
    InstructionSourceKind,
    measured_instruction_entry,
    unobservable_entry,
)

MAX_INSTRUCTION_BYTES = 1_000_000
MAX_IMPORT_DEPTH = 5
_GLOBAL_FILES = {
    ClientIntegrationId.CODEX: Path(".codex") / "AGENTS.md",
    ClientIntegrationId.CLAUDE_CODE: Path(".claude") / "CLAUDE.md",
    ClientIntegrationId.OPENCODE: Path(".config") / "opencode" / "AGENTS.md",
    ClientIntegrationId.GEMINI: Path(".gemini") / "GEMINI.md",
}
_PROJECT_FILES = {
    ClientIntegrationId.CODEX: "AGENTS.md",
    ClientIntegrationId.CLAUDE_CODE: "CLAUDE.md",
    ClientIntegrationId.OPENCODE: "AGENTS.md",
    ClientIntegrationId.GEMINI: "GEMINI.md",
}
# Resmi belgelerde her istemcinin proje-yerel skill'i taradigi dizinler (kesif gozlenmedi).
_PROJECT_SKILL_DIRS = {
    ClientIntegrationId.CODEX: (Path(".agents") / "skills",),
    ClientIntegrationId.CLAUDE_CODE: (Path(".claude") / "skills",),
    ClientIntegrationId.OPENCODE: (
        Path(".agents") / "skills",
        Path(".claude") / "skills",
        Path(".opencode") / "skills",
    ),
    ClientIntegrationId.GEMINI: (Path(".agents") / "skills", Path(".gemini") / "skills"),
}
_MAX_SKILLS = 256
_IMPORT = re.compile(r"(?m)^@(?P<target>[^\s`]+)\s*$")
_FENCE = re.compile(r"(?s)```.*?```")


def _read_bounded(path: Path, root: Path) -> bytes | None:
    """Root altindaki regular dosyayi sinirli okur; symlink/traversal/asiri boyut -> None."""

    try:
        if path.is_symlink() or not path.is_file():
            return None
        resolved = path.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
        if path.stat().st_size > MAX_INSTRUCTION_BYTES:
            return None
        return path.read_bytes()
    except (OSError, ValueError):
        return None


def _import_targets(text: str) -> tuple[str, ...]:
    return tuple(match.group("target") for match in _IMPORT.finditer(_FENCE.sub("", text)))


def _collect_imports(
    *,
    source: Path,
    text: str,
    root: Path,
    root_label: str,
    role: str | None,
    route: str | None,
    seen: set[Path],
    depth: int,
    entries: list[EffectiveLoadEntry],
) -> None:
    if depth > MAX_IMPORT_DEPTH:
        return
    for target in _import_targets(text):
        candidate = (source.parent / target).resolve(strict=False)
        if candidate in seen:
            continue
        data = _read_bounded(candidate, root)
        if data is None:
            continue
        seen.add(candidate)
        relative = candidate.relative_to(root.resolve(strict=True)).as_posix()
        entries.append(
            measured_instruction_entry(
                source_kind=InstructionSourceKind.INSTRUCTION_IMPORT,
                logical_ref=f"{root_label}/{relative}",
                data=data,
                load_reason="client-auto-import",
                role=role,
                route=route,
            )
        )
        _collect_imports(
            source=candidate,
            text=data.decode("utf-8", errors="replace"),
            root=root,
            root_label=root_label,
            role=role,
            route=route,
            seen=seen,
            depth=depth + 1,
            entries=entries,
        )


def _skill_metadata(path: Path, root: Path) -> bytes | None:
    """SKILL.md on yazisi (frontmatter); govde ve referanslar yalniz aktivasyonda yuklenir."""

    data = _read_bounded(path, root)
    if data is None:
        return None
    text = data.decode("utf-8", errors="replace")
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end < 0:
        return None
    return text[: end + 4].encode("utf-8")


def _skill_entries(
    *, client: ClientIntegrationId, project_root: Path, role: str | None, route: str | None
) -> list[EffectiveLoadEntry]:
    entries: list[EffectiveLoadEntry] = []
    for relative in _PROJECT_SKILL_DIRS[client]:
        base = project_root / relative
        try:
            if base.is_symlink() or not base.is_dir():
                continue
            children = sorted(base.iterdir(), key=lambda item: item.name.casefold())[:_MAX_SKILLS]
        except OSError:
            continue
        for child in children:
            metadata = _skill_metadata(child / "SKILL.md", project_root)
            if metadata is None:
                continue
            entries.append(
                measured_instruction_entry(
                    source_kind=InstructionSourceKind.SKILL,
                    logical_ref=f"project/{relative.as_posix()}/{child.name}",
                    data=metadata,
                    load_reason="skill-metadata-discovery",
                    role=role,
                    route=route,
                )
            )
    return entries


def duplicate_skill_discovery(report: EffectiveContextReport) -> dict[str, tuple[str, ...]]:
    """Ayni skill adinin birden fazla kesif dizininde bulundugu durumlar (A21 tanisi)."""

    by_name: dict[str, list[str]] = {}
    for entry in report.entries:
        if entry.source_kind != InstructionSourceKind.SKILL.value:
            continue
        by_name.setdefault(entry.logical_ref.rsplit("/", 1)[-1], []).append(entry.logical_ref)
    return {name: tuple(refs) for name, refs in by_name.items() if len(refs) > 1}


def _config_instruction_entries(
    *, project_root: Path, role: str | None, route: str | None
) -> list[EffectiveLoadEntry]:
    """OpenCode `opencode.json` `instructions` listesi; AGENTS.md ile ayni metin tekrar yuklenir."""

    raw = _read_bounded(project_root / "opencode.json", project_root)
    if raw is None:
        return []
    try:
        document = json.loads(raw)
    except json.JSONDecodeError:
        return []
    configured = document.get("instructions") if isinstance(document, dict) else None
    if not isinstance(configured, list):
        return []
    entries: list[EffectiveLoadEntry] = []
    for item in configured:
        if not isinstance(item, str):
            continue
        data = _read_bounded((project_root / item).resolve(strict=False), project_root)
        if data is None:
            continue
        entries.append(
            measured_instruction_entry(
                source_kind=InstructionSourceKind.INSTRUCTION_IMPORT,
                logical_ref=f"project/{item}",
                data=data,
                load_reason="client-config-instruction",
                role=role,
                route=route,
            )
        )
    return entries


def measure_instruction_load(
    *,
    user_home: Path,
    client: ClientIntegrationId,
    project_root: Path | None = None,
    role: str | None = None,
    route: str | None = None,
) -> EffectiveContextReport:
    """Global/proje instruction dosyalari ve otomatik import'lari icin rapor uretir."""

    entries: list[EffectiveLoadEntry] = []
    seen: set[Path] = set()
    sources: list[tuple[Path, Path, str, InstructionSourceKind, str]] = [
        (
            user_home / _GLOBAL_FILES[client],
            user_home,
            "home",
            InstructionSourceKind.INSTRUCTION_GLOBAL,
            f"client/{client.value}/global-instructions",
        )
    ]
    if project_root is not None:
        sources.append(
            (
                project_root / _PROJECT_FILES[client],
                project_root,
                "project",
                InstructionSourceKind.INSTRUCTION_PROJECT,
                f"client/{client.value}/project-instructions",
            )
        )
    for path, root, root_label, kind, logical in sources:
        data = _read_bounded(path, root)
        if data is None:
            continue
        seen.add(path.resolve(strict=True))
        entries.append(
            measured_instruction_entry(
                source_kind=kind,
                logical_ref=logical,
                data=data,
                load_reason="client-auto-load",
                role=role,
                route=route,
            )
        )
        _collect_imports(
            source=path,
            text=data.decode("utf-8", errors="replace"),
            root=root,
            root_label=root_label,
            role=role,
            route=route,
            seen=seen,
            depth=1,
            entries=entries,
        )
    if project_root is not None:
        if client is ClientIntegrationId.OPENCODE:
            entries.extend(
                _config_instruction_entries(project_root=project_root, role=role, route=route)
            )
        entries.extend(
            _skill_entries(client=client, project_root=project_root, role=role, route=route)
        )
    entries.append(
        unobservable_entry(
            source_kind=InstructionSourceKind.CLIENT_SYSTEM_PROMPT,
            logical_ref=f"client/{client.value}/system-prompt",
            load_reason="client-internal",
        )
    )
    entries.append(
        unobservable_entry(
            source_kind=InstructionSourceKind.CLIENT_TOOL_SCHEMA,
            logical_ref=f"client/{client.value}/tool-serialization",
            load_reason="client-internal",
        )
    )
    return EffectiveContextReport(tuple(entries))
