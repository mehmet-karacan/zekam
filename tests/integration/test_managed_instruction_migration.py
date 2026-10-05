"""Managed instruction migration: known-legacy digest, backup, replay and rollback gates.

Every test works on a TEST HOME (tmp_path) with real file operations; the real user home is
never touched. Crash tests kill a real subprocess with ``os._exit`` mid-apply.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from zekam.application.client_instruction_bootstrap import (
    CURRENT_BODY_DIGEST,
    KNOWN_LEGACY_BODY_DIGESTS,
    inspect_managed_section,
    plan_client_instruction_bootstrap,
)
from zekam.application.client_integrations import (
    IntegrationSyncPlan,
    apply_rollback_plan,
    apply_sync_plan,
    build_rollback_plan,
    build_sync_plan,
)
from zekam.application.composition import ApplicationContext, build_context
from zekam.domain.client_integration import ClientIntegrationPolicy
from zekam.domain.errors import PolicyViolation

pytestmark = pytest.mark.integration

_REPO = Path(__file__).resolve().parents[2]
_START = "<!-- zekam-managed-client-instructions/v1:start -->"
_END = "<!-- zekam-managed-client-instructions/v1:end -->"
_LEGACY_VERSION = "managed-instructions/v1/pre-w01-94f4892"
# W01 (94f4892) oncesi managed govde; kaynak: git show 94f4892^ client_instruction_bootstrap.py
_LEGACY_BODY = "\n".join(
    (
        _START,
        "## Zekam managed bootstrap",
        "",
        "- Zekam ile ilgili calismadan once `zekam doctor --json` calistir.",
        "- Git pull/merge sonrasinda doctor pending migration veya eksik routine bildirirse "
        "ve kullanici local DB hazirlamayi yetkilendirdiyse `zekam doctor --hazirla --json` "
        "calistir; bu komut kayitli migration'lari bounded uygular ve final doctor yapar.",
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
        "- Obsidian projection salt okunur gorunumdur; kanonik authority yerel "
        "operational store'dur ve projection dosyalari elle degistirilmez.",
        "- Olcumlu loop durumunu raw transcript istemeden `zekam loop status "
        "<loop-id> --json` ile oku; metric ve stop reason kanonik operational store'dan gelir.",
        "- `zekam` kullanilamiyorsa pending talebi koru ve kurulum/onarimdan once "
        "kullanici onayi iste.",
        _END,
    )
)
_PREFIX = "# Benim kurallarim\n\nHer zaman Turkce yaz.\n\n"
_SUFFIX = "\n\n# Sonum\n\nSon not.\n"


def _context(tmp_path: Path) -> ApplicationContext:
    home = tmp_path / "zekam-home"
    home.mkdir()
    (home / "config.yaml").write_text(
        "schema: zekam-config/v1\ncli:\n  integrations:\n    version: 2\n    opencode: true\n",
        encoding="utf-8",
    )
    return build_context(home=home, environ={})


def _native(tmp_path: Path) -> Path:
    native = tmp_path / "native-user"
    native.mkdir()
    return native


def _claude(native: Path) -> Path:
    return native / ".claude" / "CLAUDE.md"


def _opencode(native: Path) -> Path:
    return native / ".config" / "opencode" / "AGENTS.md"


def _seed(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def _legacy_file(*, eol: str = "\n") -> bytes:
    text = _PREFIX + _LEGACY_BODY + _SUFFIX
    return text.replace("\n", eol).encode("utf-8")


def _plan(context: ApplicationContext, native: Path) -> IntegrationSyncPlan:
    return build_sync_plan(
        build_context(home=context.home, environ={}),
        scope="user",
        native_user_root=native,
        enable=("claude-code",),
    )


def _instruction_ops(plan: IntegrationSyncPlan) -> list[dict[str, Any]]:
    return [op for op in plan.operations if op["operation"] == "write-client-instruction-config"]


def _sha(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_known_legacy_digest_table_matches_the_pre_w01_body() -> None:
    assert _sha(_LEGACY_BODY) in KNOWN_LEGACY_BODY_DIGESTS
    assert KNOWN_LEGACY_BODY_DIGESTS[_sha(_LEGACY_BODY)] == _LEGACY_VERSION
    assert _sha(_LEGACY_BODY) != CURRENT_BODY_DIGEST


def test_clean_install_creates_then_second_plan_is_empty(tmp_path: Path) -> None:
    context, native = _context(tmp_path), _native(tmp_path)
    plan = _plan(context, native)
    assert plan.conflicts == ()
    assert {op["instruction_action"] for op in _instruction_ops(plan)} == {"create"}
    assert all(op["migrated_from_version"] is None for op in _instruction_ops(plan))
    apply_sync_plan(plan, authorized_plan_digest=plan.plan_digest)

    view = inspect_managed_section(_claude(native).read_bytes().decode("utf-8"))
    assert view is not None and view.body_digest == CURRENT_BODY_DIGEST
    assert _instruction_ops(_plan(context, native)) == []


def test_known_old_install_migrates_preserving_user_text_and_is_noop_twice(
    tmp_path: Path,
) -> None:
    context, native = _context(tmp_path), _native(tmp_path)
    legacy = _legacy_file()
    _seed(_claude(native), legacy)
    _seed(_opencode(native), legacy)

    plan = _plan(context, native)
    assert plan.conflicts == ()
    ops = {op["relative_path"]: op for op in _instruction_ops(plan)}
    assert set(ops) == {".claude/CLAUDE.md", ".config/opencode/AGENTS.md"}
    for op in ops.values():
        assert op["instruction_action"] == "migrate"
        assert op["ownership_proof"] == "known-legacy-managed-digest"
        assert op["migrated_from_version"] == _LEGACY_VERSION
        assert op["previous_body_digest"] == _sha(_LEGACY_BODY)
        assert op["target_body_digest"] == CURRENT_BODY_DIGEST
    # Dry-run read-only.
    assert _claude(native).read_bytes() == legacy
    assert not (context.home / "quarantine").exists()
    with pytest.raises(PolicyViolation, match="exact plan digest"):
        apply_sync_plan(plan, authorized_plan_digest="sha256:" + "0" * 64)
    assert _claude(native).read_bytes() == legacy

    receipt = apply_sync_plan(plan, authorized_plan_digest=plan.plan_digest)
    for path in (_claude(native), _opencode(native)):
        text = path.read_bytes().decode("utf-8")
        view = inspect_managed_section(text)
        assert view is not None and view.body_digest == CURRENT_BODY_DIGEST
        assert view.prefix == _PREFIX
        assert view.suffix == _SUFFIX
    backups = {row["relative_path"]: row for row in receipt["native_config_backups"]}
    backup = context.home / str(backups[".claude/CLAUDE.md"]["backup_ref"])
    assert backup.read_bytes() == legacy

    # Ikinci apply ayni receipt'i tekrar eder; yeni effect/backup yok.
    after_bytes = _claude(native).read_bytes()
    backup_files = sorted((context.home / "quarantine").rglob("*"))
    replay = apply_sync_plan(plan, authorized_plan_digest=plan.plan_digest)
    assert replay["receipt_digest"] == receipt["receipt_digest"]
    assert _claude(native).read_bytes() == after_bytes
    assert sorted((context.home / "quarantine").rglob("*")) == backup_files
    # Yeniden planlama da instruction effect'i uretmez.
    assert _instruction_ops(_plan(context, native)) == []


def test_known_old_crlf_install_keeps_crlf_line_endings(tmp_path: Path) -> None:
    context, native = _context(tmp_path), _native(tmp_path)
    _seed(_claude(native), _legacy_file(eol="\r\n"))
    plan = _plan(context, native)
    assert plan.conflicts == ()
    apply_sync_plan(plan, authorized_plan_digest=plan.plan_digest)
    data = _claude(native).read_bytes()
    assert data.count(b"\n") == data.count(b"\r\n")
    view = inspect_managed_section(data.decode("utf-8"))
    assert view is not None and view.body_digest == CURRENT_BODY_DIGEST
    assert view.prefix == _PREFIX.replace("\n", "\r\n")
    assert view.suffix == _SUFFIX.replace("\n", "\r\n")


@pytest.mark.parametrize(
    ("label", "mutate", "reason"),
    (
        (
            "edited-body",
            lambda body: body.replace("calistir.", "calistirma."),
            "managed-instruction-ownership-drift",
        ),
        (
            "extra-line",
            lambda body: body.replace(_END, "- kullanici ekledi\n" + _END),
            "managed-instruction-ownership-drift",
        ),
        (
            "start-marker-only",
            lambda body: body.split("\n")[0],
            "managed-instruction-section-broken",
        ),
        (
            "duplicate-section",
            lambda body: body + "\n" + body,
            "managed-instruction-section-broken",
        ),
        (
            "reversed-markers",
            lambda body: _END + "\nx\n" + _START,
            "managed-instruction-section-broken",
        ),
    ),
)
def test_unknown_body_or_marker_collision_is_conflict_and_preserved(
    tmp_path: Path, label: str, mutate: Any, reason: str
) -> None:
    context, native = _context(tmp_path), _native(tmp_path)
    drifted = (_PREFIX + mutate(_LEGACY_BODY) + _SUFFIX).encode("utf-8")
    _seed(_claude(native), drifted)
    legacy = _legacy_file()
    _seed(_opencode(native), legacy)

    plan = _plan(context, native)
    assert {"relative_path": ".claude/CLAUDE.md", "reason": reason} in list(plan.conflicts), label
    # Sadece marker/benzer govde sahiplik kaniti degil: bozuk dosya plana girmez.
    assert ".claude/CLAUDE.md" not in {op["relative_path"] for op in _instruction_ops(plan)}
    with pytest.raises(PolicyViolation, match="unresolved ownership conflict"):
        apply_sync_plan(plan, authorized_plan_digest=plan.plan_digest)
    assert _claude(native).read_bytes() == drifted
    assert _opencode(native).read_bytes() == legacy


def test_marker_lookalike_in_user_text_never_grants_ownership(tmp_path: Path) -> None:
    native = _native(tmp_path)
    forged = _PREFIX + _START + "\nben bir kullanici notuyum\n" + _END + _SUFFIX
    _seed(_claude(native), forged.encode("utf-8"))
    view = inspect_managed_section(forged)
    assert view is not None and not view.owned
    plan = plan_client_instruction_bootstrap(
        user_home=native,
        integration_policy=ClientIntegrationPolicy(opencode=False, claude_code=True),
        collect_conflicts=True,
    )
    assert plan.files == ()
    assert [c.reason for c in plan.conflicts] == ["managed-instruction-ownership-drift"]
    assert _claude(native).read_bytes() == forged.encode("utf-8")


def test_rollback_restores_old_bytes_and_never_overwrites_later_user_edit(
    tmp_path: Path,
) -> None:
    context, native = _context(tmp_path), _native(tmp_path)
    legacy = _legacy_file()
    _seed(_claude(native), legacy)
    _seed(_opencode(native), legacy)
    plan = _plan(context, native)
    receipt = apply_sync_plan(plan, authorized_plan_digest=plan.plan_digest)

    # Temiz rollback (kullanici edit'i yok) once ikinci bir kopyada gosterilir.
    rollback = build_rollback_plan(
        build_context(home=context.home, environ={}),
        receipt_id=receipt["receipt_id"],
        native_user_root=native,
    )
    # Sonradan gelen kullanici edit'i: rollback conflict verir ve dosyayi ezmez.
    edited = _claude(native).read_bytes() + b"\nsonradan eklenen kullanici notu\n"
    _claude(native).write_bytes(edited)
    with pytest.raises(PolicyViolation, match="native config drift"):
        build_rollback_plan(
            build_context(home=context.home, environ={}),
            receipt_id=receipt["receipt_id"],
            native_user_root=native,
        )
    with pytest.raises(PolicyViolation, match="native config drift"):
        apply_rollback_plan(rollback, authorized_plan_digest=rollback.plan_digest)
    assert _claude(native).read_bytes() == edited

    # Edit geri alininca rollback yalniz kendi degisikligini geri alir.
    _claude(native).write_bytes(edited.removesuffix(b"\nsonradan eklenen kullanici notu\n"))
    clean = build_rollback_plan(
        build_context(home=context.home, environ={}),
        receipt_id=receipt["receipt_id"],
        native_user_root=native,
    )
    apply_rollback_plan(clean, authorized_plan_digest=clean.plan_digest)
    assert _claude(native).read_bytes() == legacy
    assert _opencode(native).read_bytes() == legacy
    again = apply_rollback_plan(clean, authorized_plan_digest=clean.plan_digest)
    assert again["state"] == "rolled-back-and-read-back"
    assert _claude(native).read_bytes() == legacy


def test_in_process_failure_compensation_does_not_clobber_concurrent_user_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import zekam.application.client_integrations as module

    context, native = _context(tmp_path), _native(tmp_path)
    legacy = _legacy_file()
    _seed(_claude(native), legacy)
    plan = _plan(context, native)
    real = module._atomic_write
    user_edit = b"kullanici bu arada tamamen yeni icerik yazdi\n"

    def interfere(path: Path, payload: bytes) -> None:
        real(path, payload)
        if path == _claude(native):
            # Native yazimdan hemen sonra baska surec dosyayi degistirir, sonra hata gelir.
            path.write_bytes(user_edit)
            raise OSError("injected failure after native write")

    monkeypatch.setattr(module, "_atomic_write", interfere)
    with pytest.raises(PolicyViolation, match="recovery-required"):
        apply_sync_plan(plan, authorized_plan_digest=plan.plan_digest)
    assert _claude(native).read_bytes() == user_edit


_CRASH_SCRIPT = textwrap.dedent(
    """
    import os, sys
    from pathlib import Path
    import zekam.application.client_integrations as ci
    from zekam.application.composition import build_context

    home, native, mode, target = sys.argv[1:5]
    plan = ci.build_sync_plan(
        build_context(home=Path(home), environ={}),
        scope="user",
        native_user_root=Path(native),
        enable=("claude-code",),
    )
    print(plan.plan_digest, flush=True)
    real_write = ci._atomic_write
    real_replace = os.replace
    if mode == "after-write":
        def write(path, payload):
            real_write(path, payload)
            if str(path).replace("\\\\", "/").endswith(target):
                os._exit(17)
        ci._atomic_write = write
    elif mode == "before-replace":
        def replace(src, dst, *args, **kwargs):
            if str(dst).replace("\\\\", "/").endswith(target):
                os._exit(17)
            return real_replace(src, dst, *args, **kwargs)
        os.replace = replace
    ci.apply_sync_plan(plan, authorized_plan_digest=plan.plan_digest)
    """
)


def _crash(context: ApplicationContext, native: Path, mode: str, target: str) -> str:
    result = subprocess.run(
        [sys.executable, "-c", _CRASH_SCRIPT, str(context.home), str(native), mode, target],
        cwd=_REPO,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert result.returncode == 17, result.stderr
    return result.stdout.strip().splitlines()[0]


def _journals(context: ApplicationContext) -> list[Path]:
    root = context.home / "state" / "manifests" / "client-integration-pending"
    return sorted(root.glob("*.json")) if root.is_dir() else []


def test_crash_before_atomic_replace_leaves_old_file_and_replays_same_plan(
    tmp_path: Path,
) -> None:
    context, native = _context(tmp_path), _native(tmp_path)
    legacy = _legacy_file()
    _seed(_claude(native), legacy)
    _seed(_opencode(native), legacy)

    crashed_digest = _crash(context, native, "before-replace", ".claude/CLAUDE.md")
    # Tek dosya ici managed parca yarim yazilmadi: eski bayt'lar sağlam, intent journal var.
    assert _claude(native).read_bytes() == legacy
    assert len(_journals(context)) == 1
    assert not (context.home / "state" / "manifests" / "client-integrations").exists()

    replay = _plan(context, native)
    assert replay.plan_digest == crashed_digest
    assert replay.pending_recovery == ()
    receipt = apply_sync_plan(replay, authorized_plan_digest=replay.plan_digest)
    for path in (_claude(native), _opencode(native)):
        view = inspect_managed_section(path.read_bytes().decode("utf-8"))
        assert view is not None and view.body_digest == CURRENT_BODY_DIGEST
        assert view.prefix == _PREFIX and view.suffix == _SUFFIX
    assert receipt["recovered_pending_journals"] == []


def test_crash_between_files_is_recovered_adopted_and_rolls_back_whole_package(
    tmp_path: Path,
) -> None:
    context, native = _context(tmp_path), _native(tmp_path)
    legacy = _legacy_file()
    _seed(_claude(native), legacy)
    _seed(_opencode(native), legacy)

    crashed_digest = _crash(context, native, "after-write", ".claude/CLAUDE.md")
    migrated = inspect_managed_section(_claude(native).read_bytes().decode("utf-8"))
    assert migrated is not None and migrated.body_digest == CURRENT_BODY_DIGEST
    assert _opencode(native).read_bytes() == legacy  # ikinci dosya henuz dokunulmadi
    assert len(_journals(context)) == 1

    plan = _plan(context, native)
    assert plan.plan_digest != crashed_digest
    assert plan.conflicts == ()
    # Yalniz kalan is planlanir; yarim kalan paket pending_recovery'de gorunur.
    assert {op["relative_path"] for op in _instruction_ops(plan)} == {".config/opencode/AGENTS.md"}
    states = {
        item["relative_path"]: item["state"]
        for row in plan.pending_recovery
        for item in row["operations"]
    }
    assert states[".claude/CLAUDE.md"] == "applied"

    receipt = apply_sync_plan(plan, authorized_plan_digest=plan.plan_digest)
    assert receipt["recovered_pending_journals"] == [crashed_digest.removeprefix("sha256:")]
    assert _journals(context) and all(
        '"state":"intent"' not in path.read_text(encoding="utf-8") for path in _journals(context)
    )
    assert {op["relative_path"] for op in receipt["operations"]} >= {
        ".claude/CLAUDE.md",
        ".config/opencode/AGENTS.md",
    }
    # Crash eden calismanin yedegi korunmus oldugu icin rollback butun paketi geri alir.
    rollback = build_rollback_plan(
        build_context(home=context.home, environ={}),
        receipt_id=receipt["receipt_id"],
        native_user_root=native,
    )
    apply_rollback_plan(rollback, authorized_plan_digest=rollback.plan_digest)
    assert _claude(native).read_bytes() == legacy
    assert _opencode(native).read_bytes() == legacy


def test_m17_crash_then_user_edit_is_preserved_and_reported_as_conflict(
    tmp_path: Path,
) -> None:
    context, native = _context(tmp_path), _native(tmp_path)
    legacy = _legacy_file()
    _seed(_claude(native), legacy)
    _seed(_opencode(native), legacy)
    _crash(context, native, "after-write", ".claude/CLAUDE.md")

    edited = _claude(native).read_bytes() + b"\nCrash sonrasi kullanici edit'i\n"
    _claude(native).write_bytes(edited)

    plan = _plan(context, native)
    states = {
        item["relative_path"]: item["state"]
        for row in plan.pending_recovery
        for item in row["operations"]
    }
    assert states[".claude/CLAUDE.md"] == "conflict"
    receipt = apply_sync_plan(plan, authorized_plan_digest=plan.plan_digest)
    assert _claude(native).read_bytes() == edited
    assert [item["relative_path"] for item in receipt["recovery_conflicts"]] == [
        ".claude/CLAUDE.md"
    ]
    # Conflict'li dosya receipt operasyonlarina alinmadigi icin rollback ona dokunmaz.
    rollback = build_rollback_plan(
        build_context(home=context.home, environ={}),
        receipt_id=receipt["receipt_id"],
        native_user_root=native,
    )
    apply_rollback_plan(rollback, authorized_plan_digest=rollback.plan_digest)
    assert _claude(native).read_bytes() == edited
    assert _opencode(native).read_bytes() == legacy


def test_crash_then_user_breaks_unmigrated_file_is_conflict_not_overwritten(
    tmp_path: Path,
) -> None:
    context, native = _context(tmp_path), _native(tmp_path)
    legacy = _legacy_file()
    _seed(_claude(native), legacy)
    _seed(_opencode(native), legacy)
    _crash(context, native, "after-write", ".claude/CLAUDE.md")

    drifted = legacy.replace(b"Zekam ile", b"Benim kendi")
    _opencode(native).write_bytes(drifted)
    plan = _plan(context, native)
    assert [c["reason"] for c in plan.conflicts] == ["managed-instruction-ownership-drift"]
    with pytest.raises(PolicyViolation, match="unresolved ownership conflict"):
        apply_sync_plan(plan, authorized_plan_digest=plan.plan_digest)
    assert _opencode(native).read_bytes() == drifted


def test_disabled_client_detaches_known_legacy_section_and_keeps_user_text(
    tmp_path: Path,
) -> None:
    context, native = _context(tmp_path), _native(tmp_path)
    _seed(_claude(native), _legacy_file())

    plan = build_sync_plan(context, scope="user", native_user_root=native, disable=("claude-code",))
    assert plan.conflicts == ()
    detach = [op for op in plan.operations if op["operation"] == "detach-client-instruction-config"]
    assert [op["relative_path"] for op in detach] == [".claude/CLAUDE.md"]
    apply_sync_plan(plan, authorized_plan_digest=plan.plan_digest)
    remaining = _claude(native).read_bytes().decode("utf-8")
    assert _START not in remaining and _END not in remaining
    assert remaining.startswith(_PREFIX.rstrip("\n")) and remaining.rstrip().endswith("Son not.")
