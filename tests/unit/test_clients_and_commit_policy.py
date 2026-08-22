"""P10-T05/T06 istemci adapter sozlesmesi ve commit/push kapisi testleri."""

from __future__ import annotations

import pytest

from zekam.domain.canonical import digest
from zekam.domain.clients import (
    ClientDescriptor,
    ClientKind,
    DispatchOutcome,
    DispatchRequest,
    DispatchResult,
    parse_result,
)
from zekam.domain.commit_policy import (
    TITLE_MAX_LENGTH,
    PushRequest,
    assert_push_allowed,
    check_commit_message,
    evaluate_push,
)
from zekam.domain.errors import AuthorizationRequired, PolicyViolation, ValidationFailed
from zekam.infrastructure.clients.adapters import opencode_adapter

GOOD_MESSAGE = """ozellik: sandbox teslim akisini ekle

Neden:
- Builder main tree'ye yazamamali.

Degisiklik:
- Detached worktree ve allowlist eklendi.

Kanit:
- Sandbox kabul testleri gecti.

Risk:
- Worktree temizligi disk kullanir.

Geri donus:
- git worktree remove ile geri alinir.
"""


def _descriptor(**kwargs: object) -> ClientDescriptor:
    defaults: dict[str, object] = {
        "kind": ClientKind.CODEX,
        "client_id": "codex",
        "executable": "codex.exe",
        "capabilities": frozenset({"chat", "code", "structured-result"}),
    }
    defaults.update(kwargs)
    return ClientDescriptor(**defaults)  # type: ignore[arg-type]


def _request(**kwargs: object) -> DispatchRequest:
    defaults: dict[str, object] = {
        "client_id": "codex",
        "role": "researcher",
        "instruction_digest": digest("instruction"),
        "context_manifest_digest": digest("manifest"),
        "timeout_seconds": 60,
    }
    defaults.update(kwargs)
    return DispatchRequest(**defaults)  # type: ignore[arg-type]


# -- T05: istemci adapter -----------------------------------------------------


def test_istemci_exact_calistirilabilir_beyan_etmeli() -> None:
    with pytest.raises(PolicyViolation):
        _descriptor(executable="   ")


def test_bilinmeyen_yetenek_beyani_reddedilir() -> None:
    with pytest.raises(ValidationFailed):
        _descriptor(capabilities=frozenset({"telepathy"}))
    with pytest.raises(ValidationFailed):
        _descriptor(capabilities=frozenset())


def test_beyan_edilmeyen_yetenek_cikarim_yoluyla_varsayilmaz() -> None:
    descriptor = _descriptor(capabilities=frozenset({"chat"}))
    assert descriptor.supports("parallel-dispatch") is False
    with pytest.raises(PolicyViolation):
        descriptor.assert_supports("parallel-dispatch")
    with pytest.raises(ValidationFailed):
        descriptor.assert_supports("telepathy")


def test_opencode_paralel_dispatch_yetenegini_acikca_beyan_eder() -> None:
    assert opencode_adapter("opencode.exe").descriptor.supports("parallel-dispatch") is True


def test_adapter_sonucu_authority_veremez() -> None:
    with pytest.raises(PolicyViolation):
        DispatchResult(
            client_id="codex",
            role="researcher",
            outcome=DispatchOutcome.SUCCESS,
            exit_code=0,
            payload={"finding": "x"},
            grants_authority=True,
        )


def test_non_success_sonuc_kategori_ister() -> None:
    with pytest.raises(ValidationFailed):
        DispatchResult(
            client_id="codex",
            role="researcher",
            outcome=DispatchOutcome.FAILED,
            exit_code=1,
            payload={},
        )


def test_adapter_payloadi_secret_tasiyamaz() -> None:
    with pytest.raises(PolicyViolation):
        DispatchResult(
            client_id="codex",
            role="researcher",
            outcome=DispatchOutcome.SUCCESS,
            exit_code=0,
            payload={"api_key": "AKIA123"},
        )


@pytest.mark.parametrize(
    ("document", "category"),
    [
        ("duz metin", "unparsable-result"),
        ({"outcome": "harika"}, "unknown-outcome"),
        ({"outcome": "success", "payload": ["liste"]}, "invalid-payload"),
    ],
)
def test_sema_disi_cikti_sessizce_kabul_edilmez(document: object, category: str) -> None:
    result = parse_result(_descriptor(), _request(), document)
    assert result.outcome is DispatchOutcome.FAILED
    assert result.failure_category == category


def test_gecerli_sonuc_ayristirilir() -> None:
    result = parse_result(
        _descriptor(),
        _request(),
        {"outcome": "success", "exit_code": 0, "payload": {"finding_count": 3}},
    )
    assert result.is_success is True
    assert result.payload == {"finding_count": 3}
    assert result.as_dict()["grants_authority"] is False


def test_istek_baska_istemciye_aitse_reddedilir() -> None:
    with pytest.raises(ValidationFailed):
        parse_result(_descriptor(client_id="codex"), _request(client_id="claude-code"), {})


def test_iptal_sonucu_gorunur_kalir() -> None:
    result = DispatchResult(
        client_id="codex",
        role="researcher",
        outcome=DispatchOutcome.CANCELLED,
        exit_code=None,
        payload={},
        failure_category="user-cancelled",
    )
    assert result.is_success is False
    assert result.as_dict()["failure_category"] == "user-cancelled"


# -- T06: commit politikasi ---------------------------------------------------


def test_gecerli_turkce_ascii_commit_kabul_edilir() -> None:
    check = check_commit_message(GOOD_MESSAGE)
    assert check.accepted is True
    check.assert_accepted()


def test_non_ascii_commit_reddedilir() -> None:
    check = check_commit_message(GOOD_MESSAGE.replace("ekle", "ekleç"))
    assert check.accepted is False
    assert any(item.code == "non-ascii" for item in check.violations)
    with pytest.raises(PolicyViolation):
        check.assert_accepted()


@pytest.mark.parametrize("title", ["update", "wip", "fix stuff", "misc"])
def test_anlamsiz_baslik_reddedilir(title: str) -> None:
    check = check_commit_message(
        GOOD_MESSAGE.replace("ozellik: sandbox teslim akisini ekle", f"bakim: {title}")
    )
    assert any(item.code == "anlamsiz-baslik" for item in check.violations)


def test_izinsiz_tur_reddedilir() -> None:
    check = check_commit_message(GOOD_MESSAGE.replace("ozellik:", "feature:", 1))
    assert any(item.code == "tur" for item in check.violations)


def test_baslik_bicimi_zorunlu() -> None:
    check = check_commit_message("sandbox teslim akisini ekle\n\nNeden:\n- x\n")
    assert any(item.code == "baslik-bicimi" for item in check.violations)


def test_yalniz_issue_kimligi_baslik_olamaz() -> None:
    check = check_commit_message(
        GOOD_MESSAGE.replace("sandbox teslim akisini ekle", "ZEKAM-123", 1)
    )
    assert any(item.code == "yalniz-id" for item in check.violations)


def test_uzun_baslik_reddedilir() -> None:
    long_title = "ozellik: " + "a" * TITLE_MAX_LENGTH
    check = check_commit_message(long_title + "\n\n" + GOOD_MESSAGE.split("\n", 2)[2])
    assert any(item.code == "baslik-uzunlugu" for item in check.violations)


def test_eksik_govde_bolumu_reddedilir() -> None:
    check = check_commit_message("ozellik: sandbox teslim akisini ekle\n\nNeden:\n- x\n")
    violation = next(item for item in check.violations if item.code == "eksik-bolum")
    assert "Degisiklik:" in violation.detail
    assert "Geri donus:" in violation.detail


def test_secret_ve_kisisel_path_reddedilir() -> None:
    with_secret = GOOD_MESSAGE.replace("- Detached worktree", "- api_key=AKIA123 eklendi")
    assert any(item.code == "secret" for item in check_commit_message(with_secret).violations)
    with_path = GOOD_MESSAGE.replace("- Detached worktree", "- C:\\Users\\biri\\zekam guncellendi")
    assert any(item.code == "personal-path" for item in check_commit_message(with_path).violations)


def test_merge_mesajina_controlled_exception() -> None:
    check = check_commit_message("Merge branch 'main' into feature")
    assert check.is_generated is True
    assert check.accepted is True


# -- T06: push kapisi ---------------------------------------------------------


def _push(**kwargs: object) -> PushRequest:
    defaults: dict[str, object] = {"remote": "origin", "branch": "main", "head": "abc1234"}
    defaults.update(kwargs)
    return PushRequest(**defaults)  # type: ignore[arg-type]


def test_push_varsayilan_olarak_reddedilir() -> None:
    decision = evaluate_push(_push(), tests_passed=True, verifier_passed=True)
    assert decision.allowed is False
    assert "varsayilan" in decision.reason


def test_push_exact_authorization_ister() -> None:
    decision = evaluate_push(_push(user_requested=True), tests_passed=True, verifier_passed=True)
    assert decision.allowed is False
    assert "authorization" in decision.reason


def test_force_push_otomatik_izinli_degil() -> None:
    decision = evaluate_push(
        _push(user_requested=True, authorization_digest=digest("auth"), force=True),
        tests_passed=True,
        verifier_passed=True,
    )
    assert decision.allowed is False


@pytest.mark.parametrize(("tests", "verifier"), [(False, True), (True, False)])
def test_kanitsiz_push_reddedilir(tests: bool, verifier: bool) -> None:
    decision = evaluate_push(
        _push(user_requested=True, authorization_digest=digest("auth")),
        tests_passed=tests,
        verifier_passed=verifier,
    )
    assert decision.allowed is False


def test_tam_kanitli_push_izinli() -> None:
    decision = assert_push_allowed(
        _push(user_requested=True, authorization_digest=digest("auth")),
        tests_passed=True,
        verifier_passed=True,
    )
    assert decision.allowed is True


def test_yetkisiz_push_istisna_uretir() -> None:
    with pytest.raises(AuthorizationRequired):
        assert_push_allowed(_push(), tests_passed=True, verifier_passed=True)
