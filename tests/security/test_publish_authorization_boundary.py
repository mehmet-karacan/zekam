"""WP-02.03 — Push/yayin yetkilendirme siniri (guvenlik negatif testleri).

Push/yayin varsayilan olarak reddedilir (`kalite/COMMIT_POLITIKASI.md` sozlesmesi).
Bu dosya yayin kapisinin fail-closed sinirlarini uc yuzeyde dogrular:

* Domain (`evaluate_push`), istisna (`assert_push_allowed` -> `AuthorizationRequired`)
  ve CLI (`zekam git push-check` exit 77) ayni varsayilan reddi uygular.
* `authorization_digest` sozlesmesi: exact authorization `sha256:` onekli 64
  karakter kucuk harf onaltilik digest olmalidir. Sozlesme disi deger (duz metin,
  bos metin, kisaltilmis/yanlis onekli/yanlis karakterli digest) fail-closed
  reddedilir.
* Yetki kapsami disi varyantlar (force push, test kanidi yok, bagimsiz verifier
  sonucu yok) gecerli bicimli digest ile bile reddedilir.
* Commit mesaji secret benzeri deger veya kisisel absolute path tasimaz; mesaj
  hijyeni push yetkilendirmesinden bagimsiz olarak uygulanir.

SINIR (bilinen ve belgelenmis): `evaluate_push` saf bir domain fonksiyonudur;
digest'in VARLIGINI ve BICIMINI dogrular, out-of-band kimligini (degerin hangi
yetkilendirme kaydindan uretildigini) dogrulayamaz. Bicim uyumlu ama uydurulmus
bir digest kapinin kendisiyle ayirt edilemez; bu sinir asagida bilincli olarak
test edilir. Kapatilmasi ayri bir yetkilendirme-kaydi baglamasi (ledger binding)
planidir; bu dosya o iddiayi tasimaz. Indirect shell yayini (``bash -c``/
here-string ile uretilen push) icin native `opencode.json` deny kurallari garanti
vermez; o sinir `tests/unit/test_opencode_native_policy.py` kapsamindadir.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from zekam.domain.canonical import digest
from zekam.domain.commit_policy import (
    PushRequest,
    assert_push_allowed,
    check_commit_message,
    evaluate_push,
)
from zekam.domain.errors import AuthorizationRequired
from zekam.interfaces.cli.main import app
from zekam.interfaces.cli.session import EXIT_POLICY_VIOLATION

pytestmark = pytest.mark.security

runner = CliRunner()

#: Digest sozlesmesine uymayan degerler: duz metin, bos metin, kisa digest,
#: yanlis onek, buyuk harf hex, oneksiz hex.
MALFORMED_DIGESTS: tuple[str, ...] = (
    "onay",
    "",
    "sha256:abc",
    "SHA256:" + "a" * 64,
    "sha256:" + "A" * 64,
    "a" * 64,
)

HYGIENE_MESSAGE = (
    "guvenlik: yayin hijyeni sinirini dogrula\n"
    "\n"
    "Neden:\n- Yayin oncesi mesaj temizligi zorunludur.\n"
    "\n"
    "Degisiklik:\n- Negatif hijyen testleri eklendi.\n"
    "\n"
    "Kanit:\n- Hijyen testleri gecti.\n"
    "\n"
    "Risk:\n- Kapsam mesaj policeleri ile sinirlidir.\n"
    "\n"
    "Geri donus:\n- Test dosyasi kaldirilarak geri alinir.\n"
)


def _push(**kwargs: object) -> PushRequest:
    defaults: dict[str, object] = {"remote": "origin", "branch": "main", "head": "abc1234"}
    defaults.update(kwargs)
    return PushRequest(**defaults)  # type: ignore[arg-type]


def test_push_without_explicit_user_request_denied_on_every_surface() -> None:
    decision = evaluate_push(_push(), tests_passed=True, verifier_passed=True)
    assert decision.allowed is False
    assert "varsayilan" in decision.reason
    with pytest.raises(AuthorizationRequired):
        assert_push_allowed(_push(), tests_passed=True, verifier_passed=True)
    result = runner.invoke(app, ["git", "push-check", "origin", "main", "abc1234", "--json"])
    assert result.exit_code == EXIT_POLICY_VIOLATION
    assert json.loads(result.stdout)["allowed"] is False


def test_push_without_authorization_digest_denied_even_when_user_requested() -> None:
    decision = evaluate_push(_push(user_requested=True), tests_passed=True, verifier_passed=True)
    assert decision.allowed is False
    assert "authorization" in decision.reason
    result = runner.invoke(
        app,
        ["git", "push-check", "origin", "main", "abc1234", "--kullanici-istedi", "--json"],
    )
    assert result.exit_code == EXIT_POLICY_VIOLATION
    assert json.loads(result.stdout)["allowed"] is False


@pytest.mark.parametrize("value", MALFORMED_DIGESTS)
def test_malformed_authorization_digest_fail_closed(value: str) -> None:
    """Sozlesme disi digest fail-closed reddedilir; kanit bayraklari kurtarmaz."""
    decision = evaluate_push(
        _push(user_requested=True, authorization_digest=value),
        tests_passed=True,
        verifier_passed=True,
    )
    assert decision.allowed is False
    assert "authorization" in decision.reason


def test_push_check_rejects_malformed_authorization_digest() -> None:
    """CLI tam kanitla bile sozlesme disi digest'i kabul etmez; exit 77."""
    result = runner.invoke(
        app,
        [
            "git",
            "push-check",
            "origin",
            "main",
            "abc1234",
            "--kullanici-istedi",
            "--yetki-digest",
            "onay",
            "--test-gecti",
            "--verifier-gecti",
            "--json",
        ],
    )
    assert result.exit_code == EXIT_POLICY_VIOLATION
    payload = json.loads(result.stdout)
    assert payload["allowed"] is False
    assert "authorization" in payload["reason"]


@pytest.mark.parametrize(
    ("force", "tests", "verifier"),
    [(True, True, True), (False, False, True), (False, True, False), (False, False, False)],
)
def test_out_of_scope_authorized_variants_denied(force: bool, tests: bool, verifier: bool) -> None:
    """Gecerli bicimli digest ile bile kapsam disi varyant reddedilir."""
    decision = evaluate_push(
        _push(user_requested=True, authorization_digest=digest("auth"), force=force),
        tests_passed=tests,
        verifier_passed=verifier,
    )
    assert decision.allowed is False


def test_only_complete_evidence_chain_allows_push() -> None:
    request = _push(user_requested=True, authorization_digest=digest("auth"))
    decision = evaluate_push(request, tests_passed=True, verifier_passed=True)
    assert decision.allowed is True
    assert "authorization" in decision.reason
    assert decision.request_digest.startswith("sha256:")
    assert assert_push_allowed(request, tests_passed=True, verifier_passed=True) == decision


def test_gate_verifies_shape_and_evidence_not_provenance_boundary() -> None:
    """Bilinen sinir: bicim uyumlu uydurulmus digest kapida ayirt edilmez.

    Bu test mevcut davranisi bilincli olarak sabitler: kapi digest bicimini ve
    kanit bayraklarini dogrular, digest'in hangi yetkilendirme kaydindan
    uretildigini dogrulayamaz. Bicim uyumlu iki farkli uydurulmus deger de
    kabul edilir; enforcement iddiasi bu testle tasınmaz.
    """
    for fabricated in (digest("auth"), digest("baska-bir-uydurma")):
        decision = evaluate_push(
            _push(user_requested=True, authorization_digest=fabricated),
            tests_passed=True,
            verifier_passed=True,
        )
        assert decision.allowed is True


def test_commit_message_publish_hygiene_blocks_secret_and_personal_path() -> None:
    with_secret = HYGIENE_MESSAGE.replace(
        "- Negatif hijyen testleri eklendi.", "- api_key=AKIA123 eklendi"
    )
    check = check_commit_message(with_secret)
    assert check.accepted is False
    assert any(item.code == "secret" for item in check.violations)

    with_path = HYGIENE_MESSAGE.replace(
        "- Negatif hijyen testleri eklendi.", "- C:\\Users\\biri\\rapor guncellendi"
    )
    check = check_commit_message(with_path)
    assert check.accepted is False
    assert any(item.code == "personal-path" for item in check.violations)
