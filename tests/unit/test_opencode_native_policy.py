"""WP-02.03 — Proje-yerel OpenCode v2 native shell/publish policy.

Kapsam:

* Proje kokundeki ``opencode.json`` OpenCode v2 ``permissions`` seklinda,
  ONAYLI GOREV icin ``shell allow`` + dogrudan ``git push`` ve ``git push *``
  ``deny`` kurallarini tasir.
* Sema, siralama (last-match-wins) ve kapsam (yalniz ``$schema`` + ``permissions``)
  dogrulanir; ``default_agent``/eager ``instructions`` yok.
* Fixture Git testi YALNIZ ``git push --dry-run`` calistirir ve remote'a ref
  gitmedigini dogrular. Native OpenCode permission kabulunun yerine gecmez.

SINIR (acik destek siniri): Bu policy native OpenCode shell araci siniridir ve
``git push`` (scanner tarafindan uretilen ``git push *`` resource) icin
deny uygular. Indirect bypass — scanner'in parcalayamadigi komutlar, ``bash
-c``/here-string/saved approval yoluyla uretilen push — native sinir tarafindan
garanti edilemez; bu durum ``unsupported`` olarak raporlanir, blankette
enforcement iddia edilmez. ``opencode.json`` izin kurali ile Zekam'in
``evaluate_push`` kapisi ayri yuzeylerdir; bu dosya yalniz config seklini ve
Git fixture'ini test eder.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest

GIT = shutil.which("git")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _project_opencode_config() -> dict[str, Any]:
    raw = (_repo_root() / "opencode.json").read_text(encoding="utf-8")
    return cast("dict[str, Any]", json.loads(raw))


def _shell_rules(config: dict[str, Any]) -> list[dict[str, Any]]:
    rules = config.get("permissions")
    assert isinstance(rules, list) and rules, "permissions listesi olmali"
    return rules


def test_opencode_project_config_has_ordered_v2_shell_rules() -> None:
    """Schema, siralama (last-match-wins) ve kapsam (yalniz $schema + permissions)."""
    config = _project_opencode_config()
    assert config["$schema"] == "https://opencode.ai/config.json"
    assert set(config) == {"$schema", "permissions"}

    # v2 field adlari: action/resource/effect (V1 ``permission``/``bash`` degil).
    for rule in _shell_rules(config):
        assert set(rule) == {"action", "resource", "effect"}
        assert rule["effect"] in {"allow", "deny", "ask"}
        assert rule["action"] == "shell"

    # Sira: onayli gorev icin genis shell allow ILK, push deny SONRA.
    # Last-match-wins: genis allow, push'u basit komutta bastirir; push'a exact
    # deny basar. Boylece push/yayin exception'ina native sinirden gelir.
    rules = _shell_rules(config)
    assert [rule["effect"] for rule in rules] == ["allow", "deny", "deny"]
    assert rules[0]["resource"] == "*"
    assert rules[1]["resource"] == "git push"
    assert rules[-1]["resource"] == "git push *"
    assert rules[-1]["effect"] == "deny"

    # Kapsam: koordinator dayatma / eager instruction yok.
    assert "default_agent" not in config
    assert "instructions" not in config


def test_push_deny_is_last_and_wins_over_allow() -> None:
    """Push deny, allow'dan SONRA gelir; last-match-wins ile push reddedilir."""
    rules = _shell_rules(_project_opencode_config())
    effects = [rule["effect"] for rule in rules]
    # Allow bir yerlerde, deny sonda; boylece ``git push`` icin son esleme deny.
    assert effects.index("deny") > effects.index("allow")
    assert rules[-1]["effect"] == "deny"


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "git",
            "-c",
            "user.name=fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.mark.integration
def test_git_fixture_push_dry_run_only_never_real_push(tmp_path: Path) -> None:
    """Git fixture kaniti: YALNIZ ``git push --dry-run``.

    Gecerli fixture repo + bos bare remote kurulur. ``git push --dry-run``
    calisir; gercek push YAPILMAZ. Kanit: (1) komut exit 0 (dry-run push
    kapisi acik), (2) remote'a hic ref gitmez (push gercekten yapilmadi).
    """
    if GIT is None:
        pytest.skip("gercek git host'ta bulunamadi")

    remote = tmp_path / "remote.git"
    remote.mkdir()
    subprocess.run([GIT, "init", "--bare", "-q", str(remote)], check=True, capture_output=True)

    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    (work / "n.txt").write_text("fixture\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "fixture")
    _git(work, "remote", "add", "origin", str(remote))

    # YALNIZ --dry-run: push'u simgeler, gercek push YAPMAZ.
    dry = subprocess.run(
        ["git", "-C", str(work), "push", "--dry-run", "origin", "main"],
        capture_output=True,
        text=True,
    )
    assert dry.returncode == 0, (
        f"git push --dry-run beklenenden farkli sonlandi: rc={dry.returncode}\n"
        f"stdout={dry.stdout}\nstderr={dry.stderr}"
    )

    # Gercek push yapilmamasi kaniti: remote'a hic ref gitmemeli.
    refs_dir = remote / "refs" / "heads"
    packed = remote / "packed-refs"
    remote_refs = (
        sorted(p.relative_to(refs_dir).as_posix() for p in refs_dir.rglob("*") if p.is_file())
        if refs_dir.exists()
        else []
    )
    if packed.exists():
        remote_refs += [
            line.split()[1]
            for line in packed.read_text(encoding="utf-8").splitlines()
            if line and not line.startswith("#")
        ]
    assert remote_refs == [], (
        f"git push --dry-run olmasina ragmen remote'a ref gitti: {remote_refs}"
    )
