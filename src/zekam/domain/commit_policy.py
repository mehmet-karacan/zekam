"""Commit mesaji ve push kapisi sozlesmesi.

Kaynak: `kalite/COMMIT_POLITIKASI.md`. Bu modul o politikayi kod olarak uygular;
ayri bir urun kurali tanimlamaz. Push varsayilan olarak reddedilir.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from zekam.domain.canonical import digest
from zekam.domain.errors import AuthorizationRequired, PolicyViolation, ValidationFailed

TITLE_MAX_LENGTH = 72

ALLOWED_TYPES = (
    "ozellik",
    "duzeltme",
    "yeniden-duzenleme",
    "test",
    "belge",
    "altyapi",
    "guvenlik",
    "performans",
    "gecis",
    "bakim",
)

REQUIRED_SECTIONS = ("Neden:", "Degisiklik:", "Kanit:", "Risk:", "Geri donus:")

#: Anlamsiz basliklar. Kapsam belirtmeyen mesaj kabul edilmez.
FORBIDDEN_TITLES = ("update", "fix stuff", "wip", "misc", "changes", "guncelleme")

#: Git'in kendi urettigi mesajlar controlled exception alir.
_GENERATED_PREFIXES = ("Merge ", "Revert ", "Reapply ")

_TITLE = re.compile(r"^(?P<type>[a-z-]+): (?P<subject>\S.*)$")
_ISSUE_ONLY = re.compile(r"^[A-Z]+-\d+$|^#\d+$")
_SENSITIVE = re.compile(
    r"(?:secret|credential|password|parola|api[-_ ]?key|private[-_ ]?key|token)",
    re.IGNORECASE,
)
_ABSOLUTE_PATH = re.compile(r"(?:^|\s)(?:[A-Za-z]:\\|/(?:home|Users|root)/)")


@dataclass(frozen=True, slots=True)
class CommitViolation:
    code: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class CommitCheck:
    """Bir commit mesajinin politika sonucu."""

    message_digest: str
    is_generated: bool
    violations: tuple[CommitViolation, ...]

    @property
    def accepted(self) -> bool:
        return not self.violations

    def assert_accepted(self) -> None:
        if self.violations:
            codes = ", ".join(item.code for item in self.violations)
            raise PolicyViolation(f"commit mesaji politikaya uymuyor: {codes}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "message_digest": self.message_digest,
            "is_generated": self.is_generated,
            "accepted": self.accepted,
            "violations": [item.as_dict() for item in self.violations],
        }


def check_commit_message(message: str, *, require_body: bool = True) -> CommitCheck:
    """Baslik bicimini, ASCII kisitini ve zorunlu govde bolumlerini dogrular."""

    problems: list[CommitViolation] = []
    if not message.strip():
        problems.append(CommitViolation("bos-mesaj", "commit mesaji bos olamaz"))
        return CommitCheck(digest(message), False, tuple(problems))

    lines = message.splitlines()
    title = lines[0]
    generated = title.startswith(_GENERATED_PREFIXES)

    if not message.isascii():
        offending = sorted({char for char in message if not char.isascii()})
        problems.append(CommitViolation("non-ascii", f"ASCII disi karakter: {' '.join(offending)}"))

    if not generated:
        problems.extend(_check_title(title))
        if require_body:
            problems.extend(_check_body(lines))

    if _SENSITIVE.search(message):
        problems.append(CommitViolation("secret", "mesaj secret benzeri deger tasiyamaz"))
    if _ABSOLUTE_PATH.search(message):
        problems.append(CommitViolation("personal-path", "mesaj kisisel absolute path tasiyamaz"))

    return CommitCheck(digest(message), generated, tuple(problems))


def _check_title(title: str) -> list[CommitViolation]:
    problems: list[CommitViolation] = []
    match = _TITLE.match(title)
    if match is None:
        problems.append(CommitViolation("baslik-bicimi", "baslik '<tur>: <cumle>' olmali"))
        return problems
    if match.group("type") not in ALLOWED_TYPES:
        problems.append(CommitViolation("tur", f"izinsiz tur: {match.group('type')}"))
    subject = match.group("subject").strip()
    if subject.lower() in FORBIDDEN_TITLES:
        problems.append(CommitViolation("anlamsiz-baslik", "baslik somut kapsam belirtmeli"))
    if _ISSUE_ONLY.match(subject):
        problems.append(CommitViolation("yalniz-id", "yalniz issue kimligi baslik olamaz"))
    if len(title) > TITLE_MAX_LENGTH:
        problems.append(
            CommitViolation("baslik-uzunlugu", f"baslik {TITLE_MAX_LENGTH} karakteri asiyor")
        )
    return problems


def _check_body(lines: list[str]) -> list[CommitViolation]:
    problems: list[CommitViolation] = []
    if len(lines) < 2 or lines[1].strip():
        problems.append(CommitViolation("bos-satir", "baslik ve govde arasinda bos satir olmali"))
    body = "\n".join(lines[1:])
    missing = tuple(section for section in REQUIRED_SECTIONS if section not in body)
    if missing:
        problems.append(CommitViolation("eksik-bolum", f"eksik bolum: {' '.join(missing)}"))
    return problems


@dataclass(frozen=True, slots=True)
class PushRequest:
    """Push istegi. Varsayilan reddir; exact authorization sarttir."""

    remote: str
    branch: str
    head: str
    authorization_digest: str | None = None
    user_requested: bool = False
    force: bool = False

    def __post_init__(self) -> None:
        for label, value in (("remote", self.remote), ("branch", self.branch), ("head", self.head)):
            if not value.strip():
                raise ValidationFailed(f"{label} bos olamaz")

    def as_dict(self) -> dict[str, Any]:
        return {
            "remote": self.remote,
            "branch": self.branch,
            "head": self.head,
            "user_requested": self.user_requested,
            "force": self.force,
            "authorization_digest": self.authorization_digest,
        }


@dataclass(frozen=True, slots=True)
class PushDecision:
    allowed: bool
    reason: str
    request_digest: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "request_digest": self.request_digest,
        }


def evaluate_push(
    request: PushRequest,
    *,
    tests_passed: bool,
    verifier_passed: bool,
) -> PushDecision:
    """Push kapisi.

    Sira: acik kullanici talebi -> exact authorization -> test -> verifier.
    Force push hicbir kosulda otomatik olarak izinli degildir.
    """

    request_digest = digest(request.as_dict())
    if not request.user_requested:
        return PushDecision(False, "push varsayilan olarak reddedilir", request_digest)
    if request.authorization_digest is None:
        return PushDecision(False, "exact authorization yok", request_digest)
    if request.force:
        return PushDecision(False, "force push otomatik olarak izinli degildir", request_digest)
    if not tests_passed:
        return PushDecision(False, "test kanidi yok", request_digest)
    if not verifier_passed:
        return PushDecision(False, "bagimsiz verifier sonucu yok", request_digest)
    return PushDecision(True, "exact authorization ve kanit dogrulandi", request_digest)


def assert_push_allowed(
    request: PushRequest, *, tests_passed: bool, verifier_passed: bool
) -> PushDecision:
    decision = evaluate_push(request, tests_passed=tests_passed, verifier_passed=verifier_passed)
    if not decision.allowed:
        raise AuthorizationRequired(decision.reason)
    return decision
