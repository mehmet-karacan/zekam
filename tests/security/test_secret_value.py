"""`SecretValue` maskeleme sozlesmesi.

Bu testler "yanlislikla loglamak" senaryolarini negatif olarak dogrular: bir
secret'i yazdirmanin, bicimlemenin, serilestirmenin veya hata mesajina koymanin
varsayilan sonucu maskelenmis degerdir.
"""

from __future__ import annotations

import json
import logging
import pickle
import traceback

import pytest

from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.security import REDACTED, SecretValue, redact

pytestmark = [pytest.mark.security, pytest.mark.unit]

SECRET = "Kx7pQm2ZrT9wLb4Nc1Vd"


@pytest.fixture
def secret() -> SecretValue:
    return SecretValue(SECRET)


def test_repr_is_masked(secret: SecretValue) -> None:
    assert SECRET not in repr(secret)
    assert repr(secret) == f"SecretValue({REDACTED})"


def test_str_is_masked(secret: SecretValue) -> None:
    assert str(secret) == REDACTED


def test_f_string_is_masked(secret: SecretValue) -> None:
    assert SECRET not in f"anahtar: {secret}"


def test_format_specifier_is_masked(secret: SecretValue) -> None:
    assert SECRET not in f"{secret:>40}"
    assert SECRET not in "{:s}".format(secret)  # noqa: UP032 - format yolu bilerek sinaniyor


def test_percent_formatting_is_masked(secret: SecretValue) -> None:
    template = "anahtar: %s"
    assert SECRET not in template % (secret,)


def test_containing_object_repr_is_masked(secret: SecretValue) -> None:
    payload = {"api_key": secret, "liste": [secret]}
    assert SECRET not in repr(payload)
    assert SECRET not in str(payload)


def test_json_serialization_is_rejected(secret: SecretValue) -> None:
    with pytest.raises(TypeError):
        json.dumps({"api_key": secret})


def test_pickle_is_rejected(secret: SecretValue) -> None:
    with pytest.raises(TypeError):
        pickle.dumps(secret)


def test_hashing_is_rejected(secret: SecretValue) -> None:
    with pytest.raises(TypeError):
        hash(secret)


def test_logging_is_masked(secret: SecretValue, caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("zekam.test")
    with caplog.at_level(logging.INFO):
        logger.info("anahtar %s kullanildi", secret)
    assert SECRET not in caplog.text


def test_exception_message_is_masked(secret: SecretValue) -> None:
    try:
        raise RuntimeError(f"cagri basarisiz: {secret}")
    except RuntimeError as exc:
        assert SECRET not in str(exc)
        assert SECRET not in "".join(traceback.format_exception(exc))


def test_reveal_returns_the_actual_value(secret: SecretValue) -> None:
    assert secret.reveal() == SECRET


def test_clear_removes_the_value(secret: SecretValue) -> None:
    secret.clear()
    assert secret.is_cleared
    with pytest.raises(PolicyViolation):
        secret.reveal()


def test_empty_value_is_rejected() -> None:
    with pytest.raises(ValidationFailed):
        SecretValue("")


def test_equality_only_compares_same_type(secret: SecretValue) -> None:
    assert secret == SecretValue(SECRET)
    assert secret != SecretValue("baska-deger")
    assert secret.__eq__(SECRET) is NotImplemented


def test_no_attribute_can_be_added(secret: SecretValue) -> None:
    with pytest.raises(AttributeError):
        secret.leaked = SECRET  # type: ignore[attr-defined]


def test_redact_masks_values_in_text(secret: SecretValue) -> None:
    text = f"istek gonderildi: Authorization: Bearer {SECRET} (200)"
    masked = redact(text, (secret,))
    assert SECRET not in masked
    assert REDACTED in masked
    assert "istek gonderildi" in masked


def test_redact_ignores_cleared_values(secret: SecretValue) -> None:
    secret.clear()
    assert redact("bir metin", (secret,)) == "bir metin"


def test_redact_skips_very_short_values() -> None:
    short = SecretValue("ab")
    assert redact("ab cd", (short,)) == "ab cd"


def test_redact_handles_multiple_values() -> None:
    first, second = SecretValue("birinci-deger"), SecretValue("ikinci-deger")
    masked = redact("birinci-deger ve ikinci-deger", (first, second))
    assert masked == f"{REDACTED} ve {REDACTED}"
