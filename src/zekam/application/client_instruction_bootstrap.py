"""Managed global instruction sections for supported CLI clients."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from zekam.domain.canonical import digest, digest_of_bytes
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
MAX_INSTRUCTION_FILE_BYTES = 4 * 1024 * 1024


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

    if path.stat(follow_symlinks=False).st_size > MAX_INSTRUCTION_FILE_BYTES:
        raise ConfigurationError("Client instruction dosyasi boyut siniri asildi")
    with path.open("rb") as stream:
        payload = stream.read(MAX_INSTRUCTION_FILE_BYTES + 1)
    if len(payload) > MAX_INSTRUCTION_FILE_BYTES:
        raise ConfigurationError("Client instruction dosyasi boyut siniri asildi")
    return payload.decode("utf-8")


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
    # `exists()` kirik symlink icin False verir; reparse/symlink kontrolu
    # varlik kontrolunden bagimsiz kalmali.
    if _unsafe(path) or (path.exists() and not path.is_file()):
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


# ---------------------------------------------------------------------------
# Proje-yerel ince giris (WP-02.01)
#
# Zekam workspace kokunde ve alt dizinlerinde kalin instruction/skill kopyasi
# degil, tek kanonik `AGENTS.md` kaynagina ince `@` girisi uretilir. Ayni kaynaga
# tekrar eden kalin kopyalar conflict olarak raporlanir; kanonik generator bu
# dosyalarin tek sahibi oldugu icin tekrar eden instruction/skill kopyalari buradan
# tekillestirilir. Zekam disi oturumda (workspace marker'i yoksa) plan inerttir:
# hic dosya hedeflenmez, hic yazim olmaz (NL-002 / inert davranis).
# ---------------------------------------------------------------------------

#: Tek kanonik giris kaynagi; ince shim'ler yalniz bu dosyaya `@` ile girer.
PROJECT_ENTRY_CANONICAL_SOURCE = "AGENTS.md"

# Istemciden bagimsiz ince giris shim'leri. OpenCode ve Codex `AGENTS.md`'yi dogrudan
# kesfeder; Claude Code ve Gemini `@` import'u kullanir. Bunlar kalin kopya degildir.
_PROJECT_ENTRY_SHIMS: tuple[tuple[ClientIntegrationId, str], ...] = (
    (ClientIntegrationId.CLAUDE_CODE, "CLAUDE.md"),
    (ClientIntegrationId.GEMINI, "GEMINI.md"),
)


@dataclass(frozen=True, slots=True)
class ProjectLocalEntryFilePlan:
    """Tek proje-yerel ince giris hedefi (kok veya alt dizin)."""

    client_id: str
    path: Path
    content: str
    action: str  # "create" | "unchanged"
    body_digest: str = ""


@dataclass(frozen=True, slots=True)
class ProjectLocalEntryConflict:
    client_id: str
    path: Path
    reason: str


@dataclass(frozen=True, slots=True)
class ProjectLocalEntryPlan:
    """Proje-yerel ince giris plani; inert ise hic hedef tasimaz."""

    project_root: Path
    inert: bool
    files: tuple[ProjectLocalEntryFilePlan, ...]
    conflicts: tuple[ProjectLocalEntryConflict, ...] = ()

    @property
    def changes_required(self) -> bool:
        return any(item.action != "unchanged" for item in self.files)


def thin_entry_body(entry_dir: Path, canonical_root: Path) -> str:
    """`entry_dir` icinde kanonik `AGENTS.md`'ye goreli `@` import'u uretir."""

    target = canonical_root / PROJECT_ENTRY_CANONICAL_SOURCE
    relative = os.path.relpath(target, start=entry_dir)
    return f"# Zekam\n\n@{relative.replace(os.sep, '/')}\n"


def _is_zekam_workspace_root(project_root: Path) -> bool:
    """Workspace marker'i ile dogrulanmis Zekam calisma alani kokunu dondurur.

    Yalniz dosya adi degil; `.ai/repository-context.json`'nin `project == "zekam"`
    ve schema onegi + `PROJE_MANIFESTI.yaml` varligi birlikte aranir. Ayni klasor
    adini tasiyan ilgisiz proje/kopya bosun sayilip inert kalir (WP-02.01).
    """

    if not project_root.is_dir():
        return False
    try:
        context = json.loads(
            (project_root / ".ai" / "repository-context.json").read_text(encoding="utf-8")
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(context, dict) or context.get("project") != "zekam":
        return False
    if not str(context.get("schema", "")).startswith("zekam-repository-context/"):
        return False
    return (project_root / "PROJE_MANIFESTI.yaml").is_file()


def _assert_project_local_target(project_root: Path, path: Path) -> None:
    if not path.is_absolute() or (
        project_root not in path.parents and path.parent != project_root
    ):
        raise ConfigurationError("Project entry hedefi proje koku altinda olmali")
    current = project_root
    for segment in path.relative_to(project_root).parts[:-1]:
        current = current / segment
        if current.exists() and (_unsafe(current) or not current.is_dir()):
            raise ConfigurationError("Project entry parent symlink/reparse olamaz")
    if path.exists() and (_unsafe(path) or not path.is_file()):
        raise ConfigurationError("Project entry hedefi regular file olmali")


def _entry_file_plan(
    client_id: ClientIntegrationId, path: Path, expected_body: str
) -> ProjectLocalEntryFilePlan | ProjectLocalEntryConflict:
    expected_digest = digest_of_bytes(expected_body.encode("utf-8"))
    if not path.exists():
        return ProjectLocalEntryFilePlan(
            client_id=client_id.value,
            path=path,
            content=expected_body,
            action="create",
            body_digest=expected_digest,
        )
    try:
        existing = read_instruction_text(path)
    except (OSError, UnicodeDecodeError):
        return ProjectLocalEntryConflict(client_id.value, path, "entry-file-unreadable")
    if _lf(existing) == _lf(expected_body):
        # Mevcut govde kanonik ince giristir (EOL farki gonderel); plan disaktaki
        # birebir baytlari ve digest'ini tasiyarak okunur kalir.
        return ProjectLocalEntryFilePlan(
            client_id=client_id.value,
            path=path,
            content=existing,
            action="unchanged",
            body_digest=digest_of_bytes(existing.encode("utf-8")),
        )
    # Mevcut ama kanonik ince govde degil: kullanici icerigi/kalin kopya drift.
    return ProjectLocalEntryConflict(
        client_id.value, path, "entry-content-not-canonical-thin-entry"
    )


def plan_project_local_entry(
    *,
    project_root: Path,
    subdirectory: Path | None = None,
    collect_conflicts: bool = True,
) -> ProjectLocalEntryPlan:
    """Proje-yerel ince giris planlar; Zekam disi kokte inerttir.

    `subdirectory` verildiginde giris o alt dizinde uretilir ve kanonik kaynaga
    goreli `@` ile baglanir (kokte tek `AGENTS.md`, kopya yok). Plan salt okunurdur;
    yazim ancak `apply_project_local_entry` ile yapilir.
    """

    if not project_root.is_absolute():
        raise ConfigurationError("Project entry absolute proje koku ister")
    entry_dir = project_root if subdirectory is None else subdirectory
    if entry_dir != project_root:
        if not entry_dir.is_absolute():
            raise ConfigurationError("Project entry alt dizini absolute olmali")
        try:
            entry_dir.relative_to(project_root)
        except ValueError as exc:
            raise ConfigurationError(
                "Project entry alt dizini proje koku icinde olmali"
            ) from exc
    for path in (entry_dir, *entry_dir.parents):
        if _unsafe(path):
            raise ConfigurationError("Project entry dizini symlink/reparse olamaz")
        if not path.is_dir():
            raise ConfigurationError("Project entry dizini regular directory olmali")

    if not _is_zekam_workspace_root(project_root):
        return ProjectLocalEntryPlan(project_root=project_root, inert=True, files=())

    expected_canonical = project_root / PROJECT_ENTRY_CANONICAL_SOURCE
    if not expected_canonical.is_file() or _unsafe(expected_canonical):
        conflict = ProjectLocalEntryConflict(
            ClientIntegrationId.OPENCODE.value, expected_canonical, "canonical-source-missing"
        )
        if not collect_conflicts:
            raise ManagedInstructionConflict(
                "Proje girisi kanonik kaynagi eksik", reason="canonical-source-missing"
            )
        return ProjectLocalEntryPlan(
            project_root=project_root, inert=False, files=(), conflicts=(conflict,)
        )

    body = thin_entry_body(entry_dir, project_root)
    files: list[ProjectLocalEntryFilePlan] = []
    conflicts: list[ProjectLocalEntryConflict] = []
    for client_id, name in _PROJECT_ENTRY_SHIMS:
        target = entry_dir / name
        _assert_project_local_target(project_root, target)
        outcome = _entry_file_plan(client_id, target, body)
        if isinstance(outcome, ProjectLocalEntryFilePlan):
            files.append(outcome)
        elif collect_conflicts:
            conflicts.append(outcome)
        else:
            raise ManagedInstructionConflict(
                "Proje girisi sahiplik drift", reason=outcome.reason
            )
    return ProjectLocalEntryPlan(
        project_root=project_root,
        inert=False,
        files=tuple(files),
        conflicts=tuple(conflicts),
    )


def apply_project_local_entry(plan: ProjectLocalEntryPlan) -> None:
    """Salt planlanmis proje-yerel ince giris hedeflerini yazar.

    Plan conflict tasimasi hic dosyaya yazmadan baslangicta red edilir.
    Hicbir hedef degistirilmeden once tum hedefler preflight ile dogrulanir
    (no-partial-write): plan sonrasi drift olan herhangi bir hedef, ILK hedef
    bile yazilmadan tum planin red edilmesini saglar.

    Not: preflight + per-file write akisi tam transaction atomisitesi degildir;
    yazma sirasindaki OS hatasi (disk dolusu, ACL, I/O) onceki hedefleri geri
    almaz. Fail-closed readback drift hata verir; OS hata icin rollback garanti
    edilmez.
    """

    if plan.conflicts:
        raise ConfigurationError(
            "Project entry plani conflict tasiyor; hic dosya yazilmedi: "
            + "; ".join(f"{conflict.path.name}:{conflict.reason}" for conflict in plan.conflicts)
        )
    # Preflight: hicbir hedef degistirilmeden once plan'daki TUM dosyalarin
    # mevcut durumu yeniden dogrulanir. Boylece siradaki (sonraki) dosya drift
    # olsa bile ilki yazilip partial state uretilmez.
    _preflight_project_local_entry(plan)
    for item in plan.files:
        if item.action == "unchanged":
            continue
        item.path.parent.mkdir(parents=True, exist_ok=True)
        _assert_project_local_target(plan.project_root, item.path)
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
        if read_instruction_text(item.path) != item.content:
            raise ConfigurationError("Project entry readback drift")


def _preflight_project_local_entry(plan: ProjectLocalEntryPlan) -> None:
    """Plan'daki tum hedefleri hic yazim yapmadan dogrular (no-partial-write).

    Conflict varsa hic dosya degisikligi olmadan red edilir. Conflict yoksa
    her hedefin mevcut durumu planla karsilastirilir: plan sonrasi drift (stale)
    olan hedef varlikta, ILK hedef bile yazilmadan tum plan red edilir. Boylece
    apply, "ilk dosya yazilir, ikinci dosya driftli" partial state uretebilir
    duruma gelmez. Not: bu preflight + per-file write akisi tam transaction
    atomisitesi degildir; yazma sirasindaki OS hatasi (disk dolusu, ACL, I/O)
    onceki hedefleri geri almaz. Readback drift fail-closed hata verir; OS
    hata icin rollback garanti edilmez.
    """

    if plan.conflicts:
        raise ConfigurationError(
            "Project entry plani conflict tasiyor; hic dosya yazilmedi: "
            + "; ".join(f"{conflict.path.name}:{conflict.reason}" for conflict in plan.conflicts)
        )
    for item in plan.files:
        _assert_project_local_target(plan.project_root, item.path)
        if not item.path.exists():
            continue
        current = read_instruction_text(item.path)
        expected_body = thin_entry_body(item.path.parent, plan.project_root)
        # Plan'daki `unchanged` karsilastirmasiyla ayni LF-normalize eslesme;
        # CRLF mevcut govde plan'da "unchanged" kabul edildigi icin apply'da
        # "stale" sayilip byte-exact kanonik LF govdeyle karsilastirilmaz.
        if _lf(current) != _lf(expected_body):
            raise ConfigurationError(
                "Project entry plani dosya drift nedeniyle stale; hic dosya yazilmedi"
            )


def project_entry_intent_document(plan: ProjectLocalEntryPlan) -> dict[str, object]:
    """Stabil, determinist plan govdesi (`plan_digest`'in kapsami).

    `action`/`apply` gibi istege bagli degisen alanlari tasimaz; yalnizca
    kanonik hedef govde (path + govde digest/icerigi) ve conflict kimligi
    uzerinden alinir. Boylece plan -> apply -> replay zincirinde ayni
    `plan_digest` yeniden uretilir ve claim idempotency payload'i drift
    ugratmaz.
    """

    return {
        "project_root": str(plan.project_root),
        "inert": plan.inert,
        "files": [
            {
                "client_id": item.client_id,
                "path": str(item.path),
                "body_digest": item.body_digest,
                "content": item.content,
            }
            for item in plan.files
        ],
        "conflicts": [
            {
                "client_id": conflict.client_id,
                "path": str(conflict.path),
                "reason": conflict.reason,
            }
            for conflict in plan.conflicts
        ],
    }


def project_entry_plan_digest(plan: ProjectLocalEntryPlan) -> str:
    """Stabil plan govdesinin `sha256:` digest'i (exact plan digest)."""

    return digest(project_entry_intent_document(plan))


def project_entry_plan_document(
    plan: ProjectLocalEntryPlan, *, apply: bool
) -> dict[str, object]:
    """CLI icin salt-okunur, provider'siz plan JSON govdesi.

    `plan_digest` yalnizca stabil intent govdesi uzerinden alinir; `action`
    gibi mevcut dosya durumuna bagli alanlar digest kapsami disinda bilgi
    olarak tasinir. Boylece plan -> apply -> replay zincirinde ayni
    `plan_digest` yeniden uretilir.
    """

    intent = project_entry_intent_document(plan)
    files: list[dict[str, object]] = [
        {
            "client_id": item.client_id,
            "path": str(item.path),
            "body_digest": item.body_digest,
            "content": item.content,
            "action": item.action,
        }
        for item in plan.files
    ]
    document: dict[str, object] = {
        "schema": "zekam-project-local-entry-plan/v1",
        **intent,
        "files": files,
        "apply": apply,
        "changes_required": plan.changes_required,
        "provider_calls": 0,
        "network_calls": 0,
        "grants_authority": False,
    }
    return document | {"plan_digest": project_entry_plan_digest(plan)}


def project_entry_effect_document(plan: ProjectLocalEntryPlan) -> dict[str, object]:
    """Claim/receipt zincirine giren stabil effect govdesi.

    `action` gibi istege bagli degisen alan tasimaz; idempotency replay'inde
    payload drift'i ugratmaz.
    """

    document: dict[str, object] = {
        "schema": "zekam-cli-integration-entry-effect/v1",
        **project_entry_intent_document(plan),
    }
    return document | {"plan_digest": project_entry_plan_digest(plan)}


def project_entry_receipt(plan: ProjectLocalEntryPlan) -> dict[str, object]:
    """Apply tamamlandiktan sonraki terminal receipt govdesi.

    Govde stabil plan intent'inden deterministik oldugu icin replay ayni
    receipt'i yeniden uretir; terminal readback ile her hedefin kanonik ince
    govdeye ulastigi dogrulanir (fail-closed).
    """

    if plan.conflicts:
        raise ConfigurationError(
            "Project entry receipt conflict tasiyor; terminal state kanitlanamadi"
        )
    for item in plan.files:
        current = read_instruction_text(item.path)
        if _lf(current) != _lf(item.content):
            raise ConfigurationError(
                "Project entry terminal readback drift; receipt kanitlanamadi"
            )
    body: dict[str, object] = {
        "schema": "zekam-project-local-entry-receipt/v1",
        "plan_digest": project_entry_plan_digest(plan),
        "project_root": str(plan.project_root),
        "inert": plan.inert,
        "state": "inert-noop" if plan.inert else "applied-and-read-back",
        "files": [
            {
                "client_id": item.client_id,
                "path": str(item.path),
                "body_digest": item.body_digest,
            }
            for item in plan.files
        ],
        "apply": True,
        "provider_calls": 0,
        "network_calls": 0,
        "grants_authority": False,
    }
    return body | {"receipt_digest": digest(body)}
